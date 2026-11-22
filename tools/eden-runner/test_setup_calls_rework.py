"""Regression probes for the reviewed setup-call races and compatibility contracts."""

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import native_picker as picker
import routes


SET_NO_BACKUP = '[Bridge] req set result=refused reason=no_backup frame=2627 wall=379784461'
SET_OK = '[Bridge] req set result=ok frame=2628 wall=379784462'
WRITER = '[TrainingRooms] settings.json written by the writer thread'


def delayed_coin(*, action='a', pending='once'):
    from test_setup_calls import CoinReplay

    class Replay(CoinReplay):
        def __init__(self):
            super().__init__()
            self.pending = None
            self.pending_reads = 0

        def send(self, segment):
            if action == 'a' and segment == 'tap-a1':
                self.actions.append(segment)
                held = self.values.get('carried', 'none')
                self.pending = ('take', self.values['hand.token']) if held == 'none' else ('place', held)
                return
            super().send(segment)
            if action == 'step' and segment.startswith('tile-'):
                self.pending = ('identity', {key: self.values[key] for key in
                                             ('carried.kind', 'carried.name', 'carried.form')})
                self.values.update({key: 'unknown' for key in self.pending[1]})

        def read(self, provider, cap):
            if provider == picker.TILE and self.pending:
                self.pending_reads += 1
                if self.pending_reads > 1 and pending != 'forever':
                    operation, value = self.pending
                    self.pending = None
                    if operation == 'take':
                        self._take(value)
                    elif operation == 'place':
                        self._place(value)
                    else:
                        self.values.update(value)
            return super().read(provider, cap)

    return Replay()


def menu_replay(*, idle=False, delay_ack=False):
    from test_setup_calls import MenuReplay
    import test_native_picker as recorded

    class Replay(MenuReplay):
        def __init__(self):
            super().__init__()
            self.cached = None
            self.previous = None

        def send(self, segment):
            previous = dict(self.menu)
            super().send(segment)
            self.cached = None
            if delay_ack:
                self.previous = previous

        def read(self, provider, cap):
            if provider == 'tr.menu' and self.previous is not None:
                self.rev += 1
                value, self.previous = self.previous, None
                return recorded.snapshot(provider, self.rev, value)
            if provider == 'tr.menu' and idle:
                if self.cached is None:
                    self.rev += 1
                    self.cached = recorded.snapshot(provider, self.rev, dict(self.menu))
                return self.cached
            return super().read(provider, cap)

    return Replay()


class SetupReworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = self.root / 'bridge'
        self.bridge.mkdir()
        self.log = self.root / 'bridge.log'
        self.log.write_text('')
        self.ctx = SimpleNamespace(runner=SimpleNamespace(routes_expect={}), on='s', row_dir=self.temp.name,
                                   instance={'bridge_log': str(self.log),
                                             'settings': str(self.bridge / 'settings.json')},
                                   check_stop=lambda: None)

    def test_take_token_waits_through_a_block_published_before_the_a_lands(self):
        io = delayed_coin()
        io.values.update({'hand.token': '1', 'target.token': '1', 'token.settled': '1',
                          'at': io.cpu_tile, 'focus': 'css.medal.1'})
        result = picker.take_token(io, 1, cap=.2)
        self.assertEqual(result.values['p1.carried'], '1')
        self.assertGreaterEqual(io.pending_reads, 2)
        self.assertEqual(io.actions, ['tap-a1'])

    def test_take_token_times_out_or_rejects_a_different_coin(self):
        for fault in ('timeout', 'wrong', 'wrong_unsettled'):
            with self.subTest(fault=fault):
                io = delayed_coin(pending='forever')
                io.values.update({'hand.token': '1', 'target.token': '1', 'token.settled': '1',
                                  'at': io.cpu_tile, 'focus': 'css.medal.1'})
                send = io.send

                def press(segment):
                    send(segment)
                    if fault != 'timeout':
                        io.values['hand.token'] = '0'
                        if fault == 'wrong_unsettled':
                            io.values['token.settled'] = '0'

                io.send = press
                message = 'A did not pick up coin 1 within' if fault == 'timeout' else 'p1.carried'
                with self.assertRaisesRegex(RuntimeError, message):
                    picker.take_token(io, 1, cap=.02)
                self.assertEqual(io.actions, ['tap-a1'])

    def test_take_p1_token_waits_through_the_pre_a_block(self):
        io = delayed_coin()
        io.values.update({'hand.token': '0', 'target.token': '0', 'token.settled': '1',
                          'at': io.p1_tile, 'focus': 'css.medal.0'})
        result = picker.take_token(io, 0, cap=.2)
        self.assertEqual(result.values['p1.carried'], '0')
        self.assertGreaterEqual(io.pending_reads, 2)

    def test_place_cpu_waits_through_the_held_block_before_the_a_lands(self):
        from test_setup_calls import FOX, modelled_block
        io = delayed_coin()
        medal = picker.p1_medal(modelled_block(1, io.values))
        io.cpu_tile = '85'
        io._take('1')
        result = picker.place_cpu(io, FOX, '85', medal, cap=.2)
        self.assertEqual(result.values['cpu.placed.name'], 'Fox')
        self.assertGreaterEqual(io.pending_reads, 2)
        self.assertEqual(io.actions, ['tap-a1'])

    def test_place_cpu_rejects_changed_state_and_reports_timeout(self):
        from test_setup_calls import FOX, modelled_block
        for fault in ('identity', 'tile', 'p1', 'timeout'):
            with self.subTest(fault=fault):
                io = delayed_coin(pending='forever')
                medal = picker.p1_medal(modelled_block(1, io.values))
                io.cpu_tile = '85'
                io._take('1')
                send = io.send

                def press(segment):
                    send(segment)
                    if fault == 'identity':
                        io.values['carried.name'] = 'Mario'
                    elif fault == 'tile':
                        io.values['at'] = '2'
                    elif fault == 'p1':
                        io.values['p1.coin.at'] = '1'

                io.send = press
                messages = {'identity': 'identity changed', 'tile': 'changed tile',
                            'p1': 'P1 coin changed', 'timeout': 'A did not place the CPU coin within'}
                with self.assertRaisesRegex(RuntimeError, messages[fault]):
                    picker.place_cpu(io, FOX, '85', medal, cap=.02)
                self.assertEqual(io.actions, ['tap-a1'])

    def test_walk_cpu_waits_for_the_carried_identity_after_settling(self):
        from test_setup_calls import FOX, TOPOLOGY
        io = delayed_coin(action='step')
        io._take('1')
        result, goal = picker.walk_cpu(io, FOX, TOPOLOGY, cap=.2)
        self.assertEqual((goal, result.values['carried.name']), ('85', 'Fox'))
        self.assertGreaterEqual(io.pending_reads, 2)
        self.assertEqual(io.actions, ['tile-left'])

    def test_walk_cpu_rejects_a_known_wrong_carried_identity(self):
        from test_setup_calls import FOX, TOPOLOGY
        io = delayed_coin(action='step')
        io._take('1')
        send = io.send

        def step(segment):
            send(segment)
            io.values['carried.name'] = 'Mario'

        io.send = step
        with self.assertRaisesRegex(RuntimeError, 'qualified topology identity'):
            picker.walk_cpu(io, FOX, TOPOLOGY, cap=.2)
        self.assertEqual(io.actions, ['tile-left'])

    def test_wait_native_preserves_one_second_minimum(self):
        with patch.object(routes, 'native_state', return_value={'stage': 3}) as state:
            routes.wait_native(self.ctx, lambda value: True, cap=.1)
        self.assertGreaterEqual(state.call_args.kwargs['cap'], 1.0)

    def test_tr_input_waits_through_a_newer_block_without_the_ack(self):
        for macro, opened in (('tap-a1', True), ('tap-b1', False)):
            with self.subTest(macro=macro):
                io = menu_replay(delay_ack=True)
                before = io.read('tr.menu', .2)
                send = io.send

                def press(segment):
                    send(segment)
                    if macro == 'tap-a1':
                        io.previous['scene'] = 'notice'

                io.send = press
                result = routes._tr_input(io, before, macro, time.monotonic() + .2, opened=opened)
                self.assertEqual(result.values['ack'], '1')
                self.assertEqual(result.values['open'], str(int(opened)))
                self.assertEqual(io.actions, [macro])

    def test_tr_input_rejects_ack_gap_ignored_and_timeout(self):
        for fault in ('gap', 'ignored', 'timeout'):
            with self.subTest(fault=fault):
                io = menu_replay()
                before = io.read('tr.menu', .2)
                send = io.send

                def press(segment):
                    if fault == 'timeout':
                        io.actions.append(segment)
                    else:
                        io.wrong_ack = fault == 'gap'
                        send(segment)
                        if fault == 'ignored':
                            io.menu['ack.result'] = 'ignored'

                io.send = press
                with self.assertRaisesRegex(RuntimeError, 'was not handled' if fault == 'timeout' else 'ACK'):
                    routes._tr_input(io, before, 'tap-a1', time.monotonic() + .02)
                self.assertEqual(io.actions, ['tap-a1'])

    def test_set_cpu_starts_from_an_idle_menu_with_no_newer_block(self):
        for initially_open in (False, True):
            with self.subTest(initially_open=initially_open):
                io = menu_replay(idle=True)
                io.menu['open'] = str(int(initially_open))
                tables = copy.deepcopy(routes.tables())
                tables['roster_catalog'] = [{'kind': 0, 'name': 'Mario'}]
                with patch.object(routes, 'BridgePickerIO', return_value=io), \
                     patch.object(routes, 'tables', return_value=tables), \
                     patch.object(routes, '_wait_writer', return_value='writer'), \
                     patch.object(routes, '_fetch_settings', return_value=b'{"menu":{"cpu_character":[1]}}'), \
                     patch.object(routes, 'wait_native', return_value={'roster': {0: {'kind': 0}, 1: {'kind': 0}}}), \
                     patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
                    result = routes.set_cpu(self.ctx, 'Mario', cap=.1)
                json.dumps(result)
                self.assertEqual(io.actions[0], 'tap-a1' if initially_open else 'tr-menu-open')
                self.assertEqual(result['verified']['kind'], 0)

    def test_writer_line_wait_is_not_a_bridge_request(self):
        self.ctx.runner.setup_metrics = {'bridge_requests': 7}
        with patch.object(routes, '_wait_bridge_lines', return_value='[TrainingRooms] settings.json written'):
            routes._wait_writer(self.ctx, object(), time.monotonic() + 1)
        self.assertEqual(self.ctx.runner.setup_metrics['bridge_requests'], 7)

    def bridge_spool(self, replies):
        self.ctx.runner.setup_metrics = {'bridge_requests': 0}
        calls = []
        replies = iter(replies)

        def spool(ctx, name, data):
            expected, lines = next(replies)
            self.assertEqual(name, expected)
            self.assertEqual(data, b'' if name == 'bridge-backup.req' else b'settings')
            calls.append(name)
            ctx.runner.setup_metrics['bridge_requests'] += 1
            with self.log.open('a') as stream:
                stream.write('\n'.join(lines) + '\n')

        return spool, calls

    def test_apply_settings_missing_backup_reports_capture_contract_without_retry(self):
        spool, calls = self.bridge_spool([('diag-settings-next.json', [SET_NO_BACKUP])])
        with patch.object(routes, '_spool_file', side_effect=spool), \
             patch.object(routes, '_fetch_settings', return_value=b'readback'):
            with self.assertRaises(RuntimeError) as error:
                routes._apply_settings(self.ctx, b'settings', cap=1)
        self.assertEqual(str(error.exception),
                         'settings set refused: the bridge has no open fixture backup. '
                         'Expected the capture to have opened one at connect '
                         '("[Bridge] req backup result=ok"; routes:bridge_on does). Saw: ' + SET_NO_BACKUP)
        self.assertEqual(calls, ['diag-settings-next.json'])
        self.assertEqual(self.ctx.runner.setup_metrics['bridge_requests'], 1)

    def test_apply_settings_open_backup_does_not_send_backup(self):
        spool, calls = self.bridge_spool([('diag-settings-next.json', [SET_OK, WRITER])])
        with patch.object(routes, '_spool_file', side_effect=spool), \
             patch.object(routes, '_fetch_settings', return_value=b'readback'):
            self.assertEqual(routes._apply_settings(self.ctx, b'settings', cap=1), (b'readback', SET_OK))
        self.assertEqual(calls, ['diag-settings-next.json'])
        self.assertEqual(self.ctx.runner.setup_metrics['bridge_requests'], 2)
        self.assertFalse((self.root / 'routes-bridge-backup.json').exists())

    def test_apply_settings_other_refusal_does_not_open_backup(self):
        refused = '[Bridge] req set result=refused reason=busy frame=2627 wall=379784461'
        spool, calls = self.bridge_spool([('diag-settings-next.json', [refused])])
        with patch.object(routes, '_spool_file', side_effect=spool), \
             patch.object(routes, '_fetch_settings', return_value=b'readback'):
            with self.assertRaises(RuntimeError) as error:
                routes._apply_settings(self.ctx, b'settings', cap=1)
        self.assertEqual(str(error.exception), refused)
        self.assertEqual(calls, ['diag-settings-next.json'])
        self.assertEqual(self.ctx.runner.setup_metrics['bridge_requests'], 1)
        self.assertFalse((self.root / 'routes-bridge-backup.json').exists())

    def test_tr_settings_file_readback_allows_added_keys_and_names_changed_paths(self):
        path = self.root / 'settings.json'
        path.write_text('{"menu":{"cpu_level":[1,0]},"version":1}')
        good = b'{"menu":{"cpu_level":[1,0],"added":42},"version":1,"extra":true}'
        bad = b'{"menu":{"cpu_level":[0,1],"added":42},"version":1}'
        missing = b'{"menu":{"added":42},"version":1}'
        with patch.object(routes, 'BridgePickerIO', return_value=menu_replay()), \
             patch.object(routes, '_fetch_settings', return_value=b'{"menu":{}}'), \
             patch.object(routes, '_apply_settings', side_effect=[(good, 'ok'), (bad, 'ok'), (missing, 'ok')]), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.tr_settings(self.ctx, file=path)
            json.dumps(result)
            self.assertEqual(result['verified']['keys'], ['menu', 'version'])
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, r'menu\.cpu_level'):
                    routes.tr_settings(self.ctx, file=path)

    def test_pick_fighters_without_cpu_calls_the_old_p1_path(self):
        topology = {'path': 'modelled', 'sha256': 'modelled'}
        with patch.object(routes, 'pick_local_fighter', return_value={'placed': True}) as local, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            json.dumps(routes.pick_fighters(self.ctx, 'Mario', topology=topology))
        local.assert_called_once_with(self.ctx, 'Mario', topology=topology, form='default')

    def test_pick_fighters_take_back_and_cpu_use_their_intended_paths(self):
        topology = {'path': 'modelled', 'sha256': 'modelled'}
        with patch.object(routes, 'pick_local_fighter', return_value={'placed': True}) as local, \
             patch.object(routes, 'pick_cpu', return_value={'placed': True}) as cpu, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            json.dumps(routes.pick_fighters(self.ctx, 'Mario', topology=topology, take_back=True))
            local.assert_called_once_with(self.ctx, 'Mario', topology=topology, form='default', take_back=True)
            cpu.assert_not_called()
            local.reset_mock()
            json.dumps(routes.pick_fighters(self.ctx, 'Mario', cpu='Fox', topology=topology))
            local.assert_called_once_with(self.ctx, 'Mario', topology=topology, form='default',
                                          take_back=True, expected_context='training')
            cpu.assert_called_once_with(self.ctx, 'Fox', topology=topology, form='default')

    def test_reach_training_requests_take_back_on_the_css_suffix(self):
        io = menu_replay()
        io.menu['open'] = '0'
        initial = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 3}}}
        final = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
             patch.object(routes, 'native_state', side_effect=[initial, final]), \
             patch.object(routes, 'quit_training', return_value={}), \
             patch.object(routes, 'enter_training', return_value={}), \
             patch.object(routes, 'pick_fighters', return_value={}) as fighters, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            json.dumps(routes.reach_training(self.ctx, 'Mario', cpu='Fox'))
        self.assertIs(fighters.call_args.kwargs['take_back'], True)

    def test_set_player_requests_take_back_at_training_character_select(self):
        import test_native_picker as recorded

        io = menu_replay()
        io.read = lambda provider, cap: recorded.l8w_native(2)
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
             patch.object(routes, 'pick_local_fighter', return_value={'placed': True}) as local, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            json.dumps(routes.set_player(self.ctx, 'Mario'))
        self.assertIs(local.call_args.kwargs['take_back'], True)


if __name__ == '__main__':
    unittest.main()
