#!/usr/bin/env python3
"""Offline tests for runner.py with a fake Eden: no emulator, no input. Run: python3 -I test_runner.py"""
import hashlib
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner  # noqa: E402

from PIL import Image  # noqa: E402

RED, BLUE = (200, 30, 30), (30, 30, 200)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FakeEden:
    """Answers the control calls runner.py makes. The 'screen' colour changes when a segment is played or recorded."""

    def __init__(self, screens, log_path=None):
        self.screens = screens  # segment name -> colour the screen shows after it
        self.colour = BLUE
        self.calls = []
        self.recording = None
        self.playing = None
        self.log_path = log_path

    def request(self, method, params):
        self.calls.append(method)
        if method == 'macro.play':
            name = Path(params['path']).name.split('.')[0]
            self.playing = name
            self.colour = self.screens.get(name, self.colour)
            if self.log_path and name == 'pause':
                with open(self.log_path, 'a') as f:
                    f.write('[TR] paused=1\n')
            return {'ok': True, 'result': {'state': 'playing'}}
        if method == 'macro.status':
            if self.recording:
                self.frame = getattr(self, 'frame', 0) + 1  # the game advances one frame per status call here
                return {'ok': True, 'result': {'state': 'recording', 'frame': self.frame}}
            return {'ok': True, 'result': {'state': 'finished', 'error': None}}
        if method == 'macro.record':
            self.recording = params
            return {'ok': True, 'result': {'state': 'recording'}}
        if method == 'macro.stop':
            if self.recording:
                path = Path(self.recording['path'])
                path.write_text(json.dumps({'format': 'eden-input-macro', 'game_id': '01006A800016E000',
                                            'duration_frames': 60,
                                            'events': [{'frame': 1, 'buttons': 1}, {'frame': 9, 'buttons': 0}]}))
                self.colour = self.screens.get(path.name.split('.')[0], self.colour)
                self.recording = None
            return {'ok': True, 'result': {}}
        if method == 'ui.tree':
            return {'ok': True, 'result': [{'id': 'o1', 'class': 'GRenderWindow'}]}
        if method == 'ui.screenshot':
            Image.new('RGB', (64, 36), self.colour).save(params['path'])
            return {'ok': True, 'result': {}}
        return {'ok': True, 'result': {}}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='eden-runner-test-'))
        (self.dir / 'segments').mkdir()
        (self.dir / 'refs').mkdir()
        Image.new('RGB', (64, 36), RED).save(self.dir / 'refs' / 'red.png')
        macro = {'format': 'eden-input-macro', 'game_id': '01006A800016E000', 'duration_frames': 30,
                 'events': [{'frame': 1, 'buttons': 1}, {'frame': 5, 'buttons': 0}]}
        for name in ('to-red', 'pause'):
            (self.dir / 'segments' / f'{name}.edenmacro.json').write_text(json.dumps(macro))
        self.log = self.dir / 'console.log'
        self.log.write_text('[TR] paused=1 (stale line before the step)\n')
        self.plan = {
            'game_id': '01006A800016E000', 'start': 'blue',
            'segments': {
                'to-red': {'file': 'segments/to-red.edenmacro.json',
                           'sha256': sha(self.dir / 'segments' / 'to-red.edenmacro.json')},
                'pause': {'file': 'segments/pause.edenmacro.json',
                          'sha256': sha(self.dir / 'segments' / 'pause.edenmacro.json')},
                'to-blue': {'file': 'segments/to-blue.edenmacro.json', 'recipe': [{'buttons': 'b', 'hold': 10, 'gap': 0}]},
            },
            'gates': {
                'red': {'kind': 'image', 'reference': 'refs/red.png', 'sha256': sha(self.dir / 'refs' / 'red.png'),
                        'roi': [0, 0, 64, 36], 'cap': 2, 'interval': .05},
                'paused': {'kind': 'log', 'source': 'console_log', 'pattern': r'paused=1', 'cap': 2},
            },
            'rows': [
                {'id': 'r1', 'start': 'blue', 'end': 'red', 'steps': [{'play': 'to-red', 'unchecked': True}, {'gate': 'red'}]},
                {'id': 'r2', 'start': 'red', 'end': 'red-paused', 'steps': [{'gate': 'red'}, {'play': 'pause'}, {'gate': 'paused'}]},
                {'id': 'r3', 'start': 'red-paused', 'end': 'blue', 'steps': [{'play': 'to-blue', 'unchecked': True}, {'shot': 'end'}]},
            ],
        }
        self.manifest = self.dir / 'rows.json'
        self.manifest.write_text(json.dumps(self.plan))
        self.instances = {'a': {'socket': 'fake', 'console_log': str(self.log)}}

    def tearDown(self):
        shutil.rmtree(self.dir)

    def make(self, eden, pace=15):
        return runner.Runner(self.manifest, self.instances, self.dir / 'out', pace, client_factory=lambda s: eden)

    def test_dry_run_ready_to_record(self):
        report = runner.dry_run(self.manifest)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['missing_segments'], ['to-blue'])
        self.assertEqual(report['result'], 'READY_TO_RECORD')

    def test_dry_run_catches_broken_chain_hash_and_missing_recipe(self):
        self.plan['rows'][1]['start'] = 'elsewhere'
        self.plan['segments']['to-red']['sha256'] = '0' * 64
        del self.plan['segments']['to-blue']['recipe']
        self.manifest.write_text(json.dumps(self.plan))
        errors = ' | '.join(runner.dry_run(self.manifest)['errors'])
        self.assertIn('starts on elsewhere', errors)
        self.assertIn('to-red: sha256 mismatch', errors)
        self.assertIn('to-blue: file missing and no recipe', errors)

    def test_run_plays_records_gates_and_writes_receipts(self):
        eden = FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)
        self.make(eden).run()
        for row in ('r1', 'r2', 'r3'):
            self.assertEqual(json.loads((self.dir / 'out' / f'{row}.json').read_text())['status'], 'PASS')
        saved = json.loads(self.manifest.read_text())['segments']['to-blue']
        self.assertEqual(saved['sha256'], sha(self.dir / 'segments' / 'to-blue.edenmacro.json'))
        self.assertIn('macro.record', eden.calls)
        self.assertEqual(eden.calls.count('macro.play'), 2)
        # Single inputs happen only inside the recording window.
        inside, recording = 0, False
        for m in eden.calls:
            recording = True if m == 'macro.record' else (False if m == 'macro.stop' else recording)
            if m in ('input.button', 'input.stick'):
                self.assertTrue(recording)
                inside += 1
        self.assertEqual(inside, 2)
        self.assertEqual(runner.dry_run(self.manifest)['result'], 'READY')

    def test_log_source_names_with_or_without_log_suffix(self):
        self.instances = {'a': {'socket': 'fake', 'console': str(self.log)}}  # gate says console_log
        eden = FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)
        self.make(eden).run()
        self.assertEqual(json.loads((self.dir / 'out' / 'r2.json').read_text())['status'], 'PASS')

    def test_log_gate_sees_a_rotated_or_truncated_log(self):
        # The plugin rotates its log mid-row: the new file is shorter than the row-start offset (lr-04, row 13).
        self.log.write_text('x' * 5000 + '\n')
        eden = FakeEden({'to-red': RED, 'to-blue': BLUE})
        original = eden.request

        def request(method, params):
            if method == 'macro.play' and 'pause' in params['path']:
                rotated = self.dir / 'console.log.new'
                rotated.write_text('[TR] paused=1\n')
                rotated.replace(self.log)  # new inode, smaller file
            return original(method, params)

        eden.request = request
        self.make(eden).run()
        self.assertEqual(json.loads((self.dir / 'out' / 'r2.json').read_text())['status'], 'PASS')

    def test_log_gate_ignores_lines_written_before_the_step(self):
        eden = FakeEden({'to-red': RED})  # never appends paused=1
        with self.assertRaises(RuntimeError):
            self.make(eden).run()
        self.assertTrue((self.dir / 'out' / 'STOP-INPUT').exists())
        self.assertEqual(json.loads((self.dir / 'out' / 'r2.json').read_text())['status'], 'FAILED')

    def test_failed_gate_stops_and_rerun_resumes_after_fix(self):
        eden = FakeEden({'to-red': BLUE}, log_path=self.log)  # wrong screen: gate fails
        with self.assertRaises(RuntimeError):
            self.make(eden).run()
        stop = self.dir / 'out' / 'STOP-INPUT'
        self.assertTrue(stop.exists())
        with self.assertRaises(runner.StopRun):  # no further input while STOP-INPUT is present
            self.make(FakeEden({'to-red': RED}, log_path=self.log)).run()
        stop.unlink()
        self.make(FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)).run()
        self.assertEqual(json.loads((self.dir / 'out' / 'r3.json').read_text())['status'], 'PASS')

    def two_instances(self):
        logs = {}
        for side in ('a', 'b'):
            logs[side] = self.dir / f'detector-{side}.log'
            logs[side].write_text('sync_end frame=1 desync_seen=0 (an earlier match)\n')
        self.instances = {side: {'socket': side, 'detector_log': str(logs[side]), 'settings': str(self.dir / 'settings.json')}
                          for side in ('a', 'b')}
        return logs

    def test_log_gate_across_instances_needs_equal_values(self):
        logs = self.two_instances()
        self.plan['gates']['synced'] = {'kind': 'log', 'source': 'detector_log', 'on_all': ['a', 'b'], 'equal': True,
                                        'pattern': r'sync_end frame=(\d+) desync_seen=(\d+)', 'expect_groups': {'2': '0'},
                                        'cap': 1.5}
        self.plan['rows'] = [{'id': 's1', 'start': 'blue', 'end': 'blue', 'steps': [{'gate': 'synced', 'on': 'a'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        eden = FakeEden({})

        def write_later(lines):
            time.sleep(.3)
            for side, line in lines.items():
                with open(logs[side], 'a') as f:
                    f.write(line + '\n')

        threading.Thread(target=write_later, args=({'a': 'sync_end frame=3353 desync_seen=0',
                                                    'b': 'sync_end frame=3353 desync_seen=0'},)).start()
        self.make(eden).run()
        self.assertEqual(json.loads((self.dir / 'out' / 's1.json').read_text())['status'], 'PASS')
        # Different frames on the two sides fail the gate.
        (self.dir / 'out' / 's1.json').unlink()
        threading.Thread(target=write_later, args=({'a': 'sync_end frame=4000 desync_seen=0',
                                                    'b': 'sync_end frame=4001 desync_seen=0'},)).start()
        with self.assertRaises(RuntimeError) as caught:
            self.make(FakeEden({})).run()
        self.assertIn('differ', str(caught.exception))

    def test_change_gate_and_json_diff(self):
        self.two_instances()
        settings = self.dir / 'settings.json'
        settings.write_text(json.dumps({'menu': {'sdi_state': 0, 'sdi_strength': 1, 'hud': 'x'}}))
        self.plan['gates']['hud-changed'] = {'kind': 'change', 'mark': 'hud', 'roi': [0, 0, 64, 36], 'cap': 3,
                                             'interval': .05}
        self.plan['rows'] = [{'id': 'c1', 'start': 'blue', 'end': 'red', 'on': 'b', 'steps': [
            {'copy': 'settings', 'as': 'before', 'on': 'b'},
            {'mark': 'hud', 'on': 'b'},
            {'play': 'to-red', 'on': 'b', 'unchecked': True},
            {'gate': 'hud-changed', 'on': 'b'},
            {'json_diff': {'source': 'settings', 'before': 'before', 'must_change': ['menu.sdi_state'],
                           'must_keep': ['menu.sdi_strength']}, 'on': 'b'},
        ]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(runner.dry_run(self.manifest)['errors'], [])
        eden = FakeEden({'to-red': RED})
        original = eden.request

        def request(method, params):
            if method == 'macro.play':  # the game saves the new SDI choice while the segment plays
                settings.write_text(json.dumps({'menu': {'sdi_state': 2, 'sdi_strength': 1, 'hud': 'x'}}))
            return original(method, params)

        eden.request = request
        self.make(eden).run()
        receipt = json.loads((self.dir / 'out' / 'c1.json').read_text())
        self.assertEqual(receipt['status'], 'PASS')
        self.assertEqual(receipt['results'][-1]['json_diff']['changes'], {'menu.sdi_state': [0, 2]})

    def test_ui_click_job_left_running_by_a_modal_is_not_a_failure(self):
        eden = FakeEden({})
        original = eden.request
        tree = [{'id': 'o91', 'class': 'QPushButton', 'name': 'host', 'text': 'Host Room', 'visible': True}]

        def request(method, params):
            if method == 'ui.tree':
                return {'ok': True, 'result': list(tree)}
            if method == 'ui.click':
                tree.append({'id': 'o95', 'class': 'QLabel', 'name': 'members', 'text': '1 / 8', 'visible': True})
                return {'ok': True, 'result': {'accepted': True, 'job': 'j1'}}
            if method == 'jobs.get':  # the click's job waits inside the room window it opened
                return {'ok': True, 'result': {'state': 'running'}}
            return original(method, params)

        eden.request = request
        self.plan['gates']['room-open'] = {'kind': 'ui', 'match': {'class': 'QLabel', 'text': '/ 8$'}, 'cap': 2}
        self.plan['rows'] = [{'id': 'host', 'start': 'blue', 'end': 'blue', 'steps': [
            {'ui': 'click', 'target': {'name': 'host'}, 'job_cap': .3}, {'gate': 'room-open'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.make(eden).run()
        self.assertEqual(json.loads((self.dir / 'out' / 'host.json').read_text())['status'], 'PASS')

    def test_dry_run_catches_change_gate_before_mark_and_diff_without_copy(self):
        self.plan['gates']['hud-changed'] = {'kind': 'change', 'mark': 'hud', 'roi': [0, 0, 64, 36], 'cap': 3}
        self.plan['rows'] = [{'id': 'c1', 'start': 'blue', 'end': 'red', 'steps': [
            {'gate': 'hud-changed'}, {'json_diff': {'source': 'settings', 'before': 'before'}}]}]
        self.manifest.write_text(json.dumps(self.plan))
        errors = ' | '.join(runner.dry_run(self.manifest)['errors'])
        self.assertIn('before its mark', errors)
        self.assertIn('has no earlier copy', errors)

    def test_play_needs_a_start_check_and_start_gate_runs_first(self):
        self.plan['rows'][0]['steps'][0].pop('unchecked')
        self.manifest.write_text(json.dumps(self.plan))
        self.assertIn('play to-red on * has no start check', ' | '.join(runner.dry_run(self.manifest)['errors']))
        # A start_gate on the segment satisfies the dry run and is checked before the macro plays.
        self.plan['gates']['blue'] = {'kind': 'hook', 'module': 'is_blue.py', 'function': 'check', 'cap': 1, 'interval': .05}
        (self.dir / 'is_blue.py').write_text('from PIL import Image\n'
                                            'def check(path, gate):\n'
                                            '    return Image.open(path).getpixel((0, 0)) == (30, 30, 200)\n')
        self.plan['segments']['to-red']['start_gate'] = 'blue'
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(runner.dry_run(self.manifest)['errors'], [])
        eden = FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)
        self.make(eden).run()
        first_play = eden.calls.index('macro.play')
        self.assertIn('ui.screenshot', eden.calls[:first_play])  # the start gate looked before the first input

    def test_acquire_reference_then_review_stop_then_compare(self):
        self.plan['gates']['red-new'] = {'kind': 'image', 'reference': 'refs/new/red-new.png', 'roi': [0, 0, 64, 36],
                                         'acquire': True, 'cap': 3, 'interval': .05}
        self.plan['rows'][0]['steps'][1] = {'gate': 'red-new'}
        self.manifest.write_text(json.dumps(self.plan))
        report = runner.dry_run(self.manifest)
        self.assertEqual((report['errors'], report['references_to_acquire']), ([], ['red-new']))
        result = self.make(FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)).run()
        self.assertEqual(result, 'REVIEW')  # stops after the row that captured a reference
        receipt = json.loads((self.dir / 'out' / 'r1.json').read_text())
        self.assertEqual((receipt['status'], len(receipt['acquired_references'])), ('PASS', 1))
        saved = json.loads(self.manifest.read_text())['gates']['red-new']
        self.assertEqual(saved['sha256'], sha(self.dir / 'refs' / 'new' / 'red-new.png'))
        self.assertFalse((self.dir / 'out' / 'r2.json').exists())
        self.assertEqual(runner.dry_run(self.manifest)['result'], 'READY_TO_RECORD')  # to-blue still to record
        # The rerun skips r1 and continues from the screen r1 left (red); the acquired reference now compares normally.
        resumed = FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)
        resumed.colour = RED
        self.assertEqual(self.make(resumed).run(), 'DONE')

    def test_recipe_holds_stick_through_button(self):
        self.plan['segments']['to-blue']['recipe'] = [{'buttons': 'b', 'with_stick': [0, -1], 'hold': 10, 'gap': 0}]
        self.manifest.write_text(json.dumps(self.plan))
        eden = FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)
        seen = []
        original = eden.request

        def request(method, params):
            if method in ('input.stick', 'input.button'):
                seen.append((method, params.get('y', params.get('state'))))
            return original(method, params)

        eden.request = request
        self.make(eden).run()
        self.assertEqual(seen, [('input.stick', -1), ('input.button', 'press'), ('input.button', 'release'),
                                ('input.stick', 0)])

    def test_recipe_holds_for_game_frames(self):
        self.plan['segments']['to-blue']['recipe'] = [{'stick': [1, 0], 'frames': 12, 'gap_frames': 3}]
        self.manifest.write_text(json.dumps(self.plan))
        eden = FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)
        frames_at = []
        original = eden.request

        def request(method, params):
            if method == 'input.stick':
                frames_at.append((params['x'], getattr(eden, 'frame', 0)))
            return original(method, params)

        eden.request = request
        self.make(eden).run()
        (x1, pressed), (x0, released) = frames_at
        self.assertEqual((x1, x0), (1, 0))
        self.assertGreaterEqual(released - pressed, 12)  # held for at least 12 game frames, however long that took

    def test_gate_recover_plays_detour_segment_once(self):
        # The game sits on the wrong screen (blue, like the attract-mode intro); the gate's recover segment fixes it.
        self.plan['gates']['red']['recover'] = 'to-red'
        self.plan['gates']['red']['cap'] = .5
        self.plan['rows'] = [{'id': 'w1', 'start': 'blue', 'end': 'red', 'steps': [{'gate': 'red'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(runner.dry_run(self.manifest)['errors'], [])
        eden = FakeEden({'to-red': RED})
        self.make(eden).run()
        self.assertEqual(eden.calls.count('macro.play'), 1)
        self.assertEqual(json.loads((self.dir / 'out' / 'w1.json').read_text())['status'], 'PASS')
        # Without a way back the gate still fails after the one recovery.
        (self.dir / 'out' / 'w1.json').unlink()
        with self.assertRaises(RuntimeError):
            self.make(FakeEden({'to-red': BLUE})).run()

    def test_library_entries_resolve_from_the_shared_folder(self):
        library = self.dir / 'library'
        (library / 'segments').mkdir(parents=True)
        shutil.copy(self.dir / 'segments' / 'to-red.edenmacro.json', library / 'segments' / 'to-red.edenmacro.json')
        (library / 'index.json').write_text(json.dumps({'segments': {'to-red': {
            'file': 'segments/to-red.edenmacro.json', 'sha256': self.plan['segments']['to-red']['sha256'],
            'start': 'blue', 'end': 'red'}}, 'gates': {}}))
        (self.dir / 'segments' / 'to-red.edenmacro.json').unlink()
        self.plan['segments']['to-red'] = {'library': 'to-red', 'cap': 30}
        self.manifest.write_text(json.dumps(self.plan))
        original = runner.LIBRARY
        runner.LIBRARY = library
        try:
            report = runner.dry_run(self.manifest)
            self.assertEqual(report['errors'], [])
            self.assertIn('to-red', report['available_segments'])
            self.plan['segments']['to-red'] = {'library': 'no-such-entry'}
            self.manifest.write_text(json.dumps(self.plan))
            self.assertIn('no library entry', runner.dry_run(self.manifest)['errors'][0])
        finally:
            runner.LIBRARY = original

    def test_harvest_copies_only_what_passing_rows_used(self):
        import harvest
        eden = FakeEden({'to-red': RED}, log_path=None)  # r2's log gate never passes: r1 PASS, r2 FAILED
        with self.assertRaises(RuntimeError):
            self.make(eden).run()
        library = self.dir / 'library'
        added = harvest.harvest(self.manifest, self.dir / 'out', 'test-01', library)
        self.assertEqual(added['segments'], ['to-red'])  # r2's pause segment isn't proven
        self.assertEqual(added['gates'], ['red'])
        index = json.loads((library / 'index.json').read_text())
        seg = index['segments']['to-red']
        self.assertEqual((seg['end_gate'], seg['from_run'], seg['row']), ('red', 'test-01', 'r1'))
        self.assertEqual(seg['sha256'], sha(library / seg['file']))
        self.assertEqual(index['gates']['red']['sha256'], sha(library / index['gates']['red']['reference']))
        self.assertTrue(index['gates']['red']['reference_is_roi'])
        # The region-only reference still passes a live screenshot of the same screen.
        shot = self.dir / 'shot.png'
        Image.new('RGB', (64, 36), RED).save(shot)
        self.assertEqual(runner.Runner.roi_diff(shot, library / index['gates']['red']['reference'], [0, 0, 64, 36], True), 0)
        # A segment's start_gate comes along even when no row step names it.
        self.plan['segments']['to-red']['start_gate'] = 'paused'
        self.manifest.write_text(json.dumps(self.plan))
        (library / 'index.json').unlink()
        self.assertIn('paused', harvest.harvest(self.manifest, self.dir / 'out', 'test-01', library)['gates'])
        # A second harvest leaves existing entries alone.
        self.assertEqual(harvest.harvest(self.manifest, self.dir / 'out', 'test-02', library)['segments'], [])

    def test_ui_steps_set_up_a_room_through_the_frontend(self):
        tree = [{'id': 'o4', 'class': 'QAction', 'name': 'action_Multiplayer_Create_Room', 'text': '&Create Room',
                 'visible': True}]
        sent = []
        eden = FakeEden({})
        original = eden.request

        def request(method, params):
            if method == 'ui.tree':
                return {'ok': True, 'result': list(tree)}
            if method.startswith('ui.') and method != 'ui.screenshot':
                sent.append((method, params))
                if method == 'ui.trigger':  # the room dialog opens
                    tree.extend([{'id': 'o90', 'class': 'QLineEdit', 'name': 'port', 'text': '24872', 'visible': True},
                                 {'id': 'o91', 'class': 'QPushButton', 'name': 'host', 'text': '&Host Room',
                                  'visible': True}])
                if method == 'ui.click':  # the room window shows its member count
                    tree.append({'id': 'o95', 'class': 'QLabel', 'name': 'members', 'text': '1 / 8', 'visible': True})
                return {'ok': True, 'result': {}}
            return original(method, params)

        eden.request = request
        self.plan['gates']['room-open'] = {'kind': 'ui', 'match': {'class': 'QLabel', 'text': r'^\d+ / 8$'}, 'cap': 2}
        self.plan['rows'] = [{'id': 'room', 'start': 'blue', 'end': 'blue', 'steps': [
            {'ui': 'trigger', 'target': 'action_Multiplayer_Create_Room'},
            {'ui': 'set', 'target': {'class': 'QLineEdit', 'name': 'port'}, 'property': 'text', 'value': '24873'},
            {'ui': 'click', 'target': {'class': 'QPushButton', 'text': '^Host Room$'}},
            {'gate': 'room-open'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(runner.dry_run(self.manifest)['errors'], [])
        self.make(eden).run()
        self.assertEqual(sent, [('ui.trigger', {'target': 'action_Multiplayer_Create_Room'}),
                                ('ui.set', {'property': 'text', 'value': '24873', 'target': 'o90'}),
                                ('ui.click', {'target': 'o91'})])
        receipt = json.loads((self.dir / 'out' / 'room.json').read_text())
        self.assertEqual((receipt['status'], receipt['results'][-1]['evidence']['text']), ('PASS', '1 / 8'))
        # A typo'd op is caught offline.
        self.plan['rows'][0]['steps'][0]['ui'] = 'press'
        self.manifest.write_text(json.dumps(self.plan))
        self.assertIn("ui op 'press'", ' | '.join(runner.dry_run(self.manifest)['errors']))

    def test_call_game_load_from_instances_then_status_gate(self):
        eden = FakeEden({})
        original = eden.request
        state = {'loaded': False}
        calls = []

        def request(method, params):
            if method == 'game.load':
                calls.append(params)
                state['loaded'] = True
                return {'ok': True, 'result': {}}
            if method == 'status':
                return {'ok': True, 'result': {'running': state['loaded'], 'loading_complete': state['loaded'],
                                               'last_frame_age_ms': 16}}
            return original(method, params)

        eden.request = request
        self.instances['a']['game'] = '/games/ssbu.xci'
        self.plan['gates']['loaded'] = {'kind': 'status', 'expect': {'running': True, 'loading_complete': True},
                                        'max_frame_age_ms': 1500, 'cap': 2}
        self.plan['rows'] = [{'id': 'load', 'start': 'blue', 'end': 'blue', 'steps': [
            {'call': 'game.load', 'params': {'path': '@game'}}, {'gate': 'loaded'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(runner.dry_run(self.manifest)['errors'], [])
        self.make(eden).run()
        self.assertEqual(calls, [{'path': '/games/ssbu.xci'}])
        self.assertEqual(json.loads((self.dir / 'out' / 'load.json').read_text())['status'], 'PASS')
        self.plan['rows'][0]['steps'][0]['call'] = 'input.button'  # input stays in recorded segments
        self.manifest.write_text(json.dumps(self.plan))
        self.assertIn("call 'input.button'", ' | '.join(runner.dry_run(self.manifest)['errors']))

    def test_project_step_hook_drives_and_checks_a_plugin_menu(self):
        (self.dir / 'steps.py').write_text(
            'def open_defaults(ctx, page="defaults"):\n'
            '    ctx.press("zr")\n'
            '    ctx.stick(0, -1, 50)\n'
            '    line = ctx.wait_log("console_log", r"screen=" + page, cap=2)\n'
            '    ctx.log({"opened": page})\n'
            '    return line\n')
        eden = FakeEden({})
        original = eden.request

        def request(method, params):
            if method == 'input.stick' and params['y'] == -1:  # the plugin logs its new screen
                with open(self.log, 'a') as f:
                    f.write('[TR] screen=defaults\n')
            return original(method, params)

        eden.request = request
        self.plan['rows'] = [{'id': 'menu', 'start': 'blue', 'end': 'defaults', 'steps': [
            {'run': 'steps.py:open_defaults', 'args': {'page': 'defaults'}, 'check': True},
            {'play': 'to-red'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(runner.dry_run(self.manifest)['errors'], [])  # the checking hook is the play's start check
        self.make(eden).run()
        receipt = json.loads((self.dir / 'out' / 'menu.json').read_text())
        self.assertEqual((receipt['status'], receipt['results'][0]['value']), ('PASS', 'screen=defaults'))
        self.assertIn('input.button', eden.calls)
        actions = (self.dir / 'out' / 'actions.jsonl').read_text()
        self.assertIn('"opened": "defaults"', actions)
        # A hook that won't load, or a moving hook before a play, is caught offline.
        self.plan['rows'][0]['steps'][0] = {'run': 'steps.py:no_such_function'}
        self.manifest.write_text(json.dumps(self.plan))
        errors = ' | '.join(runner.dry_run(self.manifest)['errors'])
        self.assertIn('no_such_function', errors)
        self.assertIn('has no start check', errors)

    def test_record_while_exploring_lands_the_segment_in_rows_json(self):
        eden = FakeEden({})
        new_rows = self.dir / 'explore' / 'rows.json'
        new_rows.parent.mkdir()
        started = runner.record_command('record-start', new_rows, self.instances, 'open-defaults', None,
                                        client_factory=lambda s: eden)
        self.assertEqual(started['recording'], 'open-defaults')
        eden.request('input.button', {'button': 'zr', 'state': 'tap'})  # the agent explores by hand
        stopped = runner.record_command('record-stop', new_rows, self.instances, 'open-defaults', None,
                                        start_gate='training', client_factory=lambda s: eden)
        plan = json.loads(new_rows.read_text())
        seg = plan['segments']['open-defaults']
        self.assertEqual((seg['sha256'], seg['start_gate']), (stopped['sha256'], 'training'))
        self.assertEqual(seg['sha256'], sha(new_rows.parent / seg['file']))
        with self.assertRaises(SystemExit):  # recorded segments are immutable: a second take needs a new name
            runner.record_command('record-start', new_rows, self.instances, 'open-defaults', None,
                                  client_factory=lambda s: eden)

    def test_named_pipe_client_round_trip(self):
        import io

        class FakePipe(io.RawIOBase):
            def __init__(self):
                self.written = b''
                self.reply = None

            def readable(self):
                return True

            def writable(self):
                return True

            def write(self, b):
                self.written += bytes(b)
                request = json.loads(self.written.decode())
                self.reply = io.BytesIO(json.dumps({'id': request['id'], 'ok': True,
                                                    'result': {'echo': request['method']}}).encode() + b'\n')
                return len(b)

            def read(self, n=-1):
                return self.reply.read(n)

        opened = []

        def opener(path, mode, buffering=0):
            opened.append(path)
            return FakePipe()

        client = runner.PipeClient('\\\\.\\pipe\\eden-ally-control', opener=opener)
        self.assertEqual(client.request('status')['result'], {'echo': 'status'})
        self.assertEqual(opened, ['\\\\.\\pipe\\eden-ally-control'])
        self.assertIsInstance(runner.default_client_factory()('\\\\.\\pipe\\x'), runner.PipeClient)

    def test_stop_file_and_pace_cap(self):
        (self.dir / 'out').mkdir()
        (self.dir / 'out' / 'STOP').write_text('primary')
        with self.assertRaises(runner.StopRun):
            self.make(FakeEden({'to-red': RED})).run()
        (self.dir / 'out' / 'STOP').unlink()
        (self.dir / 'out' / 'pace.json').write_text(json.dumps({'started': time.time() - 3600, 'first_row_done': None}))
        with self.assertRaises(runner.StopRun) as caught:
            self.make(FakeEden({'to-red': RED})).run()
        self.assertIn('pace', str(caught.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
