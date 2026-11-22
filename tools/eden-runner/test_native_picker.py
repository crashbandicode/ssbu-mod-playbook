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
                                    'dialog': 'none', 'focus.source': 'tile.cursor',
                                    'screen.context': 'unknown', 'ack': '0', 'ack.result': 'unsupported',
                                    **extra}, epoch)


def loading(rev, *, epoch=1, **extra):
    fields = dict.fromkeys(('screen', 'focus', 'input.owner', 'dialog', 'focus.source', 'screen.context'), 'unknown')
    return native(rev, epoch, **{**fields, 'observation.active': 'no', **extra})


def recorded_training_tile(rev):
    observation, generation, ack, settled, complete = {
        543: (3316, 21, 11, False, False),
        551: (3365, 21, 11, True, True),
        809: (4785, 28, 24, True, False),
        816: (4813, 28, 24, True, True),
    }[rev]
    boot = '0x3d1b9bf2497aaa1f'
    values = {
        'schema': 'tile.cursor/1', 'rev': str(rev), 'boot': boot, 'ack': str(ack),
        'ack.result': 'handled', 'focus': 'css.grid.richter' if complete else 'unknown',
        'observation': str(observation), 'observation.active': '1', 'screen': 'css',
        'generation': str(generation), 'layout': 'css.9f1fb03bf7c98fb9', 'grid': 'css.9f1fb03bf7c98fb9',
        'at': '67', 'hovered': '67' if settled else 'unknown', 'kind': '68' if complete else 'unknown',
        'name': 'Richter' if complete else 'unknown', 'form': 'default' if complete else 'unknown',
        'controller': '0', 'seat': '0', 'carried': '0', 'p1.carried': '0', 'p1.token': 'held',
        'p1.placed.at': 'unknown', 'p1.placed.kind': 'unknown', 'p1.placed.name': 'unknown',
        'p1.placed.form': 'unknown', 'local.token': 'held', 'placed.at': 'unknown',
        'placed.kind': 'unknown', 'placed.name': 'unknown', 'placed.form': 'unknown',
        'settled': '1' if settled else '0', 'applied': str(ack), 'direction': 'up',
        'from': '78', 'to': '67', 'result': 'handled', 'settled.ack': str(ack) if settled else 'unknown',
    }
    return picker.Sample({'name': 'tile.cursor', 'schema': 'tile.cursor/1', 'rev': rev,
                          'boot': boot, 'filtered': False, 'values': values}, 1)


def recorded_before(rev):
    state = recorded_training_tile(rev)
    ack = str(int(state.values['ack']) - 1)
    state.block['rev'] -= 1
    state.values.update({'rev': str(state.block['rev']), 'observation': str(int(state.values['observation']) - 1),
                         'ack': ack, 'applied': ack, 'settled.ack': ack, 'at': '78', 'hovered': '78',
                         'settled': '1', 'focus': 'css.grid.pickel', 'kind': '88', 'name': 'Steve',
                         'form': 'default'})
    return state


def recorded_native(rev):
    state = native(rev, screen='css.training', **{'screen.context': 'training'})
    state.block['boot'] = state.values['boot'] = '0x3d1b9bf2497aaa1f'
    return state


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

    @staticmethod
    def training_step(rev, *, ack=11, applied=11, source='78', dest='67', settled=False):
        unknown = picker.Target('unknown', 'unknown', 'unknown')
        state = tile(rev, at='67', target=unknown, ack=ack, applied=applied, settled=settled,
                     direction='up', source=source, dest=dest, seat='0')
        state.values.update({'layout': 'css.9f1fb03bf7c98fb9', 'focus': 'unknown', 'hovered': 'unknown',
                             'settled.ack': 'unknown'})
        return state

    @staticmethod
    def training_before(rev=540):
        state = tile(rev, at='78', target=picker.Target('88', 'Steve', 'default'), ack=10, seat='0')
        state.values.update({'layout': 'css.9f1fb03bf7c98fb9', 'focus': 'css.grid.pickel'})
        return state

    @staticmethod
    def training_settled():
        state = tile(551, at='67', target=picker.Target('68', 'Richter', 'default'), ack=11, seat='0',
                     direction='up', source='78', dest='67')
        state.values.update({'layout': 'css.9f1fb03bf7c98fb9', 'focus': 'css.grid.richter'})
        return state

    def training_move(self, replay, cap=.02):
        ui = native(540, screen='css.training', **{'screen.context': 'training'})
        return picker.move(replay, 'css', self.training_before(), ui, '78', 'up', '67',
                           picker.Target('68', 'Richter', 'default'), cap, expected_context='training')

    def test_training_step_waits_for_the_exact_outstanding_settlement(self):
        replay = Replay([self.training_step(543), self.training_step(548), self.training_settled()],
                        [native(551, screen='css.training', **{'screen.context': 'training'})])
        latest, _ = self.training_move(replay)
        self.assertEqual((latest.block['rev'], latest.values['name'], latest.values['settled.ack']),
                         (551, 'Richter', '11'))
        self.assertEqual(replay.actions, ['tile-up'])

    def test_hover_continues_to_mario_after_the_training_midstep(self):
        richter = picker.Target('68', 'Richter', 'default')
        topology = picker.Topology(
            layout='css.9f1fb03bf7c98fb9',
            neighbors={'78': {'up': '67'}, '67': {'down': '12'}},
            targets={'78': picker.Target('88', 'Steve', 'default'), '67': richter, '12': OTHER},
        )
        richter_idle = self.training_settled()
        richter_idle.block['rev'] = 552
        richter_idle.values.update({'rev': '552', 'observation': '552'})
        mario_step = self.training_step(553, ack=12, applied=12, source='67', dest='12')
        mario_step.values.update({'direction': 'down', 'at': '12'})
        mario = tile(556, at='12', target=OTHER, ack=12, seat='0', direction='down', source='67', dest='12')
        mario.values.update({'layout': topology.layout, 'focus': 'css.grid.mario'})
        native_blocks = [native(rev, screen='css.training', **{'screen.context': 'training'})
                         for rev in range(1, 6)]
        replay = Replay([self.training_before(539), self.training_before(), self.training_step(543),
                         self.training_step(548), self.training_settled(), richter_idle, mario_step, mario],
                        native_blocks)
        latest, _, target = picker.hover(replay, 'css', OTHER, topology=topology, cap=.1,
                                        expected_context='training')
        self.assertEqual((latest.values['name'], target), ('Mario', OTHER))
        self.assertEqual(replay.actions, ['tile-up', 'tile-down'])

    def test_unsettled_unknown_without_outstanding_ack_fails(self):
        sample = self.training_step(543, ack=10, applied=10)
        sample.values['at'] = '78'
        replay = Replay([sample])
        with self.assertRaisesRegex(RuntimeError, '^tile identity pending without the outstanding step$'):
            self.training_move(replay)
        self.assertEqual(replay.actions, ['tile-up'])

    def test_unsettled_unknown_with_wrong_step_fails(self):
        replay = Replay([self.training_step(543, source='0', dest='1')])
        with self.assertRaisesRegex(RuntimeError, 'wrong step:'):
            self.training_move(replay)
        self.assertEqual(replay.actions, ['tile-up'])

    def test_baseline_rejects_unknown_midstep(self):
        replay = Replay([self.training_step(543)],
                        [native(540, screen='css.training', **{'screen.context': 'training'})])
        with self.assertRaisesRegex(RuntimeError, 'incomplete, inactive or wrong-schema observation'):
            picker.baseline(replay, 'css', cap=.02, expected_context='training')
        self.assertEqual(replay.actions, [])

    def captured_move(self, samples, before_rev=809, cap=.02):
        replay = Replay(samples, [recorded_native(2)])
        result = picker.move(replay, 'css', recorded_before(before_rev), recorded_native(1), '78', 'up', '67',
                             picker.Target('68', 'Richter', 'default'), cap, expected_context='training')
        return replay, result

    def test_n02_settled_coordinate_waits_for_recorded_identity(self):
        replay, (latest, _) = self.captured_move([recorded_training_tile(809), recorded_training_tile(816)])
        self.assertEqual(latest.values, recorded_training_tile(816).values)
        self.assertEqual(replay.actions, ['tile-up'])

    def test_tg00_unsettled_replay_still_finishes(self):
        replay, (latest, _) = self.captured_move([recorded_training_tile(543), recorded_training_tile(551)], 543)
        self.assertEqual(latest.values, recorded_training_tile(551).values)
        self.assertEqual(replay.actions, ['tile-up'])

    def test_pending_identity_wrong_settled_coordinate_rejected(self):
        pending = recorded_training_tile(809)
        pending.values.update({'at': '68', 'hovered': '68'})
        with self.assertRaisesRegex(RuntimeError, '^settled tile coordinate disagrees with the outstanding step$'):
            self.captured_move([pending])

    def test_pending_identity_wrong_step_rejected(self):
        pending = recorded_training_tile(809)
        pending.values.update({'from': '0', 'to': '1'})
        with self.assertRaisesRegex(RuntimeError, '^wrong step:'):
            self.captured_move([pending])

    def test_pending_identity_without_ack_rejected(self):
        pending = recorded_training_tile(809)
        pending.values.update({'ack': '23', 'applied': '23', 'at': '78', 'hovered': '78', 'settled.ack': '23'})
        with self.assertRaisesRegex(RuntimeError, '^tile identity pending without the outstanding step$'):
            self.captured_move([pending])

    def test_unknown_focus_with_known_kind_remains_rejected(self):
        pending = recorded_training_tile(809)
        pending.values['kind'] = '68'
        with self.assertRaisesRegex(RuntimeError, 'incomplete, inactive or wrong-schema observation'):
            self.captured_move([pending])

    def test_pending_identity_never_completes(self):
        pending = recorded_training_tile(809)

        class RepeatPending(Replay):
            def read(self, provider, cap):
                return pending

        replay = RepeatPending()
        with self.assertRaisesRegex(RuntimeError, 'tile.cursor: no newer active observation'):
            picker.move(replay, 'css', recorded_before(809), recorded_native(1), '78', 'up', '67',
                        picker.Target('68', 'Richter', 'default'), .005, expected_context='training')
        self.assertEqual(replay.actions, ['tile-up'])
        self.assertEqual(replay.evidence, [])

    def test_baseline_rejects_settled_pending_identity(self):
        replay = Replay([recorded_training_tile(809)], [recorded_native(1)])
        with self.assertRaisesRegex(RuntimeError, 'incomplete, inactive or wrong-schema observation'):
            picker.baseline(replay, 'css', cap=.02, expected_context='training')
        self.assertEqual(replay.actions, [])

    def test_older_revision_after_pending_does_not_pass_the_fence(self):
        pending = recorded_training_tile(809)
        older = recorded_training_tile(809)
        older.block['rev'] = 808
        older.values.update({'rev': '808', 'observation': '4784'})

        class RepeatOlder(Replay):
            def __init__(self):
                super().__init__([pending, older])

            def read(self, provider, cap):
                try:
                    return super().read(provider, cap)
                except RuntimeError:
                    return older

        replay = RepeatOlder()
        with self.assertRaisesRegex(RuntimeError, 'tile.cursor: no newer active observation'):
            picker.move(replay, 'css', recorded_before(809), recorded_native(1), '78', 'up', '67',
                        picker.Target('68', 'Richter', 'default'), .005, expected_context='training')
        self.assertEqual(replay.actions, ['tile-up'])

    def test_unsettled_or_older_sample_never_completes_the_step(self):
        class RepeatingReplay(Replay):
            def __init__(self, samples):
                super().__init__(samples)
                self.last = samples[-1]

            def read(self, provider, cap):
                try:
                    return super().read(provider, cap)
                except RuntimeError:
                    return self.last

        for samples in ([self.training_step(548)], [self.training_step(548), self.training_step(543)]):
            with self.subTest(revisions=[sample.block['rev'] for sample in samples]):
                replay = RepeatingReplay(samples)
                with self.assertRaisesRegex(RuntimeError, 'tile.cursor: no newer active observation'):
                    self.training_move(replay)
                self.assertEqual(replay.actions, ['tile-up'])

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

    def test_stock_training_css_entry_uses_recorded_steve_step(self):
        import hashlib

        import routes

        path = routes.ROUTES / 'topologies/css-training-entry-topology.json'
        pin = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        topology = routes._tile_topology(pin)
        steve = picker.Target('88', 'Steve', 'default')
        self.assertEqual(topology.step('none', '0'), ('up', '78'))
        self.assertEqual(topology.targets['78'], steve)

        # Replay the off-grid local token and the measured Steve settlement and placement.
        unknown = picker.Target('unknown', 'unknown', 'unknown')
        entry = [tile(rev, at='none', target=unknown, ack=4, seat='0') for rev in (1, 2)]
        for state in entry:
            state.values.update(dict.fromkeys(
                ('ack.result', 'result', 'direction', 'from', 'to', 'settled.ack'), 'unknown'))
            state.values['focus'] = 'css.grid.entry'
        settled = [tile(rev, at='78', target=steve, ack=5, seat='0',
                        direction='up', source='none', dest='78') for rev in (3, 4)]
        for state in settled:
            state.values['focus'] = 'css.grid.steve'
        placed = tile(5, at='78', target=steve, ack=5, seat='0', token='placed', placed=steve)
        placed.values['focus'] = 'css.medal.steve'
        states = [*entry, *settled, placed]
        for state in states:
            state.values['layout'] = topology.layout

        ui = (native(rev, screen='css.training', focus='css.training',
                     **{'screen.context': 'training'}) for rev in range(1, 10))
        replay = Replay(states, ui)
        result = picker.pick(replay, 'css', steve, topology=topology, cap=.05, expected_context='training')
        self.assertEqual(replay.actions, ['tile-up', 'tap-a1'])
        self.assertEqual({key: replay.evidence[0][key] for key in ('from', 'to', 'kind', 'settled_ack')},
                         {'from': 'none', 'to': '78', 'kind': '88', 'settled_ack': 5})
        self.assertEqual((result['tile'], result['name'], result['placed']), ('78', 'Steve', True))

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

    def test_css_confirm_waits_for_focus_after_the_local_medal_is_placed(self):
        # Exact r6: CSS keeps its active layout and ACK 2. Steve is already
        # placed at 78 in rev 60, while focus is still unknown. Rev 61 shows
        # the placed medal owns focus.
        steve = picker.Target('88', 'Steve', 'default')
        before = [tile(rev, at='78', target=steve, seat='0', ack=2) for rev in (58, 59)]
        pending = tile(60, at='78', target=steve, seat='0', token='placed', placed=steve, ack=2)
        pending.values['focus'] = 'unknown'
        active = tile(61, at='78', target=steve, seat='0', token='placed', placed=steve, ack=2)
        active.values['focus'] = 'css.medal.steve'
        replay = Replay([*before, pending, active])
        self.assertTrue(self.run_pick(replay, target=steve, topology=None)['placed'])
        self.assertEqual(replay.actions, ['tap-a1'])

        wrong_medal = tile(60, at='78', target=steve, seat='0', token='placed', placed=OTHER, ack=2)
        wrong_medal.values['focus'] = 'unknown'
        for samples, error in (([pending], 'tile.cursor missing'),
                               ([wrong_medal, active], 'does not match the chosen fighter')):
            with self.subTest(error=error):
                replay = Replay([*before, *samples])
                with self.assertRaisesRegex(RuntimeError, error):
                    self.run_pick(replay, target=steve, topology=None)
                self.assertEqual(replay.actions, ['tap-a1'])

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


    def test_smash_stage_confirmation_waits_for_the_destination_owner(self):
        target = picker.Target('308', 'Training', 'normal')

        def stage_tile(rev):
            return snapshot('tile.cursor', rev, {
                'screen': 'sss', 'layout': 'stage-layout', 'generation': '3',
                'controller': '0', 'seat': '0', 'carried': 'none',
                'local.token': 'none', 'at': '8', 'hovered': '8', 'settled': '1',
                'kind': target.kind, 'name': target.name, 'form': target.form, 'settled.ack': '0',
                'ack': '0', 'applied': '0', 'focus': 'sss/8',
            })

        source = [native(rev, screen='stage_select', focus='stage_select') for rev in (842, 843)]
        source_pending = native(844, screen='stage_select', focus='stage_select',
                                **{'input.owner': 'unknown', 'dialog': 'unknown'})
        css_pending = native(847, screen='css.smash', focus='css.smash',
                             **{'input.owner': 'unknown', 'dialog': 'unknown'})
        css_ready = native(870, screen='css.smash', focus='css.smash')

        def replay(*tail):
            return Replay([stage_tile(841), stage_tile(842)], [*source, *tail])

        io = replay(source_pending, css_pending, css_ready)
        result = picker.pick(io, 'sss', target, next_screen='css.smash', cap=.1)
        self.assertEqual(result['screen'], 'css.smash')
        self.assertEqual(io.actions, ['tap-a1'])

        for owner, dialog in (('foreign', 'unknown'), ('native', 'confirmation'),
                              ('unknown', 'none'), ('native', 'unknown')):
            with self.subTest(owner=owner, dialog=dialog):
                io = replay(native(847, screen='css.smash', focus='css.smash',
                                   **{'input.owner': owner, 'dialog': dialog}))
                with self.assertRaisesRegex(RuntimeError, f'css.smash is owned by {owner!r} with dialog {dialog!r}'):
                    picker.pick(io, 'sss', target, next_screen='css.smash', cap=.1)
                self.assertEqual(io.actions, ['tap-a1'])

        io = replay(native(847, screen='rules', focus='rules'))
        with self.assertRaisesRegex(RuntimeError, "after stage confirmation, unexpected native screen 'rules'"):
            picker.pick(io, 'sss', target, next_screen='css.smash', cap=.1)
        self.assertEqual(io.actions, ['tap-a1'])

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
            return native(rev, screen='stage_select', focus='stage_select/8')

        target = picker.Target('308', 'Training', 'normal')
        replay = Replay([stage_tile(1), stage_tile(2)],
                        [stage_ui(1), stage_ui(2), native(3, focus='css.smash/ready')])
        result = picker.pick(replay, 'sss', target, next_screen='css.smash', cap=.1)
        self.assertEqual(replay.actions, ['tap-a1'])
        self.assertEqual(result['screen'], 'css.smash')

        source = [native(i, screen='stage_select', focus='stage_select/8',
                         **{'screen.context': 'training'}) for i in (1, 2)]
        css = {'screen': 'css.training', 'focus': 'css.training', 'screen.context': 'training'}
        passive = native(4, **{**css, 'input.owner': 'unknown', 'dialog': 'unknown'})
        replay = Replay([stage_tile(1), stage_tile(2)], [*source, loading(3), passive, native(5, **css)])
        result = picker.pick(replay, 'sss', target, expected_context='training', cap=.1)
        self.assertEqual(replay.actions, ['tap-a1'])
        self.assertEqual(result['screen'], 'css.training')

        replay = Replay([stage_tile(1), stage_tile(2)],
                        [stage_ui(1), stage_ui(2), native(3, screen='rules', focus='rules')])
        with self.assertRaisesRegex(RuntimeError, 'unexpected native screen'):
            picker.pick(replay, 'sss', target, next_screen='css.smash', cap=.1)
        self.assertEqual(replay.actions, ['tap-a1'])

    def test_stage_to_training_css_waits_through_passive_stage_owner(self):
        import time

        # Native provider rev 2971 after the single stage-confirm A kept the
        # exact Training Stage Select identity but withdrew input ownership.
        source = {'screen': 'stage_select', 'focus': 'stage_select', 'screen.context': 'training',
                  'focus.source': 'tile.cursor', 'dialog': 'none', 'input.owner': 'native'}
        target = {**source, 'screen': 'css.training', 'focus': 'css.training'}
        before = native(2969, **source)
        passive = {**source, 'dialog': 'unknown', 'input.owner': 'unknown'}
        observed = native(2973, **target)
        replay = Replay(native_blocks=[native(2971, **passive), native(2972, **passive), observed])

        self.assertEqual(
            picker.wait_scene_transition(replay, before, time.monotonic() + 1, source, target, label='stage'),
            observed,
        )
        self.assertEqual(replay.actions, [])
        for bad in ({'dialog': 'notice'}, {'input.owner': 'tr.menu'},
                    {'screen.context': 'local_wireless'}, {'focus.source': 'native'}):
            with self.subTest(fields=bad):
                replay = Replay(native_blocks=[native(2971, **{**passive, **bad}), observed])
                with self.assertRaisesRegex(RuntimeError, 'unexpected stage focus'):
                    picker.wait_scene_transition(
                        replay, before, time.monotonic() + 1, source, target, label='stage',
                    )
                self.assertEqual(replay.actions, [])

    def test_remote_token_is_never_placed(self):
        replay = Replay([tile(1, at='16', target=TARGET, seat='1', token='unknown')])
        with self.assertRaisesRegex(RuntimeError, 'token ownership'):
            self.run_pick(replay)
        self.assertEqual(replay.actions, [])

    def test_nonempty_or_missing_native_dialog_sends_no_tile_input(self):
        for values in ({'dialog': 'notice'}, {'dialog': 'unknown'}, {}):
            with self.subTest(values=values):
                focus = native(1, **values)
                if not values:
                    focus.values.pop('dialog')
                replay = Replay([tile(1)], [focus])
                with self.assertRaisesRegex(RuntimeError, 'dialog'):
                    self.run_pick(replay)
                self.assertEqual(replay.actions, [])

    def test_training_context_is_required_for_a_pinned_training_picker(self):
        for screen, context in (('css.training', 'unknown'), ('css.smash', 'training')):
            with self.subTest(screen=screen, context=context):
                focus = native(1, screen=screen, **{'screen.context': context})
                replay = Replay([tile(1)], [focus])
                with self.assertRaisesRegex(RuntimeError, 'training context'):
                    picker.hover(replay, 'css', TARGET, GRAPH, expected_context='training')
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

        def wire(rev, *, filtered=False, torn=False, crc_valid=True, include_end=True, frame_boot=boot):
            payload = [f'schema={schema}', f'rev={rev}', f'boot={boot}', 'ack=0',
                       'ack.result=unsupported', 'focus=main_menu/top/smash',
                       f'observation={rev}', 'observation.active=yes']
            begin = (f'[Bridge] state begin name={provider} schema={schema} rev={rev} boot={frame_boot} '
                     f'lines={len(payload)} filtered={int(filtered)}\n')
            body = ''.join(f'[Bridge] state.{i} {line}\n' for i, line in enumerate(payload))
            crc = zlib.crc32(chr(10).join(payload).encode())
            if not crc_valid:
                crc ^= 1
            end = f'[Bridge] state end name={provider} rev={rev} crc={crc:08x}\n' if include_end else ''
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

            fence = f'[Bridge] req providers result=ok count=2 boot={boot}\n'
            for bad_reply, reason in ((wire(56, crc_valid=False), 'malformed'),
                                      (wire(56, include_end=False), 'overlapping'),
                                      (wire(56, frame_boot='0x0000000000000002'), 'boot')):
                with self.subTest(reason=reason):
                    def after_press(_ctx, *_requests):
                        with log.open('a') as dest:
                            dest.write(fence + wire(55, filtered=True) + bad_reply + wire(57))

                    with (patch.object(routes, 'play'), patch.object(routes, 'bridge_v2', after_press)):
                        bridge.send('tap-a1')
                        try:
                            with self.assertRaisesRegex(RuntimeError, reason):
                                bridge.read(provider, 1)
                        finally:
                            bridge.finish()

    stale_ui_fence = (
        '[Bridge] provider name=ssbu.ui schema_id=2 version=1 rev=4 dropped=0 toggle_gen=1 frame=24 wall=198155737\n'
        '[Bridge] req providers result=ok count=4 boot=0xc60856d64562c887 frame=24 wall=198155738\n'
    )
    stale_ui_reply = (
        '[Bridge] state name=ssbu.ui result=empty reason=stale_toggle '
        'boot=0xc60856d64562c887 frame=24 wall=198155750\n'
    )

    def test_captured_stale_ui_reply_requests_a_fresh_read(self):
        import zlib

        payload = 'focus=title.press_any_button'
        next_reply = (
            '[Bridge] state begin name=ssbu.ui schema=ssbu.ui/1 rev=5 boot=0xc60856d64562c887 lines=1 filtered=0\n'
            f'[Bridge] state.0 {payload}\n'
            f'[Bridge] state end name=ssbu.ui rev=5 crc={zlib.crc32(payload.encode()):08x}\n'
        )

        def check(bridge, clock):
            result = bridge.read('ssbu.ui', .24)
            self.assertEqual(result.block['rev'], 5)
            self.assertEqual(result.block['values']['focus'], 'title.press_any_button')
            self.assertEqual(result.connection, 0)
            self.assertLess(clock.monotonic(), .24)

        self.assertEqual(self.replay_ui_exchange(
            check, lambda count: self.stale_ui_fence + (self.stale_ui_reply if count == 1 else next_reply),
        ), [0, .1])
        replayed = self.stale_ui_fence + next_reply.replace('rev=5', 'rev=4')
        self.assertEqual(self.replay_ui_exchange(
            check, lambda count: self.stale_ui_fence + (
                self.stale_ui_reply + replayed if count == 1 else next_reply
            ),
        ), [0, .1])

    def test_stale_ui_retries_share_the_absolute_deadline(self):
        def check(bridge, clock):
            with self.assertRaisesRegex(RuntimeError, 'no fenced bridge reply for state ssbu.ui'):
                bridge.read('ssbu.ui', .24)
            self.assertAlmostEqual(clock.monotonic(), .24)

        self.assertEqual(self.replay_ui_exchange(
            check, lambda _: self.stale_ui_fence + self.stale_ui_reply,
        ), [0, .1, .2])

    def test_unfenced_malformed_or_other_provider_empty_is_not_retried(self):
        other_boot = self.stale_ui_reply.replace('0xc60856d64562c887', '0x0000000000000002')
        for provider, reply in (
            ('ssbu.ui', self.stale_ui_reply),
            ('ssbu.ui', self.stale_ui_fence + other_boot),
            ('ssbu.ui', self.stale_ui_fence + self.stale_ui_reply.replace('stale_toggle', 'unknown')),
            ('ssbu.ui', self.stale_ui_fence + self.stale_ui_reply.replace('result=empty', 'result=refused')),
            ('ssbu.ui', self.stale_ui_fence + self.stale_ui_reply.replace('frame=24', 'extra=1 frame=24')),
            ('tile.cursor', self.stale_ui_fence + self.stale_ui_reply.replace('ssbu.ui', 'tile.cursor')),
        ):
            with self.subTest(provider=provider, reply=reply):
                def check(bridge, _clock):
                    with self.assertRaisesRegex(RuntimeError, f'no fenced bridge reply for state {provider}'):
                        bridge.read(provider, .24)

                self.assertEqual(self.replay_ui_exchange(check, lambda _: reply, provider=provider), [0])

    def test_stale_ui_retry_rejects_epoch_changes(self):
        reconnect = '[Adapter] reconnect same_boot=1\n'
        gap = '[Bridge] since result=gap cursor=4 oldest=8 latest=12 boot=0xc60856d64562c887\n'
        changed_boot = self.stale_ui_fence.replace('0xc60856d64562c887', '0x0000000000000002')
        changed_gen = self.stale_ui_fence.replace('toggle_gen=1', 'toggle_gen=2')
        no_generation = self.stale_ui_fence.splitlines(keepends=True)[1]
        stale = self.stale_ui_fence + self.stale_ui_reply

        for first, second, between, reason, expected in (
            (stale + reconnect, '', '', 'connection or replay continuity', [0]),
            (stale, '', gap, 'connection or replay continuity', [0]),
            (stale, changed_boot, '', 'bridge boot changed', [0, .1]),
            (stale, changed_gen, '', 'bridge toggle generation changed', [0, .1]),
            (stale, no_generation, '', 'bridge toggle generation changed', [0, .1]),
        ):
            with self.subTest(reason=reason, retry_after_send=len(expected) > 1):
                def check(bridge, _clock):
                    with self.assertRaisesRegex(RuntimeError, reason):
                        bridge.read('ssbu.ui', .24)

                def during_sleep(log, _elapsed):
                    if between:
                        with log.open('a') as dest:
                            dest.write(between)

                self.assertEqual(self.replay_ui_exchange(
                    check, lambda count: first if count == 1 else second, during_sleep=during_sleep,
                ), expected)

    def replay_ui_exchange(self, check, response, *, provider='ssbu.ui', during_sleep=None):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import patch

        import native_picker_io
        import routes

        elapsed = 0.0
        sent = []
        with TemporaryDirectory() as temp:
            log = Path(temp, 'bridge.log')
            log.touch()
            ctx = SimpleNamespace(instance={'bridge_log': str(log)}, check_stop=lambda: None,
                                  runner=SimpleNamespace(), on='sw')
            bridge = native_picker_io.BridgePickerIO(ctx)

            def sleep(seconds):
                nonlocal elapsed
                elapsed += seconds
                if during_sleep:
                    during_sleep(log, elapsed)

            def spool(_ctx, *requests):
                self.assertEqual(requests, ('providers', f'state {provider}'))
                sent.append(elapsed)
                with log.open('a') as dest:
                    dest.write(response(len(sent)))

            clock = SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep)
            with (patch.object(native_picker_io, 'time', clock),
                  patch.object(routes, 'bridge_v2', spool),
                  patch.object(routes, 'play') as play):
                check(bridge, clock)
                play.assert_not_called()
        return sent

    def test_arm_requires_a_one_block_watch(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import patch

        import routes
        from native_picker_io import BridgePickerIO

        boot = '0x0000000000000001'
        with TemporaryDirectory() as temp:
            log = Path(temp, 'bridge.log')
            log.touch()
            ctx = SimpleNamespace(instance={'bridge_log': str(log)}, check_stop=lambda: None,
                                  runner=SimpleNamespace(), on='sw')
            for provider in ('ssbu.ui', 'tile.cursor'):
                for maximum in (1, 4294967295):
                    with self.subTest(provider=provider, maximum=maximum):
                        def spool(_ctx, *requests):
                            self.assertEqual(requests, ('providers', f'state_watch {provider} keys=observation max=1'))
                            with log.open('a') as dest:
                                dest.write(f'[Bridge] req providers result=ok count=2 boot={boot}\n'
                                           f'[Bridge] req state_watch name={provider} result=ok keys=1 max={maximum}\n')

                        bridge = BridgePickerIO(ctx)
                        with patch.object(routes, 'bridge_v2', spool):
                            if maximum == 1:
                                bridge.arm(provider, 1)
                                self.assertEqual(bridge.boot, boot)
                            else:
                                with self.assertRaisesRegex(RuntimeError, 'watch limit'):
                                    bridge.arm(provider, 1)

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

    def run_training(self, replay, finish, *, expected=0, before_press='',
                     refreshed_context='training', after_owner='native', after_dialog='none',
                     scene_loading=False, press=True):
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
                native(1, screen='css.training', focus='css.training', **{'screen.context': 'training'}),
                native(2, screen='css.training', focus='css.training',
                       **{'screen.context': refreshed_context}),
                *([loading(3), native(4, screen='training', focus='training', dialog='unknown',
                                       **{'screen.context': 'training', 'input.owner': 'unknown'})]
                  if scene_loading else []),
                native(5 if scene_loading else 3, screen='training', focus='training', dialog=after_dialog,
                       **{'screen.context': 'training', 'input.owner': after_owner}),
            ])
            placed = picker.Target('88', 'Steve', 'default')
            tiles = iter(tile(i, at='78', target=placed, seat='0', token='placed', placed=placed)
                         for i in (1, 2))
            actions = []
            requests = []

            def spool(_ctx, fence, request):
                self.assertEqual(fence, 'providers')
                requests.append(request)
                if request == 'state ssbu.ui':
                    response = frame(next(ui_samples))
                elif request == 'state tile.cursor':
                    response = frame(next(tiles))
                elif request == 'state_watch ssbu.ui keys=observation max=1':
                    response = '[Bridge] req state_watch name=ssbu.ui result=ok keys=1 max=1\n'
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

            def start(_ctx, name):
                actions.append(name)
                self.assertEqual(requests, [
                    'state ssbu.ui', 'state tile.cursor', 'state_watch ssbu.ui keys=observation max=1',
                    'state ssbu.ui', 'state tile.cursor', 'events on',
                ])

            with (patch.object(routes, 'bridge_v2', spool),
                  patch.object(routes, 'play', side_effect=start),
                  patch.object(routes, 'native_state', return_value={'stage': expected}),
                  patch.object(routes, 'expected', return_value={'stage_id': expected}),
                  patch.object(routes, '_save', side_effect=lambda _ctx, _name, value: value)):
                try:
                    return routes.enter_training(ctx, cap=.05)
                finally:
                    self.assertEqual(actions, ['enter-training'] if press else [])

    def ending(self, last=41, *, boot=None):
        return (f'[Bridge] since result=ok cursor=41 from=42 oldest=1 latest={last}'
                f' count={last - 41} boot={boot or self.boot} frame=730 wall=10200')

    def test_training_accepts_fresh_battlefield_event(self):
        stage = '[Bridge] stage id=0 ev=42 frame=720 wall=10040\n'
        result = self.run_training(stage, self.ending(42))
        self.assertEqual(result['stage_id'], 0)
        self.assertEqual(result['stage_event'], {'sequence': 42, 'frame': 720, 'wall_ms': 10040,
                                                'boot': self.boot})

    def test_training_plus_requires_the_refreshed_training_context(self):
        with self.assertRaisesRegex(RuntimeError, 'training context'):
            self.run_training('', self.ending(), refreshed_context='unknown', press=False)

    def test_training_plus_accepts_the_loaded_scene_and_independent_stage_event(self):
        stage = '[Bridge] stage id=0 ev=42 frame=720 wall=10040\n'
        self.assertEqual(self.run_training(stage, self.ending(42), scene_loading=True)['stage_id'], 0)

    def test_training_plus_refuses_gameplay_with_non_native_owner_or_with_modal(self):
        for fields in ({'after_owner': 'overlay'}, {'after_dialog': 'notice'}):
            with self.subTest(fields=fields), self.assertRaisesRegex(RuntimeError, 'unexpected native focus'):
                self.run_training('', self.ending(), **fields)

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
            (native(2, observation='1'), 'no newer active observation|no fenced bridge reply for state ssbu.ui'),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(RuntimeError, reason):
                    self.run_gate('switch://fixture', 'css', [native(1), latest], cap=.15)

    def test_eden_navigation_gate_requires_a_bridge_endpoint(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        import routes

        ctx = SimpleNamespace(instance={'socket': '/tmp/eden-fixture.sock'},
                              runner=SimpleNamespace(plan={}), gate=Mock(return_value='standalone.png'))
        with self.assertRaisesRegex(RuntimeError, 'bridge_log'):
            routes.gate(ctx, 'css')
        ctx.gate.assert_not_called()

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

class TrainingSubmenuRouteTests(unittest.TestCase):
    chain = ('games_and_more', 'classic_mode', 'mob_smash', 'training', 'stage_select')
    actions = ('tap-a1', 'main-menu-down', 'main-menu-left', 'tap-a1')

    @staticmethod
    def checkpoint(rev, focus):
        if focus == 'stage_select':
            return native(rev, screen='stage_select', focus='stage_select', dialog='none',
                          **{'screen.context': 'training'})
        if focus.startswith('stage_pending:'):
            context = focus.partition(':')[2]
            return native(rev, screen='stage_select', focus='stage_select', dialog='unknown', **{
                'screen.context': context, 'input.owner': 'unknown',
                **dict.fromkeys(('focus.source', 'focus.id', 'focus.index', 'focus.label'), 'unknown'),
            })
        if focus == 'submenu_pending':
            # v9 rev 3361 confirmed the new layer before it had any item focus.
            return native(rev, screen='main_menu', focus='main_menu.submenu', **{
                'screen.tab': 'games_and_more', 'screen.layer': 'submenu',
                **dict.fromkeys(('focus.source', 'focus.id', 'focus.index', 'focus.label',
                                 'input.owner', 'dialog'), 'unknown'),
            })
        layer = 'top' if focus == 'games_and_more' else 'submenu'
        return native(rev, screen='main_menu', focus=f'main_menu.{layer}.{focus}', dialog='none',
                      **{'screen.tab': 'games_and_more', 'screen.layer': layer,
                         'focus.id': focus, 'focus.source': 'native'})

    def run_route(self, io, start, *, changes=(), loading_frames=(), tile_state=None, submenu_loading=False,
                  submenu_rearm=False, submenu_pending=False, tile_samples=None, stage_pending=None):
        from types import SimpleNamespace
        from unittest.mock import patch

        import routes

        focuses = self.chain[start:]
        frames = [focuses[0]]
        for source, target in zip(focuses, focuses[1:]):
            frames.extend((source, target))
        frames.append('stage_select')
        if stage_pending is not None:
            frames.insert(-2, f'stage_pending:{stage_pending}')
        if submenu_pending:
            frames[2:2] = [None, 'submenu_pending', None, 'submenu_pending']
        if submenu_loading:
            frames.insert(2, None)
        if submenu_rearm:
            frames.insert(3 + int(submenu_loading), None)
        samples = [self.checkpoint(i, focus) if focus else loading(i)
                   for i, focus in enumerate(frames, 1)]
        for index, state in changes:
            samples[index] = state
        samples[-2:-2] = loading_frames
        ui = iter(samples)
        active_tile = self.stage_tile(10)
        tiles = iter(tile_samples) if tile_samples is not None else None
        if tile_state is None:
            tile_state = active_tile

        def respond(provider, _cap):
            if provider == 'ssbu.ui':
                return next(ui)
            self.assertEqual(provider, 'tile.cursor')
            if isinstance(tile_state, RuntimeError):
                raise tile_state
            return next(tiles) if tiles is not None else tile_state

        io.connection = 1
        io.boot = samples[0].clock[0]
        io.read.side_effect = respond
        ctx = SimpleNamespace(instance={'bridge_log': 'fixture'})
        with patch.object(routes, 'BridgePickerIO', return_value=io):
            return routes.main_to_stage(ctx)

    @staticmethod
    def stage_tile(rev, epoch=1, **overrides):
        grid = 'sss.9892b3a32e5c0164'
        return snapshot('tile.cursor', rev, {
            'screen': 'sss', 'focus': 'sss.grid.normal', 'layout': grid, 'grid': grid,
            'at': '0', 'hovered': '0', 'settled': '1', 'generation': '2', 'ack': '0',
            'applied': '0', 'controller': '0', 'seat': '0', 'carried': '0', 'local.token': 'held',
            'kind': '308', 'name': 'Training', 'form': 'normal',
            **dict.fromkeys(('ack.result', 'result', 'direction', 'from', 'to', 'settled.ack'), 'unknown'),
            **overrides,
        }, epoch)

    @classmethod
    def entering_tile(cls, rev=4, **overrides):
        # Final XPS reply at frame 1364 followed native Stage Select ownership.
        unknown = dict.fromkeys(('focus', 'layout', 'hovered', 'kind', 'name', 'form',
                                 'controller', 'seat', 'carried', 'local.token'), 'unknown')
        return cls.stage_tile(rev, **{**unknown, 'settled': '0', 'observation': '18', **overrides})

    def test_stage_arrival_waits_for_qualified_tile_with_no_more_input(self):
        from unittest.mock import Mock

        io = Mock()
        pending = self.entering_tile()
        ready = self.stage_tile(5, observation='20')
        self.assertEqual(self.run_route(io, 3, tile_samples=(pending, pending, ready)),
                         {'screen': 'stage', 'proof': 'stage_select'})
        self.assertEqual([c.args[0] for c in io.read.call_args_list][-4:],
                         ['tile.cursor', 'tile.cursor', 'tile.cursor', 'ssbu.ui'])
        io.send.assert_called_once_with('tap-a1')

    def test_stage_shell_waits_for_training_context_and_owner_with_no_more_input(self):
        from unittest.mock import Mock

        for context in ('unknown', 'training'):
            with self.subTest(context=context):
                io = Mock()
                self.assertEqual(self.run_route(io, 3, stage_pending=context),
                                 {'screen': 'stage', 'proof': 'stage_select'})
                io.send.assert_called_once_with('tap-a1')

    def test_stage_shell_refuses_conflicting_smash_context(self):
        from unittest.mock import Mock

        io = Mock()
        with self.assertRaisesRegex(RuntimeError, 'expected'):
            self.run_route(io, 3, stage_pending='smash')
        io.send.assert_called_once_with('tap-a1')

    def test_stage_arrival_refuses_changed_connection_or_stale_tile_while_waiting(self):
        from unittest.mock import Mock

        pending = self.entering_tile()
        for sample, error in (
            (self.stage_tile(5, epoch=2, observation='20'), 'connection'),
            (self.stage_tile(5, observation='17'), 'stale'),
            (self.stage_tile(5, observation='20', layout='unknown'), 'no qualified active tile layout'),
        ):
            with self.subTest(error=error):
                io = Mock()
                with self.assertRaisesRegex(RuntimeError, error):
                    self.run_route(io, 3, tile_samples=(pending, sample))
                io.send.assert_called_once_with('tap-a1')
                io.finish.assert_not_called()

    def test_cold_route_and_each_supported_submenu_checkpoint(self):
        from unittest.mock import Mock, call

        for start, focus in enumerate(self.chain[:-1]):
            with self.subTest(focus=focus):
                io = Mock()
                self.assertEqual(self.run_route(io, start), {'screen': 'stage', 'proof': 'stage_select'})
                self.assertEqual(io.send.call_args_list, [call(action) for action in self.actions[start:]])
                expected = ['read']
                for _ in self.actions[start:-1]:
                    expected.extend(('arm', 'read', 'send', 'read', 'finish'))
                expected.extend(('arm', 'read', 'send', 'read', 'read', 'read', 'finish'))
                self.assertEqual([event[0] for event in io.mock_calls], expected)
                self.assertTrue(all(watch.args[0] == 'ssbu.ui' for watch in io.arm.call_args_list))

    def test_games_more_open_waits_for_inactive_then_fresh_classic_mode(self):
        from unittest.mock import Mock, call

        # Exact v3 release rev 220 was fully inactive after the one A press.
        # The same boot later published native Classic Mode focus.
        io = Mock()
        self.assertEqual(self.run_route(io, 0, submenu_loading=True),
                         {'screen': 'stage', 'proof': 'stage_select'})
        self.assertEqual(io.send.call_args_list, [call(action) for action in self.actions])

    def test_games_more_submenu_shell_waits_for_fresh_classic_mode(self):
        from unittest.mock import Mock, call

        io = Mock()
        self.assertEqual(self.run_route(io, 0, submenu_pending=True),
                         {'screen': 'stage', 'proof': 'stage_select'})
        self.assertEqual(io.send.call_args_list, [call(action) for action in self.actions])
        first, second = [i for i, event in enumerate(io.mock_calls) if event[0] == 'send'][:2]
        self.assertEqual([event[0] for event in io.mock_calls[first+1:second]],
                         ['read', 'read', 'read', 'read', 'read', 'finish', 'arm', 'read'])

    def test_games_more_does_not_wait_through_conflicting_submenu_state(self):
        from unittest.mock import Mock

        for key, value in (('screen.tab', 'smash'), ('screen.layer', 'top'),
                           ('focus.id', 'training'), ('focus.index', '2'), ('dialog', 'notice')):
            with self.subTest(key=key):
                shell = self.checkpoint(4, 'submenu_pending')
                shell.values[key] = value
                io = Mock()
                with self.assertRaisesRegex(RuntimeError, 'unexpected Main Menu focus'):
                    self.run_route(io, 0, submenu_pending=True, changes=((3, shell),))
                io.send.assert_called_once_with('tap-a1')
                io.finish.assert_not_called()

    def test_submenu_rearm_requires_fresh_classic_mode_after_inactivity(self):
        from unittest.mock import Mock, call

        # XPS r5: inactive rev 176 -> Classic Mode rev 177 after A.
        # The next watch's source read reached inactive rev 179, then the
        # same native Classic Mode focus recovered at rev 180.
        io = Mock()
        self.assertEqual(self.run_route(io, 0, submenu_loading=True, submenu_rearm=True),
                         {'screen': 'stage', 'proof': 'stage_select'})
        self.assertEqual(io.send.call_args_list, [call(action) for action in self.actions])
        first, second = [i for i, event in enumerate(io.mock_calls) if event[0] == 'send'][:2]
        self.assertEqual([item[0] for item in io.mock_calls[first+1:second]],
                         ['read', 'read', 'finish', 'arm', 'read', 'read'])

        contradictory = loading(5)
        contradictory.values['dialog'] = 'settings'
        for replacement, error in (
            ((4, contradictory), 'inactive'),
            ((5, self.checkpoint(6, 'training')), 'source changed'),
        ):
            with self.subTest(error=error):
                io = Mock()
                with self.assertRaisesRegex(RuntimeError, error):
                    self.run_route(io, 0, submenu_loading=True, submenu_rearm=True,
                                   changes=(replacement,))
                io.send.assert_called_once_with('tap-a1')

    def test_unknown_or_wrong_start_sends_no_input(self):
        from unittest.mock import Mock

        wrong = native(1, screen='main_menu', focus='main_menu.submenu.homerun',
                       dialog='none', **{'screen.tab': 'games_and_more', 'screen.layer': 'submenu',
                                         'focus.id': 'homerun', 'focus.source': 'native'})
        unknown = self.checkpoint(1, 'classic_mode')
        unknown.values['input.owner'] = 'unknown'
        for state in (wrong, unknown):
            with self.subTest(values=state.values):
                io = Mock()
                with self.assertRaisesRegex(RuntimeError, 'source'):
                    self.run_route(io, 1, changes=((0, state),))
                io.arm.assert_not_called()
                io.send.assert_not_called()

    def test_divergent_destination_stops_without_following_input(self):
        from unittest.mock import Mock, call

        for start, wrong, sent in ((0, 'mob_smash', 'tap-a1'), (1, 'training', 'main-menu-down'),
                                   (2, 'classic_mode', 'main-menu-left'), (3, 'classic_mode', 'tap-a1')):
            with self.subTest(start=self.chain[start], wrong=wrong):
                io = Mock()
                with self.assertRaisesRegex(RuntimeError, f'unexpected.*{wrong}'):
                    self.run_route(io, start, changes=((2, self.checkpoint(3, wrong)),))
                self.assertEqual(io.send.call_args_list, [call(sent)])
                io.finish.assert_not_called()
                self.assertEqual(io.read.call_count, 3)

    def test_failed_arm_or_reconnect_blocks_next_menu_input(self):
        from unittest.mock import Mock

        for mode in ('arm', 'connection'):
            with self.subTest(mode=mode):
                io = Mock()
                if mode == 'arm':
                    io.arm.side_effect = (None, RuntimeError('watch failed'))
                else:
                    def reconnect(_provider, _cap):
                        if io.arm.call_count == 2:
                            io.connection += 1

                    io.arm.side_effect = reconnect
                with self.assertRaisesRegex(RuntimeError, 'watch failed|connection'):
                    self.run_route(io, 0)
                io.send.assert_called_once_with('tap-a1')
                io.finish.assert_called_once()

    def test_stage_arrival_rejects_other_parent_or_owner_loss_during_tile_check(self):
        from unittest.mock import Mock

        wrong_parent = self.checkpoint(3, 'stage_select')
        wrong_parent.values['screen.context'] = 'local_wireless'
        lost_owner = self.checkpoint(4, 'stage_select')
        lost_owner.values['input.owner'] = 'unknown'
        for index, state, reason in (
            (2, wrong_parent, 'screen.context=local_wireless'),
            (3, lost_owner, 'Stage Select ownership changed'),
        ):
            with self.subTest(phase=index):
                io = Mock()
                with self.assertRaisesRegex(RuntimeError, reason):
                    self.run_route(io, 3, changes=((index, state),))
                io.send.assert_called_once_with('tap-a1')
                io.finish.assert_not_called()

    def test_submenu_confirm_waits_for_inactive_then_passive_stage(self):
        from unittest.mock import Mock

        io = Mock()
        selected = self.checkpoint(5, 'stage_select')
        renewed = self.checkpoint(6, 'stage_select')
        passive = native(4, screen='stage_select', focus='stage_select', dialog='unknown',
                         **{'screen.context': 'training', 'input.owner': 'unknown'})
        result = self.run_route(io, 3, changes=((2, selected), (3, renewed)),
                                loading_frames=(loading(3), passive))
        self.assertEqual(result['proof'], 'stage_select')
        io.send.assert_called_once_with('tap-a1')

    def test_stage_arrival_stays_pending_without_matching_tile_provider(self):
        from unittest.mock import Mock

        for block, error in (
            (RuntimeError('tile.cursor unavailable'), 'tile.cursor unavailable'),
            (snapshot('tile.cursor', 10, {'screen': 'css'}), 'tile.cursor.*sss'),
            (snapshot('tile.cursor', 10, {'screen': 'sss'}, epoch=2), 'connection'),
        ):
            with self.subTest(error=error):
                io = Mock()
                with self.assertRaisesRegex(RuntimeError, error):
                    self.run_route(io, 3, tile_state=block)
                io.send.assert_called_once_with('tap-a1')
                io.finish.assert_not_called()

class NativeQuitTrainingTests(unittest.TestCase):
    actions = ('pause-open', 'focus-native-quit-frames', 'confirm-native-quit-frames')

    @staticmethod
    def state(rev, screen, focus=None, **changes):
        values = {'screen': screen, 'screen.context': 'training', 'input.owner': 'native',
                  'dialog': 'none', 'focus': screen if focus is None else f'{screen}.{focus}'}
        if focus is not None:
            values.update({'focus.id': focus, 'focus.source': 'native'})
        if screen == 'css.training':
            values['focus.source'] = 'tile.cursor'
        return native(rev, **{**values, **changes})

    def setUp(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        import routes

        self.ctx = SimpleNamespace()
        self.io = Mock(connection=1, boot=self.state(1, 'training').clock[0])
        adapter = patch.object(routes, 'BridgePickerIO', return_value=self.io)
        save = patch.object(routes, '_save', side_effect=lambda _ctx, _name, result: result)
        self.addCleanup(adapter.stop)
        self.addCleanup(save.stop)
        adapter.start()
        save.start()
        self.routes = routes

    def run_quit(self, *, changes=(), loading_frames=()):
        samples = [self.state(rev, screen, focus) for rev, (screen, focus) in enumerate(
            [('training', None), ('training', None), ('pause', 'cpu_count'),
             ('pause', 'cpu_count'), ('pause', 'quit_training'), ('pause', 'quit_training'),
             ('css.training', None)], 1)]
        for index, state in changes:
            samples[index] = state
        samples[-1:-1] = loading_frames
        self.io.read.side_effect = samples
        return self.routes.quit_training(self.ctx, cap=.25)

    def test_exact_three_watched_edges_end_at_fresh_native_training_css(self):
        from unittest.mock import call

        result = self.run_quit()
        self.assertEqual(self.io.send.call_args_list, [call(action) for action in self.actions])
        self.assertEqual([event[0] for event in self.io.mock_calls],
                         ['read'] + ['arm', 'read', 'send', 'read', 'finish'] * 3)
        self.assertTrue(all(call.args[0] == 'ssbu.ui' for call in self.io.arm.call_args_list))
        self.assertEqual((result['screen'], result['observation']), ('css.training', 7))

    def test_quit_waits_through_a_fenced_inactive_interval(self):
        result = self.run_quit(changes=((6, self.state(8, 'css.training')),), loading_frames=(loading(7),))
        self.assertEqual((result['screen'], result['observation']), ('css.training', 8))
        self.assertEqual(self.io.send.call_count, 3)

    def test_same_screen_pause_move_never_tolerates_inactive_owner(self):
        with self.assertRaisesRegex(RuntimeError, 'inactive'):
            self.run_quit(changes=((4, loading(5)),))
        self.assertEqual(self.io.send.call_count, 2)
        self.assertEqual(self.io.finish.call_count, 1)

    def test_unknown_start_sends_no_input_or_watch(self):
        with self.assertRaisesRegex(RuntimeError, 'source'):
            self.run_quit(changes=((0, self.state(1, 'training', **{'input.owner': 'unknown'})),))
        self.io.arm.assert_not_called()
        self.io.send.assert_not_called()

    def test_unexpected_pause_focus_stops_before_up_or_a(self):
        from unittest.mock import call

        for index, state in ((2, self.state(3, 'pause', 'quit_training')),
                             (4, self.state(5, 'pause', 'stage_hazards'))):
            with self.subTest(index=index):
                self.io.reset_mock()
                with self.assertRaisesRegex(RuntimeError, 'unexpected'):
                    self.run_quit(changes=((index, state),))
                self.assertEqual(self.io.send.call_args_list,
                                 [call(action) for action in self.actions[:index // 2]])

    def test_quit_requires_committed_training_css_with_the_tile_provider(self):
        for state in (self.state(7, 'main_menu'), self.state(7, 'css.smash'),
                      self.state(7, 'css.training', **{'screen.context': 'unknown'}),
                      self.state(7, 'css.training', **{'focus.source': 'native'})):
            with self.subTest(values=state.values):
                self.io.reset_mock()
                with self.assertRaisesRegex(RuntimeError, 'unexpected'):
                    self.run_quit(changes=((6, state),))
                self.assertEqual(self.io.send.call_count, 3)
                self.assertEqual(self.io.finish.call_count, 2)

    def test_modal_or_reconnect_before_quit_blocks_a(self):
        modal = self.state(6, 'pause', 'quit_training', dialog='notice')
        source = self.state(6, 'pause', 'quit_training')
        reconnected = picker.Sample(source.block, connection=2)
        for state in (modal, reconnected):
            with self.subTest(state=state):
                self.io.reset_mock()
                with self.assertRaisesRegex(RuntimeError, 'source|connection'):
                    self.run_quit(changes=((5, state),))
                self.assertEqual(self.io.send.call_count, 2)
                self.assertEqual(self.io.finish.call_count, 2)

class TrainingSceneTransitionTests(unittest.TestCase):
    source = {'screen': 'css.training', 'screen.context': 'training', 'focus.source': 'tile.cursor',
              'input.owner': 'native', 'dialog': 'none', 'focus': 'css.training'}
    target = {'screen': 'training', 'screen.context': 'training', 'input.owner': 'native', 'dialog': 'none'}

    def run_wait(self, *frames):
        import time

        replay = Replay(native_blocks=frames)
        return picker.wait_scene_transition(replay, native(2, **self.source), time.monotonic() + .1,
                                            self.source, self.target, label='native')

    def test_wrong_active_screen_or_modal_stops_even_with_ready_frame_available(self):
        ready = native(5, focus='training', **self.target)
        for fields in ({'screen': 'rules'}, {'screen.context': 'smash'},
                       {'input.owner': 'overlay'}, {'dialog': 'notice'}):
            with self.subTest(fields=fields), self.assertRaisesRegex(RuntimeError, 'unexpected'):
                self.run_wait(loading(3), native(4, focus='training', **{**self.target, **fields}), ready)

    def test_inactive_owner_requires_complete_explicit_sentinels_and_continuity(self):
        ready = native(5, focus='training', **self.target)
        missing = loading(3)
        missing.values.pop('input.owner')
        different_boot = loading(3)
        different_boot.block['boot'] = different_boot.values['boot'] = '0x0000000000000002'
        for frame, reason in ((missing, 'incomplete'), (loading(3, dialog='none'), 'inactive'),
                              (loading(3, epoch=2), 'connection'), (different_boot, 'boot'),
                              (loading(1), 'stale'),
                              (loading(3, **{'observation': 'bad'}), 'invalid observation')):
            with self.subTest(frame=frame), self.assertRaisesRegex(RuntimeError, reason):
                self.run_wait(frame, ready)


if __name__ == '__main__':
    unittest.main()
