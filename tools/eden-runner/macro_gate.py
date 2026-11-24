"""Bounded, witness-aware dispatch of resolved macro files."""
import threading
import time

import closed_loop


IDLE_CAP = 15.0
BLIND = object()
_worker = threading.local()


class Ticket:
    def __init__(self, deadline=None):
        self.deadline = deadline
        self.state = 'PENDING'
        self.lock = threading.Lock()
        self.publisher = None
        self.candidates = []
        self.discarded = False
        self.reason = None

    def commit(self):
        with self.lock:
            if self.state != 'PENDING':
                return False
            self.state = 'COMMITTED'
            return True

    def revoke(self, reason=None):
        with self.lock:
            if self.state != 'PENDING':
                return False
            self.state = 'REVOKED'
            self.reason = reason
            return True

    def is_abandoned(self):
        with self.lock:
            return (self.state == 'REVOKED' or
                    (self.state == 'PENDING' and self.deadline is not None and time.monotonic() >= self.deadline))

    def revoked_for(self, reason):
        with self.lock:
            return self.state == 'REVOKED' and self.reason == reason

    def is_revoked(self):
        with self.lock:
            return self.state == 'REVOKED'

    def attach(self, owner, candidate):
        with self.lock:
            if self.state != 'PENDING':
                return False
            if self.publisher is not None and self.publisher is not owner:
                raise RuntimeError('one bounded call cannot publish evidence for multiple runners')
            self.publisher = owner
            self.candidates.append(candidate)
            return True

    def finish(self):
        if self.publisher is None:
            return self.commit()
        return self.publisher._commit_evidence(self)

    def discard(self):
        with self.lock:
            if self.discarded or self.state != 'REVOKED':
                return
            self.discarded = True
            owner, candidates = self.publisher, list(self.candidates)
        if owner is not None:
            owner._discard_evidence(candidates)

    def discard_later(self):
        with self.lock:
            needed = self.state == 'REVOKED' and not self.discarded and self.publisher is not None
        if needed:
            threading.Thread(target=self.discard, daemon=True).start()


def current_ticket():
    return getattr(_worker, 'ticket', None)


def abandoned():
    ticket = current_ticket()
    return ticket is not None and ticket.is_abandoned()


def stop(error, check=None):
    if any(base.__name__ == 'StopRun' for base in type(error).__mro__):
        return True
    code = getattr(getattr(check, '__func__', check), '__code__', None)
    trace = error.__traceback__
    while code is not None and trace is not None:
        if trace.tb_frame.f_code is code:
            return True
        trace = trace.tb_next
    return False


def bounded_call(call, method, cap, check=None, until=None, with_completion=False):
    result = []
    deadline = time.monotonic() + cap
    if until is not None:
        deadline = min(deadline, until)
    if deadline <= time.monotonic():
        return (False, None, None) if with_completion else (False, None)
    ticket = Ticket(deadline)

    def invoke():
        _worker.ticket = ticket
        try:
            value = call(method)
        except BaseException as error:
            result.append((False, error, time.monotonic()))
        else:
            result.append((True, value, time.monotonic()))
        finally:
            ticket.discard()

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        if check is None:
            worker.join(max(0, deadline - time.monotonic()))
        else:
            while worker.is_alive():
                check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                worker.join(min(.1, remaining))
            check()
        if result:
            if result[0][2] < deadline:
                try:
                    if ticket.is_revoked() and not result[0][0]:
                        pass
                    elif not ticket.finish():
                        raise closed_loop.DispatchRejected('gate evidence publication was revoked')
                except BaseException as error:
                    return (False, error, time.monotonic()) if with_completion else (False, error)
            else:
                ticket.revoke('timeout')
                ticket.discard_later()
                return (False, None, result[0][2]) if with_completion else (False, None)
            return result[0] if with_completion else result[0][:2]
        ticket.revoke('timeout')
        if not worker.is_alive():
            ticket.discard_later()
        return (False, None, None) if with_completion else (False, None)
    finally:
        ticket.revoke('cancel')
        if not worker.is_alive():
            ticket.discard_later()


def busy(call, remaining, note=None, check=None, is_stop=None):
    answered, status = bounded_call(call, 'macro.status', remaining, check=check)
    if not answered and status is not None:
        if stop(status, check) or is_stop is not None and is_stop(status):
            raise status
        if check is not None:
            check()
    try:
        if not answered:
            if status is None:
                return f'macro.status not answered within {remaining:g} s'
            return f'macro.status not understood: {repr(status)[:160]}'
        if isinstance(status, dict):
            state, error = status.get('state'), status.get('error')
            if state == 'error':
                if note is not None:
                    note(error or '')
                return None
            if state in ('countdown', 'recording', 'playing', 'branch_playing'):
                return f'macro {state}'
            if state == 'idle' or (state == 'finished' and error in (None, '')):
                return None
        return f'macro.status not understood: {repr(status)[:160]}'
    except Exception as error:
        if stop(error, check) or is_stop is not None and is_stop(error):
            raise
        if check is not None:
            check()
        return f'macro.status not understood: {repr(error)[:160]}'


def dispatch(call, path, *, name, requalify=None, until=None, on_event=None, note=None, check=None, is_stop=None):
    started = time.monotonic()
    deadline = min(started + IDLE_CAP, until) if until is not None else started + IDLE_CAP
    waits, why, waited = 0, 'macro.status slow', False

    def emit(outcome, ended=None):
        event = {'kind': 'idle-wait', 'step': name, 'seconds': (time.monotonic() if ended is None else ended) - started,
                 'waits': waits, 'why': why, 'outcome': outcome}
        if on_event is not None:
            try:
                on_event(event)
            except Exception:
                pass

    def reject(outcome):
        emit(outcome)
        if outcome == 'not sent: still busy':
            seconds = time.monotonic() - started
            raise closed_loop.DispatchRejected(
                f'macro {name} not sent: input channel busy for {seconds:g} s over {waits} waits: {why}')
        raise closed_loop.DispatchRejected(f'macro {name} {outcome}')

    def check_cancelled():
        if check is not None:
            check()

    while True:
        check_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 1e-6:
            if remaining > 0:
                time.sleep(remaining)
            reject('not sent: still busy')
        poll_started = time.monotonic()
        current = busy(call, remaining, note=note, check=check, is_stop=is_stop)
        waits += 1
        duration = time.monotonic() - poll_started
        if current is not None:
            why = current
            waited = True
        if duration > 1.0:
            waited = True
        if time.monotonic() >= deadline:
            if current is None and until is not None and time.monotonic() >= until:
                reject("not sent: the caller's bound passed while waiting")
            reject('not sent: still busy')
        if current is None:
            break
        time.sleep(min(.2, max(0.0, deadline - time.monotonic())))

    check_cancelled()
    if waited:
        if requalify is None:
            reject('not sent: no witness to requalify after the wait')
        if requalify is BLIND:
            how = 'the caller declared no witness'
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reject('not sent: the bound passed during requalification')
            answered, needed, completed = bounded_call(
                requalify, remaining, remaining, check=check, until=deadline, with_completion=True)
            if not answered:
                if needed is None:
                    if completed is not None:
                        reject('not sent: the bound passed during requalification')
                    reject('not sent: requalification did not answer within the bound')
                error = needed
                if completed >= deadline:
                    reject('not sent: the bound passed during requalification')
                if not isinstance(error, Exception):
                    raise error
                if stop(error, check) or is_stop is not None and is_stop(error):
                    raise error
                check_cancelled()
                emit(f'not sent: {error}')
                raise error
            if time.monotonic() >= deadline:
                reject('not sent: the bound passed during requalification')
            if not needed:
                emit('not sent: no longer needed')
                return False
            how = 'requalified'
        check_cancelled()
    if time.monotonic() >= deadline:
        if waited:
            reject('not sent: the bound passed during requalification' if requalify is not BLIND
                   else "not sent: the caller's bound passed while waiting")
        raise closed_loop.DispatchRejected(f'macro {name} not sent: the input bound passed')
    dispatch_started = time.monotonic()
    try:
        call('macro.play', {'path': str(path)})
    except Exception as error:
        message = str(error).lower()
        if isinstance(error, closed_loop.DispatchRejected) or 'input_busy' in message or 'macro.play busy' in message:
            if waited:
                emit(f'{how}, rejected: {error}', dispatch_started)
            if isinstance(error, closed_loop.DispatchRejected):
                raise
            raise closed_loop.DispatchRejected(str(error)) from error
        if waited:
            emit(f'{how}, delivery unknown: {error}', dispatch_started)
        raise
    if waited:
        emit(f'{how}, accepted', dispatch_started)
    return True
