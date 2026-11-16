"""Shared online-pair route blocks (docs/ROUTES.md, "Online pairs"). Runner step hooks over two instances of one plan.

  start_both        Start in BOTH windows until each shows the main menu (AGENTS.md 2026-10-26; HARNESS-LESSONS)
  lan_leader        LAN (Eden broker): the leader's console shows one `[coord] leading`, one `[auto] ready pid=P`
                    and `member joined pid=P ... members=1`, before the joiner's game.load
  lan_pair          after the joiner's load: the leader's last `members=2`, its joined pid equal to the joiner's ready
                    pid, and no `leading` on the joiner (a second room fails closed; runtime-calibration-4)
  ldn_join          Local Wireless: the guest's request and the host's approval macros start together, so the host's
                    OK is armed before the request lands (the guest gives up after about 30 s)

Order for a LAN pair in one boot (lifecycle "defer_load": true): {"call": "game.load", "on": "a"},
{"run": "routes_online:lan_leader", "on": "a"}, {"call": "game.load", "on": "b"},
{"run": "routes_online:lan_pair", "args": {"leader": "a", "joiner": "b"}}, then routes:to_main on each, promptly.
Proven by runtime-calibration-4 (XPS A, Yoga B, 2026-11-15 23:22Z) and eden-ldn-coexistence runtime-12/13; these
blocks re-state those checks and were not booted again by harness-lib.
"""
import re
import time
from pathlib import Path

LEADING = r'\[coord\] leading epoch=(\d+)'
READY = r'\[auto\] ready pid=(\d+)'
JOINED = r'\[coord\] member joined pid=(\d+)[^\n]*members=(\d+)'
BROKER = r'\[auto\] broker started title=01006A800016E000 lan=(\S+) local_only=(\w+)'


def parse_room(text):
    return {'leading': re.findall(LEADING, text), 'ready': re.findall(READY, text),
            'members': re.findall(r'members=(\d+)', text), 'joined': re.findall(JOINED, text),
            'broker': re.findall(BROKER, text)}


def leader_ok(text, lan=None):
    """ordered_start.py leader_ok: one broker (on `lan` when given, not local-only), one leading, one ready pid that
    joined as member 1, and the last member count 1."""
    p = parse_room(text)
    brokers = [b for b in p['broker'] if b[1] == 'false' and (lan is None or b[0] == lan)]
    return bool(brokers and len(p['leading']) == 1 and len(p['ready']) == 1 and p['members']
                and p['members'][-1] == '1' and (p['ready'][0], '1') in p['joined'])


def pair_joined(leader_text, joiner_text):
    """ordered_start.py joined: the leader leads alone, the joiner leads nothing, and the leader saw the joiner's
    ready pid join as member 2 (its last member count)."""
    pa, pb = parse_room(leader_text), parse_room(joiner_text)
    return bool(len(pa['leading']) == 1 and not pb['leading'] and len(pb['ready']) == 1 and pa['members']
                and pa['members'][-1] == '2' and (pb['ready'][0], '2') in pa['joined'])


def _console(runner, on):
    path = Path(runner.instances[on]['console_log'])
    return path.read_text(errors='replace') if path.is_file() else ''


def lan_leader(ctx, lan=None, cap=180):
    until = time.monotonic() + cap
    while True:
        ctx.check_stop()
        text = _console(ctx.runner, ctx.on)
        if leader_ok(text, lan):
            room = parse_room(text)
            return {'leader': ctx.on, 'ready': room['ready'], 'leading': room['leading']}
        if time.monotonic() > until:
            raise RuntimeError(f'{ctx.on} did not lead a LAN room within {cap} s: {parse_room(text)}')
        time.sleep(1.0)


def lan_pair(ctx, leader='a', joiner='b', cap=180):
    until = time.monotonic() + cap
    while True:
        ctx.check_stop()
        a, b = _console(ctx.runner, leader), _console(ctx.runner, joiner)
        if parse_room(b)['leading']:
            raise RuntimeError(f'{joiner} led its own room; the pair is two rooms. Down both before Quickplay.')
        if pair_joined(a, b):
            return {'leader': parse_room(a)['joined'][-1], 'joiner_ready': parse_room(b)['ready']}
        if time.monotonic() > until:
            raise RuntimeError(f'no members=2 room within {cap} s: leader {parse_room(a)} joiner {parse_room(b)}')
        time.sleep(1.0)


def start_both(ctx, instances=('a', 'b')):
    """routes.to_main on each instance in turn (Start promptly in BOTH windows)."""
    import routes  # noqa: PLC0415 - sibling module next to runner.py
    out = {}
    for on in instances:
        out[on] = routes.to_main(type(ctx)(ctx.runner, on))
    return out


def ldn_join(ctx, host='a', guest='b', guest_segment='guest-request', host_segment='host-approval', cap=25):
    """Play the guest's request and the host's approval together (join-approved.py:51-68). Start contract: the host
    waits in native Local Wireless with no prompt; the guest's room list has the intended room selected. The host
    macro presses A about 240 frames in, inside the guest's ~30 s timeout. The caller's next gates judge the join."""
    import routes  # noqa: PLC0415
    runner = ctx.runner
    for on in (guest, host):
        status = runner.call(on, 'status') or {}
        if not (status.get('running') and status.get('loading_complete')) or status.get('modal'):
            raise RuntimeError(f'{on} is not running a loaded game: {status}')
        runner.call(on, 'macro.stop')
        runner.call(on, 'input.reset')
    files = {guest: routes.macro_file(guest_segment)[0], host: routes.macro_file(host_segment)[0]}
    for on in (guest, host):
        runner.call(on, 'macro.play', {'path': str(files[on])})
    until = time.monotonic() + cap
    while True:
        states = {on: runner.call(on, 'macro.status') or {} for on in (guest, host)}
        if any(s.get('error') for s in states.values()):
            raise RuntimeError(f'macro error: {states}')
        if all(s.get('state') == 'finished' for s in states.values()):
            break
        if time.monotonic() > until:
            raise RuntimeError(f'join macros not finished within {cap} s: {states}')
        time.sleep(0.5)
    for on in (guest, host):
        runner.call(on, 'input.reset')
    return {'guest': guest, 'host': host, 'shots': {on: str(runner.shot(on, 'ldn-join')) for on in (guest, host)}}
