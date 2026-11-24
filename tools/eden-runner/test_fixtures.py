#!/usr/bin/env python3
"""Host tests for shared setup fixtures."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import fixtures
import recipe_macro
import routes
from test_routes import FakeCtx, FakeRunner


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.runner = FakeRunner({}, None)
        self.ctx = FakeCtx(self.runner, self.directory)
        self.events = []
        self.queues = {}
        call = self.runner.call

        def record_call(on, method, params=None, job_cap=15):
            if method == 'macro.play':
                self.events.append(('play', Path(params['path']).name.split('.')[0]))
            return call(on, method, params, job_cap)

        self.runner.call = record_call

        def read(on, requested, cap, accept, ask_every=1):
            block = self.queues[requested].pop(0)
            self.events.append(('read', block['values'].get('wizard.step')))
            okay, reason = accept(block)
            if not okay:
                raise RuntimeError(reason)
            return block, reason

        self.runner._state_block = mock.Mock(side_effect=read)

    def blocks(self, provider, values):
        self.queues[provider] = [
            {'name': provider, 'schema': provider + '/1', 'boot': 'boot1', 'rev': i + 1,
             'filtered': False, 'values': {'ack': str(i), 'ack.result': 'handled', **item}}
            for i, item in enumerate(values)]

    def test_the_page_in_cpu_view_is_switched_to_player_and_read_back(self):
        self.blocks('tr.menu', [{'scene': 'training', 'page': 'recording', 'open': '1',
                                  'recording.page.role': 'cpu'},
                                 {'scene': 'training', 'page': 'recording', 'open': '1',
                                  'recording.page.role': 'player'}])
        result = fixtures.recording_role(self.ctx, 'player')
        self.assertEqual(result.get('role'), 'player')
        self.assertEqual(result['before']['role'], 'cpu')
        self.assertEqual(result['after']['role'], 'player')
        self.assertEqual(self.runner.played, ['tap-zr1'])
        self.blocks('tr.menu', [{'scene': 'training', 'page': 'recording', 'open': '1',
                                  'recording.page.role': 'player'}])
        self.runner.played.clear()
        self.assertEqual(fixtures.recording_role(self.ctx, 'player').get('role'), 'player')
        self.assertEqual(self.runner.played, [])
        self.blocks('tr.menu', [{'scene': 'training', 'page': 'recording', 'open': '1',
                                  'recording.page.role': 'cpu'},
                                 {'scene': 'training', 'page': 'recording', 'open': '1',
                                  'recording.page.role': 'cpu'}])
        with self.assertRaisesRegex(RuntimeError, 'SETUP: recording_role:.*cpu'):
            fixtures.recording_role(self.ctx, 'player')

    def test_the_popup_role_is_set_with_l_and_closed_with_b(self):
        self.blocks('tr.menu', [{'scene': 'training', 'open': '0', 'recording.popup.open': '0'},
                                 {'scene': 'training', 'open': '0', 'recording.popup.open': '1',
                                  'recording.popup.role': 'cpu'},
                                 {'scene': 'training', 'open': '0', 'recording.popup.open': '1',
                                  'recording.popup.role': 'player'},
                                 {'scene': 'training', 'open': '0', 'recording.popup.open': '0',
                                  'recording.popup.role': 'player'}])
        result = fixtures.recording_role(self.ctx, 'player', where='popup')
        self.assertEqual(result.get('role'), 'player')
        self.assertEqual(self.runner.played, ['recording-popup-chord', 'tap-l1', 'tap-b1'])
        self.runner.played.clear()
        self.blocks('tr.menu', [{'scene': 'training', 'open': '0', 'recording.popup.open': '0',
                                  'recording.popup.role': 'player'}])
        self.assertEqual(fixtures.recording_role(self.ctx, 'player', where='popup').get('role'), 'player')
        self.assertEqual(self.runner.played, [])

    def test_recording_role_refuses_a_changed_boot(self):
        self.blocks('tr.menu', [{'scene': 'training', 'page': 'recording', 'open': '1',
                                  'recording.page.role': 'cpu'},
                                 {'scene': 'training', 'page': 'recording', 'open': '1',
                                  'recording.page.role': 'player'}])
        self.queues['tr.menu'][1]['boot'] = 'boot2'
        with self.assertRaisesRegex(RuntimeError, 'SETUP: recording_role:.*boot'):
            fixtures.recording_role(self.ctx, 'player')
        self.assertEqual(self.runner.played, ['tap-zr1'])

    def test_a_cold_guard_start_is_walked_through_cpu_player_preview(self):
        self.blocks('tr.practice', [{'game': 'guard_edge', 'phase': 'wizard', 'wizard.step': 'cpu'},
                                     {'game': 'guard_edge', 'phase': 'wizard', 'wizard.step': 'player'},
                                     {'game': 'guard_edge', 'phase': 'wizard', 'wizard.step': 'preview'},
                                     {'game': 'guard_edge', 'phase': 'ready', 'wizard.step': ''}])
        self.blocks('tr.menu', [{'scene': 'training', 'game.running': '1', 'notice.header': 'Edge',
                                 'notice.body': 'Ready to guard'}])
        result = fixtures.guard_first_use(self.ctx, 'training')
        self.assertEqual(result.get('steps'), ['cpu', 'player', 'preview'])
        self.assertEqual(result.get('phase'), 'ready')
        self.assertEqual(result['ready'], {'boot': 'boot1', 'rev': 1, 'notice.body': 'Ready to guard'})
        self.assertEqual(json.loads((self.directory / 'fixture-guard-first-use.json').read_text())['ready'],
                         result['ready'])
        self.assertEqual(fixtures.GIVES['guard_first_use']({}), ['guard.ready'])
        self.assertEqual(self.runner.played, ['tap-a1'] * 3)
        self.assertEqual(self.events, [('read', 'cpu'), ('play', 'tap-a1'), ('read', 'player'),
                                       ('play', 'tap-a1'), ('read', 'preview'), ('play', 'tap-a1'), ('read', ''),
                                       ('read', None)])
        self.assertEqual(self.runner._state_block.call_count, 5)

    def test_a_kept_guard_setup_presses_nothing(self):
        self.blocks('tr.practice', [{'game': 'guard_edge', 'phase': 'ready', 'wizard.step': ''}])
        self.blocks('tr.menu', [{'scene': 'training', 'game.running': '1', 'notice.header': 'Edge',
                                 'notice.body': 'Ready'}])
        result = fixtures.guard_first_use(self.ctx, 'training')
        self.assertEqual(result.get('steps'), [])
        self.assertEqual(result['ready']['notice.body'], 'Ready')
        self.assertEqual(self.runner.played, [])

    def test_a_cold_guard_that_never_reaches_ready_is_refused(self):
        self.blocks('tr.practice', [{'game': 'guard_edge', 'phase': 'wizard', 'wizard.step': 'cpu'},
                                     {'game': 'guard_edge', 'phase': 'wizard', 'wizard.step': 'player'},
                                     {'game': 'guard_edge', 'phase': 'wizard', 'wizard.step': 'preview'},
                                     {'game': 'guard_edge', 'phase': 'ready', 'wizard.step': ''}])
        self.blocks('tr.menu', [{'scene': 'training', 'game.running': '1', 'notice.header': 'Edge',
                                 'notice.body': 'Selecting'}])
        with self.assertRaisesRegex(RuntimeError, 'SETUP: guard_first_use: not Ready: Edge / Selecting'):
            fixtures.guard_first_use(self.ctx, 'training')
        self.assertEqual(self.runner.played, ['tap-a1'] * 3)
        self.assertFalse((self.directory / 'fixture-guard-first-use.json').exists())

    def test_a_kept_guard_without_a_same_boot_running_ready_is_refused(self):
        cases = [
            ({'scene': 'training', 'game.running': '1', 'notice.header': 'Edge', 'notice.body': 'Selecting'}, False),
            ({'scene': 'training', 'game.running': '0', 'notice.header': 'Edge', 'notice.body': 'Ready'}, False),
            ({'scene': 'lobby', 'game.running': '1', 'notice.header': 'Edge', 'notice.body': 'Ready'}, False),
            ({'scene': 'training', 'game.running': '1', 'notice.header': 'Edge', 'notice.body': 'Ready'}, True),
        ]
        for values, other_boot in cases:
            with self.subTest(values=values, other_boot=other_boot):
                self.blocks('tr.practice', [{'game': 'guard_edge', 'phase': 'ready'}])
                self.blocks('tr.menu', [values])
                if other_boot:
                    self.queues['tr.menu'][0]['boot'] = 'other-boot'
                with self.assertRaisesRegex(RuntimeError, 'SETUP: guard_first_use: not Ready: Edge / '):
                    fixtures.guard_first_use(self.ctx, 'training')
                self.assertEqual(self.runner.played, [])

    def test_guard_refuses_a_different_game_at_entry_and_between_steps(self):
        self.blocks('tr.practice', [{'game': 'parry', 'phase': 'wizard', 'wizard.step': 'cpu'}])
        with self.assertRaisesRegex(RuntimeError, 'SETUP: guard_first_use: tr.practice game=parry, expected guard_edge'):
            fixtures.guard_first_use(self.ctx, 'training')
        self.assertEqual(self.runner.played, [])
        self.blocks('tr.practice', [{'game': 'guard_edge', 'phase': 'wizard', 'wizard.step': 'cpu'},
                                     {'game': 'parry', 'phase': 'wizard', 'wizard.step': 'player'}])
        with self.assertRaisesRegex(RuntimeError, 'SETUP: guard_first_use: tr.practice game=parry, expected guard_edge'):
            fixtures.guard_first_use(self.ctx, 'training')
        self.assertEqual(self.runner.played, ['tap-a1'])

    def test_guard_game_argument_is_checked_without_hardcoding(self):
        self.blocks('tr.practice', [{'game': 'alternate', 'phase': 'ready'}])
        self.blocks('tr.menu', [{'scene': 'lobby', 'game.running': '1', 'notice.header': 'Game',
                                 'notice.body': 'Ready now'}])
        result = fixtures.guard_first_use(self.ctx, 'lobby', game='alternate')
        self.assertEqual(result.get('steps'), [])
        self.assertEqual(result['ready']['notice.body'], 'Ready now')
        self.assertEqual(self.runner.played, [])

    def test_a_step_that_does_not_advance_stops_on_its_name(self):
        values = {'game': 'guard_edge', 'phase': 'wizard', 'wizard.step': 'cpu', 'notice.header': 'Edge',
                  'notice.body': 'pick CPU'}
        self.blocks('tr.practice', [values, values])
        self.blocks('tr.menu', [{'scene': 'training', 'notice.header': 'Edge', 'notice.body': 'pick CPU'}])
        with self.assertRaisesRegex(RuntimeError, 'SETUP: guard_first_use: stuck at wizard.step=cpu: Edge / pick CPU'):
            fixtures.guard_first_use(self.ctx, 'training')

    def test_a_missing_phase_does_not_finish_the_wizard(self):
        self.blocks('tr.practice', [{'game': 'guard_edge', 'phase': 'wizard', 'wizard.step': 'preview'},
                                     {'game': 'guard_edge', 'wizard.step': ''}])
        self.blocks('tr.menu', [{'scene': 'training', 'notice.header': 'Edge', 'notice.body': 'preview'}])
        with self.assertRaisesRegex(RuntimeError, 'SETUP: guard_first_use: stuck at wizard.step=preview'):
            fixtures.guard_first_use(self.ctx, 'training')
        self.assertEqual(self.runner.played, ['tap-a1'])

    def mark_room(self, before=b'', mark_row=None):
        log = self.directory / 'bridge.log'
        log.write_bytes(before)
        self.ctx.instance['bridge_log'] = str(log)
        self.runner.out = self.directory
        if mark_row:
            self.ctx.row_dir = self.directory / mark_row
            self.ctx.row_dir.mkdir()
        self.blocks('ssbu.ui', [
            {'screen': 'main_menu', 'observation.active': 'yes', 'input.owner': 'native'},
            {'screen': 'ldn.waiting', 'ldn.role': 'host', 'observation.active': 'yes'},
            {'screen': 'ldn.waiting', 'ldn.role': 'host', 'observation.active': 'yes'},
        ])
        mark = fixtures.mark_before_room(self.ctx)
        self.assertIn('offset', mark)
        self.assertEqual(mark['offset'], len(before))
        if mark_row:
            self.ctx.row_dir = self.directory / 'consumer'
            self.ctx.row_dir.mkdir()
        return log

    def seeded(self, behavior=7, stage=3, *, mark_row=None, settings=None, native=None):
        if settings is None:
            settings = {'menu': {'lobby_practice': [1, 0]},
                        'lobby_practice_panel': {'partners': 1, 'behavior': 7}}
        if native is None:
            native = {'scene': {'class': 'local_wireless', 'battle': True, 'melee': 13},
                      'roster': {0: {}, 4: {}}, 'stage': 3, 'frame': 200}

        def wait(ctx, predicate, **kwargs):
            self.assertEqual(kwargs['fresh'], ('scene', 'roster'))
            self.assertEqual(kwargs['roster_entries'], (0, 4))
            if not predicate(native):
                raise RuntimeError('waiting battle unavailable')
            return native

        with mock.patch.object(routes, '_current_settings', return_value=settings), \
             mock.patch.object(routes, 'wait_native', side_effect=wait):
            return fixtures.seeded_room(self.ctx, behavior, stage, mark_row=mark_row)

    def append_stage(self, log, stage, frame=150):
        with log.open('ab') as stream:
            stream.write(f'[Bridge] stage id={stage} ev=1 frame={frame} wall=300\n'.encode())

    def test_the_seeded_room_is_read_back_without_input(self):
        log = self.mark_room(mark_row='admission')
        self.append_stage(log, 3)
        result = self.seeded(mark_row='admission')
        self.assertEqual(result.get('behavior'), 7)
        self.assertEqual(result.get('behavior_source'), 'settings')
        self.assertEqual(json.loads((self.ctx.row_dir / 'fixture-seeded-room.json').read_text())['behavior_source'],
                         'settings')
        self.assertEqual(result.get('stage'), 3)
        self.assertEqual(self.runner.played, [])

    def test_a_stage_only_before_the_mark_is_refused(self):
        self.mark_room(b'[Bridge] stage id=3 ev=1 frame=100 wall=200\n')
        with self.assertRaisesRegex(RuntimeError, 'SETUP: seeded_room:.*stage'):
            self.seeded()

    def test_the_last_stage_after_the_mark_must_match(self):
        log = self.mark_room()
        self.append_stage(log, 3)
        self.append_stage(log, 8)
        with self.assertRaisesRegex(RuntimeError, 'SETUP: seeded_room:.*stage.*8.*3'):
            self.seeded()

    def test_only_complete_stage_lines_after_the_mark_count(self):
        log = self.mark_room()
        with log.open('ab') as stream:
            stream.write(b'[Bridge] stage id=3 ev=1 frame=150 wall=300')
        with self.assertRaisesRegex(RuntimeError, 'SETUP: seeded_room:.*stage'):
            self.seeded()

    def test_a_rotated_log_cannot_reuse_the_mark(self):
        log = self.mark_room()
        log.rename(self.directory / 'bridge-old.log')
        log.write_bytes(b'')
        self.append_stage(log, 3)
        with self.assertRaisesRegex(RuntimeError, 'SETUP: seeded_room:.*inode'):
            self.seeded()

    def test_an_absent_mark_and_truncated_log_are_refused(self):
        self.ctx.instance['bridge_log'] = str(self.directory / 'bridge.log')
        with self.assertRaisesRegex(RuntimeError, 'SETUP: seeded_room:.*mark'):
            self.seeded()
        log = self.mark_room(b'long enough to truncate\n')
        log.write_bytes(b'')
        with self.assertRaisesRegex(RuntimeError, 'SETUP: seeded_room:.*offset'):
            self.seeded()

    def test_the_mark_and_room_must_share_a_boot(self):
        log = self.mark_room()
        self.append_stage(log, 3)
        self.queues['ssbu.ui'][0]['boot'] = 'different-boot'
        with self.assertRaisesRegex(RuntimeError, 'SETUP: seeded_room:.*boot'):
            self.seeded()

    def test_the_room_boot_must_not_change_during_the_check(self):
        log = self.mark_room()
        self.append_stage(log, 3)
        self.queues['ssbu.ui'][1]['boot'] = 'different-boot'
        with self.assertRaisesRegex(RuntimeError, 'SETUP: seeded_room:.*boot'):
            self.seeded()

    def test_a_wrong_behavior_is_named_in_the_refusal(self):
        log = self.mark_room()
        self.append_stage(log, 3)
        with self.assertRaisesRegex(RuntimeError, 'SETUP: seeded_room:.*behavior.*1.*7'):
            self.seeded(behavior=1)
        self.assertEqual(self.runner.played, [])

    def test_a_room_without_the_qualified_waiting_battle_is_refused(self):
        scenes = [{'class': 'local_wireless', 'battle': True, 'melee': 13},
                  {'class': 'training', 'battle': True, 'melee': 13},
                  {'class': 'local_wireless', 'battle': False, 'melee': 13},
                  {'class': 'local_wireless', 'battle': True, 'melee': 12}, None,
                  {'class': 'local_wireless', 'battle': True, 'melee': 13}]
        rosters = [{0: {}}, {0: {}, 4: {}}, {0: {}, 4: {}}, {0: {}, 4: {}}, {0: {}, 4: {}}, None]
        for scene, roster in zip(scenes, rosters):
            with self.subTest(scene=scene, roster=roster):
                log = self.mark_room()
                self.append_stage(log, 3)
                native = {'scene': scene, 'roster': roster, 'stage': 3, 'frame': 200}
                with self.assertRaisesRegex(RuntimeError, 'SETUP: seeded_room:.*waiting battle'):
                    self.seeded(native=native)

    def test_stage_event_must_precede_and_match_the_fresh_native_read(self):
        for event_frame, native_stage in ((250, 3), (150, 2)):
            with self.subTest(event_frame=event_frame, native_stage=native_stage):
                log = self.mark_room()
                self.append_stage(log, 3, event_frame)
                native = {'scene': {'class': 'local_wireless', 'battle': True, 'melee': 13},
                          'roster': {0: {}, 4: {}}, 'stage': native_stage, 'frame': 200}
                with self.assertRaisesRegex(RuntimeError, 'SETUP: seeded_room:.*(frame|stage)'):
                    self.seeded(native=native)

    def test_room_practice_and_partner_count_are_read_back(self):
        settings_cases = [
            ({'menu': {'lobby_practice': [0, 1]}, 'lobby_practice_panel': {'partners': 1, 'behavior': 7}},
             'lobby_practice'),
            ({'menu': {'lobby_practice': [1, 0]}, 'lobby_practice_panel': {'partners': 2, 'behavior': 7}},
             'partners'),
            ([], 'settings'),
        ]
        for settings, field in settings_cases:
            with self.subTest(field=field):
                log = self.mark_room()
                self.append_stage(log, 3)
                with self.assertRaisesRegex(RuntimeError, f'SETUP: seeded_room:.*{field}'):
                    self.seeded(settings=settings)

    def test_the_mark_requires_an_active_native_main_menu(self):
        log = self.directory / 'bridge.log'
        log.write_bytes(b'')
        self.ctx.instance['bridge_log'] = str(log)
        self.blocks('ssbu.ui', [{'screen': 'main_menu', 'observation.active': 'no', 'input.owner': 'native'}])
        with self.assertRaisesRegex(RuntimeError, 'SETUP: mark_before_room:'):
            fixtures.mark_before_room(self.ctx)

    def test_seed_settings_writes_only_the_seed_keys(self):
        base = {'menu': {'lobby_practice': [0, 1], 'other': {'keep': 'yes'}},
                'lobby_practice_panel': {'partners': 1, 'behavior': 1, 'old': 99},
                'stage_preferences': {'mode': 'random', 'direct_stage': 4, 'direct_form': 'omega', 'keep': 5},
                'lobby_stage_order': {'direct_custom': None, 'other': [3, 4]},
                'defaults_menu': {'lobby_practice': [0, 1]}, 'unrelated': {'nested': [1, 2]}}
        original = json.loads(json.dumps(base))
        actual = fixtures.seed_settings(base, behavior=7, stage=3, partners=2)
        expected = {'menu': {'lobby_practice': [1, 0], 'other': {'keep': 'yes'}},
                    'lobby_practice_panel': {'partners': 2, 'behavior': 7},
                    'stage_preferences': {'mode': 'direct', 'direct_stage': 3, 'direct_form': 'normal', 'keep': 5},
                    'lobby_stage_order': {'direct_custom': None, 'other': [3, 4]},
                    'defaults_menu': {'lobby_practice': [0, 1]}, 'unrelated': {'nested': [1, 2]}}
        self.assertEqual(actual, expected)
        self.assertEqual(base, original)
        result = fixtures.seed_settings({'menu': {}}, behavior=1, stage=7)
        self.assertEqual(result, {'menu': {'lobby_practice': [1, 0]},
                                  'lobby_practice_panel': {'partners': 1, 'behavior': 1},
                                  'stage_preferences': {'mode': 'direct', 'direct_stage': 7,
                                                        'direct_form': 'normal'}})

    def test_seed_settings_refuses_invalid_and_custom_inputs(self):
        cases = [({}, 'menu'), ({'menu': []}, 'menu'), ({'menu': {}, 'stage_preferences': []}, 'stage_preferences'),
                 ({'menu': {}, 'lobby_stage_order': None}, 'lobby_stage_order'),
                 ({'menu': {}, 'lobby_stage_order': {'direct_custom': {'id': 1}}}, 'custom Direct stage')]
        for base, message in cases:
            with self.subTest(base=base):
                with self.assertRaisesRegex(ValueError, message):
                    fixtures.seed_settings(base, behavior=7, stage=3)

    def test_seed_cli_uses_exact_bytes_and_exclusive_create(self):
        source, output = self.directory / 'base.json', self.directory / 'seed.json'
        source.write_text('{"menu": {"note": "π"}}', encoding='utf-8')
        command = [sys.executable, '-I', '-B', str(Path(fixtures.__file__).resolve()), 'seed',
                   '--base', str(source), '--out', str(output), '--behavior', '7', '--stage', '3']
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(output.is_file(), first.stderr)
        expected = (
            '{\n  "menu": {\n    "note": "π",\n    "lobby_practice": [\n      1,\n      0\n    ]\n  },\n'
            '  "lobby_practice_panel": {\n    "partners": 1,\n    "behavior": 7\n  },\n'
            '  "stage_preferences": {\n    "mode": "direct",\n    "direct_stage": 3,\n'
            '    "direct_form": "normal"\n  }\n}\n').encode()
        self.assertEqual(output.read_bytes(), expected)
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(output.read_bytes(), expected)

    def test_a_window_starts_at_the_first_grounded_wait_line(self):
        def samples(wait):
            return [(i, {'entry': '1', 'status': '1' if i < 30 else '0',
                         'situation': '1' if i < 30 else '0'}, 'line') for i in range(30 + wait)]
        window = fixtures.window_from_wait(samples(300), 1, 300)
        self.assertEqual(len(window), 300)
        self.assertEqual(window[0][0], 30)
        with self.assertRaisesRegex(RuntimeError, '270'):
            fixtures.window_from_wait(samples(270), 1, 300)
        gap = samples(300)
        gap.pop(45)
        with self.assertRaisesRegex(RuntimeError, '15'):
            fixtures.window_from_wait(gap, 1, 300)
        initial_wait = [(i, {'entry': '1', 'status': '0', 'situation': '0'}, 'line') for i in range(300)]
        with self.assertRaisesRegex(RuntimeError, '299 of 300'):
            fixtures.window_from_wait(initial_wait, 1, 300)
        initial_wait.append((300, {'entry': '1', 'status': '0', 'situation': '0'}, 'line'))
        self.assertEqual(len(fixtures.window_from_wait(initial_wait, 1, 300)), 300)

    def test_take_t_has_its_jump_at_input_frame_60(self):
        path = routes.ROUTES / 'macros' / 'take-p1-ground-jump.edenmacro.json'
        self.assertTrue(path.is_file())
        macro = json.loads(path.read_text())
        recipe = [{'stick': [1, 0], 'frames': 40, 'gap_frames': 20},
                  {'buttons': 'x', 'frames': 4, 'gap_frames': 40}]
        self.assertEqual(macro, recipe_macro.compile_recipe('take-p1-ground-jump', recipe))
        self.assertEqual([[e['frame'], e['buttons'], e['sticks']] for e in macro['events']],
                         [[0, 0, [1, 0, 0, 0]], [40, 0, [0, 0, 0, 0]],
                          [60, 4, [0, 0, 0, 0]], [64, 0, [0, 0, 0, 0]]])
        self.assertEqual(macro['duration_frames'], 104)

    def test_jump_only_has_no_run_and_jumps_at_frame_20(self):
        path = routes.ROUTES / 'macros' / 'take-p1-jump-only.edenmacro.json'
        self.assertTrue(path.is_file())
        macro = json.loads(path.read_text())
        recipe = [{'stick': [0, 0], 'frames': 20, 'gap_frames': 0},
                  {'buttons': 'x', 'frames': 4, 'gap_frames': 40}]
        self.assertEqual(macro, recipe_macro.compile_recipe('take-p1-jump-only', recipe))
        self.assertEqual([[e['frame'], e['buttons'], e['sticks']] for e in macro['events']],
                         [[0, 0, [0, 0, 0, 0]], [20, 4, [0, 0, 0, 0]], [24, 0, [0, 0, 0, 0]]])
        self.assertEqual(macro['duration_frames'], 64)


class WatchFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.log = self.directory / 'bridge.log'
        self.log.write_text('old data\n')
        self.ctx = FakeCtx(FakeRunner({'bridge_log': str(self.log)}, None), self.directory)

    @staticmethod
    def lines(entries=(1,), frames=3, start=100, changes=None):
        changes = changes or {}
        mask = hex(sum(1 << entry for entry in entries))
        lines = [f'[Bridge] req fighters_watch result=ok entries={mask} frames={frames} frame={start} wall=1']
        for frame in range(start + 1, start + frames + 1):
            for entry in entries:
                fields = {'kind': '67', 'x': '1.00', 'y': '2.00', 'status': '0', 'situation': '0',
                          'eb': '1', 'ab': '1', 'cpu': '-1'}
                fields.update(changes.get((entry, frame), {}))
                body = ' '.join(f'{key}={value}' for key, value in fields.items())
                lines.append(f'[Bridge] fighter entry={entry} {body} cbt=0 frame={frame} wall=1')
        lines.append(f'[Bridge] fighters_watch done entries={mask} frame={start + frames} wall=1')
        return lines

    def standin(self, lines, baseline=99):
        def send(_ctx, request):
            with self.log.open('a') as stream:
                stream.write('\n'.join(lines) + '\n')
            return request
        return (mock.patch.object(routes, 'native_state', return_value={'frame': baseline, 'roster': {1: {}, 4: {}}}),
                mock.patch.object(routes, 'bridge_v2', side_effect=send))

    def test_a_replayed_acknowledgement_is_not_a_second_watch(self):
        lines = self.lines()
        window, complete = fixtures.watch_window(lines[:2] + lines, [1], 3, 99)
        self.assertTrue(complete)
        self.assertEqual(window, lines)
        self.assertEqual(fixtures.check_watch(window, [1], 3), [])

    def test_a_stale_acknowledgement_before_the_baseline_is_ignored(self):
        old = self.lines(start=20)
        current = self.lines(start=100)
        window, complete = fixtures.watch_window(old + current, [1], 3, 99)
        self.assertTrue(complete)
        self.assertEqual(window, current)

    def test_a_missing_frame_is_a_problem(self):
        lines = self.lines()
        del lines[2]
        problems = fixtures.check_watch(lines, [1], 3)
        self.assertTrue(any('consecutive' in problem for problem in problems))

    def test_two_entries_on_different_frames_are_a_problem(self):
        lines = self.lines((0, 1))
        lines[2] = lines[2].replace('frame=101', 'frame=102')
        lines[4] = lines[4].replace('frame=102', 'frame=103')
        lines[6] = lines[6].replace('frame=103', 'frame=104')
        problems = fixtures.check_watch(lines, [0, 1], 3)
        self.assertTrue(any('same bridge frames' in problem for problem in problems))

    def test_no_done_line_is_incomplete(self):
        window, complete = fixtures.watch_window(self.lines()[:-1], [1], 3, 99)
        self.assertFalse(complete)
        self.assertTrue(window)

    def test_a_walking_fighter_is_not_standing(self):
        readings = fixtures.samples(self.lines(changes={(1, 102): {'x': '1.01'}}), 1)
        self.assertEqual(len(readings), 3)
        okay, reason = fixtures.standing(readings)
        self.assertFalse(okay)
        self.assertIn('frame 102', reason)
        self.assertIn('x=1.01', reason)

    def test_a_fighter_in_another_status_is_not_standing(self):
        readings = fixtures.samples(self.lines(changes={(1, 103): {'status': '24'}}), 1)
        self.assertEqual(len(readings), 3)
        okay, reason = fixtures.standing(readings)
        self.assertFalse(okay)
        self.assertIn('frame 103', reason)
        self.assertIn('status=24', reason)
        okay, _ = fixtures.standing(fixtures.samples(self.lines(), 1))
        self.assertTrue(okay)

    def test_cpu_standing_saves_a_fresh_raw_watch_and_refuses_jumping(self):
        baseline, bridge = self.standin(self.lines())
        with baseline as fresh, bridge as send:
            result = fixtures.cpu_standing(self.ctx, frames=3)
        self.assertEqual((result.get('first_frame'), result.get('last_frame')), (101, 103))
        fresh.assert_called_once_with(self.ctx, fresh=('roster',), roster_entries=(1,))
        send.assert_called_once_with(self.ctx, 'fighters watch 1 3')
        receipt_path = self.directory / 'fixture-cpu-standing-watch.json'
        self.assertTrue(receipt_path.is_file())
        receipt = json.loads(receipt_path.read_text())
        raw = self.directory / receipt.get('raw_file', 'missing')
        self.assertTrue(raw.is_file())
        self.assertEqual(fixtures.hashlib.sha256(raw.read_bytes()).hexdigest(), receipt.get('sha256'))
        baseline, bridge = self.standin(self.lines(changes={(1, 102): {'status': '24'}}))
        with baseline, bridge, self.assertRaisesRegex(RuntimeError, 'SETUP: cpu_standing: entry 1 frame 102'):
            fixtures.cpu_standing(self.ctx, frames=3)

    def test_partner_effective_accepts_stop_attack_and_observes_cpu_flag(self):
        for eb in (1, 34):
            changes = {(4, frame): {'eb': str(eb), 'ab': str(eb), 'cpu': '-1'} for frame in (101, 102, 103)}
            baseline, bridge = self.standin(self.lines((4,), changes=changes))
            with baseline, bridge:
                result = fixtures.partner_effective(self.ctx, eb, frames=3, kind=67)
            self.assertEqual(result.get('eb'), str(eb))
            self.assertEqual(result.get('cpu'), ['-1'])

    def test_partner_effective_names_the_first_mismatching_frame(self):
        baseline, bridge = self.standin(self.lines((4,), changes={(4, 102): {'eb': '34'}}))
        with baseline, bridge, self.assertRaisesRegex(RuntimeError, 'SETUP: partner_effective: entry 4 frame 102: eb=34'):
            fixtures.partner_effective(self.ctx, 1, frames=3)

    def test_partner_cpu_is_observed_not_required(self):
        lines = [line.replace(' cpu=-1', '') for line in self.lines((4,))]
        baseline, bridge = self.standin(lines)
        with baseline, bridge:
            result = fixtures.partner_effective(self.ctx, 1, frames=3)
        self.assertEqual(result.get('cpu'), [])
