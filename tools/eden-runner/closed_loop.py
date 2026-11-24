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
                 before_send=None):
        self.name = name
        self.at = at
        self.done = done
        self.macro = macro
        self.cap = cap
        self.ready_cap = ready_cap
        self.lost_after = lost_after
        self.sends = sends
        self.before_send = before_send


def run(route, steps, observe, send, finish, *, clock=time.monotonic, sleep=time.sleep, poll=0.3,
        locate_cap=20.0, on_step=None):
    records = [{'name': step.name, 'sends': 0, 'skipped': True, 'ms': 0} for step in steps]
    entered_at = 'done'

    def receipt():
        return {'entered_at': entered_at, 'steps': [dict(record) for record in records]}

    def stop(name, reason):
        data = receipt()
        data.update(step=name, reason=reason)
        raise RouteStopped(route, name, reason, data)

    def matching(obs):
        return next((i for i in range(len(steps) - 1, -1, -1)
                     if steps[i].at(obs)), None)

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
            other = matching(obs)
            if other is not None and other != index:
                record['ms'] += round((clock() - started) * 1000)
                index = other
                break
            record['skipped'] = False
            if step.at(obs):
                if record['sends'] >= step.sends:
                    record['ms'] += round((clock() - started) * 1000)
                    stop(step.name, f'no transition after {record["sends"]} sends')
                if step.before_send:
                    step.before_send(obs)
                macro = step.macro(obs) if callable(step.macro) else step.macro
                send(macro)
                record['sends'] += 1
                deadline = clock() + step.cap
                lost_since = None
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
                    other = matching(obs)
                    if other is not None and other != index:
                        finish()
                        record['ms'] += round((clock() - started) * 1000)
                        index = other
                        obs = observe()
                        break
                    if step.at(obs):
                        if lost_since is None:
                            lost_since = clock()
                        if clock() - lost_since >= step.lost_after:
                            finish()
                            if record['sends'] >= step.sends:
                                record['ms'] += round((clock() - started) * 1000)
                                stop(step.name, f'no transition after {record["sends"]} sends')
                            obs = observe()
                            ready_deadline = clock() + step.ready_cap
                            break
                    else:
                        lost_since = None
                    if clock() >= deadline:
                        record['ms'] += round((clock() - started) * 1000)
                        stop(step.name, f'no transition within cap; last read {obs!r}')
                if index != current:
                    break
                continue
            if clock() >= ready_deadline:
                record['ms'] += round((clock() - started) * 1000)
                stop(step.name, f'not ready: last read {obs!r}')
            obs = wait(ready_deadline)
    return receipt()
