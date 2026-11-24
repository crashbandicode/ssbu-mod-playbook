"""Closed-loop steps for routes that can observe their current state."""
import time


class RouteStopped(RuntimeError):
    def __init__(self, route, step, reason, receipt):
        self.route = route
        self.step = step
        self.reason = reason
        self.receipt = receipt
        super().__init__(f'{route}: stopped on step {step!r}: {reason}')


class Step:
    def __init__(self, name, at, done, macro, *, cap, ready_cap=20.0, lost_after=4.0, sends=3,
                 before_send=None, target=None):
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


def run(route, steps, observe, send, finish, *, clock=time.monotonic, sleep=time.sleep, poll=0.3,
        locate_cap=20.0, on_step=None, on_stall=None):
    records = [{'name': step.name, 'sends': 0, 'skipped': True, 'ms': 0} for step in steps]
    entered_at = 'done'
    stalls = []

    def receipt():
        return {'entered_at': entered_at, 'steps': [dict(record) for record in records],
                'stalls': list(stalls)}

    def stop(name, reason):
        data = receipt()
        data.update(step=name, reason=reason)
        raise RouteStopped(route, name, reason, data)

    def matching(obs, excluding=None):
        return next((i for i in range(len(steps) - 1, -1, -1)
                     if i != excluding and steps[i].at(obs)), None)

    def wait(deadline):
        sleep(min(poll, max(0.0, deadline - clock())))
        return observe()

    def done(step, obs):
        try:
            return step.done(obs)
        except RouteStopped as error:
            if active_index is not None:
                records[active_index]['ms'] += round((clock() - active_started) * 1000)
            stop(error.step, error.reason)

    def target(step, obs):
        return step.target(obs) if step.target else None

    def changed_reason(witness, current):
        return (f'source target changed after the send: {witness!r} -> {current!r}; '
                'the input is neither confirmed nor repeatable')

    def stall(event, obs):
        enriched = on_stall(event, obs) if on_stall else None
        stalls.append(event if enriched is None else enriched)

    active_index, active_started = None, None
    if not steps:
        return receipt()
    deadline = clock() + locate_cap
    obs = observe()
    while True:
        if done(steps[-1], obs):
            return receipt()
        index = matching(obs)
        if index is not None:
            entered_at = steps[index].name
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
        active_index, active_started = index, started
        if on_step:
            on_step(step.name)
        ready_deadline = started + step.ready_cap
        while True:
            if done(step, obs):
                record['ms'] += round((clock() - started) * 1000)
                index += 1
                if index < len(steps):
                    obs = observe()
                break
            if done(steps[-1], obs):
                record['ms'] += round((clock() - started) * 1000)
                return receipt()
            at = step.at(obs)
            other = matching(obs, index)
            if not at and other is not None:
                record['ms'] += round((clock() - started) * 1000)
                index = other
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
                        obs = wait(ready_deadline)
                        continue
                    stall(retry['event'], obs)
                    retry = None
                if record['sends'] >= step.sends:
                    record['ms'] += round((clock() - started) * 1000)
                    stop(step.name, f'no transition after {record["sends"]} sends')
                if step.before_send:
                    step.before_send(obs)
                macro = step.macro(obs) if callable(step.macro) else step.macro
                witness = target(step, obs)
                record['sends'] += 1
                record['skipped'] = False
                try:
                    send(macro)
                except RuntimeError as error:
                    record['dispatch'] = 'unknown'
                    record['ms'] += round((clock() - started) * 1000)
                    stop(step.name, f'dispatch failed, delivery unknown: {error}')
                deadline = clock() + step.cap
                lost_since = changed_since = None
                while True:
                    obs = wait(deadline)
                    if done(step, obs):
                        finish()
                        record['ms'] += round((clock() - started) * 1000)
                        index += 1
                        if index < len(steps):
                            obs = observe()
                        break
                    if done(steps[-1], obs):
                        finish()
                        record['ms'] += round((clock() - started) * 1000)
                        return receipt()
                    other = matching(obs, index)
                    at_source = step.at(obs)
                    current_target = target(step, obs) if at_source else None
                    if other is not None and not (at_source and current_target != witness):
                        finish()
                        record['ms'] += round((clock() - started) * 1000)
                        index = other
                        obs = observe()
                        break
                    if at_source and current_target != witness:
                        lost_since = None
                        if changed_since is None:
                            changed_since = clock()
                        if clock() - changed_since >= step.lost_after:
                            record['ms'] += round((clock() - started) * 1000)
                            stop(step.name, changed_reason(witness, current_target))
                    elif at_source:
                        changed_since = None
                        if lost_since is None:
                            lost_since = clock()
                        if clock() - lost_since >= step.lost_after:
                            event = {'step': step.name, 'send': record['sends'],
                                     'seconds_on_source': clock() - lost_since, 'target': repr(witness)}
                            if record['sends'] >= step.sends:
                                stall(event, obs)
                                finish()
                                record['ms'] += round((clock() - started) * 1000)
                                stop(step.name, f'no transition after {record["sends"]} sends')
                            preflight_started = clock()
                            fresh = observe()
                            if preflight_started < deadline and clock() > deadline:
                                record['ms'] += round((clock() - started) * 1000)
                                stop(step.name, f'no transition within cap; last read {fresh!r}')
                            fresh_done = done(step, fresh)
                            fresh_final_done = False if fresh_done else done(steps[-1], fresh)
                            if (not fresh_done and not fresh_final_done
                                    and matching(fresh, index) is None and step.at(fresh)
                                    and target(step, fresh) == witness):
                                finish()
                                retry = {'witness': witness, 'event': event, 'changed_since': None}
                                obs = observe()
                                ready_deadline = clock() + step.ready_cap
                                break
                            obs = fresh
                            lost_since = None
                            changed_since = (clock() if step.at(obs) and target(step, obs) != witness else None)
                            if fresh_done:
                                finish()
                                record['ms'] += round((clock() - started) * 1000)
                                index += 1
                                if index < len(steps):
                                    obs = observe()
                                break
                            if fresh_final_done:
                                finish()
                                record['ms'] += round((clock() - started) * 1000)
                                return receipt()
                            other = matching(obs, index)
                            if other is not None and not (step.at(obs) and target(step, obs) != witness):
                                finish()
                                record['ms'] += round((clock() - started) * 1000)
                                index = other
                                obs = observe()
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
            obs = wait(ready_deadline)
    return receipt()
