"""Shared setup fixtures."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import routes
import state_gate

GIVES = {
    'recording_role': lambda args: [
        f'recording.{"popup.role" if args.get("where", "page") == "popup" else "role"}={args["role"]}'],
    'guard_first_use': lambda args: (['guard.ready'] if args.get('game', 'guard_edge') == 'guard_edge'
                                     else [f'practice.ready={args["game"]}']),
    'seeded_room': lambda args: ['lobby.practice=on', f'partner.behavior={args["behavior"]}',
                                 f'lobby.stage={args["stage"]}'],
    'cpu_standing': lambda args: ['cpu.standing'],
    'partner_effective': lambda args: [f'partner.effective={args["eb"]}'],
}

ROOM_MARK = 'fixture-room-mark'
STAGE_LINE = re.compile(r'\[Bridge\] stage id=(-?\d+)\s.*\bframe=(\d+)\s')
WATCH_ACK = re.compile(r'\[Bridge\] req fighters_watch result=ok entries=(0x[\da-f]+) frames=(\d+)')
WATCH_DONE = re.compile(r'\[Bridge\] fighters_watch done entries=(0x[\da-f]+)')
WATCH_FIGHTER = re.compile(r'\[Bridge\] fighter entry=(\d+) .*\bcbt=\S+ .*\bframe=(\d+)(?:\s|$)')
FIGHTER_ENTRY = re.compile(r'\[Bridge\] fighter entry=(\d+)(?: |$)')
FRAME = re.compile(r'\bframe=(\d+)\b')
WORDS = re.compile(r'([a-z_][\w.]*)=([^ ]+)')


def watch_window(lines, entries, frames, min_frame):
    mask = hex(sum(1 << entry for entry in entries))
    starts = []
    for index, line in enumerate(lines):
        ack, stamp = WATCH_ACK.search(line), FRAME.search(line)
        if ack and stamp and ack.groups() == (mask, str(frames)) and int(stamp[1]) >= min_frame:
            starts.append((index, int(stamp[1])))
    if not starts:
        return [], False
    first_frame = starts[0][1]
    start = next(index for index, frame in reversed(starts) if frame == first_frame)
    for index in range(start + 1, len(lines)):
        done, stamp = WATCH_DONE.search(lines[index]), FRAME.search(lines[index])
        if done and stamp and done[1] == mask and int(stamp[1]) == first_frame + frames:
            return lines[start:index + 1], True
    return lines[start:], False


def check_watch(window_lines, entries, frames):
    mask = hex(sum(1 << entry for entry in entries))
    acknowledgements = [m for line in window_lines if (m := WATCH_ACK.search(line))]
    completions = [m for line in window_lines if (m := WATCH_DONE.search(line))]
    sequence = [(int(m[1]), int(m[2])) for line in window_lines if (m := WATCH_FIGHTER.search(line))]
    by_entry = {entry: [frame for actual, frame in sequence if actual == entry] for entry in entries}
    problems = []
    if len(acknowledgements) != 1 or acknowledgements[0].groups() != (mask, str(frames)):
        problems.append('the expected watch acknowledgement did not arrive exactly once')
    if len(completions) != 1 or completions[0][1] != mask:
        problems.append('the expected watch completion did not arrive exactly once')
    if len(sequence) != len(entries) * frames:
        problems.append(f'expected {len(entries) * frames} samples, observed {len(sequence)}')
    for entry, values in by_entry.items():
        if len(values) != frames or (values and values != list(range(values[0], values[0] + frames))):
            problems.append(f'entry {entry} did not have {frames} consecutive bridge frames')
    if len({tuple(values) for values in by_entry.values()}) != 1:
        problems.append('the watched entries did not share the same bridge frames')
    return problems


def samples(window_lines, entry):
    found = []
    for line in window_lines:
        match = FIGHTER_ENTRY.search(line)
        if match and int(match[1]) == entry:
            fields = dict(WORDS.findall(line.strip()))
            if 'frame' in fields:
                found.append((int(fields['frame']), fields))
    return found


def standing(readings):
    if not readings:
        return False, 'no fighter samples'
    start, initial = readings[0]
    x, y = initial.get('x'), initial.get('y')
    if x is None or y is None:
        return False, f'frame {start}: x={x} y={y}'
    for frame, fields in readings:
        if (fields.get('status'), fields.get('situation'), fields.get('x'), fields.get('y')) != ('0', '0', x, y):
            return False, (f'frame {frame}: status={fields.get("status")} situation={fields.get("situation")} '
                           f'x={fields.get("x")} y={fields.get("y")}')
    return True, ''


def fresh_watch(ctx, entries, frames, name='fixture-watch'):
    if (not entries or len(set(entries)) != len(entries) or
            any(type(entry) is not int or entry < 0 for entry in entries) or type(frames) is not int or frames < 1):
        raise ValueError('entries must be distinct non-negative integers and frames must be positive')
    baseline = routes.native_state(ctx, fresh=('roster',), roster_entries=(entries[0],))
    min_frame = baseline.get('frame')
    if type(min_frame) is not int or entries[0] not in baseline.get('roster', {}):
        raise RuntimeError(f'SETUP: fresh_watch: no fresh baseline for entry {entries[0]}: {baseline!r}')
    cursor = routes.LogCursor(routes.instance_path(ctx, 'bridge_log'))
    lines, window, complete = [], [], False
    try:
        routes.bridge_v2(ctx, f'fighters watch {",".join(map(str, entries))} {frames}')
        deadline = time.monotonic() + max(12, frames / 15)
        while time.monotonic() < deadline:
            ctx.check_stop()
            lines.extend(cursor.read())
            window, complete = watch_window(lines, entries, frames, min_frame)
            if complete:
                break
            time.sleep(.05)
    finally:
        cursor.close()
    problems = check_watch(window, entries, frames)
    if not complete:
        problems.append('watch incomplete')
    data = ('\n'.join(window) + ('\n' if window else '')).encode()
    number = 1
    while True:
        suffix = '' if number == 1 else f'-{number}'
        raw = Path(ctx.row_dir) / f'{name}{suffix}.log'
        try:
            with raw.open('xb') as stream:
                stream.write(data)
            break
        except FileExistsError:
            number += 1
    readings = samples(window, entries[0])
    receipt = {'entries': list(entries), 'frames': frames, 'first_frame': readings[0][0] if readings else None,
               'last_frame': readings[-1][0] if readings else None, 'min_frame': min_frame,
               'raw_file': raw.name, 'sha256': hashlib.sha256(data).hexdigest(), 'problems': problems}
    routes._save_block(ctx, name, receipt)
    if problems:
        raise RuntimeError(f'SETUP: fresh_watch: {problems}')
    return {**receipt, 'lines': window}


def cpu_standing(ctx, entry=1, frames=120):
    watch = fresh_watch(ctx, [entry], frames, 'fixture-cpu-standing-watch')
    readings = samples(watch['lines'], entry)
    okay, reason = standing(readings)
    if not okay:
        raise RuntimeError(f'SETUP: cpu_standing: entry {entry} {reason}')
    return routes._save(ctx, 'fixture-cpu-standing', {
        'entry': entry, 'frames': frames, 'first_frame': readings[0][0], 'last_frame': readings[-1][0],
        'x': readings[0][1]['x'], 'y': readings[0][1]['y']})


def partner_effective(ctx, eb, ab=None, entry=4, frames=120, kind=None):
    watch = fresh_watch(ctx, [entry], frames, 'fixture-partner-effective-watch')
    readings = samples(watch['lines'], entry)
    expected_ab = str(eb if ab is None else ab)
    for frame, fields in readings:
        if (fields.get('eb') != str(eb) or fields.get('ab') != expected_ab or
                (kind is not None and fields.get('kind') != str(kind))):
            raise RuntimeError(f'SETUP: partner_effective: entry {entry} frame {frame}: '
                               f'eb={fields.get("eb")} ab={fields.get("ab")} kind={fields.get("kind")}, '
                               f'expected eb={eb} ab={expected_ab} kind={kind if kind is not None else "any"}')
    return routes._save(ctx, 'fixture-partner-effective', {
        'entry': entry, 'frames': frames, 'first_frame': readings[0][0], 'last_frame': readings[-1][0],
        'eb': str(eb), 'ab': expected_ab, 'kind': kind,
        'cpu': sorted({fields['cpu'] for _, fields in readings if 'cpu' in fields})})


def _read(ctx, provider, predicate, baseline=None, ack=False, boot=None):
    last = {}

    def accept(block):
        last.clear()
        last.update(block)
        if block['schema'] != provider + '/1' or block['filtered']:
            return False, 'incomplete state block'
        if baseline is not None:
            okay, reason = state_gate.satisfies(block, {}, baseline, ack=1 if ack else None, input_gate=ack)
            if not okay:
                return False, reason
        result = predicate(block['values'])
        if boot is not None and block['boot'] != boot:
            return False, f'boot {block["boot"]} != expected {boot}'
        return result

    try:
        block, _ = ctx.runner._state_block(ctx.on, provider, 8, accept, ask_every=.08)
        return block
    except RuntimeError as error:
        raise RuntimeError(f'{error}; last read {last.get("values", {})!r}') from error


def _role_read(block, key):
    return {'boot': block['boot'], 'rev': block['rev'], 'role': block['values'].get(key)}


def recording_role(ctx, role, where='page', scene='training'):
    if role not in ('player', 'cpu') or where not in ('page', 'popup'):
        raise ValueError('role must be player or cpu, and where must be page or popup')

    def read(expected, baseline=None, ack=False):
        def matches(values):
            okay = all(values.get(key) == value for key, value in expected.items())
            return okay, f'expected {expected!r}, read {values!r}'
        return _read(ctx, 'tr.menu', matches, baseline, ack)

    try:
        if where == 'page':
            before = read({'scene': scene, 'page': 'recording', 'open': '1'})
            current = before['values'].get('recording.page.role')
            if current not in ('player', 'cpu'):
                raise RuntimeError(f'recording.page.role={current!r}')
            after = before
            if current != role:
                routes.play(ctx, 'tap-zr1')
                after = read({'scene': scene, 'page': 'recording', 'open': '1',
                              'recording.page.role': role}, before, ack=True)
        else:
            before = read({'scene': scene, 'open': '0'})
            if before['values'].get('recording.popup.role') == role:
                reading = _role_read(before, 'recording.popup.role')
                return routes._save(ctx, 'fixture-recording-role', {
                    'role': role, 'where': where, 'before': reading, 'after': reading})
            opened = before
            popup = before['values'].get('recording.popup.open')
            if popup == '0':
                routes.play(ctx, 'recording-popup-chord')
                opened = read({'scene': scene, 'open': '0', 'recording.popup.open': '1'}, before)
            elif popup != '1':
                raise RuntimeError(f'recording.popup.open={popup!r}')
            current = opened['values'].get('recording.popup.role')
            if current not in ('player', 'cpu'):
                raise RuntimeError(f'recording.popup.role={current!r}')
            selected = opened
            if current != role:
                routes.play(ctx, 'tap-l1' if role == 'player' else 'tap-r1')
                selected = read({'scene': scene, 'open': '0', 'recording.popup.open': '1',
                                 'recording.popup.role': role}, opened, ack=True)
            routes.play(ctx, 'tap-b1')
            after = read({'scene': scene, 'open': '0', 'recording.popup.open': '0',
                          'recording.popup.role': role}, selected)
        key = 'recording.page.role' if where == 'page' else 'recording.popup.role'
        return routes._save(ctx, 'fixture-recording-role', {
            'role': role, 'where': where, 'before': _role_read(before, key), 'after': _role_read(after, key)})
    except RuntimeError as error:
        raise RuntimeError(f'SETUP: recording_role: {error}') from error


def guard_first_use(ctx, scene, game='guard_edge'):
    class WrongGame(Exception):
        pass

    def practice_read(predicate, baseline=None):
        def checked(values):
            if values.get('game') != game:
                raise WrongGame(values.get('game'))
            return predicate(values)

        return _read(ctx, 'tr.practice', checked, baseline)

    def game_error(error):
        return RuntimeError(f'SETUP: guard_first_use: tr.practice game={error.args[0]}, expected {game}')

    def any_phase(values):
        return ('phase' in values, f'phase missing: {values!r}')

    try:
        current = practice_read(any_phase)
    except WrongGame as error:
        raise game_error(error) from error
    except RuntimeError as error:
        raise RuntimeError(f'SETUP: guard_first_use: {error}') from error
    steps = []
    while current['values'].get('phase') == 'wizard':
        step = current['values'].get('wizard.step')
        following = {'cpu': 'player', 'player': 'preview', 'preview': None}
        if step not in following:
            raise RuntimeError(f'SETUP: guard_first_use: unknown wizard.step={step!r}')
        before = current
        steps.append(step)
        routes.play(ctx, 'tap-a1')

        def advanced(values):
            passed = ('phase' in values and values['phase'] != 'wizard' if following[step] is None
                      else values.get('phase') == 'wizard' and values.get('wizard.step') == following[step])
            return passed, f'wizard.step={values.get("wizard.step")!r} phase={values.get("phase")!r}'

        try:
            current = practice_read(advanced, before)
        except WrongGame as error:
            raise game_error(error) from error
        except RuntimeError as error:
            try:
                menu = _read(ctx, 'tr.menu', lambda v: (v.get('scene') == scene, f'scene={v.get("scene")!r}'))
                if menu['boot'] != before['boot']:
                    raise RuntimeError('bridge boot changed')
                values = menu['values']
                notice = f'{values.get("notice.header", "")} / {values.get("notice.body", "")}'
            except RuntimeError as notice_error:
                notice = f'notice unavailable ({notice_error})'
            raise RuntimeError(f'SETUP: guard_first_use: stuck at wizard.step={step}: {notice}') from error
    observed = {}

    def is_ready(values):
        observed.clear()
        observed.update(values)
        body = values.get('notice.body')
        okay = (values.get('scene') == scene and values.get('game.running') == '1'
                and isinstance(body, str) and body.startswith('Ready'))
        return okay, f'ready state: {values!r}'

    try:
        ready = _read(ctx, 'tr.menu', is_ready, boot=current['boot'])
    except RuntimeError as error:
        notice = f'{observed.get("notice.header", "")} / {observed.get("notice.body", "")}'
        raise RuntimeError(f'SETUP: guard_first_use: not Ready: {notice}') from error
    return routes._save(ctx, 'fixture-guard-first-use', {
        'steps': steps, 'phase': current['values']['phase'], 'boot': current['boot'], 'rev': current['rev'],
        'ready': {'boot': ready['boot'], 'rev': ready['rev'], 'notice.body': ready['values']['notice.body']}})


def _ui(ctx, screen, **where):
    expected = {'screen': screen, **where}

    def matches(values):
        okay = all(values.get(key) == value for key, value in expected.items())
        return okay, f'expected {expected!r}, read {values!r}'

    return _read(ctx, 'ssbu.ui', matches)


def mark_before_room(ctx):
    try:
        native = _ui(ctx, 'main_menu', **{'observation.active': 'yes', 'input.owner': 'native'})
        path = routes.instance_path(ctx, 'bridge_log')
        location = path.stat()
        return routes._save(ctx, ROOM_MARK, {'boot': native['boot'], 'bridge_log': str(path),
                                            'device': location.st_dev, 'inode': location.st_ino,
                                            'offset': location.st_size})
    except (OSError, KeyError, RuntimeError) as error:
        raise RuntimeError(f'SETUP: mark_before_room: {error}') from error


def _waiting_battle(state):
    scene = state.get('scene', {})
    roster = state.get('roster', {})
    return (isinstance(scene, dict) and isinstance(roster, dict)
            and scene.get('class') == 'local_wireless' and scene.get('battle') is True
            and scene.get('melee') == 0x0d and {0, 4}.issubset(roster))


def seeded_room(ctx, behavior, stage, partners=1, mark_row=None):
    mark_dir = Path(ctx.row_dir) if mark_row is None else Path(ctx.runner.out) / mark_row
    mark_file = mark_dir / f'{ROOM_MARK}.json'
    try:
        try:
            mark = json.loads(mark_file.read_bytes())
        except (OSError, ValueError) as error:
            raise RuntimeError(f'no readable room mark at {mark_file}: {error}') from error
        path = routes.instance_path(ctx, 'bridge_log')
        if str(path) != mark['bridge_log']:
            raise RuntimeError(f'bridge log path {path} differs from marked {mark["bridge_log"]}')
        offset = mark['offset']
        if type(offset) is not int or offset < 0:
            raise RuntimeError(f'invalid marked offset {offset!r}')
        before = _ui(ctx, 'ldn.waiting', **{'ldn.role': 'host', 'observation.active': 'yes'})
        if before['boot'] != mark['boot']:
            raise RuntimeError(f'boot {before["boot"]} differs from marked {mark["boot"]}')

        def waiting_battle(state):
            return _waiting_battle(state) and type(state.get('stage')) is int and state['stage'] >= 0

        try:
            native = routes.wait_native(ctx, waiting_battle, cap=8, fresh=('scene', 'roster'),
                                        roster_entries=(0, 4))
        except RuntimeError as error:
            raise RuntimeError(f'waiting battle: {error}') from error
        settings = routes._current_settings(ctx, 8)
        after = _ui(ctx, 'ldn.waiting', **{'ldn.role': 'host', 'observation.active': 'yes'})
        if after['boot'] != before['boot']:
            raise RuntimeError(f'boot {after["boot"]} differs from waiting boot {before["boot"]}')
        transfers = []
        with path.open('rb') as stream:
            location = os.fstat(stream.fileno())
            if (location.st_dev, location.st_ino) != (mark['device'], mark['inode']):
                raise RuntimeError(f'bridge log device/inode {(location.st_dev, location.st_ino)} differs from '
                                   f'marked {(mark["device"], mark["inode"])}')
            if location.st_size < offset:
                raise RuntimeError(f'bridge log size {location.st_size} is before marked offset {offset}')
            stream.seek(offset)
            for raw in stream:
                if not raw.endswith(b'\n'):
                    continue
                if match := STAGE_LINE.search(raw.decode(errors='replace').rstrip()):
                    transfers.append((int(match[1]), int(match[2])))
        if not transfers:
            raise RuntimeError(f'no complete stage line after marked offset {offset}')
        found, frame = transfers[-1]
        if found != stage:
            raise RuntimeError(f'stage read {found}; expected {stage}')
        if native.get('stage') != stage:
            raise RuntimeError(f'native stage read {native.get("stage")!r}; expected {stage}')
        native_frame = native.get('frame')
        if type(native_frame) is not int or frame > native_frame:
            raise RuntimeError(f'stage frame {frame} is after native frame {native_frame!r}')
        if not isinstance(settings, dict):
            raise RuntimeError(f'settings read {settings!r}; expected an object')
        menu = settings.get('menu')
        practice = menu.get('lobby_practice') if isinstance(menu, dict) else None
        if practice != [1, 0]:
            raise RuntimeError(f'lobby_practice read {practice!r}; expected [1, 0]')
        panel = settings.get('lobby_practice_panel')
        for key, expected in (('partners', partners), ('behavior', behavior)):
            value = panel.get(key) if isinstance(panel, dict) else None
            if type(value) is not int or value != expected:
                raise RuntimeError(f'{key} expected {expected!r}; read {value!r}')
        return routes._save(ctx, 'fixture-seeded-room', {
            'behavior': behavior, 'behavior_source': 'settings', 'partners': partners, 'stage': stage, 'stage_frame': frame,
            'native_frame': native_frame, 'boot': before['boot'], 'mark': str(mark_file)})
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        raise RuntimeError(f'SETUP: seeded_room: {error}') from error


def seed_settings(base, behavior, stage, partners=1):
    if not isinstance(base, dict) or not isinstance(base.get('menu'), dict):
        raise ValueError('menu must be an object')
    preferences = base.get('stage_preferences', {})
    order = base.get('lobby_stage_order', {})
    if not isinstance(preferences, dict):
        raise ValueError('stage_preferences must be an object')
    if not isinstance(order, dict):
        raise ValueError('lobby_stage_order must be an object')
    if order.get('direct_custom') is not None:
        raise ValueError('an inherited custom Direct stage would override the seeded stage')
    settings = copy.deepcopy(base)
    settings['menu']['lobby_practice'] = [1, 0]
    settings['lobby_practice_panel'] = {'partners': partners, 'behavior': behavior}
    settings['stage_preferences'] = dict(settings.get('stage_preferences', {}), mode='direct',
                                         direct_stage=stage, direct_form='normal')
    return settings


def window_from_wait(samples, entry, frames):
    """Skip the baseline: 300 initial grounded Wait samples count as 299 of 300."""
    if frames < 1:
        raise ValueError('frames must be positive')
    stream = [item for item in samples if int(item[1].get('entry', entry)) == entry]
    start = next((i for i, (_, fields, _) in enumerate(stream[1:], 1)
                  if int(fields['status']) == 0 and int(fields['situation']) == 0), None)
    window = []
    if start is not None:
        for item in stream[start:]:
            frame, fields, _ = item
            if (int(fields['status']) != 0 or int(fields['situation']) != 0
                    or (window and frame != window[-1][0] + 1)):
                break
            window.append(item)
            if len(window) == frames:
                return window
    raise RuntimeError(f'grounded Wait window has {len(window)} of {frames} frames')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Write an offline waiting-room settings seed.')
    commands = parser.add_subparsers(dest='command', required=True)
    seed = commands.add_parser('seed')
    seed.add_argument('--base', required=True)
    seed.add_argument('--out', required=True)
    seed.add_argument('--behavior', required=True, type=int)
    seed.add_argument('--stage', required=True, type=int)
    seed.add_argument('--partners', default=1, type=int)
    args = parser.parse_args(argv)
    base = json.loads(Path(args.base).read_bytes())
    settings = seed_settings(base, args.behavior, args.stage, args.partners)
    data = (json.dumps(settings, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    path = Path(args.out)
    with path.open('xb') as stream:
        stream.write(data)
    if path.read_bytes() != data:
        raise RuntimeError(f'seed settings readback differs: {path}')


if __name__ == '__main__':
    main()
