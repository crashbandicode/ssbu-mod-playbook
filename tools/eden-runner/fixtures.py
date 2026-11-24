"""Shared setup fixtures."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys

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
}

ROOM_MARK = 'fixture-room-mark'
STAGE_LINE = re.compile(r'\[Bridge\] stage id=(-?\d+)\s.*\bframe=(\d+)\s')


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


def watch_window(lines, entries, frames, min_frame):
    return [], False


def check_watch(window_lines, entries, frames):
    return []


def samples(window_lines, entry):
    return []


def standing(readings):
    return False, "not implemented"


def fresh_watch(ctx, entries, frames, name="fixture-watch"):
    return {}


def cpu_standing(ctx, entry=1, frames=120):
    return {}


def partner_effective(ctx, eb, ab=None, entry=4, frames=120, kind=None):
    return {}


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
