"""Regression coverage for native route decisions through fenced bridge replies."""

import sys
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_clock
import native_picker_io
import routes
import test_native_picker as state_fixtures
from test_routes import FakeCtx, FakeRunner

READ_COST = .001  # One bridge read, as the fake models it.


def ui(screen, focus=None, *, context='unknown', source='unknown', tab='unknown', layer='unknown',
       owner='native', dialog='none', index='unknown', label='unknown'):
    fields = {
        'screen': screen, 'screen.context': context, 'screen.tab': tab, 'screen.layer': layer,
        'input.owner': owner, 'dialog': dialog, 'focus.source': source, 'focus.id': focus or 'unknown',
        'focus.index': index, 'focus.label': label,
        'focus': '.'.join(part for part in (screen, layer, focus) if part and part != 'unknown'),
    }
    if screen == 'unknown':
        fields.update({'focus': 'unknown', 'observation.active': 'no'})
    return fields


IDLE_TITLE = ui('title', 'press_any', source='native', index='0', label='"Press Any Button"')
TITLE_OPENING = ui('title', 'opening_playback', context='opening', source='native')
HOW_TO_PLAY = ui('attract', 'skip_how_to_play', context='how_to_play', source='native', index='0')
UNOWNED_HOW_TO_PLAY = ui('attract', context='how_to_play', owner='unknown', dialog='unknown')
MAIN = ui('main_menu', 'smash', source='native', tab='smash', layer='top', index='1', label='"Smash"')
UNOWNED_MAIN = ui('main_menu', tab='smash', layer='top', owner='unknown', dialog='unknown')
TRAINING = ui('training', context='training')
PAUSE = ui('pause', 'cpu_count', source='native', context='training', index='0', label='"No. of CPUs"')
QUIT = ui('pause', 'quit_training', source='native', context='training', index='0', label='"Quit Training"')
CSS = ui('css.training', source='tile.cursor', context='training')
STAGE = ui('stage_select', source='tile.cursor', context='training')
TRAINING_ITEM = ui('main_menu', 'training', source='native', tab='games_and_more', layer='submenu',
                   index='515', label='"Training"')
UNKNOWN = ui('unknown', owner='unknown', dialog='unknown')
TR_OVERLAY = ui('training', context='training', owner='tr.menu')


class BridgeNavigationTests(unittest.TestCase):
    def setUp(self):
        self.clock = fake_clock.install(self, routes, routes.native_picker, native_picker_io)
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.log = directory / 'bridge.log'
        self.log.touch()
        instance = {'socket': '/tmp/eden-navigation-test.sock', 'bridge_log': str(self.log)}
        self.ctx = FakeCtx(FakeRunner(instance, None), directory)
        self.ctx.gate = Mock(side_effect=AssertionError('runner consulted an image gate'))
        self.frame = self.enterContext(patch.object(
            routes, 'frame', side_effect=AssertionError('runner captured a frame to decide navigation')))
        self.match = self.enterContext(patch.object(
            routes, 'match_frame', side_effect=AssertionError('runner compared a navigation ROI')))
        self.enterContext(patch.object(routes, 'bridge_v2', side_effect=self.reply))
        self.enterContext(patch.object(routes, 'play', side_effect=self.press))
        self.revision = 0
        self.requests = []
        self.actions = []
        self.edges = []
        self.watch_armed = False
        self.checked_source = False
        self.change_on_watch = None
        self.loading_next = None
        self.transition_samples = {}
        self.initial_empty = False
        self.last_ui = None

    def assert_no_images(self):
        self.ctx.gate.assert_not_called()
        self.frame.assert_not_called()
        self.match.assert_not_called()

    @staticmethod
    def wire(sample):
        block = sample.block
        head = ('schema', 'rev', 'boot', 'ack', 'ack.result', 'focus')
        values = sample.values
        fields = (*head, *(key for key in values if key not in head))
        payload = [f'{key}={values[key]}' for key in fields]
        crc = zlib.crc32('\n'.join(payload).encode())
        name, schema, rev, boot = (block[key] for key in ('name', 'schema', 'rev', 'boot'))
        return (f'[Bridge] state begin name={name} schema={schema} rev={rev} boot={boot}'
                f' lines={len(payload)} filtered=0\n'
                + ''.join(f'[Bridge] state.{i} {value}\n' for i, value in enumerate(payload))
                + f'[Bridge] state end name={name} rev={rev} crc={crc:08x}\n')

    def reply(self, _ctx, fence, request):
        self.assertEqual(fence, 'providers')
        self.requests.append(request)
        if request.startswith('state_watch '):
            provider = request.split()[1]
            self.assertIn(provider, ('ssbu.ui', 'tile.cursor'))
            self.assertEqual(request, f'state_watch {provider} keys=observation max=1')
            if provider == 'ssbu.ui':
                self.watch_armed = True
                if self.change_on_watch is not None:
                    self.scene = self.change_on_watch
                    self.change_on_watch = None
            response = f'[Bridge] req state_watch name={provider} result=ok keys=1 max=1\n'
        else:
            self.revision += 1
            if request == 'state ssbu.ui':
                previous = self.last_ui
                self.last_ui = UNKNOWN if self.initial_empty else (self.loading_next or self.scene)
                if previous == self.last_ui and routes.time is self.clock:
                    self.clock.spend(READ_COST)
                self.loading_next = None
                if self.watch_armed:
                    self.checked_source = True
                sample = state_fixtures.native(self.revision, **self.last_ui)
                if self.initial_empty:
                    sample = state_fixtures.native(self.revision, **{**self.last_ui, 'observation': '0'})
                    self.initial_empty = False
            else:
                self.assertEqual(request, 'state tile.cursor')
                if self.scene is STAGE:
                    sample = state_fixtures.TrainingSubmenuRouteTests.stage_tile(self.revision)
                else:
                    self.assertIs(self.scene, CSS)
                    sample = state_fixtures.tile(self.revision, seat='0')
            response = self.wire(sample)
        with self.log.open('a') as dest:
            dest.write('[Bridge] req providers result=ok count=2 boot=0x0000000000000001\n')
            dest.write(response)

    def press(self, _ctx, macro, **_options):
        self.actions.append(macro)
        self.assertTrue(self.checked_source, f'{macro} had no fresh fenced source check after arming')
        self.assertTrue(self.edges, f'unexpected input {macro} at {self.scene["focus"]}')
        source, macros, destination = self.edges.pop(0)
        self.assertEqual(self.last_ui, source)
        self.assertIn(macro, macros)
        self.scene = destination
        self.loading_next = self.transition_samples.get(macro, UNKNOWN if source is IDLE_TITLE else None)
        self.checked_source = self.watch_armed = False

    def test_title_press_any_reaches_main_without_a_visual_gate(self):
        self.scene = IDLE_TITLE
        self.edges = [(IDLE_TITLE, ('tap-a1', 'attract-cancel'), MAIN)]

        routes.to_main(self.ctx, tries=1, look_s=.1)

        self.assertEqual(len(self.actions), 1)
        self.assertEqual(self.last_ui, MAIN)
        self.assertFalse(self.edges)
        self.assert_no_images()

    def fake_clock(self, timeline):
        elapsed = 0
        states = iter(timeline)
        next_state = next(states, None)
        input_times = []

        def sleep(seconds):
            nonlocal elapsed, next_state
            elapsed += seconds
            while next_state is not None and elapsed >= next_state[0]:
                self.scene = next_state[1]
                next_state = next(states, None)

        def press(ctx, macro, **options):
            input_times.append(elapsed)
            self.press(ctx, macro, **options)

        def reply(ctx, fence, request):
            sleep(.01)
            self.reply(ctx, fence, request)

        clock = SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep)
        for module in (routes, routes.native_picker, native_picker_io):
            self.enterContext(patch.object(module, 'time', clock))
        self.enterContext(patch.object(routes, 'play', side_effect=press))
        self.enterContext(patch.object(routes, 'bridge_v2', side_effect=reply))
        return clock, input_times

    def test_cold_opening_outlives_navigation_cap_and_presses_prompt_immediately(self):
        self.scene = ui('title', owner='unknown', dialog='unknown')
        self.edges = [(IDLE_TITLE, ('attract-cancel',), MAIN)]
        _, input_times = self.fake_clock([(5.5, TITLE_OPENING), (135.374, IDLE_TITLE)])
        self.ctx.log = Mock()

        result = routes.to_main(self.ctx)

        self.assertEqual(result['start_presses'], 1)
        self.assertEqual(len(input_times), 1)
        self.assertGreaterEqual(input_times[0], 135.374)
        self.assertLess(input_times[0], 135.474)
        self.assertEqual(self.last_ui, MAIN)
        readiness, navigation, _ = (call.args[0] for call in self.ctx.log.call_args_list)
        self.assertEqual(readiness['phase'], 'opening_readiness')
        self.assertEqual(readiness['opening_readiness_cap_s'], 180)
        self.assertEqual(navigation['phase'], 'navigation')
        self.assertEqual(navigation['navigation_cap_s'], 54)
        self.assertLessEqual(navigation['elapsed_s'], input_times[0])
        self.assert_no_images()

    def test_unqualified_opening_does_not_extend_54_second_wait(self):
        shell = ui('title', context='opening', owner='unknown', dialog='unknown')
        for unqualified in (shell, {**TITLE_OPENING, 'dialog': 'popup'},
                            {**TITLE_OPENING, 'focus.source': 'unknown'}):
            with self.subTest(unqualified=unqualified):
                self.scene = unqualified
                clock, input_times = self.fake_clock([(60, IDLE_TITLE)])

                with self.assertRaises(RuntimeError):
                    routes.to_main(self.ctx)

                self.assertAlmostEqual(clock.monotonic(), 54, delta=.01)
                self.assertFalse(input_times)
        self.assert_no_images()

    def test_opening_reacquisition_keeps_one_entry_anchored_deadline(self):
        self.scene = ui('title', owner='unknown', dialog='unknown')
        clock, input_times = self.fake_clock([(5.5, TITLE_OPENING), (165, self.scene),
                                              (175, TITLE_OPENING), (181, IDLE_TITLE)])

        with self.assertRaises(RuntimeError):
            routes.to_main(self.ctx)

        self.assertAlmostEqual(clock.monotonic(), 180, delta=.01)
        self.assertFalse(input_times)
        self.assert_no_images()

    def test_opening_does_not_admit_an_input_from_a_foreign_scene(self):
        self.scene = TITLE_OPENING
        clock, input_times = self.fake_clock([(1.5, TRAINING), (2, IDLE_TITLE)])

        with self.assertRaisesRegex(RuntimeError, 'Title opening'):
            routes.to_main(self.ctx)

        self.assertLess(clock.monotonic(), 2)
        self.assertFalse(input_times)
        self.assert_no_images()

    def test_title_handoff_after_opening_uses_the_original_navigation_cap(self):
        self.scene = TITLE_OPENING
        self.edges = [(IDLE_TITLE, ('attract-cancel',), IDLE_TITLE)]
        self.transition_samples['attract-cancel'] = IDLE_TITLE
        shell = ui('title', owner='unknown', dialog='unknown')
        clock, input_times = self.fake_clock([(129.874, shell), (135, IDLE_TITLE)])

        with self.assertRaises(RuntimeError):
            routes.to_main(self.ctx, cap=2)

        self.assertEqual(len(input_times), 1)
        self.assertGreaterEqual(input_times[0], 135)
        self.assertLess(input_times[0], 135.1)
        self.assertAlmostEqual(clock.monotonic(), 137, delta=.1)
        self.assert_no_images()

    def test_opening_does_not_reset_clock_from_a_replayed_observation(self):
        self.scene = {**TITLE_OPENING, 'observation': '9000'}
        clock, input_times = self.fake_clock([(1.5, TITLE_OPENING), (2, IDLE_TITLE)])

        with self.assertRaisesRegex(RuntimeError, 'stale or contradictory'):
            routes.to_main(self.ctx)

        self.assertLess(clock.monotonic(), 2)
        self.assertFalse(input_times)
        self.assert_no_images()

    def test_title_confirm_can_retire_the_prompt_before_main_owns_input(self):
        self.scene = IDLE_TITLE
        self.edges = [(IDLE_TITLE, ('attract-cancel',), MAIN)]
        self.transition_samples['attract-cancel'] = ui('title', owner='unknown', dialog='unknown')

        routes.to_main(self.ctx, tries=1, look_s=.2)

        self.assertEqual(self.actions, ['attract-cancel'])
        self.assertEqual(self.last_ui, MAIN)
        self.assert_no_images()

    def test_title_confirm_waits_for_owned_how_to_play_before_its_one_skip(self):
        self.scene = IDLE_TITLE
        self.transition_samples['attract-cancel'] = UNOWNED_HOW_TO_PLAY
        self.edges = [(IDLE_TITLE, ('attract-cancel',), HOW_TO_PLAY),
                      (HOW_TO_PLAY, ('attract-cancel',), MAIN)]

        result = routes.to_main(self.ctx, tries=1, look_s=.2)

        self.assertEqual(self.actions, ['attract-cancel', 'attract-cancel'])
        self.assertEqual(result['start_presses'], 2)
        self.assertEqual(self.last_ui, MAIN)
        self.assertFalse(self.edges)
        self.assert_no_images()

    def test_starting_at_how_to_play_waits_for_main_ownership_after_one_skip(self):
        self.scene = HOW_TO_PLAY
        self.transition_samples['attract-cancel'] = UNOWNED_MAIN
        self.edges = [(HOW_TO_PLAY, ('attract-cancel',), MAIN)]

        result = routes.to_main(self.ctx, tries=1, look_s=.2)

        self.assertEqual(self.actions, ['attract-cancel'])
        self.assertEqual(result['start_presses'], 1)
        self.assertEqual(self.last_ui, MAIN)
        self.assert_no_images()

    def test_stock_main_after_how_to_play_arming_needs_no_plus(self):
        self.scene = HOW_TO_PLAY
        self.change_on_watch = MAIN

        result = routes.to_main(self.ctx, tries=1, look_s=.2)

        self.assertFalse(self.actions)
        self.assertEqual(result['start_presses'], 0)
        self.assertEqual(self.last_ui, MAIN)
        self.assert_no_images()

    def test_identity_only_how_to_play_after_title_needs_no_second_plus(self):
        self.scene = IDLE_TITLE
        self.edges = [(IDLE_TITLE, ('attract-cancel',), MAIN)]
        self.transition_samples['attract-cancel'] = UNOWNED_HOW_TO_PLAY

        result = routes.to_main(self.ctx, tries=1, look_s=.2)

        self.assertEqual(self.actions, ['attract-cancel'])
        self.assertEqual(result['start_presses'], 1)
        self.assertEqual(self.last_ui, MAIN)
        self.assert_no_images()

    def test_how_to_play_arming_refuses_stale_or_unauthenticated_source(self):
        for change, error in (({'observation': '1'}, 'stale or contradictory'),
                              ({'ack': '1'}, 'contradictory Title'),
                              ({'screen.context': 'demo'}, 'source after arming')):
            with self.subTest(change=change):
                self.scene = {**HOW_TO_PLAY, 'observation': '2'} if 'observation' in change else HOW_TO_PLAY
                self.change_on_watch = {**HOW_TO_PLAY, **change}

                with self.assertRaisesRegex(RuntimeError, error):
                    routes.to_main(self.ctx, tries=1, look_s=.2)

                self.assertFalse(self.actions)
        self.assert_no_images()

    def test_unconfirmed_how_to_play_skip_stays_single_shot(self):
        self.scene = HOW_TO_PLAY
        self.edges = [(HOW_TO_PLAY, ('attract-cancel',), HOW_TO_PLAY)]

        with self.assertRaises(RuntimeError):
            routes.to_main(self.ctx, tries=1, look_s=.2)
        with self.assertRaisesRegex(RuntimeError, 'remains unconfirmed'):
            routes.to_main(self.ctx, tries=1, look_s=.2)

        self.assertEqual(self.actions, ['attract-cancel'])
        self.assert_no_images()

    def test_ready_main_needs_no_input(self):
        self.scene = MAIN

        routes.to_main(self.ctx, tries=0, look_s=.1)

        self.assertFalse(self.actions)
        self.assertEqual(self.last_ui, MAIN)

    def test_empty_first_boot_sample_grants_no_input_and_can_settle(self):
        self.scene = MAIN
        self.initial_empty = True

        routes.to_main(self.ctx, tries=0, look_s=.35)

        self.assertFalse(self.actions)
        self.assertEqual(self.last_ui, MAIN)
        self.assert_no_images()
        self.assert_no_images()

    def test_title_source_must_remain_qualified_after_arming(self):
        self.scene = IDLE_TITLE
        self.change_on_watch = ui('title', owner='unknown', dialog='unknown')

        with self.assertRaises(RuntimeError):
            routes.to_main(self.ctx, tries=1, look_s=.1)

        self.assertIn('state_watch ssbu.ui keys=observation max=1', self.requests)
        self.assertTrue(self.checked_source)
        self.assertFalse(self.actions)
        self.assert_no_images()

    def test_return_routes_only_through_qualified_owners(self):
        self.scene = TRAINING
        self.transition_samples['confirm-native-quit-frames'] = ui(
            'css.training', context='training', owner='unknown', dialog='unknown')
        self.transition_samples['leave-css'] = ui(
            'stage_select', context='training', owner='unknown', dialog='unknown')
        self.edges = [
            (TRAINING, ('pause-open',), PAUSE),
            (PAUSE, ('focus-native-quit-frames',), QUIT),
            (QUIT, ('confirm-native-quit-frames',), CSS),
            (CSS, ('leave-css',), STAGE),
            (STAGE, ('leave-css', 'tap-b1'), TRAINING_ITEM),
            (TRAINING_ITEM, ('tap-b1',), MAIN),
        ]

        routes.back_to_main(self.ctx)

        self.assertFalse(self.edges)
        self.assertEqual(self.last_ui, MAIN)
        self.assert_no_images()

    def test_instant_back_can_skip_stage_select_on_training_css_exit(self):
        self.scene = CSS
        self.transition_samples['leave-css'] = ui(
            'main_menu', tab='games_and_more', layer='submenu',
            owner='unknown', dialog='unknown')
        self.edges = [
            (CSS, ('leave-css',), TRAINING_ITEM),
            (TRAINING_ITEM, ('tap-b1',), MAIN),
        ]

        result = routes.back_to_main(self.ctx)

        self.assertEqual(result['looks'], ['css', 'games_and_more'])
        self.assertFalse(self.edges)
        self.assertEqual(self.last_ui, MAIN)
        self.assert_no_images()

    def test_unknown_boot_returns_without_input(self):
        self.scene = UNKNOWN
        self.ctx.gate.side_effect = RuntimeError('old visual gate found no Main menu')

        with self.assertRaises(RuntimeError):
            routes.to_main(self.ctx, tries=1, look_s=.02)

        self.assertIn('state ssbu.ui', self.requests)
        self.assertFalse(self.actions)
        self.assert_no_images()

    def test_switch_training_route_ends_on_qualified_training_without_an_image_gate(self):
        ctx = self.ctx
        self.enterContext(patch.object(routes, 'on_switch', return_value=True))
        self.enterContext(patch.object(routes, 'main_to_stage', return_value={'screen': 'stage'}))
        self.enterContext(patch.object(routes, 'pick_stage_hw'))
        self.enterContext(patch.object(routes, 'confirm_pick'))
        self.enterContext(patch.object(routes, 'ensure_local_fighter_placed', return_value={'kind': 78}))
        entered = {'screen': 'training'}
        self.enterContext(patch.object(routes, 'enter_training', return_value=entered))

        result = routes.switch_to_training(ctx, p1='Steve')

        self.assertEqual(result['training'], entered)
        self.assert_no_images()

    def test_training_overlay_refuses_return_input(self):
        self.scene = TR_OVERLAY
        self.ctx.gate.side_effect = RuntimeError('old visual gate found no matching screen')

        with self.assertRaises(RuntimeError):
            routes.back_to_main(self.ctx, presses=1)

        self.assertIn('state ssbu.ui', self.requests)
        self.assertFalse(self.actions)
        self.assert_no_images()


if __name__ == '__main__':
    unittest.main()
