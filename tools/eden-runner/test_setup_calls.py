"""Offline setup-call tests; no emulator or controller is started."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_picker as picker
import routes
import test_native_picker as recorded
from test_setup_calls_rework import SetupReworkTests


MARIO = picker.Target('0', 'Mario', 'default')
DONKEY = picker.Target('1', 'Donkey Kong', 'default')
LINK = picker.Target('2', 'Link', 'default')
FOX = picker.Target('7', 'Fox', 'default')
TOPOLOGY = picker.Topology(
    recorded.L8W_LAYOUT,
    {'0': {'right': '1'}, '1': {'left': '0', 'right': '2'},
     '2': {'left': '1', 'right': '85'}, '85': {'left': '2'}},
    {'0': MARIO, '1': DONKEY, '2': LINK, '85': FOX},
)
ADDITIONS = {
    'hand.token': 'none', 'p1.coin': 'placed', 'p1.coin.at': '0',
    'cpu.token': 'placed', 'cpu.placed.at': '86', 'cpu.placed.kind': 'unknown',
    'cpu.placed.name': 'unknown', 'cpu.placed.form': 'unknown',
    'carried.kind': 'unknown', 'carried.name': 'unknown', 'carried.form': 'unknown',
    'target.token': 'none', 'token.settled': '0',
}


def modelled_block(rev, changes=None, *, old=False):
    """Append modelled CPU lines to the recorded l8w placed-Mario block."""
    block = copy.deepcopy(recorded.l8w_tile(1789).block)
    block['rev'] = rev
    block['values'].update({'rev': str(rev), 'observation': str(10781 + rev)})
    if not old:
        block['values'].update(ADDITIONS)
    block['values'].update(changes or {})
    return picker.Sample(block, 1)


class CoinReplay:
    """Generate newer modelled blocks in response to the requested macro."""

    def __init__(self, *, overlap=False, already=False, old=False, fault=None):
        self.rev = 1
        self.actions = []
        self.evidence = []
        self.old = old
        self.fault = fault
        self.p1_tile = '0'
        self.cpu_tile = '0' if overlap else '86'
        self.cpu_target = FOX if already else None
        self.values = dict(ADDITIONS)
        self.values.update({'at': '0', 'hand.token': '0' if overlap else 'none',
                            'cpu.placed.at': self.cpu_tile, 'ack': '13', 'applied': '13'})
        if already:
            self.values.update({'cpu.placed.at': '85', 'cpu.placed.kind': FOX.kind,
                                'cpu.placed.name': FOX.name, 'cpu.placed.form': FOX.form})
            self.cpu_tile = '85'

    def arm(self, provider, cap):
        pass

    def read(self, provider, cap):
        self.rev += 1
        if provider == picker.UI:
            return recorded.l8w_native(self.rev)
        return modelled_block(self.rev, self.values, old=self.old)

    def log(self, evidence):
        self.evidence.append(evidence)

    def finish(self):
        pass

    def _identity(self, target, prefix=''):
        return {prefix + 'kind': target.kind, prefix + 'name': target.name, prefix + 'form': target.form}

    def _old_medal(self):
        target = TOPOLOGY.targets[self.p1_tile]
        return {'p1.token': 'placed', 'local.token': 'placed', 'placed.at': self.p1_tile,
                'placed.kind': target.kind, 'placed.name': target.name, 'placed.form': target.form,
                'p1.placed.at': self.p1_tile, 'p1.placed.kind': target.kind,
                'p1.placed.name': target.name, 'p1.placed.form': target.form}

    def _take(self, slot):
        self.values.update({'hand.token': 'unknown', 'token.settled': '0', 'target.token': 'none',
                            'carried': slot, 'p1.carried': slot, 'settled': '1',
                            'p1.token': 'unknown', 'local.token': 'unknown', 'placed.at': 'unknown',
                            'placed.kind': 'unknown', 'placed.name': 'unknown', 'placed.form': 'unknown'})
        if slot == '0':
            self.values.update({'p1.coin': 'held', 'p1.coin.at': 'unknown', 'local.token': 'held',
                                'p1.token': 'held', 'at': self.p1_tile, 'hovered': self.p1_tile,
                                'focus': 'css.grid.local', **self._identity(TOPOLOGY.targets[self.p1_tile]),
                                **self._identity(TOPOLOGY.targets[self.p1_tile], 'carried.')})
        else:
            self.values.update({'cpu.token': 'held', 'at': self.cpu_tile, 'hovered': self.cpu_tile,
                                'focus': 'unknown', 'carried': 'unknown', 'kind': 'unknown',
                                'name': 'unknown', 'form': 'unknown', 'settled.ack': 'unknown',
                                'ack.result': 'unknown', 'result': 'unknown', 'direction': 'unknown',
                                'from': 'unknown', 'to': 'unknown'})
            target = TOPOLOGY.targets.get(self.cpu_tile)
            if target:
                self.values.update(self._identity(target, 'carried.'))
            else:
                self.values.update({'carried.kind': 'unknown', 'carried.name': 'Random',
                                    'carried.form': 'default'})
        if slot == '0':
            self.values['settled.ack'] = self.values['ack']

    def _place(self, slot):
        at = self.values['at']
        target = TOPOLOGY.targets[at]
        if slot == '0':
            self.p1_tile = at
            self.values.update({'p1.coin': 'placed', 'p1.coin.at': at, **self._old_medal()})
        else:
            self.cpu_tile = at
            self.values.update({'cpu.token': 'placed', 'cpu.placed.at': at,
                                **self._identity(target, 'cpu.placed.'), **self._old_medal()})
            if self.fault == 'wrong_cpu':
                self.values['cpu.placed.name'] = 'Mario'
        self.values.update({'carried': 'none', 'p1.carried': 'unknown', 'hand.token': slot,
                            'token.settled': '0', 'focus': 'css.medal.' + slot,
                            'settled': '0', 'settled.ack': 'unknown'})
        if slot == '1':
            self.values.update({'hand.token': 'none', 'focus': 'css.medal.mario',
                                'kind': 'unknown', 'name': 'unknown', 'form': 'unknown',
                                'ack.result': 'unknown', 'result': 'unknown', 'direction': 'unknown',
                                'from': 'unknown', 'to': 'unknown'})

    def send(self, segment):
        self.actions.append(segment)
        if segment == 'tap-a1':
            held = self.values.get('p1.carried', self.values.get('carried', 'none'))
            if held == 'unknown':
                held = 'none'
            if held == 'none':
                slot = self.values['hand.token']
                self._take('0' if self.fault == 'wrong_carried' else slot)
            else:
                self._place(held)
            return
        if not segment.startswith('tile-'):
            raise AssertionError(segment)
        direction = segment[5:]
        carried = self.values.get('p1.carried', self.values.get('carried', 'none'))
        if carried == 'unknown':
            carried = 'none'
        before = self.values.get('at', 'none')
        ack = int(self.values['ack']) + (2 if self.fault == 'second_step' else 1)
        applied = int(self.values['applied']) + (2 if self.fault == 'second_step' else 1)
        if carried == 'none':
            pointed = self.values['hand.token']
            slot = '1' if (pointed == 'none' and direction == 'left') or (
                pointed == '0' and direction == 'right') else '0'
            to = self.cpu_tile if slot == '1' else self.p1_tile
            hand = '0' if slot == '1' and self.p1_tile == self.cpu_tile else slot
            self.values.update({'target.token': slot, 'token.settled': str(int(hand == slot)),
                                'hand.token': hand, 'at': to, 'focus': 'css.medal.' + hand})
            if self.fault == 'medal_changed':
                self.values['placed.name'] = 'Fox'
        else:
            to = '85' if before == '86' and direction == 'left' else TOPOLOGY.neighbors[before][direction]
            target = TOPOLOGY.targets[to]
            self.values.update({'at': to, 'hovered': to, 'settled': '1', 'settled.ack': str(ack),
                                'focus': 'unknown' if carried == '1' else 'css.grid.' + to,
                                'target.token': 'none', 'token.settled': '0',
                                **self._identity(target, 'carried.')})
            if carried != '1':
                self.values.update(self._identity(target))
        self.values.update({'ack': str(ack), 'applied': str(applied), 'ack.result': 'handled',
                            'result': 'handled', 'direction': direction,
                            'from': before if before != 'unknown' else 'none', 'to': to})


class SetupCallsR1Tests(unittest.TestCase):
    def setUp(self):
        self.io = CoinReplay()
        self.ctx = SimpleNamespace(runner=SimpleNamespace(routes_expect={}), on='s', instance={}, row_dir='.')

    def pick_cpu(self, io=None):
        io = io or self.io
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
             patch.object(routes, '_tile_topology', return_value=TOPOLOGY), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.pick_cpu(self.ctx, 'Fox', topology={'path': 'modelled', 'sha256': 'modelled'})
        json.dumps(result)
        return result

    def test_pick_cpu_reaches_takes_walks_and_places(self):
        result = self.pick_cpu()
        self.assertEqual(self.io.actions, ['tile-left', 'tap-a1', 'tile-left', 'tap-a1'])
        self.assertEqual(self.io.values['cpu.placed.at'], '85')
        self.assertEqual(self.io.values['cpu.placed.name'], 'Fox')
        self.assertTrue(result['verified'])

    def test_refuses_a_when_hand_token_is_not_the_cpu_coin(self):
        self.io.values['hand.token'] = '0'
        with self.assertRaisesRegex(RuntimeError, 'refusing A: hand.token is 0, not 1'):
            picker.take_token(self.io, 1)
        self.assertNotIn('tap-a1', self.io.actions)

    def test_refuses_when_p1_carried_is_not_1_after_a(self):
        self.io.values['hand.token'] = '1'
        self.io.fault = 'wrong_carried'
        with self.assertRaisesRegex(RuntimeError, 'p1.carried is .* after A'):
            picker.take_token(self.io, 1)
        self.assertEqual(self.io.actions, ['tap-a1'])

    def test_refuses_when_p1s_medal_changes_during_the_cpu_pick(self):
        self.io.fault = 'medal_changed'
        with self.assertRaisesRegex(RuntimeError, '(?i)p1.*(medal|coin).*(changed|preserv)'):
            picker.reach_token(self.io, 1)
        self.assertEqual(self.io.actions, ['tile-left'])

    def test_refuses_when_a_second_step_arrives_while_reaching(self):
        self.io.fault = 'second_step'
        with self.assertRaisesRegex(RuntimeError, 'a tile step arrived while reaching the coin'):
            picker.reach_token(self.io, 1)
        self.assertEqual(self.io.actions, ['tile-left'])

    def test_refuses_when_cpu_placed_is_not_the_target(self):
        self.io.cpu_tile = '85'
        before = picker.Target('0', 'Mario', 'default')
        p1_before = ('placed', '0', before.kind, before.name, before.form)
        self.io._take('1')
        self.io.fault = 'wrong_cpu'
        with self.assertRaisesRegex(RuntimeError, 'cpu.placed is .*not the target'):
            picker.place_cpu(self.io, FOX, '85', p1_before)
        self.assertEqual(self.io.actions, ['tap-a1'])

    def test_pick_fighters_with_cpu_records_the_cpu_kind_only_after_placement(self):
        original = routes.remember
        seen = []

        def remember(ctx, **values):
            if 'cpu_kind' in values:
                self.assertEqual(self.io.values['cpu.token'], 'placed')
                self.assertEqual(self.io.values['cpu.placed.kind'], str(values['cpu_kind']))
                seen.append(values['cpu_kind'])
            return original(ctx, **values)

        with patch.object(routes, 'pick_local_fighter', return_value={'placed': True}) as local, \
             patch.object(routes, 'BridgePickerIO', return_value=self.io), \
             patch.object(routes, '_tile_topology', return_value=TOPOLOGY), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value), \
             patch.object(routes, 'remember', side_effect=remember):
            result = routes.pick_fighters(self.ctx, 'Mario', cpu='Fox', topology={'path': 'modelled', 'sha256': 'modelled'})
        json.dumps(result)
        local.assert_called_once()
        self.assertEqual(seen, [7])

    def test_pick_fighters_without_cpu_returns_a_receipt(self):
        with patch.object(routes, 'pick_local_fighter', return_value={'placed': True}) as local, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.pick_fighters(self.ctx, 'Mario', topology={'path': 'modelled', 'sha256': 'modelled'})
        json.dumps(result)
        self.assertTrue(result['placed'])
        self.assertEqual(result['block'], 'pick_fighters')
        self.assertEqual(result['skipped'], ['pick_cpu'])
        self.assertNotIn('take_back', local.call_args.kwargs)

    def test_tile_macros_hold_the_direction_six_frames(self):
        lib = Path(routes.ROUTES)
        index = json.loads((lib / 'index.json').read_text())
        masks = {'up': 8192, 'down': 32768, 'left': 4096, 'right': 16384}
        for direction, mask in masks.items():
            with self.subTest(direction=direction):
                entry = index['macros']['tile-' + direction]
                path = lib / entry['file']
                macro = json.loads(path.read_text())
                self.assertEqual(entry['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(entry['frames'], 10)
                self.assertEqual(macro['duration_frames'], 10)
                self.assertEqual([(x['frame'], x['buttons']) for x in macro['events']],
                                 [(0, 16), (1, 16 | mask), (7, 0)])

    def test_pick_cpu_untangles_when_both_coins_share_a_tile(self):
        self.io = CoinReplay(overlap=True)
        result = self.pick_cpu()
        self.assertTrue(result['untangled'])
        self.assertEqual(result['parked_at'], '2')
        self.assertEqual(self.io.values['placed.at'], '0')
        self.assertEqual(self.io.values['placed.name'], 'Mario')
        self.assertEqual(self.io.values['cpu.placed.name'], 'Fox')
        self.assertGreaterEqual(self.io.actions.count('tap-a1'), 4)

    def test_an_old_tile_cursor_build_is_refused_before_any_input(self):
        self.io = CoinReplay(old=True)
        self.io.values = {key: value for key, value in self.io.values.items() if key not in ADDITIONS}
        with self.assertRaisesRegex(RuntimeError, "this build does not report the CPU's coin"):
            self.pick_cpu()
        self.assertEqual(self.io.actions, [])

    def test_pick_cpu_does_nothing_when_the_cpu_is_already_the_target(self):
        self.io = CoinReplay(already=True)
        result = self.pick_cpu()
        self.assertTrue(result['already'])
        self.assertEqual(self.io.actions, [])


class MenuReplay:
    """Modelled TR menu blocks with explicit input-to-ACK transitions."""

    def __init__(self, *, depth=0, wrong_cell=False, wrong_ack=False, wrong_grid=False, wrong_keyboard=False):
        self.rev = 1
        initial = recorded.native(1)
        self.connection = initial.connection
        self.boot = initial.clock[0]
        self.actions = []
        self.watches = []
        self.evidence = []
        self.picked = False
        self.wrong_cell = wrong_cell
        self.wrong_ack = wrong_ack
        self.wrong_grid = wrong_grid
        self.wrong_keyboard = wrong_keyboard
        self.writer_log = None
        self.screen = 'training'
        self.menu = {'scene': 'training', 'keyboard': '0', 'open': '1', 'page': 'fighters_stages',
                     'cursor': '4', 'action': 'cpu_character', 'depth': str(depth),
                     'editor.sel': '1' if wrong_cell else '0', 'grid': 'cpu_character',
                     'ack': '0', 'ack.result': 'handled', 'focus': 'fighters_stages'}

    def arm(self, provider, cap):
        self.watches.append(provider)

    def read(self, provider, cap):
        self.rev += 1
        if provider == 'tr.menu':
            return recorded.snapshot(provider, self.rev, self.menu)
        owner = 'tr.menu' if self.menu['open'] == '1' else 'native'
        return recorded.native(self.rev, screen=self.screen, focus=self.screen,
                               **{'screen.context': 'training', 'input.owner': owner})

    def set_screen(self, screen, result=None):
        self.screen = screen
        return {} if result is None else result

    def log(self, evidence):
        self.evidence.append(evidence)

    def finish(self):
        pass

    def send(self, segment):
        self.actions.append(segment)
        values = self.menu
        if segment != 'tr-menu-open':
            values['ack'] = str(int(values['ack']) + (2 if self.wrong_ack else 1))
        values['ack.result'] = 'handled'
        if self.wrong_keyboard:
            values['keyboard'] = '1'
        if segment == 'tap-b1':
            if values['depth'] != '0':
                values['depth'] = '0'
            else:
                values['open'] = '0'
                if self.writer_log is not None:
                    with self.writer_log.open('a') as log:
                        log.write('[TrainingRooms] settings.json written by the writer thread frame=1 wall=1\n')
        elif segment == 'tap-a1':
            if values['depth'] == '0':
                values['depth'] = '1'
                values['editor.sel'] = '1' if self.wrong_cell else '0'
                if self.wrong_grid:
                    values['grid'] = 'player_character'
            else:
                self.picked = True
        elif segment.startswith('main-menu-'):
            if self.wrong_cell:
                values['editor.sel'] = '2'
            else:
                raise AssertionError('unexpected grid step')
        elif segment == 'tr-menu-open':
            values['open'] = '1'
        else:
            raise AssertionError(segment)


def retrying_menu(ctx, advance):
    replay = MenuReplay()
    replay.ctx = ctx
    original_read, original_send = replay.read, replay.send
    dropped = []

    def read(provider, cap):
        advance()
        return original_read(provider, cap)

    def send(macro):
        if macro == 'tap-a1' and not dropped:
            dropped.append(True)
            replay.actions.append(macro)
            return
        original_send(macro)

    replay.read, replay.send = read, send
    return replay


class SetupCallsR2Tests(unittest.TestCase):
    def test_a_macro_playing_before_a_training_input_is_waited_for_and_reported(self):
        now, sent = [0.0], []
        runner = SimpleNamespace(setup_metrics={'stalls': []}, wait_macro=lambda on, cap: None)
        with tempfile.TemporaryDirectory() as folder:
            def call(method, params=None):
                if method == 'macro.status':
                    return {'state': 'playing' if now[0] < 1.2 else 'idle'}
                if method == 'macro.play':
                    sent.append((now[0], params))
                    return {}
                raise AssertionError(method)

            ctx = SimpleNamespace(runner=runner, on='s', instance={}, row_dir=folder,
                                  call=call, log=lambda event: None)
            menu = MenuReplay()
            menu.ctx = ctx
            before = menu.read('tr.menu', 2)
            original = menu.send

            def send(macro):
                routes.play(ctx, macro)
                original(macro)

            menu.send = send
            with patch.object(routes, 'macro_file', return_value=(Path('/macro'), 'sha', 3)), \
                    patch.object(routes.time, 'monotonic', side_effect=lambda: now[0]), \
                    patch.object(routes.time, 'sleep', side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
                result = routes._tr_input(menu, before, 'tap-a1', 10)
            self.assertEqual(result.values['ack'], '1')
            self.assertEqual(len(sent), 1)
            self.assertGreaterEqual(sent[0][0], 1.2)
            self.assertEqual(len(runner.setup_metrics['stalls']), 1)
            self.assertEqual(runner.setup_metrics['stalls'][0]['step'], 'tap-a1')

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.bridge = Path(self.dir.name) / 'bridge'
        self.bridge.mkdir()
        self.log = Path(self.dir.name) / 'bridge.log'
        self.log.write_text('')
        self.ctx = SimpleNamespace(
            runner=SimpleNamespace(routes_expect={'p1_kind': 0, 'cpu_kind': 7}), on='s',
            instance={'bridge_log': str(self.log), 'settings': str(self.bridge / 'settings.json')},
            row_dir=self.dir.name, check_stop=lambda: None,
        )

    def test_enter_training_refuses_a_roster_that_differs_from_the_picks(self):
        state = {'roster': {0: {'kind': 0}, 1: {'kind': 3}}, 'stage': 3}
        with patch.object(routes, '_native_enter_training', return_value={'stage_id': 3}), \
             patch.object(routes, 'wait_native', return_value=state), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            with self.assertRaisesRegex(RuntimeError, 'Training loaded fighters P1 kind 0, CPU kind 3; expected'):
                routes.enter_training(self.ctx, cap=1)

    def test_enter_training_marks_unremembered_fighters_unchecked(self):
        self.ctx.runner.routes_expect = {}
        state = {'roster': {0: {'kind': 0}, 1: {'kind': 3}}, 'stage': 3}
        with patch.object(routes, '_native_enter_training', return_value={'stage_id': 3}), \
             patch.object(routes, 'wait_native', return_value=state), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.enter_training(self.ctx, cap=1)
        json.dumps(result)
        self.assertEqual(result['fighters_checked'], {'p1': 'unchecked', 'cpu': 'unchecked'})
        self.assertEqual(result['roster'], state['roster'])

    def test_enter_training_treats_a_none_fighter_as_unchecked(self):
        remembered = self.ctx.runner.routes_expect
        original = dict(remembered)
        state = {'roster': {0: {'kind': 0}, 1: {'kind': 3}}, 'stage': 3}
        fighters = {'p1': 'Mario', 'cpu': None}
        with patch.object(routes, '_native_enter_training', return_value={'stage_id': 3}), \
             patch.object(routes, 'wait_native', return_value=state), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.enter_training(self.ctx, cap=1, fighters=fighters)
        self.assertEqual(result['fighters_checked'], {'p1': 'checked', 'cpu': 'unchecked'})
        self.assertEqual(result['args']['fighters'], fighters)
        self.assertIs(self.ctx.runner.routes_expect, remembered)
        self.assertEqual(self.ctx.runner.routes_expect, original)

    def test_enter_training_treats_a_none_p1_as_unchecked(self):
        state = {'roster': {0: {'kind': 3}, 1: {'kind': 7}}, 'stage': 3}
        with patch.object(routes, '_native_enter_training', return_value={'stage_id': 3}), \
             patch.object(routes, 'wait_native', return_value=state), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.enter_training(self.ctx, cap=1, fighters={'p1': None, 'cpu': 'Fox'})
        self.assertEqual(result['fighters_checked'], {'p1': 'unchecked', 'cpu': 'checked'})
        self.assertEqual(self.ctx.runner.routes_expect, {'p1_kind': 0, 'cpu_kind': 7})

    def test_enter_training_still_requires_both_roster_entries_when_a_seat_is_unchecked(self):
        state = {'roster': {0: {'kind': 0}}, 'stage': 3}
        with patch.object(routes, '_native_enter_training', return_value={'stage_id': 3}), \
             patch.object(routes, 'wait_native', return_value=state), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            with self.assertRaisesRegex(RuntimeError, 'CPU kind None'):
                routes.enter_training(self.ctx, cap=1, fighters={'p1': 'Mario', 'cpu': None})

    def test_enter_training_set_stage_ids_return_plain_json(self):
        state = {'roster': {0: {'kind': 0}, 1: {'kind': 7}}, 'stage': 3}
        with patch.object(routes, '_native_enter_training', return_value={'stage_id': 3}), \
             patch.object(routes, 'wait_native', return_value=state), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.enter_training(self.ctx, cap=1, stage_ids={3})
        try:
            json.dumps(result)
        except TypeError as exc:
            self.fail(str(exc))
        self.assertEqual(result['args']['stage_ids'], [3])

    def test_tr_grid_path_matches_trainingrooms_step_rule(self):
        vectors = ((0, 'left', 12), (12, 'right', 0), (0, 'up', 78), (12, 'up', 86),
                   (78, 'down', 2), (86, 'down', 10), (65, 'down', 78),
                   (78, 'left', 86), (86, 'right', 78), (13, 'up', 0))
        for start, direction, goal in vectors:
            with self.subTest(start=start, direction=direction):
                self.assertEqual(routes.tr_grid_step(start, direction), goal)
        for start, goal, distance in ((0, 12, 1), (0, 78, 1), (0, 86, 2), (78, 0, 3)):
            path = routes.tr_grid_path(start, goal)
            self.assertEqual(len(path), distance)
            cell = start
            for direction in path:
                cell = routes.tr_grid_step(cell, direction)
            self.assertEqual(cell, goal)

    def set_cpu(self, replay, roster, settings, body=None):
        replay.writer_log = self.log
        tables = routes.tables()
        tables['roster_catalog'] = [{'kind': 0, 'name': 'Mario'}, {'kind': 7, 'name': 'Fox'}]
        body = body if body is not None else {'kind': 0}
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'tables', return_value=tables), \
             patch.object(routes, '_fetch_settings', return_value=json.dumps(settings).encode(), create=True), \
             patch.object(routes, 'wait_native', return_value={'roster': roster, 'stage': 3}), \
             patch.object(routes, '_live_body', side_effect=body if isinstance(body, Exception) else None,
                          return_value=body), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.set_cpu(self.ctx, 'Mario')
        json.dumps(result)
        return result

    def test_set_cpu_stops_before_a_on_an_unexpected_cell(self):
        replay = MenuReplay(wrong_cell=True)
        with self.assertRaisesRegex(RuntimeError, '(?i)(cell|editor.sel)'):
            self.set_cpu(replay, {0: {'kind': 0}, 1: {'kind': 0}},
                         {'menu': {'cpu_character': [1, 0]}})
        self.assertEqual(replay.actions, ['tap-a1', 'main-menu-left'])

    def test_set_cpu_backs_out_of_an_open_grid_first(self):
        replay = MenuReplay(depth=1)
        result = self.set_cpu(replay, {0: {'kind': 0}, 1: {'kind': 0}},
                              {'menu': {'cpu_character': [1, 0]}})
        self.assertEqual(replay.actions, ['tap-b1', 'tap-a1', 'tap-a1', 'tap-b1', 'tap-b1'])
        self.assertTrue(result['grid_named'])

    def test_tr_settings_refuses_a_refused_set(self):
        initial = b'{"menu":{"stage_hazards":[1,0]}}'
        changed = {'stage_hazards': [0, 1]}
        with patch.object(routes, '_fetch_settings', return_value=initial), \
             patch.object(routes, '_apply_settings', side_effect=RuntimeError('[Bridge] req set result=refused reason=busy')), \
             patch.object(routes, 'BridgePickerIO', return_value=MenuReplay()), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            with self.assertRaisesRegex(RuntimeError, 'refused'):
                routes.tr_settings(self.ctx, values=changed)

    def test_reach_training_skips_what_already_holds_and_writes_one_receipt(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        state = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}
        saved = []
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', return_value=state), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: saved.append(name) or value):
            receipt = routes.reach_training(self.ctx, 'Mario', cpu='Fox', stage='Final Destination')
        json.dumps(receipt)
        self.assertEqual(saved, ['routes-reach-training'])
        self.assertEqual(replay.actions, [])
        self.assertEqual(set(receipt['skipped']), {'to_main', 'back_to_main', 'main_to_stage', 'pick_stage',
                                                   'quit_training', 'pick_fighters', 'enter_training', 'set_cpu'})

    def test_set_cpu_needs_the_live_roster_not_only_the_file(self):
        replay = MenuReplay()
        with self.assertRaisesRegex(RuntimeError, '(?i)roster'):
            self.set_cpu(replay, {0: {'kind': 0}, 1: {'kind': 7}},
                         {'menu': {'cpu_character': [1, 0]}})
        self.assertEqual(replay.actions, ['tap-a1', 'tap-a1', 'tap-b1', 'tap-b1'])

    def test_fresh_roster_requires_both_new_entries(self):
        import switch_hw

        self.log.write_text('[Bridge] roster entry=1 kind=7 nana=false frame=1 wall=1\n')
        lines = iter(('[Bridge] req roster result=ok entries=0x3 frame=2 wall=2\n'
                      '[Bridge] roster entry=0 kind=0 nana=false frame=2 wall=2\n',
                      '[Bridge] roster entry=1 kind=3 nana=false frame=2 wall=2\n'))
        writes = []

        def respond(_delay):
            line = next(lines)
            writes.append(line)
            with self.log.open('a') as stream:
                stream.write(line)

        with patch.object(switch_hw.time, 'sleep', side_effect=respond):
            state = switch_hw.bridge_state(self.dir.name, fresh=('roster',), cap=1, roster_entries=(0, 1))
        self.assertEqual(len(writes), 2)
        self.assertEqual(state['roster'][1]['kind'], 3)

    def test_settings_set_requires_fresh_success_and_reload(self):
        sent = []
        mode = ['ok']

        def respond():
            request = self.bridge / 'diag-settings-next.json'
            if request.exists():
                sent.append(request.read_bytes())
                request.unlink()
                with self.log.open('a') as stream:
                    stream.write(f'[Bridge] req set result={mode[0]} frame=2 wall=2\n')
                    if mode[0] == 'ok':
                        stream.write('[TrainingRooms] diag_settings_reload rerolls=0x0 issues=0 ev=2 frame=2 wall=2\n')

        self.ctx.check_stop = respond
        with patch.object(routes, '_fetch_settings', return_value=b'{"menu":{}}'):
            line, reload = routes._apply_settings(self.ctx, b'{"menu":{}}', cap=1)
            self.assertIn('result=ok', line)
            self.assertIn('issues=0', reload)
            mode[0] = 'refused reason=busy'
            with self.assertRaisesRegex(RuntimeError, 'refused reason=busy'):
                routes._apply_settings(self.ctx, b'{}', cap=1)
        self.assertEqual(sent, [b'{"menu":{}}', b'{}'])

    def test_set_cpu_rejects_bad_ack_or_grid_before_a_pick(self):
        for replay in (MenuReplay(wrong_ack=True), MenuReplay(wrong_grid=True), MenuReplay(wrong_keyboard=True)):
            with self.subTest(wrong_ack=replay.wrong_ack, wrong_grid=replay.wrong_grid,
                              wrong_keyboard=replay.wrong_keyboard), \
                 self.assertRaisesRegex(RuntimeError, '(ACK|grid|keyboard)'):
                self.set_cpu(replay, {0: {'kind': 0}, 1: {'kind': 0}},
                             {'menu': {'cpu_character': [1, 0]}})
            self.assertEqual(replay.actions, ['tap-a1'])

    def test_set_cpu_accepts_the_older_unnamed_grid_by_its_checked_row(self):
        replay = MenuReplay()
        del replay.menu['grid']
        result = self.set_cpu(replay, {0: {'kind': 0}, 1: {'kind': 0}},
                              {'menu': {'cpu_character': [1, 0]}})
        self.assertFalse(result['grid_named'])
        self.assertEqual(replay.actions, ['tap-a1', 'tap-a1', 'tap-b1', 'tap-b1'])

    def test_set_cpu_opens_a_closed_menu_without_a_menu_press_ack(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        result = self.set_cpu(replay, {0: {'kind': 0}, 1: {'kind': 0}},
                              {'menu': {'cpu_character': [1, 0]}})
        self.assertEqual(replay.actions, ['tr-menu-open', 'tap-a1', 'tap-a1', 'tap-b1', 'tap-b1'])
        self.assertEqual(replay.menu['ack'], '4')
        self.assertTrue(result['grid_named'])

    def test_shoulder_macros_are_plain_presses(self):
        for name, button in (('tap-l1', 64), ('tap-r1', 128)):
            with self.subTest(name=name):
                path, sha, _ = routes.macro_file(name)
                data = json.loads(path.read_text())
                self.assertEqual([(e['frame'], e['buttons']) for e in data['events']], [(0, 0), (1, button), (5, 0)])
                self.assertEqual(data['duration_frames'], 8)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha)

    def test_reach_training_uses_the_suffix_for_a_live_matching_stage(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        initial = {'stage': 3, 'roster': {0: {'kind': 7}, 1: {'kind': 7}}}
        final = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}
        calls = []
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', side_effect=[initial, final]), \
             patch.object(routes, 'quit_training', side_effect=lambda *a, **k:
                          calls.append('quit') or replay.set_screen('css.training')), \
             patch.object(routes, 'pick_fighters', side_effect=lambda *a, **k: calls.append(('pick', k))), \
             patch.object(routes, 'enter_training', side_effect=lambda *a, **k:
                          calls.append(('enter', k)) or replay.set_screen('training')), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.reach_training(self.ctx, 'Mario', cpu='Fox', stage='Final Destination')
        json.dumps(result)
        self.assertEqual([item if isinstance(item, str) else item[0] for item in calls], ['quit', 'pick', 'enter'])
        self.assertIsNone(calls[1][1].get('cpu'))
        self.assertEqual(calls[-1][1]['fighters'], {'p1': 'Mario', 'cpu': None})
        self.assertEqual(calls[-1][1]['stage_ids'], {3})
        self.assertTrue({'to_main', 'main_to_stage', 'pick_stage'} <= set(result['skipped']))
        self.assertNotIn('quit_training', result['skipped'])

    def test_reach_training_css_suffix_return_is_plain_json(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        initial = {'stage': 3, 'roster': {0: {'kind': 7}, 1: {'kind': 7}}}
        final = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', side_effect=[initial, final]), \
             patch.object(routes, 'quit_training', side_effect=lambda *a, **k: replay.set_screen('css.training')), \
             patch.object(routes, 'pick_fighters', return_value={}), \
             patch.object(routes, '_native_enter_training', side_effect=lambda *a, **k:
                          replay.set_screen('training', {'stage_id': 3})), \
             patch.object(routes, 'wait_native', return_value=final), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.reach_training(self.ctx, 'Mario', cpu='Fox', stage='Final Destination')
        json.dumps(result)
        self.assertEqual(result['phases']['enter_training']['args']['stage_ids'], [3])
        self.assertEqual(result['phases']['enter_training']['args']['fighters'], {'p1': 'Mario', 'cpu': None})

    def test_reach_training_rechecks_native_ownership_at_the_end(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'

        def lose_owner(*args, **kwargs):
            replay.menu['open'] = '1'
            return {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}

        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', side_effect=lose_owner), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            with self.assertRaisesRegex(RuntimeError, 'not owned by native'):
                routes.reach_training(self.ctx, 'Mario', cpu='Fox')
        self.assertEqual(replay.actions, [])

    def test_reach_training_css_suffix_return_is_plain_json(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        initial = {'stage': 3, 'roster': {0: {'kind': 7}, 1: {'kind': 7}}}
        final = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', side_effect=[initial, final]), \
             patch.object(routes, 'quit_training', side_effect=lambda *a, **k: replay.set_screen('css.training')), \
             patch.object(routes, 'pick_fighters', return_value={}), \
             patch.object(routes, '_native_enter_training', side_effect=lambda *a, **k:
                          replay.set_screen('training', {'stage_id': 3})), \
             patch.object(routes, 'wait_native', return_value=final), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.reach_training(self.ctx, 'Mario', cpu='Fox', stage='Final Destination')
        try:
            json.dumps(result)
        except TypeError as exc:
            self.fail(str(exc))
        self.assertEqual(result['phases']['enter_training']['args']['stage_ids'], [3])
        self.assertEqual(result['phases']['enter_training']['args']['fighters'], {'p1': 'Mario', 'cpu': None})

    def test_set_cpu_at_the_fighter_select_refuses_and_never_uses_the_tile_cursor(self):
        replay = MenuReplay()
        replay.read = lambda provider, cap: recorded.l8w_native(2)
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'pick_cpu', return_value={}) as pick, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            with self.assertRaisesRegex(RuntimeError, 'set_cpu works in Training'):
                routes.set_cpu(self.ctx, 'Mario')
        pick.assert_not_called()
        self.assertEqual(replay.actions, [])

    def test_set_player_at_the_fighter_select_still_picks_p1(self):
        replay = MenuReplay()
        replay.read = lambda provider, cap: recorded.l8w_native(2)
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'pick_local_fighter', return_value={}) as pick, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            routes.set_player(self.ctx, 'Mario')
        pick.assert_called_once()

    def test_reach_training_sets_a_wrong_cpu_through_the_tr_menu_without_leaving_training(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        replay.writer_log = self.log
        initial = {'stage': 3, 'roster': {0: {'kind': 7}, 1: {'kind': 7}}}
        final = {'stage': 3, 'roster': {0: {'kind': 7}, 1: {'kind': 0}}}
        tables = routes.tables()
        tables['roster_catalog'] = [{'kind': 0, 'name': 'Mario'}, {'kind': 7, 'name': 'Fox'}]
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'tables', return_value=tables), \
             patch.object(routes, 'native_state', side_effect=[initial, final, final]), \
             patch.object(routes, 'wait_native', return_value=final), \
             patch.object(routes, '_fetch_settings', return_value=b'{"menu":{"cpu_character":[1,0]}}'), \
             patch.object(routes, '_live_body', return_value={'kind': 0}), \
             patch.object(routes, 'quit_training', return_value={}) as quit_training, \
             patch.object(routes, 'pick_fighters', return_value={}) as pick, \
             patch.object(routes, 'enter_training', return_value={}) as enter, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.reach_training(self.ctx, 'Fox', cpu='Mario')
        self.assertEqual(replay.actions, ['tr-menu-open', 'tap-a1', 'tap-a1', 'tap-b1', 'tap-b1'])
        quit_training.assert_not_called()
        pick.assert_not_called()
        enter.assert_not_called()
        self.assertIn('set_cpu', result['phases'])
        self.assertTrue({'quit_training', 'pick_fighters', 'enter_training'} <= set(result['skipped']))
        self.assertEqual(result['verified']['roster'], final['roster'])

    def test_reach_training_repicks_p1_at_the_fighter_select_and_sets_the_cpu_in_training(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        replay.writer_log = self.log
        initial = {'stage': 3, 'roster': {0: {'kind': 7}, 1: {'kind': 7}}}
        entered = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}
        final = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 0}}}
        tables = routes.tables()
        tables['roster_catalog'] = [{'kind': 0, 'name': 'Mario'}, {'kind': 7, 'name': 'Fox'}]
        calls = []
        remembered = self.ctx.runner.routes_expect
        expected_before_entry = dict(remembered)

        def pick(*args, **kwargs):
            calls.append('pick_fighters')
            self.assertIsNone(kwargs.get('cpu'))

        def enter(*args, **kwargs):
            calls.append('enter_training')
            replay.set_screen('training')
            self.assertIs(self.ctx.runner.routes_expect, remembered)
            self.assertEqual(self.ctx.runner.routes_expect, expected_before_entry)
            self.assertEqual(kwargs['fighters'], {'p1': 'Mario', 'cpu': None})

        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'tables', return_value=tables), \
             patch.object(routes, 'native_state', side_effect=[initial, entered, final]), \
             patch.object(routes, 'wait_native', return_value=final), \
             patch.object(routes, '_fetch_settings', return_value=b'{"menu":{"cpu_character":[1,0]}}'), \
             patch.object(routes, '_live_body', return_value={'kind': 0}), \
             patch.object(routes, 'quit_training', side_effect=lambda *a, **k:
                          calls.append('quit_training') or replay.set_screen('css.training')), \
             patch.object(routes, 'pick_fighters', side_effect=pick), \
             patch.object(routes, 'enter_training', side_effect=enter), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.reach_training(self.ctx, 'Mario', cpu='Mario')
        self.assertEqual(calls, ['quit_training', 'pick_fighters', 'enter_training'])
        self.assertEqual(replay.actions, ['tr-menu-open', 'tap-a1', 'tap-a1', 'tap-b1', 'tap-b1'])
        self.assertIn('set_cpu', result['phases'])

    def test_reach_training_leaves_a_matching_cpu_alone(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        initial = {'stage': 3, 'roster': {0: {'kind': 7}, 1: {'kind': 0}}}
        final = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 0}}}
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', side_effect=[initial, final]), \
             patch.object(routes, 'quit_training', side_effect=lambda *a, **k: replay.set_screen('css.training')), \
             patch.object(routes, 'pick_fighters', return_value={}), \
             patch.object(routes, 'enter_training', side_effect=lambda *a, **k: replay.set_screen('training')), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.reach_training(self.ctx, 'Mario', cpu='Mario')
        self.assertIn('set_cpu', result['skipped'])
        self.assertEqual(replay.actions, [])

    def test_set_cpu_confirms_the_live_body_after_the_roster(self):
        roster = {0: {'kind': 0}, 1: {'kind': 0}}
        settings = {'menu': {'cpu_character': [1, 0]}}
        with self.assertRaisesRegex(RuntimeError, 'live fighter'):
            self.set_cpu(MenuReplay(), roster, settings, RuntimeError('live fighter missing'))
        result = self.set_cpu(MenuReplay(), roster, settings, {'kind': 0})
        self.assertEqual(result['verified']['fighter']['kind'], 0)

    def test_live_body_counts_requests_and_waits_for_the_requested_kind(self):
        self.ctx.runner.setup_metrics = {'bridge_requests': 0}
        replies = iter(({1: {'kind': 7}}, {1: {'kind': 0, 'x': 1.0}}))
        calls = []

        def fighters_now(run, cap):
            calls.append((run, cap))
            return next(replies)

        with patch.object(routes, '_switch_hw', return_value=SimpleNamespace(fighters_now=fighters_now)), \
             patch.object(routes.time, 'sleep'):
            body = routes._live_body(self.ctx, 1, 0, 1)
        self.assertEqual(body, {'kind': 0, 'x': 1.0})
        self.assertEqual(self.ctx.runner.setup_metrics['bridge_requests'], 2)
        self.assertEqual([call[0] for call in calls], [self.log.parent, self.log.parent])

    def test_reach_training_does_not_prejudge_the_remembered_cpu_at_entry(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        self.ctx.runner.routes_expect['cpu_kind'] = 0
        initial = {'stage': 3, 'roster': {0: {'kind': 7}, 1: {'kind': 7}}}
        entered = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}
        final = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 0}}}
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', side_effect=[initial, entered, final]), \
             patch.object(routes, 'quit_training', side_effect=lambda *a, **k: replay.set_screen('css.training')), \
             patch.object(routes, 'pick_fighters', return_value={}), \
             patch.object(routes, '_native_enter_training', side_effect=lambda *a, **k:
                          replay.set_screen('training', {'stage_id': 3})), \
             patch.object(routes, 'wait_native', return_value=entered), \
             patch.object(routes, '_set_fighter', return_value={}) as set_fighter, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.reach_training(self.ctx, 'Mario', cpu='Mario')
        self.assertEqual(result['phases']['enter_training']['fighters_checked']['cpu'], 'unchecked')
        set_fighter.assert_called_once()
        self.assertEqual(self.ctx.runner.routes_expect['cpu_kind'], 0)

    def test_reach_training_keeps_expectations_remembered_during_entry(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        initial = {'stage': 3, 'roster': {0: {'kind': 7}, 1: {'kind': 7}}}
        final = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}

        def enter(*args, **kwargs):
            routes.remember(self.ctx, entry_marker='kept')
            replay.set_screen('training')
            return {}

        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', side_effect=[initial, final]), \
             patch.object(routes, 'quit_training', side_effect=lambda *a, **k: replay.set_screen('css.training')), \
             patch.object(routes, 'pick_fighters', return_value={}), \
             patch.object(routes, 'enter_training', side_effect=enter), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            routes.reach_training(self.ctx, 'Mario', cpu='Fox')
        self.assertEqual(self.ctx.runner.routes_expect.get('entry_marker'), 'kept')

    def test_setup_state_reads_without_any_controller_input(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        state = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', return_value=state), \
             patch.object(routes, '_fetch_settings', return_value=b'{"menu":{}}'), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.setup_state(self.ctx)
        json.dumps(result)
        self.assertEqual(replay.actions, [])
        self.assertEqual(result['verified']['settings_sha256'], hashlib.sha256(b'{"menu":{}}').hexdigest())
        self.assertEqual((result['verified']['p1']['kind'], result['verified']['cpu']['kind']), (0, 7))
        self.assertEqual((result['verified']['p1']['name'], result['verified']['cpu']['name']), ('Mario', 'Fox'))

    def test_tr_settings_preserves_other_keys_and_reports_the_sent_values(self):
        original = b'{"menu":{"stage_hazards":[1,0],"other":[2,3]},"version":1}'
        bodies = []

        def apply(_ctx, data, cap):
            bodies.append(json.loads(data))
            return '[Bridge] req set result=ok', '[TrainingRooms] diag_settings_reload rerolls=0x0 issues=0'

        with patch.object(routes, 'BridgePickerIO', return_value=MenuReplay()), \
             patch.object(routes, '_fetch_settings', return_value=original), \
             patch.object(routes, '_apply_settings', side_effect=apply), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.tr_settings(self.ctx, values={'stage_hazards': [0, 1]})
        json.dumps(result)
        self.assertEqual(bodies[0], {'menu': {'stage_hazards': [0, 1], 'other': [2, 3]}, 'version': 1})
        self.assertEqual(result['verified']['keys'], ['stage_hazards'])
        self.assertEqual(result['verified']['values'], {'stage_hazards': [0, 1]})

    def test_fresh_settings_file_must_match_its_bridge_receipt(self):
        files = self.bridge / 'files'
        files.mkdir()
        data = b'{"menu":{}}'
        digest = hashlib.sha256(data).hexdigest()
        corrupt = [False]

        def respond():
            request = self.bridge / 'bridge-file.req'
            if request.exists():
                self.assertEqual(request.read_bytes(), b'settings.json\n')
                request.unlink()
                (files / 'settings.json').write_bytes(b'bad' if corrupt[0] else data)
                with self.log.open('a') as stream:
                    stream.write(f'[Bridge] file_end name=settings.json bytes={len(data)} sha256={digest} '
                                 'result=ok frame=1 wall=1 mirrored=true\n')

        self.ctx.check_stop = respond
        self.assertEqual(routes._fetch_settings(self.ctx, cap=1), data)
        corrupt[0] = True
        with self.assertRaisesRegex(RuntimeError, 'does not match the bridge receipt'):
            routes._fetch_settings(self.ctx, cap=1)


class TakeRouteReplay(CoinReplay):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.values.update({'take.ack': '0', 'take.applied': '0'})

    def send(self, segment):
        if segment == 'tile-take':
            self.actions.append(segment)
            self.values['take.ack'] = str(int(self.values['take.ack']) + 1)
            if self.values.get('p1.carried', 'unknown') in ('none', 'unknown'):
                self.values['take.applied'] = str(int(self.values['take.applied']) + 1)
                self._take('1')
            return
        if segment == 'tap-b1':
            self.actions.append(segment)
            self._take('0')
            return
        return super().send(segment)


class TileTakeRouteTests(unittest.TestCase):
    def setUp(self):
        self.io = TakeRouteReplay()
        self.ctx = SimpleNamespace(runner=SimpleNamespace(routes_expect={}), on='s',
                                   instance={'bridge_log': '/fake/bridge.log'}, row_dir='.',
                                   check_stop=lambda: None)

    def test_tile_take_macro_is_l3_then_l3_b(self):
        entry = routes.routes_index()['macros'].get('tile-take')
        self.assertIsNotNone(entry)
        path = routes.ROUTES / entry['file']
        macro = json.loads(path.read_text())
        self.assertEqual(entry['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual((entry['frames'], macro['duration_frames']), (8, 8))
        self.assertEqual([(e['frame'], e['buttons']) for e in macro['events']], [(0, 16), (1, 18), (5, 0)])

    def test_tile_take_hold_macro_holds_b_under_l3_past_the_back_hold(self):
        entry = routes.routes_index()['macros'].get('tile-take-hold')
        self.assertIsNotNone(entry)
        path = routes.ROUTES / entry['file']
        macro = json.loads(path.read_text())
        self.assertEqual(entry['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual((entry['frames'], macro['duration_frames']), (124, 124))
        events = [(event['frame'], event['buttons']) for event in macro['events']]
        self.assertEqual(events, [(0, 16), (1, 18), (121, 0)])
        self.assertGreaterEqual(events[2][0] - events[1][0], 100)
        self.assertEqual(events[2][1] & 18, 0)

    def test_place_cpu_takes_walks_at_least_one_step_and_places(self):
        with patch.object(routes, 'BridgePickerIO', return_value=self.io), \
             patch.object(routes, '_tile_topology', return_value=TOPOLOGY), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.place_cpu(self.ctx, 'Fox', must_move=True)
            self.assertEqual(self.io.actions, ['tile-take', 'tile-left', 'tap-a1'])
            self.assertEqual(result['verified']['cpu']['name'], 'Fox')
            self.assertGreaterEqual(result['verified']['steps'], 1)
            with self.assertRaisesRegex(RuntimeError, "already on Fox: this row cannot show a move"):
                routes.place_cpu(self.ctx, 'Fox', must_move=True)

    def test_place_cpu_uses_an_already_held_coin_without_taking_again(self):
        self.io._take('1')
        with patch.object(routes, 'BridgePickerIO', return_value=self.io), \
             patch.object(routes, '_tile_topology', return_value=TOPOLOGY), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.place_cpu(self.ctx, 'Fox', must_move=True)
        self.assertEqual(self.io.actions, ['tile-left', 'tap-a1'])
        self.assertFalse(result['taken_here'])
        self.assertEqual(result['verified']['p1_medal'], ['placed', '0', '0', 'Mario', 'default'])

    def test_take_cpu_route_reports_the_counter_fence(self):
        with patch.object(routes, 'BridgePickerIO', return_value=self.io), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.take_cpu(self.ctx)
        self.assertEqual(self.io.actions, ['tile-take'])
        self.assertEqual(result['verified']['take.ack'], [0, 1])
        self.assertEqual(result['verified']['take.applied'], [0, 1])
        self.assertEqual(result['verified']['p1.carried'], '1')

    def test_take_noop_route_reaches_its_own_start(self):
        def noop(io, holding, **kwargs):
            io.send('tile-take')
            return {'unchanged': {}, 'take.ack': [1, 2], 'take.applied': [1, 1], 'blocks': 5}

        with patch.object(routes, 'BridgePickerIO', return_value=self.io), \
             patch.object(picker, 'take_noop', side_effect=noop), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.take_noop(self.ctx, 'cpu')
            self.assertEqual(self.io.actions, ['tile-take', 'tile-take'])
            self.assertEqual(result['verified']['reached_by'], 'take_cpu')
        p1 = TakeRouteReplay()
        with patch.object(routes, 'BridgePickerIO', return_value=p1), \
             patch.object(picker, 'take_noop', side_effect=noop), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.take_noop(self.ctx, 'p1')
            self.assertEqual(p1.actions, ['tap-b1', 'tile-take'])
            self.assertEqual(result['verified']['reached_by'], 'b')
        held = TakeRouteReplay()
        held._take('1')
        with patch.object(routes, 'BridgePickerIO', return_value=held), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            with self.assertRaisesRegex(RuntimeError, 'P1'):
                routes.take_noop(self.ctx, 'p1')
        self.assertEqual(held.actions, [])

    def test_take_noop_route_passes_hold_through(self):
        self.io._take('0')
        with patch.object(routes, 'BridgePickerIO', return_value=self.io), \
             patch.object(picker, 'take_noop', side_effect=lambda *a, **k: {}) as noop, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            held = routes.take_noop(self.ctx, 'p1', hold=True)
            noop.assert_called_once_with(self.io, '0', hold=True)
            self.assertEqual(held['args'], {'holding': 'p1', 'hold': True})
            noop.reset_mock()
            normal = routes.take_noop(self.ctx, 'p1')
            noop.assert_called_once_with(self.io, '0', hold=False)
            self.assertEqual(normal['args'], {'holding': 'p1'})

    def test_expect_fighters_needs_roster_and_body_to_agree(self):
        io = recorded.Replay(native_blocks=[recorded.l8w_native(1)])
        ui = recorded.native(1, screen='training', **{'screen.context': 'training'})
        state = {'roster': {0: {'kind': 0}, 1: {'kind': 7}}}
        bodies = {0: {'kind': 0}, 1: {'kind': 3}}
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
             patch.object(picker, 'wait_sample', return_value=ui), \
             patch.object(routes, 'native_state', return_value=state), \
             patch.object(routes, '_switch_hw', return_value=SimpleNamespace(fighters_now=lambda *a, **k: bodies)), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            with self.assertRaisesRegex(RuntimeError, 'entry 1.*Fox.*7.*3'):
                routes.expect_fighters(self.ctx, p1='Mario', cpu='Fox')
            bodies[1]['kind'] = 7
            state['roster'][1]['kind'] = 3
            with self.assertRaisesRegex(RuntimeError, 'entry 1.*Fox.*7.*3'):
                routes.expect_fighters(self.ctx, p1='Mario', cpu='Fox')
            state['roster'][1]['kind'] = 7
            result = routes.expect_fighters(self.ctx, p1='Mario', cpu='Fox')
        self.assertEqual(result['verified']['bodies'][1], 7)
        self.assertEqual(io.actions, [])


class ReachTrainingPhaseReplay:
    def __init__(self, ctx):
        self.ctx = ctx
        self.pending = ctx.runner.native_ui_pending
        self.in_flight = False
        self.screen = 'stage_select'
        self.revision = 0
        self.now = 0.0
        self.events = []
        self.calls = []
        self.failures = {}
        self.failure_message = 'phase transition deadline'
        self.failure_exception = None
        self.after_failure = None
        self.read_fault = False
        self.good_reads_before_fault = 0
        self.read_script = []
        self.pending_read_duration = 0.0
        self.phase_duration = 0.0
        self.input_owner = 'native'

    def read(self, provider, cap):
        self.events.append(('read', self.screen, self.ctx.on in self.pending))
        if self.ctx.on in self.pending:
            self.now += self.pending_read_duration
        if self.read_fault:
            if self.good_reads_before_fault:
                self.good_reads_before_fault -= 1
            else:
                raise RuntimeError('no fenced bridge reply')
        self.revision += 1
        if self.read_script:
            screen = self.read_script.pop(0)
            if screen == 'unknown':
                return recorded.loading(self.revision)
        else:
            screen = self.screen
        if screen == 'main_menu':
            fields = {'screen': 'main_menu', 'screen.context': 'unknown', 'screen.layer': 'submenu',
                      'screen.tab': 'games_and_more', 'focus.source': 'native', 'focus.id': 'classic_mode',
                      'focus': 'main_menu.submenu.classic_mode'}
        else:
            fields = {'screen': screen, 'screen.context': 'training',
                      'focus': screen,
                      'focus.source': 'tile.cursor' if screen in ('stage_select', 'css.training') else 'native'}
        sample = recorded.native(self.revision, **{**fields, 'input.owner': self.input_owner})
        sample.block.update({'frame': self.revision * 10, 'wall': self.revision * 100})
        return sample

    def finish(self):
        self.events.append(('finish', self.screen, self.ctx.on in self.pending))
        self.pending.pop(self.ctx.on, None)
        self.in_flight = False

    def phase(self, name):
        self.calls.append(name)
        self.events.append(('phase', name, self.ctx.on in self.pending))
        self.now += self.phase_duration
        if self.failures.get(name, 0):
            self.failures[name] -= 1
            self.pending[self.ctx.on] = ('macro-' + name, str(self.ctx.row_dir))
            if self.after_failure:
                self.after_failure(self, name)
            if self.failure_exception:
                raise self.failure_exception
            raise RuntimeError(self.failure_message)
        if name in ('to_main', 'back_to_main'):
            self.screen = 'main_menu'
        elif name == 'main_to_stage':
            self.screen = 'stage_select'
        elif name in ('pick_stage', 'quit_training'):
            self.screen = 'css.training'
        elif name == 'enter_training':
            self.screen = 'training'
        return {'phase': name}


class ReachTrainingClosedLoopTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.row = Path(temp.name)
        runner = SimpleNamespace(routes_expect={'stage_id': 3}, native_ui_pending={})
        self.ctx = SimpleNamespace(runner=runner, on='s', row_dir=self.row, check_stop=lambda: None)
        self.io = ReachTrainingPhaseReplay(self.ctx)
        self.state = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}

    def invoke(self, *, set_fighter=None, picker_factory=None, **kwargs):
        with ExitStack() as stack:
            stack.enter_context(patch.object(routes, 'BridgePickerIO', **(
                {'side_effect': picker_factory} if picker_factory else {'return_value': self.io})))
            stack.enter_context(patch.object(routes.time, 'monotonic', side_effect=lambda: self.io.now))
            stack.enter_context(patch.object(routes.time, 'sleep', side_effect=self.sleep))
            stack.enter_context(patch.object(routes, 'native_state', return_value=self.state))
            stack.enter_context(patch.object(routes, 'library_topology', return_value={'path': 'modelled'}))
            for name in ('to_main', 'back_to_main', 'main_to_stage', 'pick_stage', 'quit_training',
                         'pick_fighters', 'enter_training'):
                stack.enter_context(patch.object(routes, name, side_effect=lambda *a, _name=name, **k:
                                                 self.io.phase(_name)))
            stack.enter_context(patch.object(routes, '_set_fighter', side_effect=set_fighter or
                                             (lambda *a, **k: self.io.phase('set_cpu'))))
            return routes.reach_training(self.ctx, 'Mario', cpu='Fox', **kwargs)

    def sleep(self, seconds):
        self.io.now += seconds

    def stop_receipt(self):
        return json.loads((self.row / 'routes-reach-training-stop.json').read_text())

    def test_a_phase_that_fails_once_on_the_same_screen_runs_again(self):
        self.io.failures['pick_stage'] = 1
        result = self.invoke()
        self.assertEqual(self.io.calls.count('pick_stage'), 2)
        self.assertEqual(result['starts']['pick_stage'], 2)
        self.assertEqual(result['retries'], {'pick_stage': 1})
        self.assertEqual(result['recovered'], [])

    def test_a_phase_whose_effect_already_applied_is_not_dispatched_again(self):
        self.io.screen = 'training'
        self.state['roster'][1]['kind'] = 3
        self.io.failures['set_cpu'] = 1
        self.io.after_failure = lambda io, name: self.state['roster'][1].update(kind=7)
        result = self.invoke()
        self.assertEqual(self.io.calls, ['set_cpu'])
        self.assertEqual(result['recovered'], ['set_cpu'])
        self.assertEqual(result['result'], 'PASS with 1 stall')
        self.assertEqual(result['stalls'][0]['kind'], 'phase-recovered')

    def test_a_same_screen_failure_with_the_postcondition_unmet_is_retried(self):
        self.io.screen = 'training'
        self.state['roster'][1]['kind'] = 3
        self.io.failures['set_cpu'] = 1
        original = self.io.phase

        def phase(name):
            result = original(name)
            if name == 'set_cpu':
                self.state['roster'][1]['kind'] = 7
            return result

        self.io.phase = phase
        result = self.invoke()
        self.assertEqual(self.io.calls, ['set_cpu', 'set_cpu'])
        self.assertEqual(result['retries'], {'set_cpu': 1})
        self.assertEqual(result['stalls'][0]['kind'], 'phase-retry')

    def test_a_same_screen_failure_is_not_recovered_during_an_unrelated_bounce(self):
        self.io.screen = 'training'
        self.state['roster'][1]['kind'] = 3
        self.io.failures['set_cpu'] = 1
        self.io.after_failure = lambda io, name: self.state['roster'][0].update(kind=3)
        original = self.io.phase

        def phase(name):
            result = original(name)
            if name == 'enter_training':
                self.state['roster'][0]['kind'] = 0
            elif name == 'set_cpu':
                self.state['roster'][1]['kind'] = 7
            return result

        self.io.phase = phase
        result = self.invoke()
        self.assertEqual(self.io.calls, ['set_cpu', 'quit_training', 'pick_fighters',
                                         'enter_training', 'set_cpu'])
        self.assertEqual(result['recovered'], [])
        self.assertEqual(result['retries'], {'set_cpu': 1})
        self.assertEqual([event['kind'] for event in result['stalls']], ['phase-retry'])

    def test_an_open_menu_after_failed_set_cpu_is_not_closed_or_retried(self):
        self.io.screen = 'training'
        self.state['roster'][1]['kind'] = 3
        self.io.failures['set_cpu'] = 1

        def applied_but_open(io, name):
            self.state['roster'][1]['kind'] = 7
            io.input_owner = 'tr.menu'

        self.io.after_failure = applied_but_open
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.invoke()
        self.assertEqual(caught.exception.step, 'set_cpu')
        self.assertIn('no two consecutive qualified screens', caught.exception.reason)
        self.assertEqual(self.io.calls, ['set_cpu'])
        self.assertEqual(self.stop_receipt()['last_ui']['input.owner'], 'tr.menu')
        self.assertEqual(self.stop_receipt()['pending'], 'macro-set_cpu')
        self.assertFalse(any(event[0] == 'finish' for event in self.io.events))

    def test_an_inner_training_resend_is_a_stall_and_not_a_plain_pass(self):
        self.io.screen = 'training'
        self.state['roster'][1]['kind'] = 3
        menu = retrying_menu(self.ctx, lambda: self.sleep(1))
        log = self.row / 'bridge.log'
        log.write_text('')
        self.ctx.instance = {'bridge_log': str(log)}
        menu.writer_log = log
        original_send = menu.send

        def send(macro):
            original_send(macro)
            if menu.menu['depth'] == '1' and not menu.picked:
                menu.menu['editor.sel'] = '7'

        def live_fighter(ctx, seat, kind, cap):
            self.state['roster'][1]['kind'] = 7
            return self.state['roster']

        menu.send = send
        tables = routes.tables()
        tables['roster_catalog'] = [{'kind': 0, 'name': 'Mario'}, {'kind': 7, 'name': 'Fox'}]
        real_set_fighter = routes._set_fighter
        pickers = iter((self.io, menu))
        with patch.object(routes, 'tables', return_value=tables), \
             patch.object(routes, '_fetch_settings', return_value=b'{"menu":{"cpu_character":[0,1]}}'), \
             patch.object(routes, '_live_fighter', side_effect=live_fighter), \
             patch.object(routes, '_live_body', return_value={'kind': 7}):
            result = self.invoke(set_fighter=real_set_fighter, picker_factory=lambda ctx: next(pickers))
        self.assertEqual(menu.actions, ['tap-a1', 'tap-a1', 'tap-a1', 'tap-b1', 'tap-b1'])
        self.assertEqual((result['result'], result['input_retries'], result['retries']),
                         ('PASS with 1 stall', 1, {}))
        event, = result['stalls']
        self.assertEqual((event['kind'], event['block'], event['step'], event['send']),
                         ('resend', 'tr_input', 'tap-a1', 2))
        self.assertEqual(result['phases']['set_cpu']['stalls'], [event])
        self.assertEqual(result['phases']['set_cpu']['retries'], 1)
        rows = [json.loads(line) for line in (self.row / 'stall-events.jsonl').read_text().splitlines()]
        self.assertEqual(rows, [event])

    def test_an_inner_training_resend_is_retained_in_a_phase_stop(self):
        self.io.screen = 'training'
        self.state['roster'][1]['kind'] = 3
        menu = retrying_menu(self.ctx, lambda: self.sleep(1))

        def set_cpu(ctx, fighter, seat, cap, metrics):
            before = menu.read('tr.menu', 2)
            routes._tr_input(menu, before, 'tap-a1', self.io.now + 20)
            raise RuntimeError('bridge connection changed after the retry')

        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.invoke(set_fighter=set_cpu)
        self.assertEqual(caught.exception.step, 'set_cpu')
        stop = self.stop_receipt()
        self.assertEqual((stop['result'], stop['input_retries'], stop['retries']),
                         ('STOPPED with 1 stall', 1, {}))
        self.assertEqual(len(stop['stalls']), 1)
        self.assertEqual(stop['stalls'][0]['kind'], 'resend')
        rows = [json.loads(line) for line in (self.row / 'stall-events.jsonl').read_text().splitlines()]
        self.assertEqual(rows, stop['stalls'])

    def test_an_inner_resend_event_is_recorded_exactly_once_when_blocks_nest(self):
        menu = retrying_menu(self.ctx, lambda: self.sleep(1))

        def inner(metrics):
            before = menu.read('tr.menu', 2)
            routes._tr_input(menu, before, 'tap-a1', self.io.now + 20)
            return {'block': 'inner', **metrics}

        def outer(metrics):
            inner_receipt = routes._block(self.ctx, 'inner', 'inner', {}, inner)
            return {'block': 'outer', 'inner': inner_receipt, **metrics}

        with patch.object(routes.time, 'monotonic', side_effect=lambda: self.io.now):
            result = routes._block(self.ctx, 'outer', 'outer', {}, outer)
        self.assertEqual(result['retries'], 1)
        self.assertEqual(len(result['stalls']), 1)
        self.assertEqual(result['inner']['stalls'], result['stalls'])
        rows = [json.loads(line) for line in (self.row / 'stall-events.jsonl').read_text().splitlines()]
        self.assertEqual(rows, result['stalls'])

    def test_a_with_setup_metrics_resend_propagates_once_to_its_parent_block(self):
        menu = retrying_menu(self.ctx, lambda: self.sleep(1))

        def inner(metrics):
            before = menu.read('tr.menu', 2)
            routes._tr_input(menu, before, 'tap-a1', self.io.now + 20)
            return {'block': 'inner', **metrics}

        def outer(metrics):
            inner_receipt = routes._with_setup_metrics(self.ctx, inner)
            return {'block': 'outer', 'inner': inner_receipt, **metrics}

        with patch.object(routes.time, 'monotonic', side_effect=lambda: self.io.now):
            result = routes._block(self.ctx, 'outer', 'outer', {}, outer)
        self.assertEqual(result['retries'], 1)
        self.assertEqual(result['inner']['retries'], 1)
        self.assertEqual(len(result['stalls']), 1)
        self.assertEqual(result['inner']['stalls'], result['stalls'])
        rows = [json.loads(line) for line in (self.row / 'stall-events.jsonl').read_text().splitlines()]
        self.assertEqual(rows, result['stalls'])

    def test_a_block_receipt_with_a_retry_lists_its_stall(self):
        menu = retrying_menu(self.ctx, lambda: self.sleep(1))

        def open_menu(io, until, page):
            before = io.read('tr.menu', 2)
            return routes._tr_input(io, before, 'tap-a1', until), []

        owner = recorded.native(99, **{'input.owner': 'tr.menu'})
        with patch.object(routes.time, 'monotonic', side_effect=lambda: self.io.now), \
             patch.object(routes, 'BridgePickerIO', return_value=menu), \
             patch.object(routes, '_open_menu', side_effect=open_menu), \
             patch.object(routes, '_wait_training_owner', return_value=owner):
            result = routes.tr_open(self.ctx)
        self.assertEqual(result['retries'], 1)
        self.assertEqual(len(result['stalls']), 1)
        self.assertEqual(result['stalls'][0]['step'], 'tap-a1')

    def test_a_phase_whose_transition_happened_anyway_is_not_run_again(self):
        self.io.failures['pick_stage'] = 1
        self.io.after_failure = lambda io, name: setattr(io, 'screen', 'css.training')
        result = self.invoke()
        self.assertEqual(self.io.calls.count('pick_stage'), 1)
        self.assertEqual(result['recovered'], ['pick_stage'])
        self.assertEqual(self.io.calls, ['pick_stage', 'pick_fighters', 'enter_training'])

    def test_a_phase_retry_writes_a_stall_event_and_is_not_a_plain_pass(self):
        self.io.failures['pick_stage'] = 1
        self.io.phase_duration = 1
        log = self.row / 'bridge.log'
        log.write_text('old line\nphase retry evidence\n\n')
        self.ctx.instance = {'bridge_log': str(log)}
        result = self.invoke()
        self.assertEqual(result['result'], 'PASS with 1 stall')
        event, = result['stalls']
        self.assertEqual((event['kind'], event['step'], event['start']),
                         ('phase-retry', 'pick_stage', 1))
        self.assertEqual(event['error'], 'phase transition deadline')
        self.assertEqual(event['screen'], 'stage_select')
        self.assertEqual(event['seconds'], 1)
        self.assertEqual(event['frame'], event['rev'] * 10)
        self.assertEqual(event['wall'], event['rev'] * 100)
        self.assertEqual(event['last_bridge_line'], 'phase retry evidence')
        self.assertIn('utc', event)
        rows = [json.loads(line) for line in (self.row / 'stall-events.jsonl').read_text().splitlines()]
        self.assertEqual(rows, [{**event, 'block': 'reach_training'}])

    def test_a_recovered_phase_writes_a_stall_event(self):
        self.io.failures['pick_stage'] = 1
        self.io.after_failure = lambda io, name: setattr(io, 'screen', 'css.training')
        result = self.invoke()
        self.assertEqual(result['result'], 'PASS with 1 stall')
        event, = result['stalls']
        self.assertEqual((event['kind'], event['step'], event['start'], event['screen']),
                         ('phase-recovered', 'pick_stage', 1, 'css.training'))
        self.assertEqual(event['error'], 'phase transition deadline')
        self.assertEqual(event['rev'], 3)
        self.assertIsNone(event['last_bridge_line'])
        rows = [json.loads(line) for line in (self.row / 'stall-events.jsonl').read_text().splitlines()]
        self.assertEqual(rows, [{**event, 'block': 'reach_training'}])

    def test_a_phase_stop_keeps_the_retry_stall_events(self):
        self.io.failures['pick_stage'] = 3
        with self.assertRaises(routes.closed_loop.RouteStopped):
            self.invoke()
        stop = self.stop_receipt()
        self.assertEqual(stop['result'], 'STOPPED with 2 stalls')
        self.assertEqual([(event['kind'], event['start']) for event in stop['stalls']],
                         [('phase-retry', 1), ('phase-retry', 2)])
        self.assertEqual(len((self.row / 'stall-events.jsonl').read_text().splitlines()), 2)

    def test_a_css_reached_without_a_recorded_stage_goes_back_and_picks_the_stage_again(self):
        self.ctx.runner.routes_expect.clear()
        self.io.failures['pick_stage'] = 1
        self.io.after_failure = lambda io, name: setattr(io, 'screen', 'css.training')
        original_phase = self.io.phase

        def phase(name):
            result = original_phase(name)
            if name == 'pick_stage':
                self.ctx.runner.routes_expect['stage_id'] = 3
            return result

        self.io.phase = phase
        result = self.invoke()
        self.assertEqual(self.io.calls, ['pick_stage', 'back_to_main', 'main_to_stage', 'pick_stage',
                                         'pick_fighters', 'enter_training'])
        self.assertEqual(result['starts']['pick_stage'], 2)
        self.assertEqual(result['retries'], {'pick_stage': 1})
        self.assertEqual(result['recovered'], [])
        self.assertEqual(result['verified']['stage_id'], 3)

    def test_three_failures_of_one_phase_stop_on_that_phase(self):
        self.io.failures['pick_stage'] = 3
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.invoke()
        self.assertEqual(caught.exception.step, 'pick_stage')
        self.assertEqual(self.io.calls.count('pick_stage'), 3)
        stop = self.stop_receipt()
        self.assertEqual(stop['starts']['pick_stage'], 3)
        self.assertIn('phase transition deadline', stop['reason'])
        self.assertEqual(stop['last_ui']['screen'], 'stage_select')

    def test_a_bounce_does_not_restart_a_phase_budget(self):
        self.io.failures['pick_fighters'] = 3

        def bounce(io, name):
            if name == 'pick_fighters' and io.failures[name]:
                io.screen = 'stage_select'
        self.io.after_failure = bounce
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.invoke()
        self.assertEqual(caught.exception.step, 'pick_fighters')
        self.assertEqual(self.io.calls.count('pick_fighters'), 3)
        self.assertEqual(self.io.calls.count('pick_stage'), 3)
        self.assertEqual(self.stop_receipt()['starts']['pick_fighters'], 3)

    def test_a_continuity_error_is_never_retried(self):
        for message in ('bridge connection or replay continuity changed',
                        'native picker bridge boot changed', 'bridge log rotated or truncated'):
            with self.subTest(message=message):
                self.io = ReachTrainingPhaseReplay(self.ctx)
                self.io.pending.clear()
                self.io.failures['pick_stage'] = 1
                self.io.failure_message = message
                with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
                    self.invoke()
                self.assertEqual(caught.exception.step, 'pick_stage')
                self.assertIn(message, caught.exception.reason)
                self.assertEqual(self.io.calls.count('pick_stage'), 1)
                self.assertEqual(self.stop_receipt()['pending'], 'macro-pick_stage')

    def test_unreadable_state_after_a_failure_stops_and_keeps_the_input_unconfirmed(self):
        self.io.failures['pick_stage'] = 1
        self.io.after_failure = lambda io, name: setattr(io, 'read_fault', True)
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.invoke()
        self.assertEqual(caught.exception.step, 'pick_stage')
        self.assertIn('phase transition deadline', caught.exception.reason)
        self.assertIn(self.ctx.on, self.io.pending)
        self.assertEqual(self.stop_receipt()['pending'], 'macro-pick_stage')
        self.assertFalse(any(event[0] == 'finish' for event in self.io.events))

    def test_an_inner_route_stopped_is_not_retried(self):
        self.io.failures['pick_stage'] = 1
        self.io.failure_exception = routes.closed_loop.RouteStopped('inner', 'tile', 'unsafe', {})
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.invoke()
        self.assertEqual(caught.exception.step, 'pick_stage')
        self.assertIn("inner: stopped on step 'tile': unsafe", caught.exception.reason)
        self.assertEqual(self.io.calls, ['pick_stage'])
        self.assertIn(self.ctx.on, self.io.pending)

    def test_a_single_qualified_read_is_not_enough_to_continue(self):
        self.io.failures['pick_stage'] = 1

        def unreadable_after_one(io, name):
            io.read_fault = True
            io.good_reads_before_fault = 1
        self.io.after_failure = unreadable_after_one
        with self.assertRaises(routes.closed_loop.RouteStopped):
            self.invoke()
        self.assertIn(self.ctx.on, self.io.pending)
        self.assertEqual(self.io.calls, ['pick_stage'])
        self.assertFalse(any(event[0] == 'finish' for event in self.io.events))

    def test_a_second_read_after_the_settle_cap_does_not_clear_the_pending_input(self):
        self.io.failures['pick_stage'] = 1
        self.io.pending_read_duration = 16
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.invoke()
        self.assertEqual(caught.exception.step, 'pick_stage')
        self.assertIn('settle cap', caught.exception.reason)
        self.assertEqual(self.stop_receipt()['pending'], 'macro-pick_stage')
        self.assertIn(self.ctx.on, self.io.pending)

    def test_unknown_between_good_reads_requires_another_qualified_pair(self):
        self.io.failures['pick_stage'] = 1
        self.io.after_failure = lambda io, name: setattr(
            io, 'read_script', ['stage_select', 'unknown', 'stage_select', 'stage_select'])
        self.invoke()
        first_finish = next(i for i, event in enumerate(self.io.events) if event[0] == 'finish')
        pending_reads = [event for event in self.io.events[:first_finish] if event[0] == 'read' and event[2]]
        self.assertGreaterEqual(len(pending_reads), 4)

    def test_the_unconfirmed_mark_is_cleared_only_after_the_qualified_pair(self):
        self.io.failures['pick_stage'] = 1
        result = self.invoke()
        first_finish = next(i for i, event in enumerate(self.io.events) if event[0] == 'finish')
        preceding = self.io.events[:first_finish]
        pending_reads = [event for event in preceding if event[0] == 'read' and event[2]]
        self.assertGreaterEqual(len(pending_reads), 2)
        second_start = [event for event in self.io.events if event[:2] == ('phase', 'pick_stage')][1]
        self.assertFalse(second_start[2])
        self.assertEqual(result['starts']['pick_stage'], 2)

    def test_the_overall_deadline_stops_with_the_deadline_step(self):
        self.io.phase_duration = 2
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.invoke(cap=1)
        self.assertEqual(caught.exception.step, 'deadline')
        self.assertEqual(self.io.calls, ['pick_stage'])
        self.assertEqual(self.stop_receipt()['step'], 'deadline')

    def test_the_plain_path_receipt_has_empty_retries_and_recovered(self):
        result = self.invoke()
        self.assertEqual(self.io.calls, ['pick_stage', 'pick_fighters', 'enter_training'])
        self.assertEqual(result['retries'], {})
        self.assertEqual(result['recovered'], [])
        self.assertEqual(result['starts'], {'pick_stage': 1, 'pick_fighters': 1, 'enter_training': 1})

    def test_a_games_and_more_submenu_checkpoint_continues_to_stage(self):
        self.io.screen = 'main_menu'
        result = self.invoke()
        self.assertEqual(self.io.calls[0], 'main_to_stage')
        self.assertNotIn('back_to_main', self.io.calls)
        self.assertEqual(result['starts']['main_to_stage'], 1)


if __name__ == '__main__':
    unittest.main()
