#!/usr/bin/env python3
"""Offline tests for routes.py and routes_online.py: a fake Eden and fake plugin logs, no emulator.

Run: python3 -I test_routes.py
"""
import json
import os
import socket
import sys
import tempfile
import time
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import routes  # noqa: E402
import routes_online  # noqa: E402
import runner  # noqa: E402
import test_native_picker as picker_fixtures  # noqa: E402


class LogCursorTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / 'practice-diagnostics.log'
        self.path.write_text('old 1\nold 2\n')

    def test_only_new_lines(self):
        cursor = routes.LogCursor(self.path)
        with self.path.open('a') as f:
            f.write('new 1\nnew')
        self.assertEqual(cursor.read(), ['new 1'])
        with self.path.open('a') as f:
            f.write(' 2\n')
        self.assertEqual(cursor.read(), ['new 2'])

    @unittest.skipIf(os.name == 'nt', 'Windows uses the unheld cursor; a held handle blocks the rename')
    def test_rotation_drains_old_file_then_reads_new(self):
        cursor = routes.LogCursor(self.path)
        with self.path.open('a') as f:
            f.write('before rename\n')
        os.replace(self.path, self.dir / 'practice-diagnostics.previous.log')
        with (self.dir / 'practice-diagnostics.previous.log').open('a') as f:
            f.write('late write to old\n')
        self.path.write_text('first of new\n')
        self.assertEqual(cursor.read(), ['before rename', 'late write to old', 'first of new'])
        self.assertEqual(cursor.rotations, 1)
        with self.path.open('a') as f:
            f.write('second of new\n')
        self.assertEqual(cursor.read(), ['second of new'])

    def test_unheld_mode_for_windows(self):
        cursor = routes.LogCursor(self.path, hold=False)
        self.assertIsNone(cursor.handle)
        with self.path.open('a') as f:
            f.write('a\n')
        self.assertEqual(cursor.read(), ['a'])
        os.replace(self.path, self.dir / 'practice-diagnostics.previous.log')  # works: no handle is held
        self.path.write_text('new\n')
        self.assertEqual(cursor.read(), ['new'])
        self.assertEqual(cursor.rotations, 1)
        self.path.write_text('t\n')  # truncated below the cursor's offset
        self.assertEqual(cursor.read(), ['t'])

    def test_truncation_reads_from_start(self):
        cursor = routes.LogCursor(self.path)
        self.path.write_text('x\n')
        self.assertEqual(cursor.read(), ['x'])

    def test_missing_file_then_created(self):
        missing = self.dir / 'later.log'
        cursor = routes.LogCursor(missing)
        self.assertEqual(cursor.read(), [])
        missing.write_text('hello\n')
        self.assertEqual(cursor.read(), ['hello'])

    def test_wait_reports_lines_seen(self):
        cursor = routes.LogCursor(self.path)
        with self.path.open('a') as f:
            f.write('nothing here\n')
        with self.assertRaises(RuntimeError) as raised:
            cursor.wait('never', 0.2)
        self.assertIn('nothing here', str(raised.exception))


class FakeTileGame:
    """A tile grid with the tile cursor's log lines. widths: tiles per row."""

    def __init__(self, log, widths, screen='sss', start=0, holding=True):
        self.log, self.widths, self.screen, self.at, self.holding = log, widths, screen, start, holding
        self.rows = []
        n = 0
        for w in widths:
            self.rows.append(list(range(n, n + w)))
            n += w

    def where(self, tile):
        for r, row in enumerate(self.rows):
            if tile in row:
                return r, row.index(tile)
        raise KeyError(tile)

    def step(self, direction):
        if not self.holding:
            return
        r, c = self.where(self.at)
        target = None
        if direction == 'left' and c > 0:
            target = self.rows[r][c - 1]
        elif direction == 'right' and c + 1 < len(self.rows[r]):
            target = self.rows[r][c + 1]
        elif direction == 'up' and r > 0:
            target = self.rows[r - 1][min(c, len(self.rows[r - 1]) - 1)]
        elif direction == 'down' and r + 1 < len(self.rows):
            target = self.rows[r + 1][min(c, len(self.rows[r + 1]) - 1)]
        with self.log.open('a') as f:
            f.write(f'1 step screen={self.screen} dir={direction} from={self.at} to={target if target is not None else "none"} x=0.0 y=0.0\n')
            if target is not None:
                self.at = target
                hovered = target if self.screen == 'css' else 'none'
                f.write(f'2 settled screen={self.screen} at={target} hovered={hovered} x=0.0 y=0.0\n')


class FakeRunner:
    def __init__(self, instance, game):
        self.instances = {'s': instance}
        self.plan = {'gates': {}}
        self.game = game
        self.played = []

    def call(self, on, method, params=None, job_cap=15):
        if method == 'macro.play':
            name = Path(params['path']).name.split('.')[0]
            self.played.append(name)
            if name.startswith('tile-'):
                self.game.step(name.split('-', 1)[1])
        return {}

    def wait_macro(self, on, cap):
        return None

    def log(self, record):
        pass

    def check_stop(self):
        pass


class FakeCtx:
    def __init__(self, runner_obj, row_dir):
        self.runner, self.on = runner_obj, 's'
        self.instance = runner_obj.instances['s']
        self.row_dir = row_dir

    def call(self, method, params=None, job_cap=15):
        return self.runner.call(self.on, method, params, job_cap)

    def log(self, record):
        pass

    def check_stop(self):
        pass

    def shot(self, name):
        return f'{name}.png'


class HostNetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.out = self.run / 'out'
        self.row = self.out / 'r00'
        self.row.mkdir(parents=True)
        self.config = self.run / 'config.json'
        self.config.write_text(json.dumps({'network_interface': 'wifi0'}))
        self.values = {}
        self.calls = []
        self.running = False
        self.wrong_readback = False
        self.ldn_error = None
        self.ctx = types.SimpleNamespace(runner=types.SimpleNamespace(out=self.out),
                                         row_dir=self.row, call=self.call)

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == 'status':
            return {'running': self.running}
        if method == 'settings.set':
            self.values[params['key']] = params['value']
        if method == 'settings.get':
            if params['key'] == 'core.ldn_mitm' and self.ldn_error:
                raise RuntimeError(self.ldn_error)
            if params['key'] == 'core.ldn_mitm' and params['key'] not in self.values:
                return {'value': True}
            value = self.values[params['key']]
            return {'value': 'wrong' if self.wrong_readback else value}
        return {}

    def test_reads_base_and_sets_and_reads_network_settings_without_saving(self):
        result = routes.host_network(self.ctx)
        self.assertEqual(result, {
            'block': 'host_network', 'interface': 'wifi0', 'lan_online': False, 'ldn_mitm': False,
            'readback': {'core.network_interface': 'wifi0', 'core.lan_online': False, 'core.ldn_mitm': False}})
        self.assertEqual(json.loads((self.row / 'routes-host-network.json').read_text()), result)
        self.assertEqual([name for name, _ in self.calls],
                         ['status', 'settings.set', 'settings.get', 'settings.set', 'settings.get',
                          'settings.get', 'settings.set', 'settings.get'])

    def test_argument_overrides_config_and_lan_online(self):
        result = routes.host_network(self.ctx, interface='ethernet0', lan_online=True)
        self.assertEqual(result['interface'], 'ethernet0')
        self.assertEqual(result['readback'], {
            'core.network_interface': 'ethernet0', 'core.lan_online': True, 'core.ldn_mitm': False})

    def test_absent_ldn_mitm_is_recorded_but_other_errors_propagate(self):
        self.ldn_error = "settings.get on a: {'ok': False, 'error': {'code': 'unknown_setting'}}"
        result = routes.host_network(self.ctx)
        self.assertEqual(result['readback']['core.ldn_mitm'], 'absent')
        self.assertNotIn('core.ldn_mitm', self.values)
        self.ldn_error = 'settings.get on a: connection lost'
        with self.assertRaisesRegex(RuntimeError, 'connection lost'):
            routes.host_network(self.ctx)

    def test_explicit_ldn_mitm_is_set_and_read_back(self):
        result = routes.host_network(self.ctx, ldn_mitm=True)
        self.assertTrue(result['ldn_mitm'])
        self.assertTrue(result['readback']['core.ldn_mitm'])
        self.assertTrue(self.values['core.ldn_mitm'])

    def test_rejects_loopback_missing_interface_and_running_game_before_setting(self):
        for interface, running, expected in (
            ('lo', False, 'host_network: Eden never lists loopback, so "lo" means no network and no local wireless room; name a real interface'),
            (None, False, 'host_network: no interface: pass "interface" or use a base that names "network_interface"'),
            ('wifi0', True, 'host_network runs before game.load: core.network_interface is not changeable at run time'),
        ):
            with self.subTest(interface=interface, running=running):
                self.config.write_text('{}')
                self.running = running
                self.calls.clear()
                with self.assertRaises(RuntimeError) as caught:
                    routes.host_network(self.ctx, interface=interface)
                self.assertEqual(str(caught.exception), expected)
                self.assertFalse(any(name == 'settings.set' for name, _ in self.calls))

    def test_readback_mismatch_refuses(self):
        self.wrong_readback = True
        with self.assertRaisesRegex(RuntimeError, 'setting core.network_interface reads'):
            routes.host_network(self.ctx)


class LoadGameNetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.out = self.run / 'out'
        self.row = self.out / 'r00'
        self.row.mkdir(parents=True)
        self.config = self.run / 'config.json'
        self.calls = []
        self.live = 'wifi0'
        self.ctx = types.SimpleNamespace(
            runner=types.SimpleNamespace(out=self.out), row_dir=self.row, instance={'game': '/fake/game.xci'},
            call=self.call, check_stop=lambda: None)

    def call(self, method, params=None, job_cap=15):
        self.calls.append((method, params))
        if method == 'settings.get':
            return {'value': self.live}
        if method == 'status':
            return {'running': True, 'loading_complete': True, 'shaders_building': False, 'game_fps': 60}
        return {}

    def test_different_live_interface_refuses_before_game_load(self):
        self.config.write_text(json.dumps({'network_check': {'interface': 'wifi0', 'no_lobby': False}}))
        self.live = 'lo'
        with self.assertRaises(RuntimeError) as caught:
            routes.load_game(self.ctx)
        self.assertEqual(str(caught.exception),
                         'load_game: core.network_interface is "lo" but this run\'s checked interface is "wifi0"; '
                         'a step changed it after host_network. No game was loaded.')
        self.assertEqual([name for name, _ in self.calls], ['settings.get'])

    def test_matching_interface_is_checked_and_recorded_before_load(self):
        self.config.write_text(json.dumps({'network_check': {'interface': 'wifi0', 'no_lobby': False}}))
        result = routes.load_game(self.ctx)
        self.assertEqual([name for name, _ in self.calls], ['settings.get', 'game.load', 'status'])
        self.assertEqual(result['network_interface'], 'wifi0')
        self.assertEqual(json.loads((self.row / 'routes-load.json').read_text()), result)

    def test_no_config_or_no_lobby_loads_without_network_check(self):
        for config in (None, {}, {'network_check': {'interface': 'wifi0', 'no_lobby': True}}):
            with self.subTest(config=config):
                self.calls.clear()
                if config is None:
                    self.config.unlink(missing_ok=True)
                else:
                    self.config.write_text(json.dumps(config))
                result = routes.load_game(self.ctx)
                self.assertEqual([name for name, _ in self.calls], ['game.load', 'status'])
                self.assertNotIn('network_interface', result)


class FrontendRoomTests(unittest.TestCase):
    # p00-rooms.json in phase23/1f-save-hold/ldn-pair/rc15-ldn-xps-2/out.
    HOST_ACTION = {'id': 'o130', 'class': 'QAction', 'name': 'action_Start_Room', 'text': '&Create Room'}
    HOST_DIALOG = {'id': 'o217', 'class': 'QLineEdit', 'name': 'username', 'text': None}
    HOST_LEAVE = {'id': 'o272', 'class': 'QPushButton', 'name': 'disconnect', 'text': 'Leave Room'}
    PEER_ACTION = {'id': 'o53', 'class': 'QAction', 'name': 'action_Connect_To_Room',
                   'text': '&Direct Connect to Room'}
    PEER_DIALOG = {'id': 'o220', 'class': 'QLineEdit', 'name': 'nickname', 'text': None}
    ROOM = {'id': 'o259', 'class': 'ClientRoomWindow', 'name': 'ClientRoom', 'text': None,
            # xps-1/out/c2-match-end/a-GATE-FAILED-trace-sched-tree.json, 2 changed to 1.
            'title': 'TRB3Waiting - Super Smash Bros. Ultimate (1/8 members) - connected'}
    # tss-lobby-17060a5e-1: final-L2-live-widgets.json.log, captured before any recovery.
    MESSAGE = {'id': 'o259', 'class': 'QMessageBox', 'name': '', 'text': None, 'title': 'Error',
               'visible': True, 'enabled': True, 'parent': '', 'window': 'o259',
               'properties': {'text': {'type': 'QString', 'value':
                                       'Room name is not valid. Must be 4 to 20 alphanumeric characters.',
                                       'writable': True}}}
    OPEN_HOST = {'id': 'o244', 'class': 'QLineEdit', 'name': 'username', 'text': None, 'title': '',
                 'visible': True, 'enabled': True, 'parent': 'o214', 'window': 'o235'}
    OK = {'id': 'o261', 'class': 'QPushButton', 'name': '', 'text': 'OK', 'title': '', 'visible': True,
          'enabled': True, 'parent': 'o262', 'window': 'o259'}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.out = self.run / 'out'
        self.row = self.out / 'r00'
        self.row.mkdir(parents=True)
        (self.run / 'config.json').write_text(json.dumps({'label': 'proof'}))
        self.actions = []
        self.running = False
        self.nodes = [self.HOST_ACTION, self.PEER_ACTION]
        self.room_node = self.ROOM
        self.modal_after_host = False
        self.runner = types.SimpleNamespace(out=self.out, ui=self.ui, ui_matches=runner.Runner.ui_matches)
        self.ctx = types.SimpleNamespace(runner=self.runner, on='a', row_dir=self.row,
                                         call=self.call, check_stop=lambda: None)

    def call(self, method, params=None, job_cap=15):
        if method == 'status':
            return {'running': self.running}
        if method == 'ui.tree':
            return self.nodes
        raise AssertionError(method)

    def ui(self, on, step):
        self.actions.append((on, step))
        target = step.get('target', {})
        if step['ui'] == 'trigger':
            self.nodes.append(self.HOST_DIALOG if target['name'] == 'action_Start_Room' else self.PEER_DIALOG)
        elif step['ui'] == 'click' and target.get('id') == self.OK['id']:
            self.nodes = [node for node in self.nodes if node not in (self.MESSAGE, self.OK)]
        elif step['ui'] == 'click' and target.get('name') in ('host', 'connect'):
            if self.modal_after_host:
                self.nodes.extend([self.MESSAGE, self.OK])
            else:
                self.nodes.append(self.HOST_LEAVE)
                if self.room_node is not None:
                    self.nodes.append(self.room_node)

    def test_host_replays_real_sequence_and_saves_room_handoff(self):
        with mock.patch.object(routes, 'free_udp_port', return_value=24872):
            receipt = routes.frontend_room(self.ctx)
        target = lambda cls, name: {'class': cls, 'name': name}
        self.assertEqual(self.actions, [('a', step) for step in [
            {'ui': 'trigger', 'target': target('QAction', 'action_Start_Room')},
            {'ui': 'set', 'target': target('QLineEdit', 'username'), 'property': 'text', 'value': 'TRHost'},
            {'ui': 'set', 'target': target('QLineEdit', 'room_name'), 'property': 'text', 'value': 'TR24872'},
            {'ui': 'set', 'target': target('QLineEdit', 'port'), 'property': 'text', 'value': '24872'},
            {'ui': 'set', 'target': target('QComboBox', 'host_type'), 'property': 'currentIndex', 'value': 1},
            {'ui': 'click', 'target': target('QPushButton', 'host')}]])
        self.assertEqual(receipt['port'], 24872)
        self.assertEqual(receipt['room_name'], 'TR24872')
        self.assertFalse(receipt['game_running'])
        self.assertEqual(receipt['member_count'], 1)
        self.assertEqual(receipt['window_title'], self.ROOM['title'])
        self.assertEqual(receipt['evidence']['host_action'], self.HOST_ACTION)
        self.assertEqual(receipt['evidence']['host_dialog'], self.HOST_DIALOG)
        self.assertEqual(receipt['evidence']['frontend_leave'], self.HOST_LEAVE)
        self.assertEqual(json.loads((self.row / 'routes-frontend-room.json').read_text()), receipt)
        self.assertEqual(json.loads((self.out / 'frontend-room.json').read_text()),
                         {'port': 24872, 'room_name': 'TR24872', 'instance': 'a'})

    def test_join_replays_peer_sequence_and_reads_host_port(self):
        (self.out / 'frontend-room.json').write_text(json.dumps({'port': 24872, 'room_name': 'TR-proof', 'instance': 'a'}))
        self.ctx.on = 'b'
        receipt = routes.frontend_room(self.ctx, role='join', nickname='Guest', ip='127.0.0.1')
        target = lambda cls, name: {'class': cls, 'name': name}
        self.assertEqual(self.actions, [('b', step) for step in [
            {'ui': 'trigger', 'target': target('QAction', 'action_Connect_To_Room')},
            {'ui': 'set', 'target': target('QLineEdit', 'nickname'), 'property': 'text', 'value': 'Guest'},
            {'ui': 'set', 'target': target('QLineEdit', 'ip'), 'property': 'text', 'value': '127.0.0.1'},
            {'ui': 'set', 'target': target('QLineEdit', 'port'), 'property': 'text', 'value': '24872'},
            {'ui': 'click', 'target': target('QPushButton', 'connect')}]])
        self.assertEqual(receipt['port'], 24872)
        self.assertEqual(receipt['evidence']['peer_action'], self.PEER_ACTION)
        self.assertEqual(receipt['evidence']['peer_dialog'], self.PEER_DIALOG)

    def test_no_connected_window_and_insufficient_members_fail_with_observed_titles(self):
        self.room_node = None
        with self.assertRaisesRegex(RuntimeError, 'at least 1 member\\(s\\); saw: no room window'):
            routes.frontend_room(self.ctx, port=24872, cap=.01)
        self.nodes = [self.HOST_ACTION, self.PEER_ACTION]
        self.room_node = self.ROOM
        with self.assertRaisesRegex(RuntimeError, r'at least 2 member\(s\); saw: .*1/8 members'):
            routes.frontend_room(self.ctx, port=24872, members=2, cap=.01)
        self.nodes = [self.HOST_ACTION, self.PEER_ACTION]
        self.room_node = {**self.ROOM, 'title': self.ROOM['title'].replace(' - connected', ' - disconnected')}
        with self.assertRaisesRegex(RuntimeError, r'at least 1 member\(s\); saw: .*disconnected'):
            routes.frontend_room(self.ctx, port=24872, cap=.01)

    def test_explicit_join_port_and_nested_two_member_title(self):
        title = self.ROOM['title'].replace('(1/8 members)', '(2/8 members)')
        self.room_node = {key: value for key, value in self.ROOM.items() if key != 'title'}
        self.room_node['properties'] = {'windowTitle': {'value': title}}
        result = routes.frontend_room(self.ctx, role='join', port=24890, members=2)
        self.assertEqual((result['nickname'], result['port'], result['member_count'], result['window_title']),
                         ('TRGuest', 24890, 2, title))

    def test_running_game_is_allowed_and_recorded(self):
        self.running = True
        result = routes.frontend_room(self.ctx, port=24872)
        self.assertTrue(result['game_running'])
        result = routes.frontend_room(self.ctx, port=24872)
        self.assertTrue(result['already_connected'])
        self.assertTrue(result['game_running'])

    def test_join_without_port_and_unknown_role_refuse_before_ui(self):
        with self.assertRaisesRegex(RuntimeError, 'frontend_room:.*port'):
            routes.frontend_room(self.ctx, role='join')
        with self.assertRaisesRegex(RuntimeError, 'frontend_room:.*role'):
            routes.frontend_room(self.ctx, role='invalid', port=24872)
        self.assertEqual(self.actions, [])

    def test_udp_selection_skips_a_bound_port(self):
        first = routes.free_udp_port(first=24872, last=24960)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserved:
            reserved.bind(('0.0.0.0', first))
            chosen = routes.free_udp_port(first=first, last=24971)
        self.assertGreater(chosen, first)

    def test_names_default_from_port_and_invalid_values_refuse_before_ui(self):
        (self.run / 'config.json').write_text(json.dumps({'label': 'tss-lobby-17060a5e-1'}))
        with mock.patch.object(routes, 'free_udp_port', return_value=24872):
            result = routes.frontend_room(self.ctx)
        self.assertEqual(result['room_name'], 'TR24872')
        for field, value in (('room_name', 'TR-tss-lobby-17060a5e-1'), ('room_name', ''),
                             ('room_name', 'a' * 21), ('nickname', ''), ('nickname', 'Bob'),
                             ('nickname', 'åUser'), ('nickname', 'bad/name')):
            with self.subTest(field=field, value=value):
                self.actions.clear()
                with self.assertRaises(RuntimeError) as caught:
                    routes.frontend_room(self.ctx, port=24872, **{field: value})
                self.assertEqual(str(caught.exception), f'frontend_room: {field} "{value}" is not valid for Eden: '
                                 '4 to 20 characters of letters, digits, ".", "_", " " or "-" '
                                 '(yuzu/multiplayer/validation.h)')
                self.assertEqual(self.actions, [])

    def test_names_allow_full_boundaries_and_eden_punctuation(self):
        result = routes.frontend_room(self.ctx, port=24872, nickname='A._-', room_name='A._ -123456789012345')
        self.assertEqual(result['nickname'], 'A._-')
        self.assertEqual(result['room_name'], 'A._ -123456789012345')

    def test_message_during_wait_aborts_with_real_text(self):
        self.modal_after_host = True
        with self.assertRaises(RuntimeError) as caught:
            routes.frontend_room(self.ctx, port=24872, cap=.05)
        self.assertIn('frontend_room: Eden showed "Room name is not valid. Must be 4 to 20 alphanumeric characters."',
                      str(caught.exception))
        self.assertIn('while waiting for', str(caught.exception))

    def test_captured_modal_is_dismissed_and_host_dialog_reused(self):
        self.nodes = [self.MESSAGE, self.OK, self.OPEN_HOST]
        result = routes.frontend_room(self.ctx, port=24872)
        self.assertEqual(result['dismissed'], self.MESSAGE['properties']['text']['value'])
        self.assertTrue(result['reused_dialog'])
        self.assertEqual(self.actions[0], ('a', {'ui': 'click',
                                                'target': {'class': 'QPushButton', 'id': 'o261', 'text': '^OK$'}}))
        self.assertFalse(any(step['ui'] == 'trigger' for _, step in self.actions))
        self.assertTrue(any(step['ui'] == 'set' and step['target']['name'] == 'room_name'
                            and step['value'] == 'TR24872' for _, step in self.actions))
        self.assertEqual(result['member_count'], 1)

    def test_modal_without_matching_ok_fails_without_clicking_unrelated_button(self):
        self.nodes = [self.MESSAGE, {**self.OK, 'window': 'other'}, self.OPEN_HOST]
        with self.assertRaisesRegex(RuntimeError, 'frontend_room:.*OK'):
            routes.frontend_room(self.ctx, port=24872)
        self.assertEqual(self.actions, [])

    def test_invalid_name_does_not_dismiss_existing_modal(self):
        self.nodes = [self.MESSAGE, self.OK, self.OPEN_HOST]
        with self.assertRaisesRegex(RuntimeError, 'frontend_room: room_name .* not valid'):
            routes.frontend_room(self.ctx, port=24872, room_name='bad/name')
        self.assertEqual(self.actions, [])

    def test_join_reuses_open_dialog(self):
        self.nodes = [self.PEER_DIALOG]
        result = routes.frontend_room(self.ctx, role='join', port=24872)
        self.assertTrue(result['reused_dialog'])
        self.assertFalse(any(step['ui'] == 'trigger' for _, step in self.actions))
        self.assertEqual(result['member_count'], 1)

    def test_already_connected_returns_without_ui_or_port_allocation(self):
        self.nodes = [self.ROOM]
        with mock.patch.object(routes, 'free_udp_port', side_effect=AssertionError('allocated port')):
            result = routes.frontend_room(self.ctx)
        self.assertTrue(result['already_connected'])
        self.assertEqual((result['window_title'], result['member_count']), (self.ROOM['title'], 1))
        self.assertFalse(result['game_running'])
        self.assertEqual(self.actions, [])
        self.assertEqual(json.loads((self.row / 'routes-frontend-room.json').read_text()), result)

    def test_already_connected_even_with_a_modal_has_no_ui_action(self):
        self.nodes = [{**self.ROOM, 'id': 'o300'}, self.MESSAGE, self.OK]
        result = routes.frontend_room(self.ctx, port=24872)
        self.assertTrue(result['already_connected'])
        self.assertEqual(self.actions, [])

    def test_connected_room_with_too_few_members_is_not_accepted(self):
        self.nodes = [self.HOST_ACTION, self.PEER_ACTION, self.ROOM]
        self.room_node = {**self.ROOM, 'id': 'o400',
                          'title': self.ROOM['title'].replace('(1/8 members)', '(2/8 members)')}
        result = routes.frontend_room(self.ctx, port=24872, members=2)
        self.assertFalse(result['already_connected'])
        self.assertEqual(result['member_count'], 2)
        self.assertTrue(any(step['ui'] == 'trigger' for _, step in self.actions))


class BridgeAliveTests(unittest.TestCase):
    BOOT = '0x901afdfc49eabde3'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / 'bridge.log'
        self.log.write_text('[Adapter] reconnect same_boot=1\n[Adapter] reconnect same_boot=1\n')
        self.ctx = types.SimpleNamespace(instance={'bridge_log': str(self.log)}, on='a',
                                         runner=types.SimpleNamespace(), row_dir=self.root,
                                         check_stop=lambda: None)
        self.requests = []
        self.orphan_end = False
        self.supply = [self.wire('ssbu.ui', 5, 200, 1000), self.wire('ssbu.ui', 6, 201, 1010),
                       self.wire('tr.menu', 1, 202, 1020), self.wire('tr.menu', 1, 203, 1030)]

    @classmethod
    def wire(cls, provider, rev, frame, wall):
        schema = provider + '/1'
        # tr.menu keys and framing follow rc15-intermediate-next/runtime-17060a5e/xps/bridge.log:4077.
        payload = ([f'schema={schema}', f'rev={rev}', f'boot={cls.BOOT}', f'observation={rev}',
                    'observation.active=yes', 'focus=main_menu', 'screen=main_menu'] if provider == 'ssbu.ui' else
                   [f'schema={schema}', 'ack=0', 'ack.result="handled"', 'focus=/-/0/row', 'open=0'])
        suffix = f' frame={frame} wall={wall}\n'
        begin = (f'[Bridge] state begin name={provider} schema={schema} rev={rev} boot={cls.BOOT} '
                 f'lines={len(payload)} filtered=0' + suffix)
        body = ''.join(f'[Bridge] state.{i} {line}' + suffix for i, line in enumerate(payload))
        crc = zlib.crc32('\n'.join(payload).encode())
        return begin + body + f'[Bridge] state end name={provider} rev={rev} crc={crc:08x}' + suffix

    def spool(self, ctx, *requests):
        self.assertEqual(ctx, self.ctx)
        self.requests.append(requests)
        if self.supply:
            with self.log.open('a') as f:
                f.write(f'[Bridge] req providers result=ok count=2 boot={self.BOOT}\n')
                if self.orphan_end:
                    f.write('[Bridge] state end name=ssbu.ui rev=4 crc=00000000 frame=199 wall=990\n')
                f.write(self.supply.pop(0))

    def test_fenced_replies_allow_equal_menu_revision_and_save_reply_clocks(self):
        with mock.patch.object(routes, 'bridge_v2', self.spool):
            result = routes.bridge_alive(self.ctx, cap=2)
        self.assertEqual(result['block'], 'bridge_alive')
        self.assertEqual(result['ssbu.ui'], {'rev': [5, 6], 'screen': 'main_menu'})
        self.assertEqual(result['tr.menu'], {'rev': [1, 1], 'frame': [202, 203], 'wall': [1020, 1030]})
        self.assertEqual(result['reconnects'], 2)
        self.assertEqual(self.requests, [('providers', 'state ssbu.ui')] * 2 +
                         [('providers', 'state tr.menu')] * 2)
        self.assertEqual(json.loads((self.root / 'routes-bridge-alive.json').read_text()), result)

    def test_orphan_end_before_reply_is_ignored(self):
        self.orphan_end = True
        with mock.patch.object(routes, 'bridge_v2', self.spool):
            result = routes.bridge_alive(self.ctx, cap=2)
        self.assertEqual(result['tr.menu']['rev'], [1, 1])

    def test_missing_second_menu_reply_names_provider_and_cap(self):
        self.supply.pop()
        with mock.patch.object(routes, 'bridge_v2', self.spool):
            with self.assertRaisesRegex(RuntimeError, r'bridge_alive: tr\.menu.*0\.1'):
                routes.bridge_alive(self.ctx, cap=.1)

    def test_missing_newer_ui_sample_names_provider_and_cap(self):
        self.supply = [self.wire('ssbu.ui', 5, 200, 1000), self.wire('ssbu.ui', 5, 201, 1010)]
        with mock.patch.object(routes, 'bridge_v2', self.spool):
            with self.assertRaisesRegex(RuntimeError, r'bridge_alive: ssbu\.ui.*0\.1'):
                routes.bridge_alive(self.ctx, cap=.1)

    def test_wrong_boot_on_second_menu_reply_is_refused(self):
        self.supply[3] = self.supply[3].replace(self.BOOT, '0x0000000000000002')
        with mock.patch.object(routes, 'bridge_v2', self.spool):
            with self.assertRaisesRegex(RuntimeError, r'bridge_alive: tr\.menu.*0\.1'):
                routes.bridge_alive(self.ctx, cap=.1)


class NativeStateTests(unittest.TestCase):
    """routes.native_state / wait_native / frame_advancing over a bridge capture's bridge.log."""

    def ctx(self):
        import types
        run = Path(tempfile.mkdtemp())
        (run / 'bridge').mkdir()
        log = run / 'bridge.log'
        log.write_text('[Bridge] stage id=3 frame=100 wall=1\n[Bridge] roster entry=0 kind=0 nana=false frame=100 wall=1\n')
        return types.SimpleNamespace(instance={'bridge_log': str(log)}, check_stop=lambda: None), log

    def test_native_state_reads_the_bridge_log(self):
        ctx, _ = self.ctx()
        state = routes.native_state(ctx)
        self.assertEqual((state['stage'], state['frame'], state['roster'][0]['kind']), (3, 100, 0))

    def test_frame_advancing_waits_for_the_counter(self):
        import threading
        ctx, log = self.ctx()

        def run():
            for frame in range(110, 200, 10):
                time.sleep(0.05)
                with log.open('a') as f:
                    f.write(f'[Bridge] item entry=0 held=false kind=-1 frame={frame} wall=2\n')

        worker = threading.Thread(target=run)
        worker.start()
        state = routes.frame_advancing(ctx, frames=50, cap=5)
        worker.join()
        self.assertGreaterEqual(state['frame'], 150)

    def test_wait_native_reports_the_last_state_on_timeout(self):
        ctx, _ = self.ctx()
        with self.assertRaisesRegex(RuntimeError, 'frame=100 stage=3'):
            routes.wait_native(ctx, lambda s: s.get('stage') == 8, cap=0.3, every=0.05)


class TileWalkerTests(unittest.TestCase):
    def walker(self, widths, start, screen='sss'):
        folder = Path(tempfile.mkdtemp())
        log = folder / 'events.log'
        log.write_text('')
        game = FakeTileGame(log, widths, screen, start)
        ctx = FakeCtx(FakeRunner({'tile_log': str(log)}, game), folder)
        return routes.TileWalker(ctx, screen), game, ctx

    def test_walks_to_target_by_rows_read_from_the_log(self):
        walker, game, ctx = self.walker([11] * 10 + [6], start=0)
        out = walker.walk_to(4)
        self.assertEqual(out['at'], 4)
        self.assertEqual(game.at, 4)
        walker, game, ctx = self.walker([11] * 10 + [6], start=37)
        self.assertEqual(walker.walk_to(57)['at'], 57)
        self.assertEqual(game.at, 57)

    def test_last_short_row(self):
        walker, game, ctx = self.walker([13] * 6 + [9], start=0, screen='css')
        self.assertEqual(walker.walk_to(85)['at'], 85)

    def test_plain_presses_are_never_sent(self):
        walker, game, ctx = self.walker([11] * 3, start=5)
        walker.walk_to(14)
        self.assertTrue(all(name.startswith('tile-') for name in ctx.runner.played))

    def test_settled_mismatch_fails(self):
        walker, game, ctx = self.walker([11] * 3, start=0)
        log = Path(ctx.instance['tile_log'])

        def lying(direction):
            with log.open('a') as f:
                f.write(f'1 step screen=sss dir={direction} from=0 to=1 x=0.0 y=0.0\n'
                        '3 settled screen=sss at=99 hovered=none\n')
        game.step = lying
        with self.assertRaises(RuntimeError):
            walker.step('right')


class StageTransitionTests(unittest.TestCase):
    def test_generic_stage_finish_rejects_a_different_destination_context(self):
        pending = picker_fixtures.recorded_smash_ui(847)
        pending.values['screen.context'] = 'training'
        io, prepared = picker_fixtures.stage_confirmation_replay(
            'smash', pending, picker_fixtures.recorded_smash_ui(870))
        ctx = FakeCtx(FakeRunner({}, None), Path(tempfile.mkdtemp()))
        with self.assertRaisesRegex(RuntimeError, 'unexpected pending css.smash observation'):
            routes._finish_native_pick(ctx, io, 'sss', prepared, {'id': 308, 'name': 'Training'},
                                       'css.smash', .1)
        self.assertEqual(io.actions, ['tap-a1'])

    def test_transition_deadline_changes_only_the_css_wait(self):
        from unittest.mock import Mock, patch
        ctx = Mock()
        for cap in (60, 180):
            with patch.multiple(routes, BridgePickerIO=Mock(), _tile_topology=Mock(),
                                _finish_native_pick=Mock(), gate=Mock()), \
                    patch.object(routes.native_picker, 'hover') as hover:
                if cap == 60:
                    routes.pick_stage(ctx, "Battlefield")
                else:
                    routes.pick_stage(ctx, "Battlefield", transition_cap=cap)
                io = routes.BridgePickerIO.return_value
                hover.assert_called_once_with(
                    io, 'sss', routes._native_target(routes.stage_entry('Battlefield'), 'normal'),
                    routes._tile_topology.return_value, expected_context='training')
                routes._finish_native_pick.assert_called_once_with(
                    ctx, io, 'sss', hover.return_value, routes.stage_entry('Battlefield'), 'css.training', cap)
                routes.gate.assert_not_called()


class TableTests(unittest.TestCase):
    def test_tables_come_from_trainingrooms(self):
        t = routes.tables()
        self.assertEqual(routes.fighter_entry('Mario')['tile'], 0)
        self.assertEqual(routes.fighter_entry('Lucas')['kind'], 0x28)
        self.assertEqual(routes.fighter_entry('0x28')['tile'], 39)
        self.assertEqual(routes.fighter_entry('Steve')['tile'], 78)
        self.assertEqual(routes.stage_entry('Battlefield')['tile'], 1)
        self.assertEqual(routes.stage_entry('final destination')['id'], 3)
        self.assertEqual(routes.stage_entry('Kongo Jungle')['tile'], 6)
        self.assertTrue(routes.stage_entry('Kongo Jungle').get('proven'))
        self.assertIn('commit', t['source'])
        with self.assertRaises(KeyError):
            routes.fighter_entry('Pokemon Trainer')

    def test_macro_index_hashes_match(self):
        for name in routes.routes_index()['macros']:
            path, sha, cap = routes.macro_file(name)
            self.assertTrue(path.is_file(), name)

    def test_chord_macros_press_in_one_frame(self):
        reset = json.loads(routes.macro_file('reset-lra')[0].read_text())
        self.assertIn(1 | 64 | 128, [e['buttons'] for e in reset['events']])
        tile = json.loads(routes.macro_file('tile-right')[0].read_text())
        self.assertEqual([e['buttons'] for e in tile['events']], [16, 16 | 16384, 0])


class RecipeTests(unittest.TestCase):
    def test_combo_in_one_frame_with_min_hold(self):
        import recipe_macro
        doc = recipe_macro.compile_recipe('open', [{'buttons': 'b+up', 'hold': 10, 'gap': 0.1}])
        self.assertEqual([(e['frame'], e['buttons']) for e in doc['events']], [(0, 8194), (2, 0)])
        self.assertEqual(doc['duration_frames'], 2 + 6)
        doc = recipe_macro.compile_recipe('x', [{'buttons': 'plus', 'hold': 100}, {'stick': [0, -1, 250], 'gap': 0}])
        self.assertEqual([(e['frame'], e['buttons'], e['sticks'][1]) for e in doc['events']],
                         [(0, 1024, 0), (6, 0, 0), (42, 0, -1.0), (57, 0, 0)])
        with self.assertRaises(ValueError):
            recipe_macro.compile_recipe('h', [{'buttons': 'home'}])

    def test_plan_compiles_to_a_ready_dry_run(self):
        import recipe_macro
        folder = Path(tempfile.mkdtemp())
        plan = {'game_id': '01006A800016E000', 'start': 's',
                'gates': {'g': {'kind': 'log', 'source': 'console_log', 'pattern': 'x', 'cap': 1}},
                'segments': {'menu': {'recipe': [{'buttons': 'b+up', 'hold': 100}]}},
                'rows': [{'id': 'r', 'start': 's', 'end': 's', 'steps': [{'gate': 'g'}, {'play': 'menu'}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        self.assertEqual(recipe_macro.compile_plan(folder / 'rows.json'), ['menu'])
        report = runner.dry_run(folder / 'rows.json')
        self.assertEqual(report['result'], 'READY', report)
        self.assertEqual(recipe_macro.compile_plan(folder / 'rows.json'), [])  # already compiled


# vendored hardware stills (fixtures/training-ink/PROVENANCE.md): never read from the artifacts tree, which is pruned
INK_FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'training-ink'


class TrainingInkGateTests(unittest.TestCase):
    """The hw Training gate on another stage: the plate is translucent, the text is not (run-1 S-B07, 19:34Z)."""

    def test_peachs_castle_passes_and_other_screens_fail(self):
        from runner import Runner
        spec = routes._switch_spec('training')
        compare = Runner.compare_region  # a staticmethod
        positives = [INK_FIXTURES / 'pos-other-stage-plate.png', INK_FIXTURES / 'pos-t01-closed.png']
        negatives = [INK_FIXTURES / n for n in ('neg-t01-rec-reset-row.png', 'neg-up-still.png', 'neg-css.png',
                                                'neg-tr-menu.png')]
        for png in positives:
            self.assertLessEqual(routes.ink_compare(png, spec), spec['ink_limit'], png)
        for png in negatives:
            self.assertGreater(routes.ink_compare(png, spec), 0.6, png)
        diff, note = routes.match_frame(positives[0], spec, compare)
        self.assertEqual(diff, 0.0)
        self.assertIn('ink_mismatch', note)
        for png in negatives:
            self.assertGreater(routes.match_frame(png, spec, compare)[0], spec['tolerance'], png)


class SwitchHwTests(unittest.TestCase):
    BRIDGE_LINES = [
        '[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=v1 port=6970 guards=0x7f frame=0 wall=10',
        '[TrainingRooms] cpu_ids FIGHTER_STATUS_KIND_WAIT=0 FIGHTER_STATUS_KIND_CAPTURE_PULLED=63 frame=1 wall=11',
        '[Bridge] stage id=3 frame=182 wall=12',
        '[Bridge] roster entry=0 kind=0 nana=false frame=182 wall=12',
        '[Bridge] roster entry=1 kind=84 nana=false frame=182 wall=12',
        '[Bridge] item entry=0 held=true kind=429 frame=651 wall=13',
        '[Bridge] speed fps=59.94 ratio=0.9990 frames=599 ms=9994 frame=700 wall=14',
        '[TrainingRooms] state pos entry=0 kind=0 x=-35.999996 y=0.010259508 lr=1 dmg=0 status=0 situation=0 frame=701 wall=15',
        '[TrainingRooms] state pos entry=1 kind=84 x=36 y=0.010259508 lr=-1 dmg=12.5 status=63 situation=2 frame=701 wall=15',
    ]

    def test_bridge_state_parses_every_native_read(self):
        import switch_hw
        state = switch_hw.parse_bridge_state(self.BRIDGE_LINES)
        self.assertEqual((state['frame'], state['wall_ms']), (701, 15))
        self.assertEqual(state['stage'], 3)
        self.assertEqual(state['roster'], {0: {'kind': 0, 'nana': False}, 1: {'kind': 84, 'nana': False}})
        self.assertEqual(state['items'], {0: {'held': True, 'kind': 429}})
        self.assertEqual(state['speed'], {'fps': 59.94, 'ratio': 0.999, 'frames': 599, 'ms': 9994})
        self.assertEqual(state['pos'][1], {'kind': 84, 'x': 36.0, 'y': 0.010259508, 'lr': -1.0, 'dmg': 12.5, 'status': 63,
                                           'situation': 2, 'frame': 701})
        self.assertEqual(state['status_ids'], {'FIGHTER_STATUS_KIND_WAIT': 0, 'FIGHTER_STATUS_KIND_CAPTURE_PULLED': 63})

    V2_LINES = [
        '[Bridge] caps proto=2 peek chain frame scene fighters input inject watch inject_net input_net tr=none frame=5 wall=1',
        '[Bridge] scene mode=0x04040000 melee=0x20 init=1 ldn=none class=training frame=40 wall=2',
        '[Bridge] fighter entry=0 kind=0 x=-36 y=0.01 lr=1 dmg=0 status=0 situation=0 stock=-1 dead=0 item=-1 frame=41 wall=2',
        '[Bridge] fighter entry=1 kind=84 x=36 y=0.01 lr=-1 dmg=3.5 status=11 situation=0 stock=-1 dead=0 item=-1 frame=41 wall=2',
        '[Bridge] input slot=0 src=final buttons=0x4 lx=0 ly=0 rx=0 ry=0 frame=42 wall=3',
        '[Bridge] input slot=0 src=net buttons=0x0 lx=-120 ly=0 rx=0 ry=0 frame=42 wall=3',
        '[Bridge] watch addr=main+0x53050f0 len=4 val=0x4040000 left=3 frame=43 wall=3',
        '[Bridge] fighter entry=1 gone frame=44 wall=4',
        '[Bridge] scene mode=0x02010000 melee=0x0d init=1 ldn=station_connected class=local_wireless tr_frame=7 frame=50 wall=5',
    ]

    def test_bridge_state_parses_the_v2_lines(self):
        import switch_hw
        state = switch_hw.parse_bridge_state(self.V2_LINES)
        self.assertEqual(state['caps'], {'proto': 2, 'tr': 'none', 'features': [
            'peek', 'chain', 'frame', 'scene', 'fighters', 'input', 'inject', 'watch', 'inject_net', 'input_net']})
        self.assertEqual(state['scene'], {'mode': 0x02010000, 'melee': 0x0d, 'init': True, 'ldn': 'station_connected',
                                          'class': 'local_wireless', 'frame': 50})
        self.assertEqual(sorted(state['fighters']), [0], 'entry 1 went away')
        self.assertEqual(state['fighters'][0], {'kind': 0, 'x': -36.0, 'y': 0.01, 'lr': 1.0, 'dmg': 0.0, 'status': 0,
                                                'situation': 0, 'stock': -1, 'dead': 0, 'item': -1, 'frame': 41})
        self.assertEqual(state['inputs']['final'][0], {'buttons': 4, 'lx': 0, 'ly': 0, 'rx': 0, 'ry': 0, 'frame': 42})
        self.assertEqual(state['inputs']['net'][0]['lx'], -120)
        self.assertEqual(state['watch'], {'addr': 'main+0x53050f0', 'len': 4, 'val': 0x4040000, 'left': 3, 'frame': 43})

    def test_v2_requests_go_through_their_own_spool_files_and_other_verbs_are_dropped(self):
        import time
        import switch_hw
        run = Path(tempfile.mkdtemp())
        spool = run / 'bridge'
        switch_hw.bridge_v2(run, 'scene', 'inject 0 AAAA')
        time.sleep(0.002)
        (spool / 'bridge-v2-x.req').write_bytes(b'inject_start 0 at=next\nrestore\n\nwatch main+0x53050f0 4 count=5\n')
        lines = switch_hw.spool_requests(spool)
        self.assertEqual(lines, [b'scene', b'inject 0 AAAA', b'inject_start 0 at=next', b'watch main+0x53050f0 4 count=5'])
        self.assertEqual(list(spool.iterdir()), [])

    def test_v2_spool_forwards_bridge_toggle_verbs_but_not_restore(self):
        import switch_hw

        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp)
            (spool / 'bridge-v2-toggle.req').write_bytes(b'bridge status\nbridge off\nrestore\n')
            self.assertEqual(switch_hw.spool_requests(spool), [b'bridge status', b'bridge off'])

    def test_file_capture_mirrors_the_stream_and_hands_requests_over_whole(self):
        import threading
        import switch_hw
        with tempfile.TemporaryDirectory() as tmp:
            run, sd = Path(tmp) / 'run', Path(tmp) / 'sd'
            sd.mkdir()
            stream = sd / 'stream.log'
            stream.write_bytes(b'[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 frame=0 wall=1\n[Bridge] scene mo')
            stop = threading.Event()
            worker = threading.Thread(target=switch_hw.capture_bridge_file, args=(run, sd, Path(tmp) / 'console.log'),
                                      kwargs={'stop': stop.is_set, 'poll_s': 0.01})
            worker.start()
            try:
                for _ in range(300):  # the opening requests arrive as one whole file
                    if (sd / 'requests.txt').exists():
                        break
                    time.sleep(0.01)
                self.assertEqual((sd / 'requests.txt').read_bytes(), b'backup\nfile settings.json\n')
                (sd / 'requests.txt').unlink()  # the bridge takes it
                switch_hw.bridge_v2(run, 'caps')
                with stream.open('ab') as f:
                    f.write(b'de=0x04040000 melee=0x20 init=1 ldn=none class=training frame=3 wall=2\n')
                for _ in range(300):
                    if (sd / 'requests.txt').exists() and 'class=training' in (run / 'bridge.log').read_text():
                        break
                    time.sleep(0.01)
                self.assertEqual((sd / 'requests.txt').read_bytes(), b'caps\n')
            finally:
                stop.set()
                worker.join()
            lines = (run / 'bridge.log').read_text().splitlines()
            self.assertEqual(lines[0], '[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 frame=0 wall=1')
            self.assertEqual(lines[1], '[Bridge] scene mode=0x04040000 melee=0x20 init=1 ldn=none class=training frame=3 wall=2')

    def test_wait_scene_asks_fresh_and_waits_for_the_class(self):
        import threading
        import switch_hw
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'bridge').mkdir()
            log = run / 'bridge.log'
            log.write_text(self.V2_LINES[1] + '\n')

            def bridge():
                for _ in range(200):
                    if any(p.read_bytes() == b'scene' for p in (run / 'bridge').glob('bridge-v2*.req')):
                        for p in (run / 'bridge').glob('bridge-v2*.req'):
                            p.unlink()
                        with log.open('a') as f:
                            f.write(self.V2_LINES[-1] + '\n')
                        return
                    time.sleep(0.02)

            worker = threading.Thread(target=bridge)
            worker.start()
            scene = switch_hw.wait_scene(run, 'local_wireless', cap=5)
            worker.join()
            self.assertEqual(scene['mode'], 0x02010000)
            with self.assertRaisesRegex(RuntimeError, 'scene class training not reached'):
                switch_hw.wait_scene(run, 'training', cap=0.5)

    def test_bridge_state_newest_line_wins(self):
        import switch_hw
        state = switch_hw.parse_bridge_state(self.BRIDGE_LINES + ['[Bridge] stage id=8 frame=900 wall=20',
                                                                  '[Bridge] item entry=0 held=false kind=-1 frame=901 wall=21'])
        self.assertEqual((state['stage'], state['items'][0]['held'], state['frame']), (8, False, 901))

    def test_bridge_state_fresh_reads_wait_for_their_answers(self):
        import tempfile
        import threading
        import switch_hw
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'bridge').mkdir()
            log = run / 'bridge.log'
            log.write_text(self.BRIDGE_LINES[0] + '\n')

            def bridge():  # the capture: a request file in the spool becomes the bridge's answer in bridge.log
                for _ in range(100):
                    if (run / 'bridge' / 'diag-pos.req').exists() and (run / 'bridge' / 'bridge-speed.req').exists():
                        with log.open('a') as f:
                            f.write('[Bridge] req pos result=ok entries=0x3 frame=700 wall=14\n')
                            f.write('\n'.join(self.BRIDGE_LINES[6:]) + '\n')
                        return
                    time.sleep(0.02)

            worker = threading.Thread(target=bridge)
            worker.start()
            state = switch_hw.bridge_state(run, fresh=['pos', 'speed'], cap=5)
            worker.join()
            self.assertEqual(sorted(state['pos']), [0, 1])
            self.assertEqual(state['speed']['fps'], 59.94)
            with self.assertRaisesRegex(RuntimeError, r"no answer for \['roster'\]"):
                switch_hw.bridge_state(run, fresh=['roster'], cap=0.3)

    def test_the_bridges_own_status_names_fill_status_ids_without_tr(self):
        import switch_hw
        lines = ['[Bridge] status_names FIGHTER_STATUS_KIND_NONE=-1 FIGHTER_STATUS_KIND_WAIT=0 frame=5 wall=1',
                 '[Bridge] status_names FIGHTER_STATUS_KIND_JUMP_SQUAT=10 FIGHTER_STATUS_KIND_JUMP=11 frame=5 wall=1',
                 '[Bridge] req status_names result=ok count=4 frame=5 wall=1']
        state = switch_hw.parse_bridge_state(lines)
        self.assertEqual(state['status_ids'], {'FIGHTER_STATUS_KIND_NONE': -1, 'FIGHTER_STATUS_KIND_WAIT': 0,
                                               'FIGHTER_STATUS_KIND_JUMP_SQUAT': 10, 'FIGHTER_STATUS_KIND_JUMP': 11})
        self.assertIn('status_names', switch_hw.V2_VERBS)
        self.assertEqual(switch_hw.FRESH['status_names'][2], b'status_names')

    def test_bridge_bodies_read_the_v2_fighters_in_any_battle_and_tr_pos_only_without_them(self):
        import threading
        import switch_hw
        for caps, want in ((self.V2_LINES[0], 'fighters'), (self.BRIDGE_LINES[0], 'pos')):
            with tempfile.TemporaryDirectory() as tmp:
                run = Path(tmp)
                (run / 'bridge').mkdir()
                log = run / 'bridge.log'
                log.write_text(caps + '\n')
                asked = []

                def bridge():  # the capture: a spool file becomes the bridge's (or TR's) answer in bridge.log
                    for _ in range(300):
                        if not (run / 'bridge').exists():
                            return
                        for p in (run / 'bridge').iterdir():
                            asked.append(p.name if p.name == 'diag-pos.req' else p.read_bytes().decode())
                            p.unlink()
                            with log.open('a') as f:
                                if asked[-1] == 'fighters':
                                    f.write('\n'.join(self.V2_LINES[2:4]) + '\n[Bridge] fighters entries=0x3 count=2 '
                                            'frame=41 wall=2\n')
                                    return
                                if asked[-1] == 'diag-pos.req':
                                    f.write('[Bridge] req pos result=ok entries=0x3 frame=700 wall=14\n'
                                            + '\n'.join(self.BRIDGE_LINES[7:]) + '\n')
                                    return
                                # a slice 2 bridge has no `caps`: no answer
                        time.sleep(0.02)

                worker = threading.Thread(target=bridge)
                worker.start()
                state = switch_hw.bridge_bodies(run, cap=1.0)
                worker.join()
                self.assertEqual(asked, ['fighters'] if want == 'fighters' else ['caps', 'diag-pos.req'])
                self.assertEqual(state['pos'], state[want])
                self.assertEqual(state['pos'][1]['dmg'], 3.5 if want == 'fighters' else 12.5)
                self.assertEqual(state['bodies_from'], want)

    def test_external_log_needs_a_connection_line(self):
        import switch_hw
        folder = Path(tempfile.mkdtemp())
        mine, theirs, dead = folder / 'a.log', folder / 'b.log', folder / 'c.log'
        mine.write_text('[capture] connected 192.168.0.224:6969 2026-11-16T19:20:09Z\nTCP Socket Connected.\n')
        theirs.write_text('### 2026-11-16T19:20:09.266Z skyline_tcplog start host=192.168.0.224:6969\n'
                          '### 2026-11-16T19:20:09.269Z connected (#1)\nTCP Socket Connected.\n')
        dead.write_text('### 2026-11-16T19:20:09.266Z skyline_tcplog start host=192.168.0.224:6969\n')
        self.assertTrue(switch_hw.external_log_connected(mine).startswith('[capture] connected'))
        self.assertIn('connected (#1)', switch_hw.external_log_connected(theirs))
        self.assertIsNone(switch_hw.external_log_connected(dead))
        self.assertIsNone(switch_hw.external_log_connected(folder / 'missing.log'))

    def test_line_writer_keeps_lines_whole(self):
        import io
        import switch_hw
        buf = io.BytesIO()
        w = switch_hw.LineWriter(buf)
        w.feed(b'plugin=training_rooms/0.2.0 game=13.0.5 res')
        self.assertEqual(buf.getvalue(), b'')
        w.feed(b'ult=installed\r\nnext')
        self.assertEqual(buf.getvalue(), b'plugin=training_rooms/0.2.0 game=13.0.5 result=installed\n')
        w.close()
        self.assertTrue(buf.getvalue().endswith(b'next\n'))

    def test_capture_reconnects_and_stops(self):
        import switch_hw
        chunks = [[b'a\nb', b'c\n'], [b'd\n']]
        calls = []

        class Sock:
            def __init__(self, data):
                self.data = list(data)

            def settimeout(self, t):
                pass

            def recv(self, n):
                return self.data.pop(0) if self.data else b''

            def close(self):
                pass

        def connect(addr, timeout):
            calls.append(addr)
            if not chunks:
                raise OSError('refused')
            return Sock(chunks.pop(0))
        path = Path(tempfile.mkdtemp()) / 'skyline.log'
        switch_hw.capture_log(path, 'h', 1, stop=lambda: len(calls) >= 3, connect=connect, retry_s=0)
        lines = path.read_text().splitlines()
        self.assertEqual([l for l in lines if not l.startswith('[capture]')], ['a', 'bc', 'd'])
        self.assertEqual(sum(l.startswith(switch_hw.CONNECTED + ' h:1 ') for l in lines), 2)  # up's live line

    def test_capture_drops_a_silent_connection_and_reconnects(self):
        import switch_hw
        calls, now = [], [0.0]

        class Silent:
            def settimeout(self, t):
                pass

            def recv(self, n):
                now[0] += 50.0
                raise switch_hw.socket.timeout()

            def close(self):
                pass

        def connect(addr, timeout):
            calls.append(addr)
            return Silent()
        path = Path(tempfile.mkdtemp()) / 'skyline.log'
        switch_hw.capture_log(path, 'h', 1, stop=lambda: len(calls) >= 3, connect=connect, retry_s=0, idle_s=120,
                              clock=lambda: now[0])
        self.assertEqual(path.read_text().count('idle 120 s: reconnecting'), 2)  # half-open: not forever

    def test_capture_keeps_a_quiet_connection_by_default(self):
        """Skyline takes one client per boot: the default never drops a quiet live connection (session-2 17:53Z)."""
        import switch_hw
        calls, now = [], [0.0]

        class Quiet:
            def settimeout(self, t):
                pass

            def setsockopt(self, *a):
                pass

            def recv(self, n):
                now[0] += 50.0
                raise switch_hw.socket.timeout()

            def close(self):
                pass

        def connect(addr, timeout):
            calls.append(addr)
            return Quiet()
        path = Path(tempfile.mkdtemp()) / 'skyline.log'
        switch_hw.capture_log(path, 'h', 1, stop=lambda: now[0] > 1000, connect=connect, retry_s=0,
                              clock=lambda: now[0])
        self.assertEqual(len(calls), 1)
        self.assertNotIn('reconnecting', path.read_text())

    def test_up_refuses_when_obs_cannot_split_per_row(self):
        import contextlib
        import io
        import switch_hw
        folder = Path(tempfile.mkdtemp())
        split = {'ok': False, 'why': 'simple_file_output.split_file is None: manual file splitting is off'}
        calls = []

        class Client:
            def request(self, method, params=None):
                calls.append(method)
                if method == 'ui.screenshot':
                    Path(params['path']).write_bytes(b'png')
                if method == 'recording.split_check':
                    return {'ok': True, 'result': dict(split)}
                return {'ok': True, 'result': {'version': 'x'}}

            def close(self):
                pass
        saved = (switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen)
        switch_hw.CLAIM = folder / 'claim'
        switch_hw.holders = lambda device: []
        switch_hw._client = lambda device: Client()
        live = [True]

        def popen(args, **k):  # the capture: its connected line arrives (or, with live off, nothing does)
            if live[0]:
                with open(args[4], 'a') as log:
                    log.write('[capture] connected h:1 now\n')
            return type('P', (), {'pid': 4242, 'terminate': lambda self: None})()
        switch_hw.subprocess.Popen = popen
        try:
            with self.assertRaises(SystemExit) as cm:
                switch_hw.up(folder / 'run', '/dev/null', 'h:1', 'test')
            self.assertIn('split-enable', str(cm.exception))
            self.assertFalse((folder / 'claim').exists())  # released on the refusal
            self.assertNotIn('SetOutputSettings', calls)    # read-only: up never changes OBS
            with contextlib.redirect_stdout(io.StringIO()):
                switch_hw.up(folder / 'run2', '/dev/null', 'h:1', 'test', allow_no_split=True)
            journal = json.loads((folder / 'run2' / 'journal.json').read_text())
            self.assertFalse(journal['obs_split']['ok'])
            (folder / 'claim').unlink()
            split['ok'], split['why'] = True, None
            with contextlib.redirect_stdout(io.StringIO()):
                switch_hw.up(folder / 'run3', '/dev/null', 'h:1', 'test')
            journal = json.loads((folder / 'run3' / 'journal.json').read_text())
            self.assertTrue(journal['obs_split']['ok'])
            self.assertEqual(journal['log_live'], '[capture] connected h:1 now')
            (folder / 'claim').unlink()
            live[0] = False  # no Skyline line within the cap: refused, claim released
            with self.assertRaises(SystemExit) as cm:
                switch_hw.up(folder / 'run4', '/dev/null', 'h:1', 'test', log_cap_s=0.5)
            self.assertIn('no Skyline log line', str(cm.exception))
            self.assertFalse((folder / 'claim').exists())
        finally:
            switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen = saved

    def test_bridge_spool_turns_eden_request_files_into_bridge_requests(self):
        import base64
        import time
        import switch_hw
        spool = Path(tempfile.mkdtemp())
        for name, data in [('diag-settings-next.json', b'{"menu":{}}'), ('diag-pos.req', b''), ('diag-item.req', b' 7\n'),
                           ('bridge-file.req', b'save_states.json\nlibrary/a.json\n'), ('diag-save.req', b''),
                           ('diag-load.req', b''), ('settings.json', b'{}')]:
            (spool / name).write_bytes(data)
            time.sleep(0.002)
        lines = switch_hw.spool_requests(spool)
        self.assertEqual(lines, [b'set ' + base64.b64encode(b'{"menu":{}}'), b'pos', b'item 7', b'file save_states.json',
                                 b'file library/a.json', b'save', b'load'])
        self.assertEqual(sorted(p.name for p in spool.iterdir()), ['settings.json'])  # requests consumed, the mirror kept
        self.assertEqual(switch_hw.spool_requests(spool / 'missing'), [])

    def test_bridge_mirror_writes_settings_and_keeps_lines_whole(self):
        import base64
        import hashlib
        import io
        import switch_hw
        run = Path(tempfile.mkdtemp())
        blog, clog = io.BytesIO(), io.BytesIO()
        mirror = switch_hw.BridgeMirror(run, [blog, clog])
        body = b'{"menu": {"input_delay": [3]}}' * 20
        for seq, start in enumerate(range(0, len(body), 384)):
            mirror.line(b'[Bridge] file_data name=settings.json seq=%d b64=' % seq + base64.b64encode(body[start:start + 384]))
        digest = hashlib.sha256(body).hexdigest().encode()
        mirror.line(b'[Bridge] file_end name=settings.json bytes=%d sha256=%s result=ok frame=9 wall=10\r' % (len(body), digest))
        self.assertEqual((run / 'bridge' / 'settings.json').read_bytes(), body)
        self.assertEqual((run / 'bridge' / 'files' / 'settings.json').read_bytes(), body)
        mirror.line(b'[Bridge] file_end name=x.json bytes=1 sha256=' + b'0' * 64 + b' result=ok')
        mirror.line(b'[Bridge] file_data name=cut.json seq=0 b64=Zm9vYm')  # a chunk cut short
        mirror.line(b'[Bridge] file_end name=cut.json bytes=4 sha256=' + hashlib.sha256(b'foob').hexdigest().encode() + b' result=ok')
        self.assertFalse((run / 'bridge' / 'files' / 'cut.json').exists())
        self.assertFalse((run / 'bridge' / 'files' / 'x.json').exists())
        mirror.line(b'[TrainingRooms] settings.json written by the writer thread frame=12 wall=13')
        mirror.line(b'[Ui] press A page=3 cursor=0 depth=1 screen=Page row=None recording= frame=14 wall=15')
        self.assertEqual(mirror.wanted, [b'file settings.json'])
        text = blog.getvalue().decode()
        self.assertEqual(text, clog.getvalue().decode())
        self.assertNotIn('file_data', text)
        self.assertIn(' mirrored=true\n', text)
        self.assertIn('x.json bytes=1 sha256=' + '0' * 64 + ' result=ok mirrored=false\n', text)
        self.assertTrue(text.endswith('recording= frame=14 wall=15\n'))

    def test_capture_bridge_sends_hello_and_spool_then_reconnects(self):
        import switch_hw
        run = Path(tempfile.mkdtemp())
        console = run / 'skyline.log'
        (run / 'bridge').mkdir()
        (run / 'bridge' / 'diag-pos.req').write_bytes(b'')
        sent, calls = [], []
        chunks = [[b'[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 frame=0 wall=1\n[TrainingRooms] state pos entry=0 kind=0 ',
                   b'x=1 y=2 lr=1 dmg=0 status=0 situation=0 frame=5 wall=6\n']]

        class Sock:
            def __init__(self, data):
                self.data = list(data)

            def settimeout(self, t):
                pass

            def sendall(self, b):
                sent.append(b)

            def recv(self, n):
                return self.data.pop(0) if self.data else b''

            def close(self):
                pass

        def connect(addr, timeout):
            calls.append(addr)
            if not chunks:
                raise OSError('refused')
            return Sock(chunks.pop(0))
        switch_hw.capture_bridge(run, 'h', 6970, console, stop=lambda: len(calls) >= 2, connect=connect, retry_s=0)
        self.assertEqual(sent[0], b'backup\nfile settings.json\n')
        self.assertIn(b'pos\n', sent)
        log = (run / 'bridge.log').read_text()
        self.assertIn('[TrainingRooms] state pos entry=0 kind=0 x=1 y=2 lr=1 dmg=0 status=0 situation=0 frame=5 wall=6\n', log)
        self.assertEqual(console.read_text(), log)
        self.assertEqual(json.loads((run / 'bridge.requests.jsonl').read_text().splitlines()[0])['request'], 'pos')
        import fcntl
        with (run / 'bridge.lock').open('w') as held:  # a second capture of the same run is refused
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(SystemExit):
                switch_hw.capture_bridge(run, 'h', 6970, console, stop=lambda: True, connect=connect, retry_s=0)

    def test_read_state_runs_unchanged_on_a_bridge_instance(self):
        import threading
        run = Path(tempfile.mkdtemp())
        (run / 'bridge').mkdir()
        log = run / 'bridge.log'
        log.write_text('[TrainingRooms] state pos entry=0 kind=9 x=0 y=0 lr=1 dmg=0 status=0 situation=0 frame=1 wall=1\n')
        instance = {'socket': 'usb:///dev/ttyACM0', 'diag_log': str(log), 'settings': str(run / 'bridge' / 'settings.json')}
        ctx = type('Ctx', (), {'instance': instance, 'check_stop': lambda self: None})()

        def bridge():
            for _ in range(100):
                if (run / 'bridge' / 'diag-pos.req').exists():
                    with log.open('a') as f:
                        f.write('[TrainingRooms] state pos entry=1 kind=66 x=-65.37304 y=0 lr=-1 dmg=12.5 status=27 situation=2 frame=8 wall=9\n')
                        f.write('[TrainingRooms] state pos entry=0 kind=0 x=6.5219345 y=0 lr=1 dmg=0 status=0 situation=0 frame=8 wall=9\n')
                    return
                threading.Event().wait(0.02)
        t = threading.Thread(target=bridge)
        t.start()
        bodies = routes.read_state(ctx, cap=3)
        t.join()
        self.assertEqual(bodies[0]['kind'], 0)  # the stale entry-0 line before the request is not used
        self.assertEqual((bodies[1]['kind'], bodies[1]['x'], bodies[1]['lr'], bodies[1]['status']), (66, -65.37304, -1.0, 27))

    def test_up_with_a_bridge_gives_the_instance_eden_keys(self):
        import contextlib
        import io
        import switch_hw
        folder = Path(tempfile.mkdtemp())
        popen = []

        class Client:
            def request(self, method, params=None):
                if method == 'ui.screenshot':
                    Path(params['path']).write_bytes(b'png')
                return {'ok': True, 'result': {'version': 'x', 'ok': True}}

            def close(self):
                pass
        saved = (switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen)
        switch_hw.CLAIM = folder / 'claim'
        switch_hw.holders = lambda device: []
        switch_hw._client = lambda device: Client()
        def fake_popen(args, **k):  # the Skyline capture writes its connected line (up waits for a live log line)
            popen.append(args)
            if 'capture-log' in args:
                with open(args[args.index('capture-log') + 1], 'a') as log:
                    log.write('[capture] connected h:1 now\n')
            return type('P', (), {'pid': 4242 + len(popen), 'terminate': lambda self: None})()
        switch_hw.subprocess.Popen = fake_popen
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                switch_hw.up(folder / 'run', '/dev/null', 'h:1', 'test', bridge_host='192.168.0.224:6970')
            sw = json.loads((folder / 'run' / 'instances.json').read_text())['sw']
            self.assertEqual(sw['diag_log'], str(folder / 'run' / 'bridge.log'))
            self.assertEqual(Path(sw['settings']).parent, folder / 'run' / 'bridge')
            self.assertTrue(Path(sw['bridge_files']).is_dir())
            self.assertIn('capture-bridge', popen[1])
            self.assertEqual(popen[1][popen[1].index('--console') + 1], sw['console_log'])
            self.assertEqual(json.loads((folder / 'run' / 'journal.json').read_text())['bridge_pid'], 4244)
        finally:
            switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen = saved

    def test_an_unrestored_backup_is_restored_before_a_new_one(self):
        import io
        import switch_hw
        mirror = switch_hw.BridgeMirror(Path(tempfile.mkdtemp()), [io.BytesIO()])
        mirror.line(b'[Bridge] req backup result=refused reason=backup_open frame=0 wall=1')
        self.assertEqual(mirror.wanted, [b'restore', b'backup'])
        mirror.line(b'[Bridge] req backup result=refused reason=backup_open frame=0 wall=2')
        self.assertEqual(mirror.wanted, [b'restore', b'backup'], 'once per connection, never a loop')

    def test_down_restores_the_fixture_and_keeps_a_receipt(self):
        import switch_hw
        run = Path(tempfile.mkdtemp())
        sent = []

        class Sock:
            def __init__(self):
                self.data = [b'[Bridge] hello x\n[Bridge] req restore result=ok files=3 written=1 removed=0 frame=1 wall=2\n']

            def settimeout(self, t):
                pass

            def sendall(self, b):
                sent.append(b)

            def recv(self, n):
                return self.data.pop(0) if self.data else b''

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        receipt = switch_hw.restore_fixture(run, {'bridge_host': 'h:6970'}, connect=lambda a, timeout: Sock())
        self.assertTrue(receipt['ok'])
        self.assertEqual(sent, [b'restore\n'])
        self.assertTrue(json.loads((run / 'bridge-restore.json').read_text())['ok'])
        failed = switch_hw.bridge_request('h', 1, 'restore', cap=0.5, connect=lambda a, timeout: (_ for _ in ()).throw(OSError()))
        self.assertIsNone(failed)

        def replying(line):
            sock = Sock()
            sock.data = [b'[Bridge] hello x\n' + line + b'\n']
            return lambda a, timeout: sock
        # switch-hw-sol-3 14:11Z: after an explicit successful restore the backup is gone, so Down's restore replies
        # no_backup; that is nothing outstanding, not a dirty fixture. A pending restore is still dirty.
        closed = switch_hw.restore_fixture(run, {'bridge_host': 'h:6970'},
                                           connect=replying(b'[Bridge] req restore result=refused reason=no_backup frame=9 wall=9'))
        self.assertEqual((closed['ok'], closed['state']), (True, 'no_open_backup'))
        pending = switch_hw.restore_fixture(run, {'bridge_host': 'h:6970'}, connect=replying(
            b'[Bridge] req restore result=pending reason=live_settings_refused_scene backup=kept frame=9 wall=9'))
        self.assertEqual((pending['ok'], pending['state']), (False, 'incomplete'))

    def test_a_failed_restore_keeps_the_claim_and_fails_loudly(self):
        from unittest import mock
        import switch_hw
        run = Path(tempfile.mkdtemp())
        claim = run / 'claim.json'
        claim.write_text(json.dumps({'run': str(run)}))
        (run / 'journal.json').write_text(json.dumps({'device': 'x', 'claim': str(claim), 'bridge_host': 'h:6970'}))
        with mock.patch.object(switch_hw, '_client', side_effect=OSError('no pad')), \
                mock.patch.object(switch_hw, 'restore_fixture', return_value={'ok': False, 'restore': None}), \
                mock.patch.object(switch_hw.time, 'sleep'):
            with self.assertRaises(SystemExit):
                switch_hw.down(run)
        self.assertTrue(claim.is_file(), 'the console stays claimed')
        self.assertIn('fixture restore incomplete', json.loads(claim.read_text())['dirty'])
        self.assertFalse(json.loads((run / 'journal.json').read_text())['down_result']['released'])
        with mock.patch.object(switch_hw, '_client', side_effect=OSError('no pad')), \
                mock.patch.object(switch_hw, 'restore_fixture', return_value={'ok': True, 'restore': 'ok'}), \
                mock.patch.object(switch_hw.time, 'sleep'):
            switch_hw.down(run)
        self.assertFalse(claim.is_file(), 'a verified restore releases it')

    def test_factory_routes_usb_to_switch_remote(self):
        folder = Path(tempfile.mkdtemp())
        (folder / 'switch_remote.py').write_text('def make_client(s):\n    return ("fake", s)\n')
        old = os.environ.get('SWITCH_REMOTE_DIR')
        os.environ['SWITCH_REMOTE_DIR'] = str(folder)
        sys.modules.pop('switch_remote', None)
        try:
            self.assertEqual(runner.default_client_factory()('usb:///dev/ttyACM0'), ('fake', 'usb:///dev/ttyACM0'))
        finally:
            sys.modules.pop('switch_remote', None)
            if old is None:
                os.environ.pop('SWITCH_REMOTE_DIR', None)
            else:
                os.environ['SWITCH_REMOTE_DIR'] = old


class SwitchGateTests(unittest.TestCase):
    def test_recorder_evidence_stills(self):
        from PIL import Image
        folder = Path(tempfile.mkdtemp())
        spec = routes._switch_spec('tr-menu')
        frame = Image.new('RGB', (1280, 720))
        frame.paste(Image.open(spec['reference']).convert('RGB'), tuple(spec['roi'][:2]))
        good, blank = folder / 'good.png', folder / 'blank.png'
        frame.save(good)
        Image.new('RGB', (1280, 720)).save(blank)
        shots = []

        class R:
            plan = {'gates': {}}

            @staticmethod
            def compare_region(*a):
                return runner.Runner.compare_region(*a)

        ctx = FakeCtx.__new__(FakeCtx)
        ctx.runner, ctx.on, ctx.row_dir = R(), 'sw', folder
        ctx.instance = {'socket': 'usb:///dev/ttyACM0'}
        counter = []

        def shot(name):  # a fresh file per still, as runner.shot makes (the gate deletes failed stills)
            counter.append(1)
            path = folder / f'shot-{len(counter)}.png'
            path.write_bytes(Path(shots.pop(0)).read_bytes())
            return str(path)
        ctx.shot = shot
        self.assertTrue(routes.on_switch(ctx))
        def evidence(cap):
            return routes._frame_gate(ctx, 'tr-menu-evidence', spec, cap)

        shots[:] = [str(blank), str(good)]
        passed = evidence(5)
        self.assertEqual(Path(passed).read_bytes(), good.read_bytes())
        shots[:] = [str(blank)] * 50
        with self.assertRaises(RuntimeError):
            evidence(0.5)
        # The console's auto-dim (about 1/3 the luminance) still passes; a dark unrelated frame doesn't.
        dim = folder / 'dim.png'
        frame.point(lambda v: int(v * 0.33)).save(dim)
        shots[:] = [str(dim)]
        self.assertEqual(Path(evidence(1)).read_bytes(), dim.read_bytes())
        gray = folder / 'gray.png'
        Image.new('RGB', (1280, 720), (20, 20, 20)).save(gray)
        shots[:] = [str(gray)] * 50
        with self.assertRaises(RuntimeError):
            evidence(0.5)


class SwitchDiscriminationTests(unittest.TestCase):
    """Every real-Switch gate passes its own screen, normal and dimmed (the console's ~1/3 auto-dim), and fails
    every other gate's screen content placed at its ROI, normal and dimmed: HOME vs main vs TR menu vs HUD and the
    rest. Built from the committed references only (no game stills in the repo)."""

    @staticmethod
    def frame_with(spec, content, factor):
        from PIL import Image
        frame = Image.new('RGB', (1280, 720))
        x0, y0, x1, y1 = spec['roi']
        frame.paste(content.resize((x1 - x0, y1 - y0)), (x0, y0))
        return frame.point(lambda v: int(v * factor)) if factor != 1 else frame

    def test_every_gate_only_matches_its_own_screen(self):
        from PIL import Image
        folder = Path(tempfile.mkdtemp())
        gates = sorted(routes.SWITCH_REFS)
        self.assertIn('home', gates)
        refs = {g: Image.open(routes._switch_spec(g)['reference']).convert('RGB') for g in gates}
        wrong = []
        for g in gates:
            spec = routes._switch_spec(g)
            for factor in (1, 0.33, 0.35):
                own = folder / f'{g}-own-{factor}.png'
                self.frame_with(spec, refs[g], factor).save(own)
                diff, note = routes.match_frame(own, spec, runner.Runner.compare_region)
                self.assertLessEqual(diff, spec['tolerance'], f'{g} own x{factor}: {diff} {note}')
                for h in gates:
                    if h == g:
                        continue
                    other = folder / f'{g}-as-{h}-{factor}.png'
                    self.frame_with(spec, refs[h], factor).save(other)
                    diff, _ = routes.match_frame(other, spec, runner.Runner.compare_region)
                    if diff <= spec['tolerance']:
                        wrong.append((g, h, factor, round(diff, 1)))
        self.assertEqual(wrong, [], 'a gate passed another screen')


class PreloadNoticeTests(unittest.TestCase):
    """TR's preloader notice (switch-hw-session-1 T-01, 15:03Z: an R lost at "Fighters / Loading 4/7"): the
    classifier and the preload_clear gate, on frames built from the committed notice references only."""

    BOX = (310, 290, 970, 387)

    @staticmethod
    def ref(name):
        from PIL import Image
        entry = json.loads((routes.ROUTES / 'refs' / 'hw-refs.json').read_text())[name]
        return Image.open(routes.ROUTES / 'refs' / f'{name}.png').convert('RGB'), entry['roi']

    def frame(self, box=True, header=True, word='Loading', dx=0, dim=1.0, menu=False):
        from PIL import Image, ImageDraw
        im = Image.new('RGB', (1280, 720), (13, 13, 13) if menu else (212, 212, 212))
        draw = ImageDraw.Draw(im)
        if not menu:
            for x in range(0, 1280, 48):  # the Training stage grid
                draw.line([(x, 0), (x, 720)], fill=(150, 150, 150), width=2)
            for y in range(0, 720, 48):
                draw.line([(0, y), (1280, y)], fill=(150, 150, 150), width=2)
        else:
            draw.text((60, 140), 'Reactions (4)   Mash   0 of 30', fill=(230, 230, 230))
        if box:
            draw.rectangle(self.BOX, fill=(13, 13, 13))
            if header:
                head, roi = self.ref('hw-notice-fighters')
                im.paste(head, tuple(roi[:2]))
            if word:
                img, roi = self.ref('hw-notice-loading')
                if word == 'Load':  # "Loaded n/m" and "All loaded" share it: blank "ing" (from column 37)
                    img = img.copy()
                    ImageDraw.Draw(img).rectangle((37, 0, img.size[0], img.size[1]), fill=(13, 13, 13))
                elif word == 'Loaded':  # "Load" + an "e"-like (the "o") + the "d", glyph-aligned
                    base = img.copy()
                    img = base.copy()
                    ImageDraw.Draw(img).rectangle((37, 0, img.size[0], img.size[1]), fill=(13, 13, 13))
                    img.paste(base.crop((9, 0, 19, base.size[1])), (38, 0))
                    img.paste(base.crop((28, 0, 37, base.size[1])), (49, 0))
                elif word == 'other':  # some other second line ("Ready 7/7", "Swap P1 once to load")
                    img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                im.paste(img, (roi[0] + dx, roi[1]))
        if dim != 1.0:
            im = im.point(lambda v: int(v * dim))
        return im

    def save(self, im, folder, name):
        path = Path(folder) / f'{name}.png'
        im.save(path)
        return path

    def test_classifier(self):
        folder = tempfile.mkdtemp()
        cases = {
            'loading': dict(), 'loading-right': dict(dx=24), 'loading-left': dict(dx=-30),
            'loading-dim': dict(dim=0.33),
            'final-load': dict(word='Load'), 'final-loaded': dict(word='Loaded'), 'final-other': dict(word='other'),
            'final-blank': dict(word=None), 'final-dim': dict(word='Load', dim=0.35),
            'other-notice': dict(header=False, word='other'), 'stage': dict(box=False),
            'stage-dim': dict(box=False, dim=0.33), 'menu': dict(box=False, menu=True),
            'menu-with-box-pixels': dict(menu=True)}
        want = {'loading': 'loading', 'loading-right': 'loading', 'loading-left': 'loading', 'loading-dim': 'loading',
                'final-load': 'fighters', 'final-loaded': 'fighters', 'final-other': 'fighters',
                'final-blank': 'fighters', 'final-dim': 'fighters', 'other-notice': 'other', 'stage': None,
                'stage-dim': None, 'menu': None, 'menu-with-box-pixels': None}
        got = {name: routes.preload_notice(self.save(self.frame(**kw), folder, name)) for name, kw in cases.items()}
        self.assertEqual(got, want)

    def test_word_margin(self):
        """The "Loading" word stays far from its look-alikes: the limit (0.3) sits between them."""
        word, _ = self.ref('hw-notice-loading')
        ref = routes._ink(word)
        self.assertEqual(routes.ink_mismatch(ref, ref), 0.0)
        for kw in (dict(word='Load'), dict(word='Loaded')):
            im = self.frame(**kw)
            _, roi = self.ref('hw-notice-loading')
            self.assertGreater(routes.ink_mismatch(routes._ink(im.crop(tuple(roi))), ref), 0.4, kw)

    def ctx_with_frames(self, frames, on_look=None):
        out = Path(tempfile.mkdtemp())
        log = out / 'skyline.log'
        log.write_text('[NRO hook] Loaded richter. BindFlag: Lazy\n')
        fake = FakeRunner({'socket': 'usb:///dev/ttyACM0', 'console_log': str(log)}, None)
        fake.out = str(out)
        row = out / 'S-BOOT'
        row.mkdir()
        ctx = FakeCtx(fake, row)
        seq = list(frames)
        looks = []

        def shot(name):
            kw = seq.pop(0) if len(seq) > 1 else seq[0]
            looks.append(kw)
            if on_look:
                on_look(len(looks), log)
            return self.save(self.frame(**kw), row, f'{name}-{len(looks)}')
        ctx.shot = shot
        return ctx, log, looks

    def test_gate_waits_through_gaps_for_the_final_notice(self):
        none, loading, final = dict(box=False), dict(), dict(word='Load')
        # HUD before the preloader starts, Loading, a gap between steps, Loading again, the final notice.
        ctx, log, looks = self.ctx_with_frames([none, loading, none, none, loading, final])
        result = routes.preload_clear(ctx, cap=30, appear=60)
        self.assertEqual(result['preload'], 'final notice')
        self.assertEqual(len(looks), 6)
        self.assertEqual([k for _, k in result['looks']], [None, 'loading', None, 'loading', 'fighters'])
        record = json.loads((Path(ctx.runner.out) / 'routes-preload.json').read_text())
        self.assertEqual(record['size'], log.stat().st_size)
        # Same scene: passes at once, no look.
        again = routes.preload_clear(ctx, cap=30, appear=60)
        self.assertEqual(again['preload'], 'done earlier')
        self.assertEqual(len(looks), 6)
        # Unloads (helpers given back) don't re-arm it; a new load (new scene, fighter change) does.
        with log.open('a') as f:
            f.write('[NRO hook] Unloaded lucas.\n')
        self.assertEqual(routes.preload_clear(ctx, cap=30, appear=60)['preload'], 'done earlier')
        with log.open('a') as f:
            f.write('[NRO hook] Loaded mario. BindFlag: Lazy\n')
        self.assertEqual(routes.preload_clear(ctx, cap=30, appear=60)['preload'], 'final notice')
        self.assertEqual(len(looks), 7)

    def test_no_notice_needs_the_appear_window_and_a_quiet_log(self):
        def loads(n, log):
            if n <= 4:
                with log.open('a') as f:
                    f.write(f'[NRO hook] Loaded fighter{n}. BindFlag: Lazy\n')
        ctx, _, looks = self.ctx_with_frames([dict(box=False)], on_look=loads)
        result = routes.preload_clear(ctx, cap=30, appear=1.0)
        self.assertEqual(result['preload'], 'no notice')
        self.assertGreaterEqual(len(looks), 5)  # not before the loads stopped
        self.assertGreaterEqual(result['seconds'], 1.0)

    def test_a_standing_other_notice_is_no_preload(self):
        ctx, _, _ = self.ctx_with_frames([dict(header=False, word='other')])
        self.assertEqual(routes.preload_clear(ctx, cap=30, appear=0.6)['preload'], 'no notice')

    def test_gate_fails_while_loading_never_ends(self):
        ctx, _, _ = self.ctx_with_frames([dict()])
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            routes.preload_clear(ctx, cap=1, appear=60)

    def test_refs_are_pinned(self):
        for name in ('hw-notice-fighters', 'hw-notice-loading'):
            self.assertIsNotNone(routes._notice_ref(name)[0])


class SessionRecordingTests(unittest.TestCase):
    """plan "session_recording": one continuous recording, marks around every executed row (failed ones too), a
    mark per shot, the stop in run()'s finally; a backend without it runs on without one."""

    class Client:
        def __init__(self, supports=True):
            self.supports, self.calls, self.rec_ms = supports, [], 0

        def request(self, method, params=None):
            self.calls.append(method)
            if method.startswith('recording.session'):
                if not self.supports:
                    return {'ok': False, 'error': {'code': 'unknown_method'}}
                self.rec_ms += 1000
                return {'ok': True, 'result': {'rec_ms': self.rec_ms, 'active': True, 'wall': 1.0,
                                               'path': '/rec.mp4' if method.endswith('stop') else None}}
            if method == 'ui.tree':
                return {'ok': True, 'result': [{'id': 'w', 'class': 'GRenderWindow'}]}
            if method == 'ui.screenshot':
                Path(params['path']).write_bytes(b'png')
            return {'ok': True, 'result': {}}

    def run_plan(self, client, **extra):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'gates': {'never': {'kind': 'log', 'source': 'console_log', 'pattern': 'never', 'cap': 0.3}},
                'session_recording': {'marks': ['row-start', 'row-end', 'shot'], 'chapters': True},
                'rows': [{'id': 'a', 'start': 's', 'end': 's', 'steps': [{'shot': 'one'}]},
                         {'id': 'b', 'start': 's', 'end': 's', 'steps': [{'gate': 'never'}]}], **extra}
        (folder / 'rows.json').write_text(json.dumps(plan))
        log = folder / 'console.log'
        log.write_text('')
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'x', 'console_log': str(log)}}, folder / 'out', 15,
                          client_factory=lambda s: client)
        try:
            r.run()
        except BaseException:
            pass
        return folder / 'out'

    def test_marks_and_stop_even_after_a_failure(self):
        client = self.Client()
        out = self.run_plan(client)
        events = [json.loads(l)['event'] for l in (out / 'recording.jsonl').read_text().splitlines()]
        self.assertEqual(events, ['start', 'row-start', 'shot', 'row-end', 'row-start', 'row-end', 'stop'])
        self.assertEqual(json.loads((out / 'b.json').read_text())['status'], 'FAILED')
        self.assertEqual(len(json.loads((out / 'b.json').read_text())['rec_ms']), 2)
        self.assertEqual(client.calls.count('recording.session_start'), 1)
        self.assertEqual(client.calls[-1] if client.calls[-1].startswith('recording') else
                         [c for c in client.calls if c.startswith('recording')][-1], 'recording.session_stop')

    def test_hardware_gif_is_marked_not_recorded(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'session_recording': {'marks': ['row-start', 'row-end']},
                'rows': [{'id': 'g', 'start': 's', 'end': 's',
                          'steps': [{'gif': {'name': 'clip', 'preset': 'evidence', 'steps': [{'shot': 'mid'}]}}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        client = self.Client()
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'usb:///dev/ttyACM0'}}, folder / 'out', 15,
                          client_factory=lambda s: client)
        r.run()
        events = [json.loads(l) for l in (folder / 'out' / 'recording.jsonl').read_text().splitlines()]
        self.assertEqual([e['event'] for e in events], ['start', 'row-start', 'gif-start', 'gif-end', 'row-end', 'stop'])
        self.assertIn('"gif":"clip"', events[2]['label'])
        self.assertNotIn('recording.start', client.calls)
        self.assertNotIn('recording.stop', client.calls)
        receipt = json.loads((folder / 'out' / 'g.json').read_text())
        self.assertEqual(receipt['status'], 'PASS')
        self.assertIn('deferred', receipt['results'][0]['gif'])

    def test_hardware_gif_without_session_is_refused(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'rows': [{'id': 'g', 'start': 's', 'end': 's',
                                        'steps': [{'gif': {'name': 'clip', 'preset': 'evidence', 'steps': []}}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'usb:///dev/ttyACM0'}}, folder / 'out', 15,
                          client_factory=lambda s: self.Client())
        try:
            r.run()
        except BaseException:
            pass
        receipt = json.loads((folder / 'out' / 'g.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertIn('session recording', receipt['error'])

    def test_backend_without_it_runs_on(self):
        client = self.Client(supports=False)
        out = self.run_plan(client)
        self.assertFalse((out / 'recording.jsonl').exists())
        self.assertEqual(json.loads((out / 'a.json').read_text())['status'], 'PASS')

    class SplitClient:
        """recording.session_split like switch-remote's OBS client: a new file per acknowledged split."""

        def __init__(self, folder, ack=lambda label: True, stopped_at=(), refuse_start=False):
            self.folder, self.ack, self.calls, self.n, self.rec_ms, self.cur = folder, ack, [], 0, 0, None
            # OBS is found stopped at these row starts (1 = the first executed row); refuse_start: it won't restart
            self.stopped_at, self.refuse_start, self.ensures = set(stopped_at), refuse_start, 0

        def new_file(self):
            self.n += 1
            path = self.folder / f'rec-{self.n}.mp4'
            path.write_bytes(b'mp4' * self.n)
            return str(path)

        def request(self, method, params=None):
            self.calls.append(method)
            params = params or {}
            if method == 'recording.session_start':
                self.cur = self.new_file()
                return {'ok': True, 'result': {'started_wall': 1.0, 'already_active': False, 'path': self.cur}}
            if method == 'recording.session_ensure':
                self.ensures += 1
                if self.ensures not in self.stopped_at:
                    return {'ok': True, 'result': {'active_before': True, 'restarted': False, 'path': self.cur}}
                if self.refuse_start:
                    return {'ok': False, 'error': {'code': 'capture_failed', 'message': 'obs: obs_500'}}
                self.cur, self.rec_ms = self.new_file(), 0
                return {'ok': True, 'result': {'active_before': False, 'restarted': True, 'path': self.cur,
                                               'started_wall': 1.0, 'rec_ms': 0}}
            if method == 'recording.session_split':
                self.rec_ms += 1000
                base = {'active': True, 'rec_ms': self.rec_ms, 'wall': 1.0, 'closed': [], 'late_paths': []}
                if not self.ack(params['label']):
                    return {'ok': True, 'result': {**base, 'split_ok': False, 'error': 'split_unacknowledged'}}
                prev, self.cur = self.cur, self.new_file()
                return {'ok': True, 'result': {**base, 'split_ok': True, 'new_path': self.cur, 'prev_path': prev,
                                               'file_rec_ms': self.rec_ms + 40,
                                               'closed': [{'path': prev, 'finalized': True}]}}
            if method == 'recording.session_mark':
                self.rec_ms += 1000
                return {'ok': True, 'result': {'active': True, 'rec_ms': self.rec_ms, 'wall': 1.0}}
            if method == 'recording.session_stop':
                return {'ok': True, 'result': {'path': self.cur, 'finalized': True, 'stopped_wall': 2.0,
                                               'late_paths': []}}
            if method == 'ui.tree':
                return {'ok': True, 'result': [{'id': 'w', 'class': 'GRenderWindow'}]}
            if method == 'ui.screenshot':
                Path(params['path']).write_bytes(b'png')
            return {'ok': True, 'result': {}}

    def run_split_plan(self, rows, ack=lambda label: True, **client_options):
        folder = Path(tempfile.mkdtemp())
        client = self.SplitClient(folder, ack, **client_options)
        plan = {'start': 's', 'gates': {'never': {'kind': 'log', 'source': 'console_log', 'pattern': 'never', 'cap': 0.3}},
                'session_recording': {'marks': ['row-start', 'row-end', 'shot'], 'per_row_files': True},
                'rows': [{'id': rid, 'start': 's', 'end': 's', 'steps': steps} for rid, steps in rows]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        log = folder / 'console.log'
        log.write_text('')
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'x', 'console_log': str(log)}}, folder / 'out', 15,
                          client_factory=lambda s: client)
        try:
            r.run()
        except BaseException:
            pass
        out = folder / 'out'
        events = [json.loads(l) for l in (out / 'recording.jsonl').read_text().splitlines()]
        return folder, out, events, client

    def test_per_row_files_split_at_each_row_start(self):
        folder, out, events, client = self.run_split_plan([('a', [{'shot': 'one'}]), ('b', [])])
        self.assertEqual([(e['event'], e.get('row')) for e in events],
                         [('start', None), ('row-start', 'a'), ('clip', None), ('shot', 'a'), ('row-end', 'a'),
                          ('row-start', 'b'), ('clip', 'a'), ('row-end', 'b'), ('stop', None), ('clip', 'b')])
        self.assertEqual(client.calls.count('recording.session_split'), 2)
        self.assertEqual(client.calls.count('recording.session_mark'), 3)  # shot, row-end x2 stay plain marks
        start_a = events[1]
        self.assertTrue(start_a['split']['ok'])
        self.assertEqual((start_a['rec_ms'], start_a['split']['requested_rec_ms']), (1040, 1000))
        pre = events[2]
        self.assertEqual((pre['path'], pre['clip']), (str(folder / 'rec-1.mp4'), None))  # before any row: no clip
        for row, n in (('a', 2), ('b', 3)):
            receipt = json.loads((out / f'{row}.json').read_text())
            self.assertEqual(receipt['clip'], str(out / row / 'clip.mp4'))
            self.assertEqual(receipt['clip_source'], str(folder / f'rec-{n}.mp4'))
            self.assertTrue(receipt['clip_finalized'])
            # a hard link: the same file, never a move (the original recording stays)
            self.assertTrue((folder / f'rec-{n}.mp4').is_file())
            self.assertTrue((out / row / 'clip.mp4').samefile(folder / f'rec-{n}.mp4'))
        self.assertEqual(events[-1]['clip'], str(out / 'b' / 'clip.mp4'))
        self.assertEqual(client.calls.count('recording.session_start'), 1)
        self.assertNotIn('recording.start', client.calls)
        self.assertNotIn('recording.stop', client.calls)

    def test_unacknowledged_split_fails_loudly_and_the_row_has_no_clip(self):
        folder, out, events, client = self.run_split_plan([('a', []), ('b', []), ('c', [])],
                                                           ack=lambda label: not label.startswith('b:'))
        start_b = [e for e in events if e['event'] == 'row-start' and e['row'] == 'b'][0]
        self.assertEqual((start_b['split']['ok'], start_b['split']['error']), (False, 'split_unacknowledged'))
        clips = {e['row']: e for e in events if e['event'] == 'clip'}
        self.assertNotIn('b', clips)
        self.assertNotIn('clip', json.loads((out / 'b.json').read_text()))
        # a's file went on through b: a's clip says so
        self.assertEqual(clips['a']['rows'], ['a', 'b'])
        self.assertEqual(json.loads((out / 'a.json').read_text())['clip_spans_rows'], ['a', 'b'])
        self.assertEqual(clips['c']['path'], str(folder / 'rec-3.mp4'))
        actions = (out / 'actions.jsonl').read_text()
        self.assertIn('"session_split": "failed"', actions)
        # never a Start/Stop substitute mid-session
        self.assertEqual((client.calls.count('recording.session_start'), client.calls.count('recording.session_stop')),
                         (1, 1))

    def test_failed_rows_get_their_clip_and_the_stop_closes_the_last(self):
        # b fails and the run stops there (rows after it never run): the stop closes b's file
        folder, out, events, client = self.run_split_plan([('a', []), ('b', [{'gate': 'never'}]), ('c', [])])
        self.assertFalse((out / 'c.json').exists())
        receipt = json.loads((out / 'b.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertEqual(receipt['clip'], str(out / 'b' / 'clip.mp4'))
        self.assertTrue((out / 'b' / 'clip.mp4').samefile(folder / 'rec-3.mp4'))
        self.assertEqual([e['event'] for e in events[-2:]], ['stop', 'clip'])
        self.assertEqual(events[-1]['row'], 'b')
        self.assertEqual(json.loads((out / 'a.json').read_text())['clip'], str(out / 'a' / 'clip.mp4'))

    def test_stopped_recording_is_restarted_before_the_row_and_is_its_clip(self):
        folder, out, events, client = self.run_split_plan([('a', []), ('b', [{'shot': 'x'}])], stopped_at=[2])
        self.assertEqual([(e['event'], e.get('row')) for e in events],
                         [('start', None), ('row-start', 'a'), ('clip', None), ('row-end', 'a'),
                          ('recording-restarted', 'b'), ('clip', 'a'), ('row-start', 'b'), ('shot', 'b'),
                          ('row-end', 'b'), ('stop', None), ('clip', 'b')])
        restarted = events[4]
        self.assertEqual((restarted['ok'], restarted['path']), (True, str(folder / 'rec-3.mp4')))
        self.assertTrue(events[6]['split']['restarted'])
        self.assertEqual(client.calls.count('recording.session_split'), 1)  # b needed no split
        # ensure came before b's first input (its shot)
        self.assertLess(len(client.calls) - client.calls[::-1].index('recording.session_ensure'),
                        len(client.calls) - client.calls[::-1].index('ui.screenshot'))
        b = json.loads((out / 'b.json').read_text())
        self.assertEqual((b['status'], b['clip_source']), ('PASS', str(folder / 'rec-3.mp4')))
        self.assertEqual(json.loads((out / 'a.json').read_text())['clip_source'], str(folder / 'rec-2.mp4'))

    def test_a_recording_that_cannot_restart_stops_the_run_before_the_row(self):
        folder, out, events, client = self.run_split_plan([('a', []), ('b', [{'shot': 'x'}]), ('c', [])],
                                                           stopped_at=[2], refuse_start=True)
        b = json.loads((out / 'b.json').read_text())
        self.assertEqual((b['status'], b['not_executed'], b['results']), ('FAILED', True, []))
        self.assertIn('could not be restarted', b['error'])
        self.assertFalse((out / 'c.json').exists())
        self.assertNotIn('ui.screenshot', client.calls)  # b's shot never ran
        failed = [e for e in events if e['event'] == 'recording-restarted']
        self.assertEqual((failed[0]['row'], failed[0]['ok']), ('b', False))

    def test_dry_run_validates_per_row_files(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'session_recording': {'marks': ['shot'], 'per_row_files': 'yes', 'split_cap_s': 0},
                'rows': [{'id': 'a', 'start': 's', 'end': 's', 'steps': [{'shot': 'x'}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        errors = runner.dry_run(folder / 'rows.json')['errors']
        self.assertTrue(any('per_row_files must be' in e for e in errors), errors)
        self.assertTrue(any('split_cap_s' in e for e in errors), errors)
        plan['session_recording'] = {'marks': ['shot'], 'per_row_files': True}
        (folder / 'rows.json').write_text(json.dumps(plan))
        errors = runner.dry_run(folder / 'rows.json')['errors']
        self.assertTrue(any('keep it in marks' in e for e in errors), errors)

    def test_dry_run_validates_it(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'session_recording': {'marks': ['bogus'], 'log': '../x'},
                'rows': [{'id': 'a', 'start': 's', 'end': 's', 'steps': [{'shot': 'x'}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        errors = runner.dry_run(folder / 'rows.json')['errors']
        self.assertTrue(any('marks' in e for e in errors), errors)
        self.assertTrue(any('log' in e for e in errors), errors)


class TrainingReturnTests(unittest.TestCase):
    class IO:
        def __init__(self, token):
            self.token = token
            self.screen = 0
            self.revision = 0
            self.actions = []
            self.connection = 1
            self.boot = '0x0000000000000001'

        def read(self, provider, cap):
            import test_native_picker as fixtures

            self.revision += 1
            if provider == routes.native_picker.TILE:
                if self.screen == 0:
                    return fixtures.tile(self.revision, seat='0', token=self.token)
                if self.screen == 1:
                    return fixtures.snapshot(provider, self.revision, {
                        'screen': 'sss', 'focus': 'sss.grid.normal', 'layout': 'sss.grid',
                        'grid': 'normal', 'generation': '1',
                    })
                return fixtures.snapshot(provider, self.revision, {
                    'screen': 'unknown', 'focus': 'unknown', 'observation.active': '0',
                })
            states = [
                {**routes.TRAINING_CSS, 'focus': 'css.training'},
                {**routes.TRAINING_SSS, 'focus': 'stage_select'},
                routes.GAMES_TRAINING,
                routes._main_tab('smash'),
            ]
            return fixtures.native(self.revision, **states[self.screen])

        def arm(self, provider, cap):
            pass

        def send(self, macro):
            self.actions.append(macro)
            if self.screen == 0 and self.token == 'placed':
                self.token = 'held'
            else:
                self.screen += 1

        def finish(self):
            pass

    def execute(self, io):
        from itertools import count
        from unittest.mock import patch

        clock = count(step=.01)
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
                patch.object(routes.time, 'monotonic', side_effect=lambda: next(clock)), \
                patch.object(routes, '_save', side_effect=lambda _ctx, _name, data: data):
            return routes.back_to_main(object())

    def run_return(self, token):
        io = self.IO(token)
        return io, self.execute(io)

    def test_placed_training_token_returns_before_leaving_css(self):
        io, result = self.run_return('placed')
        self.assertEqual(io.actions, ['leave-css', 'leave-css', 'tap-b1', 'tap-b1'])
        self.assertEqual(result['looks'][0], 'css-token-back')

    def test_held_training_token_needs_only_one_css_return(self):
        io, result = self.run_return('held')
        self.assertEqual(io.actions, ['leave-css', 'tap-b1', 'tap-b1'])
        self.assertEqual(result['looks'], ['css', 'sss', 'games_and_more'])

    def test_reconnected_tile_cannot_authorize_token_back(self):
        from unittest.mock import patch

        io = self.IO('placed')
        read = io.read

        def reconnect_on_tile(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE:
                io.connection = 2
                return routes.native_picker.Sample(sample.block, 2)
            return sample

        io.read = reconnect_on_tile
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
                self.assertRaisesRegex(RuntimeError, 'bridge connection changed'):
            routes.back_to_main(object())
        self.assertEqual(io.actions, [])

    def test_foreign_sss_tile_cannot_authorize_css_token_back(self):
        io = self.IO('placed')
        read = io.read

        def foreign_tile(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE:
                sample.values.update(screen='sss', controller='7', seat='7')
            return sample

        io.read = foreign_tile
        with self.assertRaisesRegex(RuntimeError, 'css: no qualified active tile layout'):
            self.execute(io)
        self.assertEqual(io.actions, [])

    def test_missing_local_token_after_b_is_not_success(self):
        io = self.IO('placed')
        read = io.read

        def missing_after_b(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE and io.actions:
                sample.values.pop('local.token')
            return sample

        io.read = missing_after_b
        with self.assertRaisesRegex(RuntimeError, 'local medal changed while taking it back'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_changed_placed_target_after_b_is_rejected(self):
        io = self.IO('placed')
        io.send = io.actions.append
        read = io.read

        def changed_target(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE and io.actions:
                sample.values['placed.name'] = 'Other'
            return sample

        io.read = changed_target
        with self.assertRaisesRegex(RuntimeError, 'local medal changed while taking it back'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_unowned_held_token_rejects_input(self):
        io = self.IO('held')
        read = io.read

        def wrong_carried(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE:
                sample.values['carried'] = '7'
            return sample

        io.read = wrong_carried
        with self.assertRaisesRegex(RuntimeError, 'local token ownership'):
            self.execute(io)
        self.assertEqual(io.actions, [])

    def test_inactive_tile_can_fence_direct_return_to_games_and_more(self):
        io = self.IO('placed')
        send = io.send

        def return_directly(macro):
            if not io.actions:
                io.actions.append(macro)
                io.screen = 2
            else:
                send(macro)

        io.send = return_directly
        result = self.execute(io)
        self.assertEqual(result['looks'], ['css', 'games_and_more'])
        self.assertEqual(io.actions, ['leave-css', 'tap-b1'])

    def test_sss_tile_then_foreign_main_dialog_is_rejected(self):
        io = self.IO('placed')
        send, read = io.send, io.read

        def return_to_stage(macro):
            send(macro)
            if len(io.actions) == 1:
                io.screen = 1

        def foreign_main(provider, cap):
            sample = read(provider, cap)
            if io.actions and provider == routes.native_picker.UI:
                sample.values.update(**routes._main_tab('smash'))
                sample.values.update({'input.owner': 'foreign', 'dialog': 'confirmation'})
            return sample

        io.send, io.read = return_to_stage, foreign_main
        with self.assertRaisesRegex(RuntimeError, 'unexpected Training return focus'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_pre_input_placement_change_rejects_b(self):
        io = self.IO('placed')
        read = io.read
        tile_reads = 0

        def changing_tile(provider, cap):
            nonlocal tile_reads
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE:
                tile_reads += 1
                if tile_reads == 2:
                    sample.values['placed.at'] = '8'
            return sample

        io.read = changing_tile
        with self.assertRaisesRegex(RuntimeError, 'local medal placement changed before B'):
            self.execute(io)
        self.assertEqual(io.actions, [])

    def test_changed_placed_identity_before_b_sends_nothing(self):
        for field, changed in (('placed.kind', '999'), ('placed.name', 'Other'), ('placed.form', 'other')):
            with self.subTest(field=field):
                io = self.IO('placed')
                read = io.read
                tile_reads = 0

                def changed_identity(provider, cap):
                    nonlocal tile_reads
                    sample = read(provider, cap)
                    if provider == routes.native_picker.TILE:
                        tile_reads += 1
                        if tile_reads == 2:
                            sample.values[field] = changed
                    return sample

                io.read = changed_identity
                with self.assertRaisesRegex(RuntimeError, 'local medal placement changed before B'):
                    self.execute(io)
                self.assertEqual(io.actions, [])

    def test_held_tile_then_direct_return_is_accepted(self):
        io = self.IO('placed')
        read = io.read

        def held_then_games(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE and io.actions and io.screen == 0:
                self.assertEqual(sample.values['local.token'], 'held')
                io.screen = 2
            return sample

        io.read = held_then_games
        result = self.execute(io)
        self.assertEqual(result['looks'], ['css', 'games_and_more'])
        self.assertEqual(io.actions, ['leave-css', 'tap-b1'])

    def test_held_then_foreign_main_before_games_is_rejected(self):
        import test_native_picker as fixtures

        io = self.IO('placed')
        read = io.read
        post_input_ui = 0

        def foreign_then_games(provider, cap):
            nonlocal post_input_ui
            sample = read(provider, cap)
            if provider == routes.native_picker.UI and io.actions:
                post_input_ui += 1
                if post_input_ui == 1:
                    io.screen = 2
                    return fixtures.native(io.revision, **{
                        **routes._main_tab('smash'), 'input.owner': 'foreign', 'dialog': 'confirmation',
                    })
            return sample

        io.read = foreign_then_games
        with self.assertRaisesRegex(RuntimeError, 'unexpected Training return focus'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_held_then_newer_sss_before_older_games_is_rejected(self):
        import test_native_picker as fixtures

        io = self.IO('placed')
        read = io.read
        post_input_ui = 0

        def sss_then_older_games(provider, cap):
            nonlocal post_input_ui
            sample = read(provider, cap)
            if provider == routes.native_picker.UI and io.actions:
                post_input_ui += 1
                if post_input_ui == 1:
                    io.screen = 2
                    return fixtures.native(900, **{**routes.TRAINING_SSS, 'focus': 'stage_select'})
            return sample

        io.read = sss_then_older_games
        with self.assertRaisesRegex(RuntimeError, 'ssbu.ui: no newer active observation'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_held_then_loading_and_settling_games_is_accepted(self):
        import test_native_picker as fixtures

        io = self.IO('placed')
        read = io.read
        post_input_ui = 0

        def loading_then_games(provider, cap):
            nonlocal post_input_ui
            sample = read(provider, cap)
            if provider == routes.native_picker.UI and io.actions:
                post_input_ui += 1
                if post_input_ui == 1:
                    io.screen = 2
                    return fixtures.loading(io.revision)
                if post_input_ui == 2:
                    return fixtures.native(io.revision, **routes._GAMES_TRAINING_SETTLING)
            return sample

        io.read = loading_then_games
        result = self.execute(io)
        self.assertEqual(result['looks'], ['css', 'games_and_more'])
        self.assertEqual(io.actions, ['leave-css', 'tap-b1'])

    def test_held_tile_then_foreign_main_dialog_is_rejected(self):
        io = self.IO('placed')
        read = io.read

        def held_then_foreign_main(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE and io.actions and io.screen == 0:
                self.assertEqual(sample.values['local.token'], 'held')
                io.screen = 3
            elif provider == routes.native_picker.UI and io.actions:
                sample.values.update({'input.owner': 'foreign', 'dialog': 'confirmation'})
            return sample

        io.read = held_then_foreign_main
        with self.assertRaisesRegex(RuntimeError, 'unexpected Training return focus'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_token_back_does_not_send_after_source_deadline(self):
        from unittest.mock import patch

        io = self.IO('placed')
        read = io.read
        now, tile_reads = [0], [0]

        def late_second_tile(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE:
                tile_reads[0] += 1
                if tile_reads[0] == 2:
                    now[0] = 10
            return sample

        io.read = late_second_tile
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
                patch.object(routes.time, 'monotonic', side_effect=lambda: now[0]), \
                self.assertRaisesRegex(RuntimeError, 'Training return source deadline expired before leave-css'):
            routes.back_to_main(object())
        self.assertEqual(io.actions, [])

    def test_tile_step_during_token_back_is_rejected(self):
        for field, value in (('ack', '1'), ('applied', '1'), ('ack', 'unknown'), ('applied', 'unknown')):
            with self.subTest(field=field, value=value):
                io = self.IO('placed')
                read = io.read

                def stepped_tile(provider, cap):
                    sample = read(provider, cap)
                    if provider == routes.native_picker.TILE and io.actions:
                        sample.values[field] = value
                    return sample

                io.read = stepped_tile
                with self.assertRaisesRegex(RuntimeError, 'a tile step arrived while taking back the local token'):
                    self.execute(io)
                self.assertEqual(io.actions, ['leave-css'])

    def test_css_token_back_requires_a_fresh_qualified_css_ui(self):
        io = self.IO('placed')
        read = io.read

        def changed_owner(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.UI and io.actions:
                sample.values['dialog'] = 'confirmation'
            return sample

        io.read = changed_owner
        with self.assertRaisesRegex(RuntimeError, 'unexpected Training return focus'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_token_return_can_already_leave_css(self):
        from unittest.mock import patch

        io = self.IO('placed')
        send = io.send

        def send_and_leave(macro):
            send(macro)
            if len(io.actions) == 1:
                io.screen = 1

        io.send = send_and_leave
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
                patch.object(routes, '_save', side_effect=lambda _ctx, _name, data: data):
            result = routes.back_to_main(object())
        self.assertEqual(io.actions, ['leave-css', 'tap-b1', 'tap-b1'])
        self.assertEqual(result['looks'], ['css', 'sss', 'games_and_more'])

    def test_placed_token_timeout_does_not_claim_a_return(self):
        from itertools import count
        from unittest.mock import patch

        io = self.IO('placed')
        io.send = io.actions.append
        clock = count(step=.25)
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
                patch.object(routes.time, 'monotonic', side_effect=lambda: next(clock)), \
                self.assertRaisesRegex(RuntimeError, 'ssbu.ui: no newer qualified Training return scene transition'):
            routes.back_to_main(object())
        self.assertEqual(io.actions, ['leave-css'])


class SwitchBootTests(unittest.TestCase):
    def test_launched_switch_uses_the_same_title_gate_and_deadline(self):
        from unittest.mock import Mock, patch

        ctx = Mock(instance={'socket': 'usb:///dev/ttyACM0'})
        with patch.object(routes, 'to_main', return_value={'main': True}) as to_main, \
                patch.object(routes, '_save', side_effect=lambda _ctx, _name, result: result):
            self.assertEqual(routes.switch_boot(ctx, cap=5), {'main': True})
        to_main.assert_called_once_with(ctx, cap=5)
        ctx.call.assert_not_called()
        ctx.shot.assert_not_called()

    def test_system_applet_needs_owner_and_focus(self):
        from unittest.mock import Mock, patch

        ctx = Mock(instance={'socket': 'usb:///dev/ttyACM0'})
        with patch.object(routes, 'to_main', side_effect=RuntimeError('ssbu.ui unavailable')), \
                self.assertRaisesRegex(RuntimeError, 'system applets have no bridge owner or focus'):
            routes.switch_boot(ctx, cap=5)
        ctx.call.assert_not_called()
        ctx.shot.assert_not_called()


class ExpectTests(unittest.TestCase):
    def test_expectations_survive_a_new_runner_process(self):
        folder = Path(tempfile.mkdtemp())

        class R:
            out = folder
        first = FakeCtx.__new__(FakeCtx)
        first.runner = R()
        routes.remember(first, stage_id=3)
        routes.remember(first, p1_kind=0)
        second = FakeCtx.__new__(FakeCtx)
        second.runner = R()  # a retry: a new runner object, nothing in memory
        self.assertEqual(routes.expected(second), {'stage_id': 3, 'p1_kind': 0})


class SettledTests(unittest.TestCase):
    def body(self, x, status=0, situation=0):
        return {'x': x, 'y': 0.01, 'status': status, 'situation': situation, 'kind': 0, 'lr': 1.0, 'dmg': 0.0}

    def test_two_equal_grounded_reads(self):
        a = {0: self.body(-40.0), 1: self.body(40.0)}
        self.assertTrue(routes.settled(a, {0: self.body(-40.0), 1: self.body(40.0)}))
        self.assertFalse(routes.settled(None, a))
        self.assertFalse(routes.settled(a, {0: self.body(-39.0), 1: self.body(40.0)}))
        self.assertFalse(routes.settled(a, {0: self.body(-40.0, status=7), 1: self.body(40.0)}))
        self.assertFalse(routes.settled(a, {0: self.body(-40.0), 1: self.body(40.0, situation=2)}))


class LoadStepTests(unittest.TestCase):
    def test_shared_hook_names_resolve_next_to_the_runner(self):
        fn = runner.load_step(Path('/nonexistent'), 'routes:to_main')
        self.assertEqual(fn.__name__, 'to_main')
        fn = runner.load_step(Path('/nonexistent'), 'routes_online:lan_pair')
        self.assertEqual(fn.__name__, 'lan_pair')


class OnlineTests(unittest.TestCase):
    A1 = ('[auto] broker started title=01006A800016E000 lan=192.168.0.222 local_only=false\n'
          '[coord] leading epoch=137 tcp=192.168.0.222:35095\n'
          '[coord] member joined pid=1031977419 vip=10.253.109.237 members=1\n'
          '[auto] ready pid=1031977419 vip=10.253.109.237\n')
    B = ('[auto] broker started title=01006A800016E000 lan=192.168.0.166 local_only=false\n'
         '[auto] ready pid=1709175818 vip=10.253.175.95\n')

    def test_leader_then_pair(self):
        self.assertTrue(routes_online.leader_ok(self.A1, '192.168.0.222'))
        self.assertFalse(routes_online.leader_ok(self.A1, '192.168.0.1'))
        a2 = self.A1 + '[coord] member joined pid=1709175818 vip=10.253.175.95 members=2\n'
        self.assertFalse(routes_online.pair_joined(self.A1, self.B))
        self.assertTrue(routes_online.pair_joined(a2, self.B))

    def test_two_rooms_fail(self):
        b_leads = self.B + '[coord] leading epoch=9 tcp=192.168.0.166:1\n'
        a2 = self.A1 + '[coord] member joined pid=1709175818 vip=10.253.175.95 members=2\n'
        self.assertFalse(routes_online.pair_joined(a2, b_leads))
        self.assertFalse(routes_online.pair_joined(self.A1 + '[coord] member joined pid=5 members=2\n', self.B))


if __name__ == '__main__':
    unittest.main()
