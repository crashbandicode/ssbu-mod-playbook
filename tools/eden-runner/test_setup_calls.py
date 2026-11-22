"""Offline setup-call tests; no emulator or controller is started."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
                                'focus': 'css.grid.cpu'})
            target = TOPOLOGY.targets.get(self.cpu_tile)
            if target:
                self.values.update({**self._identity(target), **self._identity(target, 'carried.')})
            else:
                self.values.update({'kind': 'unknown', 'name': 'unknown', 'form': 'unknown',
                                    'carried.kind': 'unknown', 'carried.name': 'unknown',
                                    'carried.form': 'unknown'})
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

    def send(self, segment):
        self.actions.append(segment)
        if segment == 'tap-a1':
            held = self.values.get('carried', 'none')
            if held == 'none':
                slot = self.values['hand.token']
                self._take('0' if self.fault == 'wrong_carried' else slot)
            else:
                self._place(held)
            return
        if not segment.startswith('tile-'):
            raise AssertionError(segment)
        direction = segment[5:]
        carried = self.values.get('carried', 'none')
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
                                'focus': 'css.grid.' + to, 'target.token': 'none', 'token.settled': '0',
                                **self._identity(target), **self._identity(target, 'carried.')})
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
            return routes.pick_cpu(self.ctx, 'Fox', topology={'path': 'modelled', 'sha256': 'modelled'})

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
            routes.pick_fighters(self.ctx, 'Mario', cpu='Fox', topology={'path': 'modelled', 'sha256': 'modelled'})
        local.assert_called_once()
        self.assertEqual(seen, [7])

    def test_pick_fighters_without_cpu_returns_a_receipt(self):
        with patch.object(routes, 'pick_local_fighter', return_value={'placed': True}) as local, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.pick_fighters(self.ctx, 'Mario', topology={'path': 'modelled', 'sha256': 'modelled'})
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
        return recorded.native(self.rev, screen='training', focus='training',
                               **{'screen.context': 'training', 'input.owner': owner})

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


class SetupCallsR2Tests(unittest.TestCase):
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
        self.assertEqual(result['fighters_checked'], {'p1': 'unchecked', 'cpu': 'unchecked'})
        self.assertEqual(result['roster'], state['roster'])

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

    def set_cpu(self, replay, roster, settings):
        replay.writer_log = self.log
        tables = routes.tables()
        tables['roster_catalog'] = [{'kind': 0, 'name': 'Mario'}, {'kind': 7, 'name': 'Fox'}]
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'tables', return_value=tables), \
             patch.object(routes, '_fetch_settings', return_value=json.dumps(settings).encode(), create=True), \
             patch.object(routes, 'wait_native', return_value={'roster': roster, 'stage': 3}), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            return routes.set_cpu(self.ctx, 'Mario')

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

    def test_tr_settings_refuses_a_refused_set_and_a_readback_difference(self):
        initial = b'{"menu":{"stage_hazards":[1,0]}}'
        changed = {'stage_hazards': [0, 1]}
        cases = ((RuntimeError('[Bridge] req set result=refused reason=busy'), 'refused'),
                 ((b'{"menu":{"stage_hazards":[1,0]}}', '[Bridge] req set result=ok'), 'stage_hazards'))
        for outcome, message in cases:
            with self.subTest(message=message):
                apply = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
                with patch.object(routes, '_fetch_settings', return_value=initial, create=True), \
                     patch.object(routes, '_apply_settings', create=True, **apply), \
                     patch.object(routes, 'BridgePickerIO', return_value=MenuReplay()), \
                     patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
                    with self.assertRaisesRegex(RuntimeError, message):
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
        self.assertEqual(saved, ['routes-reach-training'])
        self.assertEqual(replay.actions, [])
        self.assertEqual(set(receipt['skipped']), {'to_main', 'back_to_main', 'main_to_stage', 'pick_stage',
                                                   'quit_training', 'pick_fighters', 'enter_training'})

    def test_set_cpu_needs_the_live_roster_not_only_the_file(self):
        replay = MenuReplay()
        with self.assertRaisesRegex(RuntimeError, '(?i)roster'):
            self.set_cpu(replay, {0: {'kind': 0}, 1: {'kind': 7}},
                         {'menu': {'cpu_character': [1, 0]}})
        self.assertEqual(replay.actions, ['tap-a1', 'tap-a1', 'tap-b1', 'tap-b1'])

    def test_fresh_roster_requires_both_new_entries(self):
        import switch_hw

        self.log.write_text('[Bridge] roster entry=1 kind=7 nana=false frame=1 wall=1\n')
        lines = iter(('[Bridge] roster entry=0 kind=0 nana=false frame=2 wall=2\n',
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

    def test_settings_set_requires_fresh_success_and_writer(self):
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
                        stream.write('[TrainingRooms] settings.json written by the writer thread frame=2 wall=2\n')

        self.ctx.check_stop = respond
        with patch.object(routes, '_fetch_settings', return_value=b'{"menu":{}}'):
            data, line = routes._apply_settings(self.ctx, b'{"menu":{}}', cap=1)
            self.assertEqual(data, b'{"menu":{}}')
            self.assertIn('result=ok', line)
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
        initial = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 3}}}
        final = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}
        calls = []
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', side_effect=[initial, final]), \
             patch.object(routes, 'quit_training', side_effect=lambda *a, **k: calls.append('quit')), \
             patch.object(routes, 'pick_fighters', side_effect=lambda *a, **k: calls.append('pick')), \
             patch.object(routes, 'enter_training', side_effect=lambda *a, **k: calls.append(('enter', k))), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.reach_training(self.ctx, 'Mario', cpu='Fox', stage='Final Destination')
        self.assertEqual([item if isinstance(item, str) else item[0] for item in calls], ['quit', 'pick', 'enter'])
        self.assertEqual(calls[-1][1]['stage_ids'], {3})
        self.assertTrue({'to_main', 'main_to_stage', 'pick_stage'} <= set(result['skipped']))
        self.assertNotIn('quit_training', result['skipped'])

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

    def test_setup_state_reads_without_any_controller_input(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        state = {'stage': 3, 'roster': {0: {'kind': 0}, 1: {'kind': 7}}}
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', return_value=state), \
             patch.object(routes, '_fetch_settings', return_value=b'{"menu":{}}'), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.setup_state(self.ctx)
        self.assertEqual(replay.actions, [])
        self.assertEqual(result['verified']['settings_sha256'], hashlib.sha256(b'{"menu":{}}').hexdigest())
        self.assertEqual((result['verified']['p1']['kind'], result['verified']['cpu']['kind']), (0, 7))
        self.assertEqual((result['verified']['p1']['name'], result['verified']['cpu']['name']), ('Mario', 'Fox'))

    def test_tr_settings_preserves_other_keys_and_checks_the_fresh_file(self):
        original = b'{"menu":{"stage_hazards":[1,0],"other":[2,3]},"version":1}'
        bodies = []

        def apply(_ctx, data, cap):
            bodies.append(json.loads(data))
            return data, '[Bridge] req set result=ok'

        with patch.object(routes, 'BridgePickerIO', return_value=MenuReplay()), \
             patch.object(routes, '_fetch_settings', return_value=original), \
             patch.object(routes, '_apply_settings', side_effect=apply), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.tr_settings(self.ctx, values={'stage_hazards': [0, 1]})
        self.assertEqual(bodies[0], {'menu': {'stage_hazards': [0, 1], 'other': [2, 3]}, 'version': 1})
        self.assertEqual(result['verified']['keys'], ['stage_hazards'])

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


if __name__ == '__main__':
    unittest.main()
