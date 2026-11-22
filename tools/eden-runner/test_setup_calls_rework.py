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

    def test_walk_cpu_waits_for_the_carried_identity_after_settling(self):
        from test_setup_calls import FOX, TOPOLOGY
        io = delayed_coin(action='step')
        io._take('1')
        result, goal = picker.walk_cpu(io, FOX, TOPOLOGY, cap=.2)
        self.assertEqual((goal, result.values['carried.name']), ('85', 'Fox'))
        self.assertGreaterEqual(io.pending_reads, 2)
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
                result = routes._tr_input(io, before, macro, time.monotonic() + .2, opened=opened)
                self.assertEqual(result.values['ack'], '1')
                self.assertEqual(result.values['open'], str(int(opened)))
                self.assertEqual(io.actions, [macro])

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
                self.assertEqual(io.actions[0], 'tap-a1' if initially_open else 'tr-menu-open')
                self.assertEqual(result['verified']['kind'], 0)

    def test_writer_line_wait_is_not_a_bridge_request(self):
        self.ctx.runner.setup_metrics = {'bridge_requests': 7}
        with patch.object(routes, '_wait_bridge_lines', return_value='[TrainingRooms] settings.json written'):
            routes._wait_writer(self.ctx, object(), time.monotonic() + 1)
        self.assertEqual(self.ctx.runner.setup_metrics['bridge_requests'], 7)

    def test_tr_settings_file_readback_allows_added_keys_and_names_changed_paths(self):
        path = self.root / 'settings.json'
        path.write_text('{"menu":{"cpu_level":[1,0]},"version":1}')
        good = b'{"menu":{"cpu_level":[1,0],"added":42},"version":1,"extra":true}'
        bad = b'{"menu":{"cpu_level":[0,1],"added":42},"version":1}'
        with patch.object(routes, 'BridgePickerIO', return_value=menu_replay()), \
             patch.object(routes, '_fetch_settings', return_value=b'{"menu":{}}'), \
             patch.object(routes, '_apply_settings', side_effect=[(good, 'ok'), (bad, 'ok')]), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.tr_settings(self.ctx, file=path)
            self.assertEqual(result['verified']['keys'], ['menu', 'version'])
            with self.assertRaisesRegex(RuntimeError, r'menu\.cpu_level'):
                routes.tr_settings(self.ctx, file=path)

    def test_pick_fighters_without_cpu_calls_the_old_p1_path(self):
        topology = {'path': 'modelled', 'sha256': 'modelled'}
        with patch.object(routes, 'pick_local_fighter', return_value={'placed': True}) as local, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            routes.pick_fighters(self.ctx, 'Mario', topology=topology)
        local.assert_called_once_with(self.ctx, 'Mario', topology=topology, form='default')


if __name__ == '__main__':
    unittest.main()
