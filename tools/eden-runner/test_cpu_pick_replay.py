"""Replay the Tile Cursor Publisher's CPU-pick blocks through the real routes."""

import copy
import hashlib
import json
import sys
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_picker as picker
import routes
import state_gate
import test_native_picker as recorded
from test_setup_calls import CoinReplay, modelled_block

FIXTURE = Path(__file__).resolve().parent / 'fixtures/tile-cursor-cpu-pick-wire.txt'
FIXTURE_SHA256 = 'e6e5036b2c53a9eea39ccbc9cbd16341231f590e3b21174fd9d873e796b8011c'
TILE_COMMIT = 'e68d8adce1a7ff721d8cd0490db0ee95f3cfa88b'


def blocks():
    result = {}
    for part in FIXTURE.read_text().strip().split('\n\n'):
        title, *lines = part.splitlines()
        result[title.removeprefix('### ')] = {
            key: state_gate.unquote(value)
            for line in lines for key, value in [line.split('=', 1)]
        }
    return result


def sample(name, rev=1):
    values = blocks()[name]
    boot = recorded.l8w_native(rev).block['boot']
    values.update(boot=boot, rev=str(rev), observation=str(10000 + rev))
    return picker.Sample({'name': picker.TILE, 'schema': 'tile.cursor/1', 'boot': boot,
                          'rev': rev, 'filtered': False, 'values': values}, 1)


class ProducerReplay:
    def __init__(self, phase='rest', fault=None):
        self.phase = phase
        self.fault = fault
        self.pending = deque()
        self.rev = 1
        self.actions = []
        self.evidence = []

    def arm(self, provider, cap):
        pass

    def read(self, provider, cap):
        self.rev += 1
        if provider == picker.UI:
            return recorded.l8w_native(self.rev)
        if self.pending:
            self.phase = self.pending.popleft()
        name = self.phase
        if self.fault == 'wrong-coin' and name in ('held-moving', 'held'):
            name = 'wrong-coin'
        elif self.fault == 'p1-moved' and name == 'step1-settled':
            name = 'p1-moved'
        elif self.fault == 'unacknowledged' and name == 'step1-settled':
            name = 'unacknowledged'
        return sample(name, self.rev)

    def send(self, segment):
        transitions = {
            ('rest', 'tile-left'): ('reach-moving', 'reach-settled'),
            ('reach-settled', 'tap-a1'): ('held-moving', 'held'),
            ('held', 'tile-left'): ('step1-moving', 'step1-settled'),
            ('step1-settled', 'tile-up'): ('step2-moving', 'step2-settled'),
            ('step2-settled', 'tap-a1'): ('placed',),
        }
        self.actions.append(segment)
        transition = transitions.get((self.phase, segment))
        if transition is None:
            raise AssertionError((self.phase, segment))
        self.pending.extend(transition)

    def log(self, evidence):
        self.evidence.append(evidence)

    def finish(self):
        pass


class HeldRichterReplay:
    def __init__(self, topology):
        self.topology = topology
        self.tile = recorded.recorded_training_tile(551)
        self.first_tile = True
        self.ui_rev = 550
        self.actions = []
        self.moves = []
        self.evidence = []

    def arm(self, provider, cap):
        pass

    def read(self, provider, cap):
        if provider == picker.UI:
            self.ui_rev += 1
            return recorded.recorded_native(self.ui_rev)
        if self.first_tile:
            self.first_tile = False
        else:
            self.tile.block['rev'] += 1
            self.tile.values['rev'] = str(self.tile.block['rev'])
            self.tile.values['observation'] = str(int(self.tile.values['observation']) + 1)
        return copy.deepcopy(self.tile)

    def send(self, segment):
        self.actions.append(segment)
        values = self.tile.values
        if segment.startswith('tile-'):
            direction = segment.removeprefix('tile-')
            source = values['at']
            dest = self.topology.neighbors[source][direction]
            identity = self.topology.targets[dest]
            ack = str(int(values['ack']) + 1)
            values.update({'at': dest, 'hovered': dest, 'kind': identity.kind, 'name': identity.name,
                           'form': identity.form, 'focus': f'css.grid.{dest}', 'settled': '1',
                           'ack': ack, 'applied': ack, 'settled.ack': ack, 'ack.result': 'handled',
                           'direction': direction, 'from': source, 'to': dest, 'result': 'handled'})
            self.moves.append((source, direction, dest))
        elif segment == 'tap-a1':
            values.update({'local.token': 'placed', 'p1.token': 'placed', 'carried': 'none',
                           'p1.carried': 'none', 'placed.at': values['at'], 'placed.kind': values['kind'],
                           'placed.name': values['name'], 'placed.form': values['form'],
                           'focus': 'css.grid.placed'})
        else:
            raise AssertionError(f'unexpected input {segment}')

    def log(self, evidence):
        self.evidence.append(evidence)

    def finish(self):
        pass


class ProducerReplayTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA256)
        self.ctx = SimpleNamespace(runner=SimpleNamespace(routes_expect={}), on='s', instance={}, row_dir='.')

    def pick(self, replay):
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            return routes.pick_cpu(self.ctx, 'Banjo & Kazooie')

    def test_pick_cpu_passes_on_the_producers_blocks(self):
        replay = ProducerReplay()
        try:
            result = self.pick(replay)
        except RuntimeError as exc:
            self.fail(str(exc))
        self.assertEqual(replay.actions, ['tile-left', 'tap-a1', 'tile-left', 'tile-up', 'tap-a1'])
        self.assertEqual((result['verified']['cpu']['tile'], result['verified']['cpu']['kind']), ('74', '84'))
        json.dumps(result)

    def test_the_reviewers_block_passes_the_sample_gate(self):
        held = sample('held')
        try:
            result = picker.check_sample(held, picker.TILE, stepping=True, pending_placement=True)
        except RuntimeError as exc:
            self.fail(str(exc))
        self.assertIs(result, held)

    def test_take_token_waits_for_the_settled_hover(self):
        replay = ProducerReplay('reach-settled')
        try:
            result = picker.take_token(replay, 1)
        except RuntimeError as exc:
            self.fail(str(exc))
        self.assertEqual((result.values['settled'], result.values['at']), ('1', '86'))

    def test_take_token_reports_picked_up_but_unsettled_on_timeout(self):
        replay = ProducerReplay('reach-settled')
        original = replay.send

        def send(segment):
            original(segment)
            replay.pending = deque(['held-moving'])

        replay.send = send
        with self.assertRaisesRegex(RuntimeError, 'coin 1 was picked up but its hover did not settle within'):
            picker.take_token(replay, 1, cap=.01)
        self.assertEqual(replay.actions, ['tap-a1'])

    def test_the_wrong_coin_is_refused(self):
        replay = ProducerReplay(fault='wrong-coin')
        with self.assertRaisesRegex(RuntimeError, 'p1.carried is 0 after A'):
            self.pick(replay)
        self.assertEqual(replay.actions, ['tile-left', 'tap-a1'])

    def test_a_changed_p1_coin_is_refused(self):
        replay = ProducerReplay(fault='p1-moved')
        with self.assertRaisesRegex(RuntimeError, 'CPU coin or P1 coin changed while walking'):
            self.pick(replay)
        self.assertEqual(replay.actions, ['tile-left', 'tap-a1', 'tile-left'])

    def test_an_unacknowledged_tile_step_is_refused(self):
        replay = ProducerReplay(fault='unacknowledged')
        with self.assertRaisesRegex(RuntimeError, 'incomplete, inactive or wrong-schema observation'):
            self.pick(replay)
        self.assertEqual(replay.actions, ['tile-left', 'tap-a1', 'tile-left'])

    def test_a_settled_block_with_a_step_but_no_ack_is_rejected_without_the_cpu_coin(self):
        value = sample('step1-settled')
        value.values.update({'p1.carried': '0', 'settled.ack': 'unknown'})
        with self.assertRaisesRegex(RuntimeError, 'incomplete, inactive or wrong-schema'):
            picker.check_sample(value, picker.TILE, stepping=True)

    def test_the_modelled_replay_has_the_producers_shape(self):
        replay = CoinReplay()
        replay._take('1')
        phases = [('held', modelled_block(1, replay.values).values)]
        replay.send('tile-left')
        phases.append(('step1-settled', modelled_block(2, replay.values).values))
        replay._place('1')
        phases.append(('placed', modelled_block(3, replay.values).values))
        unknown_keys = ('focus', 'kind', 'name', 'form', 'carried', 'p1.carried', 'p1.token', 'local.token',
                        'placed.at', 'placed.kind', 'placed.name', 'placed.form', 'settled', 'settled.ack',
                        'ack.result', 'result', 'direction', 'from', 'to', 'hand.token', 'p1.coin', 'cpu.token',
                        'target.token', 'token.settled')
        equal_keys = ('carried', 'p1.carried', 'p1.token', 'local.token', 'settled', 'p1.coin', 'cpu.token',
                      'target.token', 'token.settled', 'ack.result', 'result')
        for phase, model in phases:
            producer = sample(phase).values
            for key in unknown_keys:
                with self.subTest(phase=phase, key=key, comparison='unknown'):
                    self.assertEqual(model[key] == 'unknown', producer[key] == 'unknown')
            for key in equal_keys:
                with self.subTest(phase=phase, key=key, comparison='value'):
                    self.assertEqual(model[key], producer[key])

    def test_setup_state_distinguishes_live_and_remembered_stage(self):
        self.ctx.runner.routes_expect['stage_id'] = 3
        replay = ProducerReplay()
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, '_current_settings_observation', return_value=({}, 'file', b'{}')), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            receipt = routes.setup_state(self.ctx)
        self.assertEqual(receipt['verified']['stage_id'], 'unknown')
        self.assertEqual(receipt['remembered']['stage_id'], 3)

        class TrainingReplay(ProducerReplay):
            def read(self, provider, cap):
                state = super().read(provider, cap)
                if provider == picker.UI:
                    state.values['screen'] = 'training'
                return state

        with patch.object(routes, 'BridgePickerIO', return_value=TrainingReplay()), \
             patch.object(routes, 'native_state', return_value={'stage': 5, 'roster': {}}), \
             patch.object(routes, '_current_settings_observation', return_value=({}, 'file', b'{}')), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            receipt = routes.setup_state(self.ctx)
        self.assertEqual(receipt['verified']['stage_id'], 5)
        self.assertEqual(receipt['remembered']['stage_id'], 3)

    def test_tk05_first_step_reaches_mario_from_a_held_non_mario_coin(self):
        fragment = Path(__file__).resolve().parent.parent / 'eden-test' / 'example' / 'rows-tile-take.json'
        rows = json.loads(fragment.read_text())['rows']
        step = next(row for row in rows if row['id'] == 'tk05')['steps'][0]
        module, name = step['run'].split(':', 1)
        self.assertEqual(module, 'routes')
        function = getattr(routes, name)
        topology = routes._tile_topology(routes.library_topology('css-training'))
        replay = HeldRichterReplay(topology)
        self.assertEqual((replay.tile.block['rev'], replay.tile.values['at'], replay.tile.values['name'],
                          replay.tile.values['local.token']), (551, '67', 'Richter', 'held'))
        goal = next(tile for tile, identity in topology.targets.items()
                    if identity == picker.Target('0', 'Mario', 'default'))
        expected = []
        at = '67'
        while at != goal:
            direction, dest = topology.step(at, goal)
            expected.append((at, direction, dest))
            at = dest
        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'pick_cpu') as pick_cpu, \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            try:
                result = function(self.ctx, **step.get('args', {}))
            except RuntimeError:
                print(f'replay actions before error: {replay.actions!r}', file=sys.stderr)
                raise
        self.assertTrue(expected)
        self.assertEqual(replay.moves, expected)
        self.assertEqual(replay.actions, ['tile-' + direction for _, direction, _ in expected] + ['tap-a1'])
        self.assertEqual((result['name'], result['kind']), ('Mario', '0'))
        pick_cpu.assert_not_called()


if __name__ == '__main__':
    unittest.main()
