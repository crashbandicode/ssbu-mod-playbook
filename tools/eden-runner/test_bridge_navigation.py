"""Regression coverage for native route decisions through fenced bridge replies."""

import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import routes
import test_native_picker as state_fixtures
from test_routes import FakeCtx, FakeRunner


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
MAIN = ui('main_menu', 'smash', source='native', tab='smash', layer='top', index='1', label='"Smash"')
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
        self.loading_next = False
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
                self.last_ui = UNKNOWN if self.loading_next or self.initial_empty else self.scene
                self.loading_next = False
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

    def press(self, _ctx, macro):
        self.actions.append(macro)
        self.assertTrue(self.checked_source, f'{macro} had no fresh fenced source check after arming')
        self.assertTrue(self.edges, f'unexpected input {macro} at {self.scene["focus"]}')
        source, macros, destination = self.edges.pop(0)
        self.assertEqual(self.last_ui, source)
        self.assertIn(macro, macros)
        self.scene = destination
        self.loading_next = source is IDLE_TITLE
        self.checked_source = self.watch_armed = False

    def test_title_press_any_reaches_main_without_a_visual_gate(self):
        self.scene = IDLE_TITLE
        self.edges = [(IDLE_TITLE, ('tap-a1', 'attract-cancel'), MAIN)]

        routes.to_main(self.ctx, tries=1, look_s=.1)

        self.assertEqual(len(self.actions), 1)
        self.assertEqual(self.last_ui, MAIN)
        self.assertFalse(self.edges)
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

    def test_unknown_boot_returns_without_input(self):
        self.scene = UNKNOWN
        self.ctx.gate.side_effect = RuntimeError('old visual gate found no Main menu')

        with self.assertRaises(RuntimeError):
            routes.to_main(self.ctx, tries=1, look_s=.02)

        self.assertIn('state ssbu.ui', self.requests)
        self.assertFalse(self.actions)
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
