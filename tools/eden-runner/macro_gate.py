"""Bounded, witness-aware dispatch of resolved macro files."""
import threading
import time

import closed_loop


IDLE_CAP = 15.0
BLIND = object()


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


def bounded_call(call, method, cap, check=None):
    result = []

    def invoke():
        try:
            result.append((True, call(method)))
        except BaseException as error:
            result.append((False, error))

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    if check is None:
        worker.join(cap)
    else:
        deadline = time.monotonic() + cap
        while worker.is_alive():
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(min(.1, remaining))
        check()
    return result[0] if not worker.is_alive() else (False, None)


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
            try:
                needed = requalify(remaining)
            except Exception as error:
                if stop(error, check) or is_stop is not None and is_stop(error):
                    raise
                check_cancelled()
                emit(f'not sent: {error}')
                raise
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
