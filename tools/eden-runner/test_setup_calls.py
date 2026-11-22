"""Offline setup-call tests; no emulator or controller is started."""

import copy
import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import native_picker as picker
import routes
import test_native_picker as recorded


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


if __name__ == '__main__':
    unittest.main()
