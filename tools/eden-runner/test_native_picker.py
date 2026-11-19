"""Replay exact tile and native state facts without controller or bridge I/O."""

import unittest

import native_picker as picker


def snapshot(provider, rev, fields, epoch=1):
    boot = '0x0000000000000001'
    schema = provider + '/1'
    return picker.Sample(
        {'name': provider, 'boot': boot, 'schema': schema, 'rev': rev, 'filtered': False,
         'values': {'schema': schema, 'boot': boot, 'rev': str(rev), 'observation': str(rev),
                    'observation.active': '1', 'focus': provider, **fields}},
        epoch,
    )


TARGET = picker.Target('2', 'Link', 'default')
OTHER = picker.Target('0', 'Mario', 'default')
GRAPH = picker.Topology(
    layout='irregular-css-pin',
    neighbors={'7': {'down': '12'}, '12': {'up': '7', 'right': '16'}, '16': {'left': '12'}},
    targets={'7': OTHER, '12': OTHER, '16': TARGET},
)


def tile(rev, *, at='7', target=OTHER, ack=0, applied=None, settled=True, direction='none',
         source='none', dest='none', seat='1', token='held', epoch=1, placed=TARGET):
    return snapshot('tile.cursor', rev, {
        'screen': 'css', 'layout': GRAPH.layout, 'generation': '3', 'controller': '0',
        'seat': seat, 'carried': seat if token == 'held' else 'none', 'p1.token': 'unknown',
        'local.token': token, 'at': at, 'hovered': at, 'kind': target.kind,
        'name': target.name, 'form': target.form, 'settled': str(int(settled)), 'ack': str(ack),
        'ack.result': 'handled', 'applied': str(ack if applied is None else applied),
        'direction': direction, 'from': source, 'to': dest, 'result': 'handled',
        'settled.ack': str(ack if settled else ack-1),
        'placed.at': at if token == 'placed' else 'none',
        'placed.kind': placed.kind if token == 'placed' else 'none',
        'placed.name': placed.name if token == 'placed' else 'none',
        'placed.form': placed.form if token == 'placed' else 'none',
        'focus': f'css/{seat}/{token}/{at}',
    }, epoch)


def native(rev, epoch=1, **extra):
    return snapshot('ssbu.ui', rev, {'screen': 'css.smash', 'input.owner': 'native',
                                    'focus.source': 'tile.cursor', **extra}, epoch)


class Replay:
    def __init__(self, tiles=(), native_blocks=()):
        self.tiles = iter(tiles)
        self.native = iter(native_blocks or [native(i) for i in range(1, 100)])
        self.actions = []
        self.watches = []
        self.evidence = []

    def arm(self, provider, cap):
        self.watches.append(provider)

    def read(self, provider, cap):
        try:
            return next(self.tiles if provider == 'tile.cursor' else self.native)
        except StopIteration:
            raise RuntimeError(f'{provider} missing') from None

    def send(self, segment):
        self.actions.append(segment)

    def log(self, evidence):
        self.evidence.append(evidence)


class PickerTests(unittest.TestCase):
    def run_pick(self, replay, *, target=TARGET, topology=GRAPH):
        return picker.pick(replay, 'css', target, topology=topology, cap=.01)

    def test_irregular_grid_and_native_settlement(self):
        replay = Replay([
            tile(1), tile(2),
            tile(3, at='12', ack=1, settled=False, direction='down', source='7', dest='12'),
            tile(4, at='12', ack=1, direction='down', source='7', dest='12'),
            tile(5, at='12', ack=1, direction='down', source='7', dest='12'),
            tile(6, at='16', target=TARGET, ack=2, direction='right', source='12', dest='16'),
            tile(7, at='16', target=TARGET, ack=2, direction='right', source='12', dest='16'),
            tile(8, at='16', target=TARGET, ack=2, token='placed'),
        ])
        self.run_pick(replay)
        self.assertEqual(replay.actions, ['tile-down', 'tile-right', 'tap-a1'])
        self.assertEqual(replay.evidence[0]['to'], '12')

    def test_wrong_step_stops_before_a(self):
        replay = Replay([tile(1), tile(2), tile(3, at='12', ack=1, direction='right', source='7', dest='12')])
        with self.assertRaisesRegex(RuntimeError, 'wrong step'):
            self.run_pick(replay)
        self.assertEqual(replay.actions, ['tile-down'])

    def test_multiple_ack_stops_before_a(self):
        replay = Replay([tile(1), tile(2), tile(3, at='12', ack=2, direction='down', source='7', dest='12')])
        with self.assertRaisesRegex(RuntimeError, 'ACK'):
            self.run_pick(replay)
        self.assertEqual(replay.actions, ['tile-down'])

    def test_unsettled_step_blocks_confirmation(self):
        replay = Replay([tile(1), tile(2), tile(3, at='12', ack=1, settled=False, direction='down', source='7', dest='12')])
        with self.assertRaises(RuntimeError):
            self.run_pick(replay)
        self.assertEqual(replay.actions, ['tile-down'])

    def test_missing_provider_blocks_input(self):
        replay = Replay()
        with self.assertRaisesRegex(RuntimeError, 'tile.cursor missing'):
            self.run_pick(replay)
        self.assertEqual(replay.actions, [])

    def test_guest_uses_local_p2_and_confirms_placement(self):
        replay = Replay([tile(1, at='16', target=TARGET), tile(2, at='16', target=TARGET),
                         tile(3, at='16', target=TARGET, token='placed')])
        result = self.run_pick(replay)
        self.assertEqual(replay.actions, ['tap-a1'])
        self.assertEqual(result['seat'], '1')
        self.assertEqual(result['kind'], TARGET.kind)

    def test_wrong_kind_blocks_a(self):
        replay = Replay([tile(1, at='16', target=picker.Target('99', 'Link', 'default'))])
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            self.run_pick(replay)
        self.assertEqual(replay.actions, [])

    def test_wrong_placement_blocks_progress(self):
        replay = Replay([tile(1, at='16', target=TARGET), tile(2, at='16', target=TARGET),
                         tile(3, at='16', target=TARGET, token='placed', placed=OTHER)])
        with self.assertRaisesRegex(RuntimeError, 'placement'):
            self.run_pick(replay)
        self.assertEqual(replay.actions, ['tap-a1'])

    def test_reconnect_during_input_blocks_progress(self):
        replay = Replay([tile(1), tile(2),
                         tile(3, at='12', ack=1, direction='down', source='7', dest='12', epoch=2)])
        with self.assertRaisesRegex(RuntimeError, 'connection'):
            self.run_pick(replay)
        self.assertEqual(replay.actions, ['tile-down'])

    def test_same_boot_idle_checkpoint(self):
        replay = Replay([tile(1, at='16', target=TARGET), tile(2, at='16', target=TARGET, epoch=2),
                         tile(3, at='16', target=TARGET, token='placed', epoch=2)],
                        [native(1), native(2, epoch=2), native(3, epoch=2)])
        self.run_pick(replay)
        self.assertEqual(replay.actions, ['tap-a1'])


class JoinTests(unittest.TestCase):
    boot = '0x0000000000000001'
    bssid = 'eeddccbbaa99'
    session = '1234567890abcdef' * 2

    def room(self, *, bssid=None, session=None, gen='17'):
        bssid, session = bssid or self.bssid, session or self.session
        return snapshot('ssbu.ui', 3, {'screen': 'ldn.room_list', 'input.owner': 'native',
                        'ldn.scan.generation': gen, 'ldn.room.ready': 'yes', 'ldn.room.bssid': bssid,
                        'ldn.room.session': session, 'focus.id': f'ldn.room.{bssid}.{session}',
                        'focus.index': '1', 'focus.label': 'Owned room'})

    def assert_no_join(self, current, reason):
        calls = []
        with self.assertRaisesRegex(RuntimeError, reason):
            picker.request_join(current, (self.bssid, self.session), '17', calls.append)
        self.assertEqual(calls, [])

    def test_stale_identical_row_has_no_join(self):
        self.assert_no_join(self.room(bssid='feddccbbaa99'), 'room key')

    def test_room_recreated_or_generation_changed(self):
        self.assert_no_join(self.room(session='a' * 32), 'room key')
        self.assert_no_join(self.room(gen='18'), 'generation')

    def test_exact_room_passes_predicate_but_route_stays_closed(self):
        current = self.room()
        picker.verify_room_selection(current, (self.bssid, self.session), '17')
        self.assert_no_join(current, 'release trace')


if __name__ == '__main__':
    unittest.main()

