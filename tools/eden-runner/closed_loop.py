"""Closed-loop steps for routes that can observe their current state."""
import inspect
import math
import time


NO_READ = object()


class DispatchRejected(RuntimeError):
    pass


class DeliveryUnknown(RuntimeError):
    pass


class RouteStopped(RuntimeError):
    def __init__(self, route, step, reason, receipt):
        self.route = route
        self.step = step
        self.reason = reason
        self.receipt = receipt
        super().__init__(f'{route}: stopped on step {step!r}: {reason}')


class Step:
    """A before_send hook may return a rollback for a known busy rejection."""

    def __init__(self, name, at, done, macro, *, cap, ready_cap=20.0, lost_after=4.0, sends=3,
                 before_send=None, target=None, source=None, screen=None):
        self.name = name
        self.at = at
        self.done = done
        self.macro = macro
        self.cap = cap
        self.ready_cap = ready_cap
        self.lost_after = lost_after
        self.sends = sends
        self.before_send = before_send
        self.target = target
        self.source = source
        self.screen = screen


def run(route, steps, observe, send, finish, *, clock=time.monotonic, sleep=time.sleep, poll=0.3,
        locate_cap=20.0, on_step=None, on_stall=None, idle=None, idle_cap=15.0, revalidate_cap=0.0):
    if not isinstance(revalidate_cap, (int, float)) or not math.isfinite(revalidate_cap) or revalidate_cap < 0:
        raise ValueError(f'revalidate_cap must be finite and non-negative, got {revalidate_cap!r}')
    records = [{'name': step.name, 'sends': 0, 'skipped': True, 'ms': 0, 'unanswered': 0} for step in steps]
    entered_at = 'done'
    stalls = []
    last_answered, last_answered_at, locate_unanswered = None, None, 0
    idle_budget = None
    idle_with_cap = True
    if idle is not None:
        try:
            signature = inspect.signature(idle)
            try:
                signature.bind(1.0)
            except TypeError:
                idle_with_cap = False
        except (TypeError, ValueError):
            pass

    def receipt():
        flush_idle_budget()
        return {'entered_at': entered_at, 'steps': [dict(record) for record in records],
                'stalls': list(stalls)}

    def stop(name, reason, *, continuity=False):
        data = receipt()
        data.update(step=name, reason=reason)
        if continuity:
            data['continuity'] = 'lost'
        raise RouteStopped(route, name, reason, data)

    def matching(obs, excluding=None):
        result = next((i for i in range(len(steps) - 1, -1, -1)
                       if i != excluding and step_at(steps[i], obs)), None)
        if result is not None:
            return result
        return next((i for i in range(len(steps) - 1, -1, -1)
                     if i != excluding and steps[i].source and steps[i].source(obs)), None)

    def read(index=None):
        nonlocal last_answered, last_answered_at, locate_unanswered
        if idle_budget is not None:
            check_idle_budget()
            if idle_cap - (clock() - idle_budget['started']) < revalidate_cap:
                stop_idle_budget(f'not enough of the bound left to re-read the screen (needs {revalidate_cap:g} s); '
                                 'nothing was sent')
        try:
            value = observe()
        except RuntimeError as error:
            reason = f'bridge continuity lost, nothing was sent after it: {error}'
            if index is None:
                stop('locate', reason, continuity=True)
            records[index]['continuity'] = 'lost'
            if active_index == index and active_started is not None:
                records[index]['ms'] += round((clock() - active_started) * 1000)
            stop(steps[index].name, reason)
        if value is NO_READ:
            if index is None:
                locate_unanswered += 1
            else:
                records[index]['unanswered'] += 1
        else:
            last_answered, last_answered_at = value, clock()
        return value

    def wait(deadline, index=None):
        sleep(min(poll, max(0.0, deadline - clock())))
        return read(index)

    def unreadable_reason(count):
        return (f'no transition within cap; last answered read {last_answered!r}; '
                f'{count} unanswered reads')

    def done(step, obs):
        try:
            return step.done(obs)
        except RouteStopped as error:
            if active_index is not None:
                records[active_index]['ms'] += round((clock() - active_started) * 1000)
            stop(error.step, error.reason)

    def step_at(step, obs):
        nonlocal entered_at
        try:
            return step.at(obs)
        except RouteStopped as error:
            if active_index is not None:
                records[active_index]['ms'] += round((clock() - active_started) * 1000)
            else:
                entered_at = error.step
            stop(error.step, error.reason)

    def target(step, obs):
        return step.target(obs) if step.target else None

    def source(step, obs):
        return step.source(obs) if step.source else step_at(step, obs)

    def changed_reason(witness, current):
        return (f'source target changed after the send: {witness!r} -> {current!r}; '
                'the input is neither confirmed nor repeatable')

    def stall(event, obs, *, skip_diagnostic=False):
        payload = {**event, '_skip_diagnostic': True} if skip_diagnostic else event
        enriched = on_stall(payload, obs) if on_stall else None
        if enriched is not None and skip_diagnostic:
            enriched = {key: value for key, value in enriched.items() if key != '_skip_diagnostic'}
        stalls.append(event if enriched is None else enriched)

    def flush_idle_budget(ended=None):
        nonlocal idle_budget
        budget, idle_budget = idle_budget, None
        if budget is not None:
            now = clock() if ended is None else ended
            elapsed = now - budget['measure_started']
            record = records[budget['index']]
            total_ms = budget['previous_ms'] + round((now - budget['segment_started']) * 1000)
            if total_ms:
                record['idle_wait_ms'] = total_ms
            if elapsed > 1.0:
                stall({'kind': 'idle-wait', 'step': steps[budget['index']].name,
                       'seconds': elapsed, 'waits': budget['waits'], 'why': budget['why']}, last_answered)

    def begin_idle_budget(index, started, why, *, measure_started=None):
        nonlocal idle_budget
        if idle_budget is None:
            record = records[index]
            idle_budget = {'index': index, 'started': started,
                           'measure_started': started if measure_started is None else measure_started,
                           'segment_started': started if measure_started is None else measure_started,
                           'why': why, 'waits': 0, 'segment_waits': 0,
                           'previous_waits': record.get('idle_waits', 0),
                           'previous_ms': record.get('idle_wait_ms', 0)}

    def transfer_idle_budget(index):
        if idle_budget is not None:
            budget = idle_budget
            now = clock()
            previous = records[budget['index']]
            total_ms = budget['previous_ms'] + round((now - budget['segment_started']) * 1000)
            if total_ms:
                previous['idle_wait_ms'] = total_ms
            record = records[index]
            budget.update(index=index, segment_started=now, segment_waits=0,
                          previous_waits=record.get('idle_waits', 0), previous_ms=record.get('idle_wait_ms', 0))

    def note_idle_wait(why):
        budget = idle_budget
        budget['waits'] += 1
        budget['segment_waits'] += 1
        if why is not None:
            budget['why'] = why
        record = records[budget['index']]
        record['idle_waits'] = budget['previous_waits'] + budget['segment_waits']
        total_ms = budget['previous_ms'] + round((clock() - budget['segment_started']) * 1000)
        if total_ms:
            record['idle_wait_ms'] = total_ms

    def stop_idle_budget(tail):
        elapsed = clock() - idle_budget['started']
        record = records[idle_budget['index']]
        record['input_channel'] = idle_budget['why']
        if active_index == idle_budget['index']:
            record['ms'] += round((clock() - active_started) * 1000)
        stop(steps[idle_budget['index']].name,
             f'input channel busy for {elapsed:g} s over {idle_budget["waits"]} waits: '
             f'{idle_budget["why"]}; {tail}')

    def check_idle_budget(*, min_remaining=0.0):
        if idle_budget is not None:
            elapsed = clock() - idle_budget['started']
            if elapsed >= idle_cap or idle_cap - elapsed < min_remaining:
                stop_idle_budget('nothing was sent')

    def wait_idle(step, record, obs, *, rejected=False, rejection_reason=None):
        check_idle_budget(min_remaining=0.05 if idle is not None else 0.0)
        if idle is None:
            if rejected:
                begin_idle_budget(active_index, clock(), rejection_reason)
                started_wait = clock()
                remaining = idle_cap - (started_wait - idle_budget['started'])
                sleep(min(poll, max(0.0, remaining)))
                note_idle_wait(rejection_reason)
                check_idle_budget()
                return True
            return False
        started_wait = clock()
        remaining = idle_cap if idle_budget is None else idle_cap - (clock() - idle_budget['started'])
        if idle_budget is not None and remaining < 0.05:
            stop_idle_budget('nothing was sent')
        why = idle(remaining) if idle_with_cap else idle()
        duration = clock() - started_wait
        if idle_budget is None and (why is not None or duration > 1.0):
            begin_idle_budget(active_index, started_wait,
                              why if why is not None else 'macro.status slow', measure_started=started_wait)
        if why is None:
            if duration > 1.0:
                note_idle_wait(None)
                check_idle_budget()
                return True
            check_idle_budget()
            return False
        deadline = idle_budget['started'] + idle_cap
        current = why
        while current is not None and clock() < deadline:
            sleep(min(poll, max(0.0, deadline - clock())))
            if clock() >= deadline:
                break
            remaining = deadline - clock()
            if remaining < 0.05:
                note_idle_wait(why)
                stop_idle_budget('nothing was sent')
            current = idle(remaining) if idle_with_cap else idle()
            if current is not None:
                why = current
        note_idle_wait(why)
        check_idle_budget()
        return True

    active_index, active_started = None, None
    if not steps:
        return receipt()
    deadline = clock() + locate_cap
    obs = read()
    while True:
        grace = False
        if obs is NO_READ:
            if clock() >= deadline:
                grace = True
                obs = read()
                if obs is NO_READ:
                    stop('locate', unreadable_reason(locate_unanswered))
            else:
                obs = wait(deadline)
                continue
        if done(steps[-1], obs):
            records[-1]['unanswered'] += locate_unanswered
            return receipt()
        if grace:
            stop('locate', unreadable_reason(locate_unanswered))
        index = matching(obs)
        if index is None:
            index = next((i for i in range(len(steps) - 1, -1, -1)
                          if steps[i].screen and steps[i].screen(obs)), None)
        if index is not None:
            entered_at = steps[index].name
            records[index]['unanswered'] += locate_unanswered
            break
        if clock() >= deadline:
            stop('locate', f'no matching step; last read {obs!r}')
        obs = wait(deadline)

    while index < len(steps):
        current = index
        step = steps[index]
        record = records[index]
        started = clock()
        retry = None
        visit_sends = 0
        active_index, active_started = index, started
        if on_step:
            on_step(step.name)
        ready_deadline = started + step.ready_cap
        ready_grace = False
        while True:
            check_idle_budget()
            if obs is NO_READ:
                if retry is not None and retry['pause_at'] is None:
                    retry['pause_at'] = last_answered_at
                unreadable_deadline = min(ready_deadline, retry['deadline']) if retry is not None else ready_deadline
                if clock() >= unreadable_deadline:
                    if ready_grace or (retry is not None and retry['grace_used']):
                        record['ms'] += round((clock() - started) * 1000)
                        stop(step.name, unreadable_reason(record['unanswered']))
                    ready_grace = True
                    if retry is not None:
                        retry['grace_used'] = True
                    obs = read(index)
                    if obs is NO_READ:
                        record['ms'] += round((clock() - started) * 1000)
                        stop(step.name, unreadable_reason(record['unanswered']))
                else:
                    obs = wait(unreadable_deadline, index)
                    continue
            if retry is not None and retry['pause_at'] is not None:
                if retry['changed_since'] is not None:
                    retry['changed_since'] += clock() - retry['pause_at']
                retry['pause_at'] = None
            # A prepared retry retains its target even if its first dispatch was rejected as busy.
            if done(step, obs):
                if retry is not None and not retry.get('finished'):
                    finish()
                record['ms'] += round((clock() - started) * 1000)
                index += 1
                if index < len(steps):
                    transfer_idle_budget(index)
                    obs = read(index)
                break
            if done(steps[-1], obs):
                if retry is not None and not retry.get('finished'):
                    finish()
                record['ms'] += round((clock() - started) * 1000)
                return receipt()
            at = step_at(step, obs)
            other = matching(obs, index)
            if ready_grace and retry is not None and (not at or target(step, obs) != retry['witness']):
                record['ms'] += round((clock() - started) * 1000)
                stop(step.name, unreadable_reason(record['unanswered']))
            if ready_grace and retry is None:
                record['ms'] += round((clock() - started) * 1000)
                stop(step.name, unreadable_reason(record['unanswered']))
            if retry is not None and source(step, obs) and target(step, obs) != retry['witness']:
                current_target = target(step, obs)
                if retry['changed_since'] is None:
                    retry['changed_since'] = clock()
                if clock() - retry['changed_since'] >= step.lost_after:
                    record['ms'] += round((clock() - started) * 1000)
                    stop(step.name, changed_reason(retry['witness'], current_target))
                if clock() >= ready_deadline:
                    record['ms'] += round((clock() - started) * 1000)
                    stop(step.name, f'not ready: last read {obs!r}')
                obs = wait(ready_deadline, index)
                continue
            if not at and other is not None:
                if retry is not None and not retry.get('finished'):
                    finish()
                record['ms'] += round((clock() - started) * 1000)
                index = other
                transfer_idle_budget(index)
                break
            if at:
                if retry is not None:
                    current_target = target(step, obs)
                    if current_target != retry['witness']:
                        if retry['changed_since'] is None:
                            retry['changed_since'] = clock()
                        if clock() - retry['changed_since'] >= step.lost_after:
                            record['ms'] += round((clock() - started) * 1000)
                            stop(step.name, changed_reason(retry['witness'], current_target))
                        if clock() >= ready_deadline:
                            record['ms'] += round((clock() - started) * 1000)
                            stop(step.name, f'not ready: last read {obs!r}')
                        obs = wait(ready_deadline, index)
                        continue
                if record['sends'] >= step.sends:
                    record['ms'] += round((clock() - started) * 1000)
                    stop(step.name, f'no transition after {record["sends"]} sends')
                if wait_idle(step, record, obs):
                    obs = read(index)
                    continue
                rollback = None
                if step.before_send:
                    rollback = step.before_send(obs)
                macro = step.macro(obs) if callable(step.macro) else step.macro
                if idle_budget is not None and clock() - idle_budget['started'] >= idle_cap:
                    if callable(rollback):
                        rollback()
                    check_idle_budget()
                if retry is not None and not retry.get('finished'):
                    if not retry.get('stall_reported'):
                        stall(retry['event'], obs, skip_diagnostic=idle_budget is not None)
                        retry['stall_reported'] = True
                    if idle_budget is not None and clock() - idle_budget['started'] >= idle_cap:
                        if callable(rollback):
                            rollback()
                        check_idle_budget()
                    finish()
                    retry['finished'] = True
                if idle_budget is not None and clock() - idle_budget['started'] >= idle_cap:
                    if callable(rollback):
                        rollback()
                    check_idle_budget()
                witness = target(step, obs)
                record['sends'] += 1
                record['skipped'] = False
                dispatch_started = clock()
                try:
                    send(macro)
                except DispatchRejected as error:
                    begin_idle_budget(index, clock(), str(error))
                    record['sends'] -= 1
                    finish()
                    if callable(rollback):
                        rollback()
                    count = record.get('busy_rejections', 0) + 1
                    record['busy_rejections'] = count
                    stall({'kind': 'busy-rejected', 'step': step.name, 'seconds': clock() - dispatch_started,
                           'why': str(error)}, obs)
                    if count >= 2:
                        record['input_channel'] = str(error)
                        record['ms'] += round((clock() - started) * 1000)
                        stop(step.name, 'dispatch rejected as busy twice; nothing was delivered')
                    wait_idle(step, record, obs, rejected=True, rejection_reason=str(error))
                    obs = read(index)
                    continue
                except Exception as error:
                    if visit_sends == 0 and record['sends'] > 1:
                        stall({'kind': 'repeat', 'step': step.name, 'send': record['sends'],
                               'target': repr(witness)}, obs, skip_diagnostic=idle_budget is not None)
                    record['dispatch'] = 'unknown'
                    record['ms'] += round((clock() - started) * 1000)
                    flush_idle_budget(dispatch_started)
                    stop(step.name, f'dispatch failed, delivery unknown: {error}')
                if visit_sends == 0 and record['sends'] > 1:
                    stall({'kind': 'repeat', 'step': step.name, 'send': record['sends'],
                           'target': repr(witness)}, obs, skip_diagnostic=idle_budget is not None)
                visit_sends += 1
                flush_idle_budget(dispatch_started)
                retry = None
                deadline = clock() + step.cap
                lost_since = changed_since = None
                pause_at = None
                grace_used = False
                while True:
                    obs = wait(deadline, index)
                    grace = False
                    if obs is NO_READ:
                        if pause_at is None:
                            pause_at = last_answered_at
                        if clock() < deadline:
                            continue
                        if grace_used:
                            record['ms'] += round((clock() - started) * 1000)
                            stop(step.name, unreadable_reason(record['unanswered']))
                        grace_used = grace = True
                        obs = read(index)
                        if obs is NO_READ:
                            record['ms'] += round((clock() - started) * 1000)
                            stop(step.name, unreadable_reason(record['unanswered']))
                    if pause_at is not None:
                        paused = clock() - pause_at
                        if lost_since is not None:
                            lost_since += paused
                        if changed_since is not None:
                            changed_since += paused
                        pause_at = None
                    if done(step, obs):
                        finish()
                        record['ms'] += round((clock() - started) * 1000)
                        index += 1
                        if index < len(steps):
                            obs = read(index)
                        break
                    if done(steps[-1], obs):
                        finish()
                        record['ms'] += round((clock() - started) * 1000)
                        return receipt()
                    other = matching(obs, index)
                    at_source = source(step, obs)
                    current_target = target(step, obs) if at_source else None
                    if grace and not (at_source and current_target == witness and lost_since is not None
                                      and clock() - lost_since >= step.lost_after):
                        record['ms'] += round((clock() - started) * 1000)
                        stop(step.name, unreadable_reason(record['unanswered']))
                    if other is not None and not (at_source and current_target != witness):
                        finish()
                        record['ms'] += round((clock() - started) * 1000)
                        index = other
                        obs = read(index)
                        break
                    if at_source and current_target != witness:
                        lost_since = None
                        if changed_since is None:
                            changed_since = clock()
                        if clock() - changed_since >= step.lost_after:
                            record['ms'] += round((clock() - started) * 1000)
                            stop(step.name, changed_reason(witness, current_target))
                    elif at_source and step_at(step, obs):
                        changed_since = None
                        if lost_since is None:
                            lost_since = clock()
                        if clock() - lost_since >= step.lost_after:
                            event = {'step': step.name, 'send': record['sends'],
                                     'seconds_on_source': clock() - lost_since, 'target': repr(witness)}
                            if record['sends'] >= step.sends:
                                stall(event, obs, skip_diagnostic=True)
                                finish()
                                record['ms'] += round((clock() - started) * 1000)
                                stop(step.name, f'no transition after {record["sends"]} sends')
                            preflight_started = clock()
                            fresh = read(index)
                            if fresh is NO_READ:
                                pause_at = last_answered_at
                                if clock() < deadline:
                                    continue
                                if grace_used:
                                    record['ms'] += round((clock() - started) * 1000)
                                    stop(step.name, unreadable_reason(record['unanswered']))
                                grace_used = True
                                fresh = read(index)
                                if fresh is NO_READ:
                                    record['ms'] += round((clock() - started) * 1000)
                                    stop(step.name, unreadable_reason(record['unanswered']))
                                paused = clock() - pause_at
                                lost_since += paused
                                if changed_since is not None:
                                    changed_since += paused
                                pause_at = None
                                grace = True
                            if preflight_started < deadline and clock() > deadline:
                                if not grace:
                                    record['ms'] += round((clock() - started) * 1000)
                                    stop(step.name, f'no transition within cap; last read {fresh!r}')
                            fresh_done = done(step, fresh)
                            fresh_final_done = False if fresh_done else done(steps[-1], fresh)
                            if (grace and not fresh_done and not fresh_final_done
                                    and not (step_at(step, fresh) and target(step, fresh) == witness)):
                                record['ms'] += round((clock() - started) * 1000)
                                stop(step.name, unreadable_reason(record['unanswered']))
                            if (not fresh_done and not fresh_final_done
                                    and matching(fresh, index) is None and step_at(step, fresh)
                                    and target(step, fresh) == witness):
                                retry = {'witness': witness, 'event': event, 'changed_since': None,
                                         'deadline': deadline, 'grace_used': grace_used, 'pause_at': None}
                                obs = read(index)
                                ready_deadline = clock() + step.ready_cap
                                ready_grace = False
                                break
                            obs = fresh
                            lost_since = None
                            changed_since = (clock() if source(step, obs) and target(step, obs) != witness else None)
                            if fresh_done:
                                finish()
                                record['ms'] += round((clock() - started) * 1000)
                                index += 1
                                if index < len(steps):
                                    obs = read(index)
                                break
                            if fresh_final_done:
                                finish()
                                record['ms'] += round((clock() - started) * 1000)
                                return receipt()
                            other = matching(obs, index)
                            if other is not None and not (source(step, obs) and target(step, obs) != witness):
                                finish()
                                record['ms'] += round((clock() - started) * 1000)
                                index = other
                                obs = read(index)
                                break
                    else:
                        lost_since = changed_since = None
                    if clock() >= deadline:
                        record['ms'] += round((clock() - started) * 1000)
                        stop(step.name, f'no transition within cap; last read {obs!r}')
                if index != current:
                    break
                continue
            if clock() >= ready_deadline:
                record['ms'] += round((clock() - started) * 1000)
                record['skipped'] = False
                stop(step.name, f'not ready: last read {obs!r}')
            if retry is not None:
                retry['changed_since'] = None
            obs = wait(ready_deadline, index)
    return receipt()
