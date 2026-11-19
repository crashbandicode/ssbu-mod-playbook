"""Replay exact tile and native state facts without controller or bridge I/O."""

import unittest

import native_picker as picker


def snapshot(provider, rev, fields, epoch=1):
    boot = '0x0000000000000001'
    schema = provider + '/1'
    active = 'yes' if provider == 'ssbu.ui' else '1'
    return picker.Sample(
        {'name': provider, 'boot': boot, 'schema': schema, 'rev': rev, 'filtered': False,
         'values': {'schema': schema, 'boot': boot, 'rev': str(rev), 'observation': str(rev),
                    'observation.active': active, 'focus': provider, **fields}},
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

    def finish(self):
        pass

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

    def test_idle_native_hover_needs_no_step_ack(self):
        idle = [tile(rev, at='16', target=TARGET, ack=5, applied=4) for rev in (1, 2)]
        for observation in idle:
            observation.values.update({
                'settled.ack': 'unknown', 'ack.result': 'unknown', 'result': 'unknown',
                'direction': 'unknown', 'from': 'unknown', 'to': 'unknown',
            })
        replay = Replay([*idle, tile(3, at='16', target=TARGET, ack=5, applied=4, token='placed')])
        self.assertTrue(self.run_pick(replay)['placed'])
        self.assertEqual(replay.actions, ['tap-a1'])

    def test_native_rest_does_not_substitute_for_step_settlement(self):
        moved = tile(3, at='12', ack=1, direction='down', source='7', dest='12')
        moved.values['settled.ack'] = 'unknown'
        replay = Replay([tile(1), tile(2), moved])
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

    def test_untyped_current_tile_does_not_authorize_a(self):
        for target in (picker.Target('bad-kind', 'Link', 'default'),
                       picker.Target('2', 'Link', 'normal')):
            with self.subTest(target=target):
                replay = Replay([
                    tile(1, at='16', target=target),
                    tile(2, at='16', target=target),
                    tile(3, at='16', target=target, token='placed', placed=target),
                ])
                with self.assertRaisesRegex(RuntimeError, 'identity'):
                    self.run_pick(replay, target=None)
                self.assertEqual(replay.actions, [])

    def test_owner_screen_change_stops_after_the_single_step(self):
        replay = Replay([
            tile(1), tile(2),
            tile(3, at='12', ack=1, direction='down', source='7', dest='12'),
            tile(4, at='12', ack=1, direction='down', source='7', dest='12'),
            tile(5, at='16', ack=2, direction='right', source='12', dest='16', target=TARGET),
            tile(6, at='16', ack=2, direction='right', source='12', dest='16', target=TARGET),
            tile(7, at='16', ack=2, target=TARGET, token='placed'),
        ], [native(1), native(2)] + [native(i, screen='css.training') for i in range(3, 8)])
        with self.assertRaisesRegex(RuntimeError, 'native screen changed'):
            self.run_pick(replay)
        self.assertEqual(replay.actions, ['tile-down'])

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


    def test_stage_confirmation_requires_native_transition(self):
        def stage_tile(rev):
            return snapshot('tile.cursor', rev, {
                'screen': 'sss', 'layout': 'stage-layout', 'generation': '3',
                'controller': '0', 'seat': '0', 'carried': 'none',
                'local.token': 'none', 'at': '8', 'hovered': '8', 'settled': '1',
                'kind': '308', 'name': 'Training', 'form': 'normal', 'settled.ack': '0',
                'ack': '0', 'applied': '0', 'focus': 'sss/8',
            })

        def stage_ui(rev):
            return snapshot('ssbu.ui', rev, {'screen': 'stage_select', 'input.owner': 'native',
                            'focus.source': 'tile.cursor', 'focus': 'stage_select/8'})

        target = picker.Target('308', 'Training', 'normal')
        replay = Replay([stage_tile(1), stage_tile(2)],
                        [stage_ui(1), stage_ui(2), native(3, focus='css.smash/ready')])
        result = picker.pick(replay, 'sss', target, next_screen='css.smash', cap=.1)
        self.assertEqual(replay.actions, ['tap-a1'])
        self.assertEqual(result['screen'], 'css.smash')

        replay = Replay([stage_tile(1), stage_tile(2)],
                        [stage_ui(1), stage_ui(2), native(3, screen='rules', focus='rules')])
        with self.assertRaisesRegex(RuntimeError, 'unexpected native screen'):
            picker.pick(replay, 'sss', target, next_screen='css.smash', cap=.1)
        self.assertEqual(replay.actions, ['tap-a1'])

    def test_remote_token_is_never_placed(self):
        replay = Replay([tile(1, at='16', target=TARGET, seat='1', token='unknown')])
        with self.assertRaisesRegex(RuntimeError, 'token ownership'):
            self.run_pick(replay)
        self.assertEqual(replay.actions, [])

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
        previous = self.room()
        replaced = self.room(bssid='feddccbbaa99')
        self.assertEqual(previous.values['focus.index'], replaced.values['focus.index'])
        self.assertEqual(previous.values['focus.label'], replaced.values['focus.label'])
        picker.verify_room_selection(previous, (self.bssid, self.session), '17')
        self.assert_no_join(replaced, 'room key')

    def test_room_recreated_or_generation_changed(self):
        self.assert_no_join(self.room(session='a' * 32), 'room key')
        self.assert_no_join(self.room(gen='18'), 'generation')

    def test_exact_room_passes_predicate_but_route_stays_closed(self):
        current = self.room()
        picker.verify_room_selection(current, (self.bssid, self.session), '17')
        self.assert_no_join(current, 'release trace')


    def test_local_wireless_route_and_raw_library_entry_are_closed(self):
        from types import SimpleNamespace

        import routes
        import routes_online

        calls = []
        with self.assertRaisesRegex(RuntimeError, 'release trace'):
            routes_online.ldn_join(SimpleNamespace(call=calls.append))
        self.assertEqual(calls, [])
        for name in ('guest-request', 'host-approval', 'host-fighter', 'peer-fighter'):
            with self.subTest(name=name), self.assertRaises(KeyError):
                routes.macro_file(name)

class BridgeFramingTests(unittest.TestCase):
    def test_reject_inactive_or_wrong_provider_activity_value(self):
        for provider, values in {'ssbu.ui': ('no', '1'), 'tile.cursor': ('0', 'yes')}.items():
            for value in values:
                with self.subTest(provider=provider, value=value), self.assertRaisesRegex(RuntimeError, 'inactive'):
                    picker.check_sample(snapshot(provider, 1, {'observation.active': value}), provider)

    def test_fresh_query_requires_full_block_after_reply_fence(self):
        import zlib
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import patch

        import routes
        from native_picker_io import BridgePickerIO

        boot = '0x0000000000000001'
        provider = 'ssbu.ui'
        schema = provider + '/1'

        def wire(rev, *, filtered=False, torn=False):
            payload = [f'schema={schema}', f'rev={rev}', f'boot={boot}', 'ack=0',
                       'ack.result=unsupported', 'focus=main_menu/top/smash',
                       f'observation={rev}', 'observation.active=yes']
            begin = (f'[Bridge] state begin name={provider} schema={schema} rev={rev} boot={boot} '
                     f'lines={len(payload)} filtered={int(filtered)}\n')
            body = ''.join(f'[Bridge] state.{i} {line}\n' for i, line in enumerate(payload))
            end = f'[Bridge] state end name={provider} rev={rev} crc={zlib.crc32(chr(10).join(payload).encode()):08x}\n'
            return begin + body + ('[Adapter] reconnect same_boot=1\n' if torn else '') + end

        with TemporaryDirectory() as temp:
            log = Path(temp, 'bridge.log')
            log.write_text(wire(50))
            ctx = SimpleNamespace(instance={'bridge_log': str(log)}, check_stop=lambda: None,
                                  runner=SimpleNamespace(), on='sw')
            bridge = BridgePickerIO(ctx)

            def spool(ctx, *requests):
                self.assertEqual(requests, ('providers', 'state ssbu.ui'))
                fence = f'[Bridge] req providers result=ok count=2 boot={boot}\n'
                with log.open('a') as dest:
                    dest.write(wire(51) + fence + wire(52, torn=True) + wire(53)
                               + fence + wire(54, filtered=True) + wire(55))
            with patch.object(routes, 'bridge_v2', spool):
                state = bridge.read(provider, 1)
            self.assertEqual(state.block['rev'], 55)
            self.assertEqual(state.connection, 1)

    def test_new_route_cannot_overwrite_unconfirmed_input(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import patch

        import routes
        from native_picker_io import BridgePickerIO

        with TemporaryDirectory() as temp:
            log = Path(temp, 'bridge.log')
            log.touch()
            ctx = SimpleNamespace(instance={'bridge_log': str(log)}, runner=SimpleNamespace(), on='sw')
            bridge = BridgePickerIO(ctx)
            with patch.object(routes, 'play'):
                bridge.send('tile-left')
            with self.assertRaisesRegex(RuntimeError, 'remains unconfirmed'):
                BridgePickerIO(ctx)
            bridge.finish()
            BridgePickerIO(ctx)

    def test_exact_since_gap_blocks_pending_input(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import patch

        import routes
        from native_picker_io import BridgePickerIO

        with TemporaryDirectory() as temp:
            log = Path(temp, 'bridge.log')
            log.touch()
            ctx = SimpleNamespace(instance={'bridge_log': str(log)}, runner=SimpleNamespace(), on='sw')
            bridge = BridgePickerIO(ctx)
            with patch.object(routes, 'play'):
                bridge.send('tile-left')
            log.write_text('[Bridge] since result=gap cursor=4 oldest=8 latest=12 boot=0x0000000000000001\n')
            with self.assertRaisesRegex(RuntimeError, 'replay continuity changed'):
                bridge.read('tile.cursor', 1)

    def test_short_direction_macros_release_before_repeat(self):
        import json
        import routes

        for direction, bit in {'left': 4096, 'up': 8192, 'right': 16384, 'down': 32768}.items():
            with self.subTest(direction=direction):
                macro, _, _ = routes.macro_file(f'tile-{direction}')
                data = json.loads(macro.read_text())
                self.assertEqual(data['player'], 0)
                self.assertEqual([(e['frame'], e['buttons']) for e in data['events']],
                                 [(0, 16), (1, 16 | bit), (3, 0)])
                self.assertGreater(data['duration_frames'], 3)

class TrainingStageProofTests(unittest.TestCase):
    boot = '0x0000000000000001'

    def run_training(self, replay, finish, *, expected=0, before_press=''):
        import zlib
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import patch

        import routes

        def frame(sample):
            block = sample.block
            fields = {**sample.values}
            keys = ('schema', 'rev', 'boot', 'ack', 'ack.result', 'focus')
            fields.setdefault('ack', '0')
            fields.setdefault('ack.result', 'unsupported')
            order = (*keys, *(k for k in fields if k not in keys))
            payload = [f'{k}={fields[k]}' for k in order]
            crc = zlib.crc32('\n'.join(payload).encode())
            begin = f'[Bridge] state begin name={block["name"]} schema={block["schema"]} rev={block["rev"]}'
            begin += f' boot={self.boot} lines={len(payload)} filtered=0\n'
            lines = ''.join(f'[Bridge] state.{i} {line}\n' for i, line in enumerate(payload))
            return begin + lines + f'[Bridge] state end name={block["name"]} rev={block["rev"]} crc={crc:08x}\n'

        with TemporaryDirectory() as temp:
            log = Path(temp, 'bridge.log')
            log.write_text('[Bridge] stage id=0 ev=41 frame=680 wall=9900\n')
            ctx = SimpleNamespace(instance={'bridge_log': str(log)}, check_stop=lambda: None,
                                  runner=SimpleNamespace(), on='sw', log=lambda evidence: None)
            ui_samples = iter([
                native(1, screen='css.training', focus='css.training'),
                native(2, screen='css.training', focus='css.training'),
                native(3, screen='training', focus='training'),
            ])
            placed = picker.Target('88', 'Steve', 'default')
            tiles = iter(tile(i, at='78', target=placed, seat='0', token='placed', placed=placed)
                         for i in (1, 2))
            actions = []

            def spool(_ctx, fence, request):
                self.assertEqual(fence, 'providers')
                if request == 'state ssbu.ui':
                    response = frame(next(ui_samples))
                elif request == 'state tile.cursor':
                    response = frame(next(tiles))
                elif request == 'events on':
                    response = '[Bridge] req events result=ok on=1 latest=41 frame=700 wall=10000\n'
                else:
                    self.assertEqual(request, 'since 41')
                    response = replay + finish + '\n'
                with log.open('a') as dst:
                    dst.write(f'[Bridge] req providers result=ok count=2 boot={self.boot} frame=700 wall=10000\n')
                    dst.write(response)
                    if request == 'events on':
                        dst.write(before_press)

            with (patch.object(routes, 'bridge_v2', spool),
                  patch.object(routes, 'play', side_effect=lambda _ctx, name: actions.append(name)),
                  patch.object(routes, 'native_state', return_value={'stage': expected}),
                  patch.object(routes, 'expected', return_value={'stage_id': expected}),
                  patch.object(routes, '_save', side_effect=lambda _ctx, _name, value: value)):
                try:
                    return routes.enter_training(ctx, cap=.05)
                finally:
                    self.assertEqual(actions, ['enter-training'])

    def ending(self, last=41, *, boot=None):
        return (f'[Bridge] since result=ok cursor=41 from=42 oldest=1 latest={last}'
                f' count={last - 41} boot={boot or self.boot} frame=730 wall=10200')

    def test_training_accepts_fresh_battlefield_event(self):
        stage = '[Bridge] stage id=0 ev=42 frame=720 wall=10040\n'
        result = self.run_training(stage, self.ending(42))
        self.assertEqual(result['stage_id'], 0)
        self.assertEqual(result['stage_event'], {'sequence': 42, 'frame': 720, 'wall_ms': 10040,
                                                'boot': self.boot})

    def test_repeated_training_stage_cannot_reuse_historical_id(self):
        with self.assertRaisesRegex(RuntimeError, 'no fresh gameplay stage'):
            self.run_training('', self.ending())

    def test_buffered_stage_uses_native_timestamp_not_replay_order(self):
        stage = '[Bridge] stage id=0 ev=42 frame=690 wall=9999\n'
        with self.assertRaisesRegex(RuntimeError, 'no fresh gameplay stage'):
            self.run_training(stage, self.ending(42))

    def test_stage_seen_before_press_is_not_a_launch_result(self):
        stage = '[Bridge] stage id=0 ev=42 frame=701 wall=10001\n'
        with self.assertRaisesRegex(RuntimeError, 'no fresh gameplay stage'):
            self.run_training(stage, self.ending(42), before_press=stage)

    def test_different_fresh_stage_overrides_cached_matching_id(self):
        stage = '[Bridge] stage id=347 ev=42 frame=720 wall=10040\n'
        with self.assertRaisesRegex(RuntimeError, 'loaded stage 347'):
            self.run_training(stage, self.ending(42))

    def test_reconnect_or_wrong_boot_cannot_authorize_stage(self):
        stage = '[Bridge] stage id=0 ev=42 frame=720 wall=10040\n'
        with self.assertRaisesRegex(RuntimeError, 'wrong Training stage replay fence'):
            self.run_training(stage, self.ending(42, boot='0x0000000000000002'))
        with self.assertRaisesRegex(RuntimeError, 'connection or replay continuity'):
            self.run_training(stage + '[Adapter] reconnect same_boot=1\n', self.ending(42))

    def test_missing_event_or_transport_loss_stops(self):
        stage = '[Bridge] stage id=0 ev=42 frame=720 wall=10040\n'
        with self.assertRaisesRegex(RuntimeError, 'incomplete stage event replay'):
            self.run_training(stage, self.ending(43))
        with self.assertRaisesRegex(RuntimeError, 'event continuity'):
            self.run_training(stage + '[Bridge] lost lines=1 reason=overrun frame=725 wall=10050\n',
                              self.ending(42))

class NativeScreenGateTests(unittest.TestCase):
    def run_gate(self, socket, name, samples, *, cap=1):
        import json
        import zlib
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        import routes

        def wire(sample):
            boot, rev, _ = sample.clock
            # The framing parser requires the canonical six headers first.
            header = ('schema', 'rev', 'boot', 'ack', 'ack.result', 'focus')
            fields = {**sample.values, 'ack': '0', 'ack.result': 'unsupported'}
            order = (*header, *(key for key in fields if key not in header))
            payload = [f'{key}={fields[key]}' for key in order]
            crc = zlib.crc32('\n'.join(payload).encode())
            return (f'[Bridge] req providers result=ok count=1 boot={boot}\n'
                    f'[Bridge] state begin name=ssbu.ui schema=ssbu.ui/1 rev={rev} boot={boot}'
                    f' lines={len(payload)} filtered=0\n'
                    + ''.join(f'[Bridge] state.{i} {line}\n' for i, line in enumerate(payload))
                    + f'[Bridge] state end name=ssbu.ui rev={rev} crc={crc:08x}\n')

        with TemporaryDirectory() as directory:
            log = Path(directory, 'bridge.log')
            log.touch()
            ctx = SimpleNamespace(instance={'socket': socket, 'bridge_log': str(log), 'screenshot_target': 'screen'},
                                  runner=SimpleNamespace(plan={}), on='p1', row_dir=directory,
                                  check_stop=Mock(), gate=Mock(side_effect=AssertionError('visual gate')),
                                  log=Mock(), shot=Mock(side_effect=AssertionError('still')))
            replies = iter(samples)

            def respond(_ctx, *commands):
                self.assertEqual(commands, ('providers', 'state ssbu.ui'))
                sample = next(replies, samples[-1])
                with log.open('a') as dest:
                    if sample is None:
                        dest.write('[Bridge] req providers result=ok count=1 boot=0x0000000000000001\n'
                                   '[Bridge] state name=ssbu.ui result=error reason=unknown-provider\n')
                    else:
                        dest.write(wire(sample))

            with (patch.object(routes, 'bridge_v2', side_effect=respond),
                  patch.object(routes, 'frame', side_effect=AssertionError('still')),
                  patch.object(routes, 'play', side_effect=AssertionError('controller input'))):
                result = routes.gate(ctx, name, cap=cap)
            self.assertIsInstance(result, str)
            ctx.log.assert_called_once()
            return json.loads(Path(result).read_text())

    def test_native_gates_use_fresh_screen_state_without_input_or_still(self):
        for socket in ('switch://fixture', '/tmp/eden-fixture.sock'):
            for name, screen in (('css', 'css.training'), ('css', 'css.smash'), ('stage', 'stage_select')):
                with self.subTest(socket=socket, screen=screen):
                    samples = [native(i, screen=screen, focus=screen) for i in (1, 2)]
                    receipt = self.run_gate(socket, name, samples)
                    self.assertEqual(receipt['screen'], screen)
                    self.assertEqual(receipt['observation'], 2)

    def test_unqualified_inputs_never_fall_back_to_images_or_probes(self):
        for latest, reason in (
            (None, 'unknown-provider'),
            (native(2, screen='stage_select'), 'native screen'),
            (native(2, **{'input.owner': 'unknown'}), 'native screen'),
            (native(2, **{'focus.source': 'native'}), 'native screen'),
            (native(2, **{'observation.active': 'no'}), 'inactive'),
            (native(2, observation='1'), 'no newer active observation'),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(RuntimeError, reason):
                    self.run_gate('switch://fixture', 'css', [native(1), latest], cap=.15)

    def test_standalone_eden_keeps_image_gate_return_value(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        import routes

        ctx = SimpleNamespace(instance={'socket': '/tmp/eden-fixture.sock'},
                              runner=SimpleNamespace(plan={}), gate=Mock(return_value='standalone.png'))
        with patch.object(routes, 'BridgePickerIO', side_effect=AssertionError('bridge query')):
            self.assertEqual(routes.gate(ctx, 'css'), 'standalone.png')
        ctx.gate.assert_called_once_with('routes.css')

class MainMenuStepTests(unittest.TestCase):
    @staticmethod
    def top(rev, focus, **changes):
        fields = {'screen': 'main_menu', 'screen.layer': 'top', 'screen.tab': focus,
                  'input.owner': 'native', 'dialog': 'none', 'focus.source': 'native',
                  'focus.id': focus, 'focus': f'main_menu.top.{focus}'}
        return native(rev, **{**fields, **changes})

    def setUp(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        import routes

        self.ctx = SimpleNamespace()
        self.io = Mock(connection=1, boot=self.top(1, 'smash').clock[0])
        test_io = patch.object(routes, 'BridgePickerIO', return_value=self.io)
        save = patch.object(routes, '_save', side_effect=lambda _ctx, _name, result: result)
        self.addCleanup(test_io.stop)
        self.addCleanup(save.stop)
        test_io.start()
        save.start()
        self.routes = routes

    def step(self, direction='right', from_focus='smash', to_focus='spirits'):
        return self.routes.main_menu_step(self.ctx, direction, from_focus, to_focus, cap=.25)

    def test_one_press_from_refreshed_focus_then_exact_destination(self):
        # These endpoint IDs exercise the predicate without asserting stock adjacency.
        self.io.read.side_effect = [self.top(1, 'smash'), self.top(2, 'smash'),
                                    self.top(3, 'smash'), self.top(4, 'spirits')]
        result = self.step()
        self.assertEqual([call[0] for call in self.io.mock_calls],
                         ['read', 'arm', 'read', 'send', 'read', 'read', 'finish'])
        self.io.arm.assert_called_once()
        self.assertEqual(self.io.arm.call_args.args[0], 'ssbu.ui')
        self.assertGreater(self.io.arm.call_args.args[1], 0)
        self.io.send.assert_called_once_with('main-menu-right')
        self.assertEqual((result['from_focus'], result['to_focus']), ('smash', 'spirits'))
        self.assertEqual((result['from_observation'], result['to_observation']), (2, 4))

    def test_failed_arm_blocks_before_input_or_baseline_read(self):
        self.io.read.side_effect = [self.top(1, 'smash'), self.top(2, 'smash'), self.top(3, 'spirits')]
        self.io.arm.side_effect = RuntimeError('watch failed')
        with self.assertRaisesRegex(RuntimeError, 'watch failed'):
            self.step()
        self.assertEqual([call[0] for call in self.io.mock_calls], ['read', 'arm'])

    def test_connection_or_boot_change_during_arm_blocks_before_input(self):
        for field, value in (('connection', 2), ('boot', '0x0000000000000002')):
            with self.subTest(field=field):
                self.io.reset_mock()
                self.io.connection = 1
                self.io.boot = self.top(1, 'smash').clock[0]
                self.io.read.side_effect = [self.top(1, 'smash'), self.top(2, 'smash'), self.top(3, 'spirits')]
                self.io.arm.side_effect = lambda *_: setattr(self.io, field, value)
                with self.assertRaisesRegex(RuntimeError, 'connection or boot'):
                    self.step()
                self.assertEqual([call[0] for call in self.io.mock_calls], ['read', 'arm'])

    def test_wrong_committed_focus_stops_without_retry_or_finish(self):
        self.io.read.side_effect = [self.top(1, 'smash'), self.top(2, 'smash'),
                                    self.top(3, 'vault'), self.top(4, 'spirits')]
        with self.assertRaisesRegex(RuntimeError, 'unexpected.*vault'):
            self.step('down')
        self.io.send.assert_called_once_with('main-menu-down')
        self.io.finish.assert_not_called()
        self.assertEqual(self.io.read.call_count, 3)

    def test_source_gate_refuses_changed_tab_or_unqualified_context(self):
        for snapshot in (
            self.top(2, 'spirits'),
            self.top(2, 'smash', **{'dialog': 'notice'}),
            self.top(2, 'smash', **{'screen.layer': 'submenu'}),
            self.top(2, 'smash', **{'input.owner': 'unknown'}),
            self.top(2, 'smash', **{'focus.source': 'unknown'}),
            self.top(2, 'smash', **{'screen.tab': 'online'}),
        ):
            with self.subTest(values=snapshot.values):
                self.io.read.side_effect = [self.top(1, 'smash'), snapshot]
                with self.assertRaisesRegex(RuntimeError, 'source'):
                    self.step()
                self.io.send.assert_not_called()

    def test_post_press_context_change_reusing_source_focus_stops(self):
        self.io.read.side_effect = [self.top(1, 'smash'), self.top(2, 'smash'),
                                    self.top(3, 'smash', dialog='notice'), self.top(4, 'spirits')]
        with self.assertRaisesRegex(RuntimeError, 'unexpected'):
            self.step()
        self.io.send.assert_called_once()
        self.io.finish.assert_not_called()
        self.assertEqual(self.io.read.call_count, 3)

    def test_reconnect_cannot_confirm_step(self):
        sample = self.top(3, 'spirits')
        self.io.read.side_effect = [self.top(1, 'smash'), self.top(2, 'smash'),
                                    picker.Sample(sample.block, connection=2)]
        with self.assertRaisesRegex(RuntimeError, 'connection'):
            self.step()
        self.io.send.assert_called_once()
        self.io.finish.assert_not_called()

    def test_arguments_fail_before_bridge_or_input(self):
        for args in (('a', 'smash', 'spirits'), ('up', 'unknown', 'spirits'), ('up', 'smash', 'smash')):
            with self.subTest(args=args), self.assertRaisesRegex(RuntimeError, 'direction|focus'):
                self.step(*args)
        self.io.read.assert_not_called()
        self.io.send.assert_not_called()

    def test_macros_press_one_plain_dpad_direction_then_release(self):
        import json

        for direction, bit in {'left': 4096, 'up': 8192, 'right': 16384, 'down': 32768}.items():
            with self.subTest(direction=direction):
                path, _, _ = self.routes.macro_file(f'main-menu-{direction}')
                data = json.loads(path.read_text())
                self.assertEqual(data['player'], 0)
                self.assertEqual([(frame['frame'], frame['buttons']) for frame in data['events']],
                                 [(0, 0), (1, bit), (3, 0)])
                self.assertTrue(all(frame['sticks'] == [0, 0, 0, 0] and frame['triggers'] == [0, 0]
                                    for frame in data['events']))
                self.assertGreater(data['duration_frames'], 3)

if __name__ == '__main__':
    unittest.main()
