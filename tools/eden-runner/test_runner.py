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
        # Every receipt's manifest_sha256 names a retained copy, also the version from before the recording.
        for row in ('r1', 'r2', 'r3'):
            sha_ = json.loads((self.dir / 'out' / f'{row}.json').read_text())['manifest_sha256']
            self.assertEqual(sha(self.dir / 'out' / 'manifests' / f'{sha_}.json'), sha_)
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
        self.plan['rows'][0]['steps'][0]['call'] = 'trace.arm'  # tracing is allowed in rows
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(runner.dry_run(self.manifest)['errors'], [])
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

    def test_failed_ui_gate_saves_the_tree_and_the_visible_dialog(self):
        eden = FakeEden({})
        original = eden.request
        # The real API's QMessageBox carries its text only under properties.text.value (pkg4, 2026-11-03).
        tree = [{'id': 'o7', 'class': 'QMessageBox', 'name': '', 'title': '', 'text': None, 'visible': True,
                 'properties': {'text': {'value': 'Unable to connect to the room'}, 'windowTitle': {'value': 'Error'}}}]

        def request(method, params):
            if method == 'ui.tree':
                return {'ok': True, 'result': list(tree)}
            return original(method, params)

        eden.request = request
        self.plan['gates']['two'] = {'kind': 'ui', 'match': {'class': 'QLabel', 'text': '^2 / 8$'}, 'cap': .6}
        self.plan['rows'] = [{'id': 'join', 'start': 'blue', 'end': 'blue', 'steps': [{'gate': 'two'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        with self.assertRaises(RuntimeError):
            self.make(eden).run()
        saved = json.loads((self.dir / 'out' / 'join' / 'a-GATE-FAILED-two-tree.json').read_text())
        self.assertEqual(saved[0]['properties']['text']['value'], 'Unable to connect to the room')
        self.assertTrue(list((self.dir / 'out' / 'join').glob('*GATE-FAILED-two.png')))
        actions = (self.dir / 'out' / 'actions.jsonl').read_text()
        self.assertIn('"text": "Unable to connect to the room"', actions)  # summarised, not null
        self.assertIn('"title": "Error"', actions)
        # A gate can match a message box by its text too.
        self.assertTrue(runner.Runner.ui_matches(tree[0], {'class': 'QMessageBox', 'text': 'Unable to connect'}))

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

    def test_old_rows_json_still_stops_at_the_first_failure(self):
        # No recover and no on_failure: the first failing row writes STOP-INPUT and later rows do not run.
        eden = FakeEden({'to-red': BLUE}, log_path=self.log)
        with self.assertRaises(RuntimeError):
            self.make(eden).run()
        self.assertTrue((self.dir / 'out' / 'STOP-INPUT').exists())
        self.assertEqual(json.loads((self.dir / 'out' / 'r1.json').read_text())['status'], 'FAILED')
        self.assertFalse((self.dir / 'out' / 'r2.json').exists())
        summary = json.loads((self.dir / 'out' / 'summary.json').read_text())
        self.assertEqual([row['status'] for row in summary['rows']], ['fail', 'not run', 'not run'])
        self.assertTrue(summary['rows'][0]['evidence'])

    def _continue_plan(self):
        Image.new('RGB', (64, 36), BLUE).save(self.dir / 'refs' / 'blue.png')
        self.plan['gates']['paused']['cap'] = 0.4
        self.plan['gates']['red']['cap'] = 0.4
        self.plan['gates']['red']['interval'] = 0.05
        (self.dir / 'steps.py').write_text(
            'def back(ctx):\n'
            '    ctx.play("to-red")\n'
            '    return "back"\n'
            'def collect(ctx):\n'
            '    path = ctx.row_dir / "hook.txt"\n'
            '    path.write_text("saw\\n")\n'
            '    return str(path)\n')
        self.instances['a']['trace_jsonl'] = str(self.dir / 'trace.jsonl')
        self.plan['rows'] = [
            {'id': 'r1', 'start': 'blue', 'end': 'red', 'on_failure_hook': 'steps.py:collect',
             'steps': [{'gate': 'paused'}]},
            {'id': 'r2', 'start': 'red', 'end': 'red', 'start_gate': 'red',
             'recover': [{'run': 'steps.py:back'}], 'steps': [{'gate': 'red'}, {'shot': 'ok'}]},
            {'id': 'r3', 'start': 'red', 'end': 'blue', 'steps': [{'shot': 'after'}]},
        ]
        self.manifest.write_text(json.dumps(self.plan))

    def test_failure_recovers_and_the_next_rows_run(self):
        self._continue_plan()
        eden = FakeEden({'to-red': RED}, log_path=self.log)
        result = self.make(eden).run()
        self.assertEqual(result, 'FAILED')  # r1 failed; the run still finished the later rows
        self.assertFalse((self.dir / 'out' / 'STOP-INPUT').exists())
        self.assertEqual(json.loads((self.dir / 'out' / 'r1.json').read_text())['status'], 'FAILED')
        self.assertEqual(json.loads((self.dir / 'out' / 'r2.json').read_text())['status'], 'PASS')
        self.assertEqual(json.loads((self.dir / 'out' / 'r3.json').read_text())['status'], 'PASS')
        receipt = json.loads((self.dir / 'out' / 'r1.json').read_text())
        self.assertEqual(receipt['recovery'], {'to': 'r2', 'ok': True})
        evidence = ' '.join(receipt['evidence_paths'])
        self.assertIn('hook.txt', evidence)
        self.assertIn('log-tail-', evidence)
        self.assertIn('trace-counters', evidence)
        self.assertIn('trace-read', evidence)
        self.assertIn('FAIL', evidence)
        summary = json.loads((self.dir / 'out' / 'summary.json').read_text())
        self.assertEqual([row['status'] for row in summary['rows']], ['fail', 'pass', 'pass'])
        self.assertEqual(summary['failed'], 1)
        self.assertIn('trace.counters', eden.calls)
        self.assertIn('trace.read', eden.calls)

    def test_failed_recovery_stops_the_run(self):
        self._continue_plan()
        eden = FakeEden({'to-red': BLUE}, log_path=self.log)  # recover never reaches the red start gate
        with self.assertRaises(RuntimeError):
            self.make(eden).run()
        self.assertTrue((self.dir / 'out' / 'STOP-INPUT').exists())
        self.assertFalse((self.dir / 'out' / 'r3.json').exists())
        receipt = json.loads((self.dir / 'out' / 'r1.json').read_text())
        self.assertFalse(receipt['recovery']['ok'])
        summary = json.loads((self.dir / 'out' / 'summary.json').read_text())
        self.assertEqual(summary['rows'][2]['status'], 'not run')

    def test_a_crashed_game_stops_without_recovery(self):
        self._continue_plan()

        class CrashEden(FakeEden):
            def request(self, method, params):
                if method == 'status':
                    self.calls.append(method)
                    return {'ok': True, 'result': {'running': False, 'crash': True}}
                return super().request(method, params)

        eden = CrashEden({'to-red': RED}, log_path=self.log)
        with self.assertRaises(RuntimeError):
            self.make(eden).run()
        self.assertTrue((self.dir / 'out' / 'STOP-INPUT').exists())
        self.assertNotIn('macro.play', eden.calls)  # r1 never played; recovery must not either
        self.assertFalse((self.dir / 'out' / 'r2.json').exists())

    def test_rows_failed_reruns_only_the_failed_rows(self):
        eden = FakeEden({'to-red': BLUE}, log_path=self.log)
        with self.assertRaises(RuntimeError):
            self.make(eden).run()
        (self.dir / 'out' / 'STOP-INPUT').unlink()
        instances = self.dir / 'instances.json'
        instances.write_text(json.dumps(self.instances))
        fixed = FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)
        original = runner.default_client_factory
        runner.default_client_factory = lambda: (lambda socket: fixed)
        try:
            code = runner.main(['run', str(self.manifest), '--instances', str(instances),
                                '--rows', 'failed', '--pace-minutes', '15'])
        finally:
            runner.default_client_factory = original
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((self.dir / 'out' / 'r1.json').read_text())['status'], 'PASS')
        summary = json.loads((self.dir / 'out' / 'summary.json').read_text())
        self.assertEqual([row['status'] for row in summary['rows']], ['pass', 'skipped', 'skipped'])
        # A later full run still resumes after the passed row. The new fake starts where the fixed row left it.
        follow = FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)
        follow.colour = RED
        self.make(follow).run()
        self.assertEqual(json.loads((self.dir / 'out' / 'r3.json').read_text())['status'], 'PASS')

    def test_rows_failed_with_nothing_failed_runs_nothing(self):
        result = self.make(FakeEden({'to-red': RED})).run(only=set())
        self.assertEqual(result, 'DONE')
        self.assertFalse((self.dir / 'out' / 'r1.json').exists())
        summary = json.loads((self.dir / 'out' / 'summary.json').read_text())
        self.assertEqual([row['status'] for row in summary['rows']], ['skipped', 'skipped', 'skipped'])

    def test_gif_presets_record_and_a_breaking_file_is_refused(self):
        class GifEden(FakeEden):
            def __init__(self, screens, frames=4, frame_ms=100):
                super().__init__(screens)
                self.frames, self.frame_ms, self.gif, self.params = frames, frame_ms, None, []

            def request(self, method, params):
                self.params.append((method, dict(params or {})))
                if method == 'recording.start':
                    self.gif = dict(params)
                    return {'ok': True, 'result': {'state': 'recording'}}
                if method == 'recording.stop':
                    spec = self.gif or {}
                    width = spec.get('width', 640)
                    height = max(1, round(width * 9 / 16))
                    images = [Image.new('RGB', (width, height), RED) for _ in range(self.frames)]
                    images[0].save(spec['path'], save_all=True, append_images=images[1:],
                                   duration=self.frame_ms, loop=0)
                    self.gif = None
                    return {'ok': True, 'result': {'state': 'finished'}}
                if method == 'recording.status':
                    return {'ok': True, 'result': {'state': 'finished', 'frames': self.frames,
                                                   'dropped_frames': 0, 'error': None}}
                return super().request(method, params)

        self.plan['rows'] = [{'id': 'g1', 'start': 'blue', 'end': 'blue', 'steps': [
            {'gif': {'name': 'hover', 'preset': 'docs', 'steps': [{'shot': 'mid'}]}}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(runner.dry_run(self.manifest)['errors'], [])
        eden = GifEden({})
        self.assertEqual(self.make(eden).run(), 'DONE')
        started = next(params for method, params in eden.params if method == 'recording.start')
        self.assertEqual(started['width'], 1280)
        self.assertEqual(started['fps'], 10)
        self.assertEqual(started['duration_ms'], 10000)
        info = json.loads((self.dir / 'out' / 'g1.json').read_text())['results'][0]['gif']
        self.assertEqual(set(info) & {'frames', 'duration', 'size', 'sha256', 'path'},
                         {'frames', 'duration', 'size', 'sha256', 'path'})
        self.assertLessEqual(info['duration'], 10)
        self.assertEqual(info['sha256'], sha(info['path']))
        with Image.open(info['path']) as frame:
            self.assertEqual(frame.size[0], 1280)

        self.plan['rows'][0]['steps'] = [
            {'gif': {'name': 'hit', 'preset': 'evidence', 'steps': [{'shot': 'mid'}]}}]
        self.manifest.write_text(json.dumps(self.plan))
        (self.dir / 'out' / 'g1.json').unlink()
        evidence = GifEden({})
        self.make(evidence).run()
        started = next(params for method, params in evidence.params if method == 'recording.start')
        self.assertEqual(started['width'], 640)
        self.assertLessEqual(started['fps'], 15)
        info = json.loads((self.dir / 'out' / 'g1.json').read_text())['results'][0]['gif']
        self.assertLessEqual(info['size'], 10 * 1024 * 1024)
        with Image.open(info['path']) as frame:
            self.assertEqual(frame.size[0], 640)

        self.plan['rows'][0]['steps'] = [
            {'gif': {'name': 'huge', 'preset': 'evidence', 'max_bytes': 1, 'steps': [{'shot': 'mid'}]}}]
        self.manifest.write_text(json.dumps(self.plan))
        (self.dir / 'out' / 'g1.json').unlink()
        with self.assertRaises(RuntimeError):
            self.make(GifEden({})).run()
        refused = json.loads((self.dir / 'out' / 'g1.json').read_text())
        self.assertEqual(refused['status'], 'FAILED')
        self.assertIn('size', refused['gif_refused']['why'])
        self.assertFalse((self.dir / 'out' / 'g1' / 'huge.gif').exists())

        long = GifEden({}, frames=12, frame_ms=1000)
        self.plan['rows'][0]['steps'] = [
            {'gif': {'name': 'long', 'preset': 'docs', 'steps': [{'shot': 'mid'}]}}]
        self.manifest.write_text(json.dumps(self.plan))
        (self.dir / 'out' / 'g1.json').unlink()
        (self.dir / 'out' / 'STOP-INPUT').unlink()
        self.make(long).run()
        info = json.loads((self.dir / 'out' / 'g1.json').read_text())['results'][0]['gif']
        self.assertLessEqual(info['duration'], 10)

    def test_dry_run_catches_a_bad_gif_or_recover_and_keeps_a_reason(self):
        self.plan['rows'] = [{'id': 'g1', 'start': 'blue', 'end': 'blue', 'steps': [
            {'gif': {'name': 'hover', 'preset': 'docs', 'width': 900, 'steps': [{'shot': 'mid'}]}}]}]
        self.manifest.write_text(json.dumps(self.plan))
        errors = ' | '.join(runner.dry_run(self.manifest)['errors'])
        self.assertIn('reason', errors)

        self.plan['rows'][0]['steps'][0]['gif']['reason'] = 'the plugin text is already readable narrower'
        self.plan['rows'][0]['steps'][0]['gif']['out'] = '../outside.gif'
        self.manifest.write_text(json.dumps(self.plan))
        errors = ' | '.join(runner.dry_run(self.manifest)['errors'])
        self.assertIn('out', errors)

        self.plan['rows'][0]['steps'][0]['gif'].pop('out')
        self.plan['gif_presets'] = {'lobby': {'width': 800, 'fps': 8, 'max_duration_s': 4, 'max_bytes': 2_000_000}}
        self.manifest.write_text(json.dumps(self.plan))
        self.assertIn('reason', ' | '.join(runner.dry_run(self.manifest)['errors']))
        self.plan['gif_presets']['lobby']['reason'] = 'lobby art is drawn for 800'
        self.plan['rows'][0]['steps'] = [
            {'gif': {'name': 'lobby', 'preset': 'lobby', 'steps': [{'shot': 'mid'}]}}]
        self.plan['rows'].append({'id': 'g2', 'start': 'blue', 'end': 'blue', 'recover': [{'play': 'missing'}],
                                  'steps': [{'shot': 'x'}]})
        self.manifest.write_text(json.dumps(self.plan))
        errors = ' | '.join(runner.dry_run(self.manifest)['errors'])
        self.assertIn('missing', errors)
        self.assertNotIn('lobby', errors)

        self.plan['rows'][1]['recover'] = [{'play': 'to-red', 'unchecked': True}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(runner.dry_run(self.manifest)['errors'], [])

        class GifEden(FakeEden):
            def __init__(self):
                super().__init__({})
                self.params = []

            def request(self, method, params):
                self.params.append((method, dict(params or {})))
                if method == 'recording.start':
                    self.gif = dict(params)
                    return {'ok': True, 'result': {'state': 'recording'}}
                if method == 'recording.stop':
                    Image.new('RGB', (self.gif['width'], 32), BLUE).save(self.gif['path'])
                    return {'ok': True, 'result': {}}
                if method == 'recording.status':
                    return {'ok': True, 'result': {'state': 'finished', 'frames': 1, 'error': None}}
                return super().request(method, params)

        # A one-frame PNG is not a multi-frame gif; ImageSequence still yields one frame. Width follows the preset.
        self.plan['rows'] = [{'id': 'g1', 'start': 'blue', 'end': 'blue', 'steps': [
            {'gif': {'name': 'lobby', 'preset': 'lobby', 'steps': [{'shot': 'mid'}]}}]}]
        self.manifest.write_text(json.dumps(self.plan))
        eden = GifEden()
        self.make(eden).run()
        info = json.loads((self.dir / 'out' / 'g1.json').read_text())['results'][0]['gif']
        self.assertEqual(info['reason'], 'lobby art is drawn for 800')
        self.assertEqual(info['width'], 800)
        started = next(params for method, params in eden.params if method == 'recording.start')
        self.assertEqual(started['width'], 800)

    def test_long_with_stick_hold_is_split_under_the_lease_cap(self):
        self.assertEqual(runner.lease_parts(10200), [10000, 200])
        self.plan['segments']['to-blue']['recipe'] = [
            {'buttons': 'b', 'with_stick': [0, -1], 'frames': 12, 'gap': 0, 'stick_lead_ms': 0}]
        self.plan['rows'] = [
            {'id': 'hold', 'start': 'blue', 'end': 'blue',
             'steps': [{'play': 'to-blue', 'unchecked': True}]}]
        self.manifest.write_text(json.dumps(self.plan))
        short = self._leases_during_record(FakeEden({'to-blue': BLUE}))
        self.assertTrue(short)
        self.assertTrue(all(1 <= lease <= 10000 for lease in short))
        self.assertNotIn(10200, short)
        self.assertEqual(sum(1 for lease in short if lease == 10000), 2)  # one stick, one button; hold is short

        # A hold that outlasts one lease is renewed. The clock jumps with sleep so the test does not wait 10 s.
        path = self.dir / 'segments' / 'to-blue.edenmacro.json'
        path.unlink()
        self.plan['segments']['to-blue'].pop('sha256', None)
        self.plan['segments']['to-blue']['recipe'][0]['frames'] = 1500
        self.manifest.write_text(json.dumps(self.plan))
        (self.dir / 'out' / 'hold.json').unlink()
        clock = {'now': 0.0}
        real_sleep, real_mono = runner.time.sleep, runner.time.monotonic

        def sleep(seconds):
            clock['now'] += seconds

        def monotonic():
            return clock['now']

        runner.time.sleep, runner.time.monotonic = sleep, monotonic
        try:
            leases = self._leases_during_record(FakeEden({'to-blue': BLUE}))
        finally:
            runner.time.sleep, runner.time.monotonic = real_sleep, real_mono
        self.assertTrue(all(1 <= lease <= 10000 for lease in leases))
        downs = [lease for lease in leases if lease == 10000]
        self.assertGreaterEqual(len(downs), 3)  # first stick, first button, and at least one renewal
        saved = json.loads(self.manifest.read_text())['segments']['to-blue']
        self.assertEqual(saved['sha256'], sha(path))
        self.assertEqual(json.loads((self.dir / 'out' / 'hold.json').read_text())['status'], 'PASS')

    def _leases_during_record(self, eden):
        leases = []
        original = eden.request

        def request(method, params):
            if method in ('input.stick', 'input.button') and 'lease_ms' in (params or {}):
                leases.append(params['lease_ms'])
            return original(method, params)

        eden.request = request
        self.make(eden).run()
        return leases

    def test_recording_failure_leaves_no_segment_and_fails_the_row(self):
        self.plan['rows'] = [
            {'id': 'rec', 'start': 'blue', 'end': 'blue', 'steps': [{'play': 'to-blue', 'unchecked': True}]}]
        self.manifest.write_text(json.dumps(self.plan))
        eden = FakeEden({'to-blue': BLUE})
        original = eden.request

        def request(method, params):
            if method == 'input.button':
                return {'ok': False, 'error': {'message': 'injected recording failure'}}
            return original(method, params)

        eden.request = request
        with self.assertRaises(RuntimeError) as caught:
            self.make(eden).run()
        self.assertNotIsInstance(caught.exception, KeyError)
        self.assertIn('injected recording failure', str(caught.exception))
        path = self.dir / 'segments' / 'to-blue.edenmacro.json'
        self.assertFalse(path.exists())
        saved = json.loads(self.manifest.read_text())['segments']['to-blue']
        self.assertNotIn('sha256', saved)
        receipt = json.loads((self.dir / 'out' / 'rec.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertNotIn('KeyError', receipt['error'])

    def test_sha_less_play_fails_cleanly(self):
        del self.plan['segments']['to-red']['sha256']
        self.plan['rows'] = [
            {'id': 'bare', 'start': 'blue', 'end': 'red', 'steps': [{'play': 'to-red', 'unchecked': True}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertIn('to-red: no sha256', ' | '.join(runner.dry_run(self.manifest)['errors']))
        with self.assertRaises(RuntimeError) as caught:
            self.make(FakeEden({'to-red': RED})).run()
        self.assertIn('no sha256', str(caught.exception))
        self.assertNotIsInstance(caught.exception, KeyError)
        receipt = json.loads((self.dir / 'out' / 'bare.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertIn('sha256', receipt['error'])
        self.assertNotIn('KeyError', receipt['error'])

    def _paint(self, eden, size):
        original = eden.request

        def request(method, params):
            reply = original(method, params)
            if method == 'ui.screenshot':
                Image.new('RGB', size, eden.colour).save(params['path'])
            return reply

        eden.request = request
        return eden

    def test_scale_fit_matches_a_half_size_frame_and_is_off_by_default(self):
        self.plan['rows'] = [
            {'id': 'r1', 'start': 'blue', 'end': 'red',
             'steps': [{'play': 'to-red', 'unchecked': True}, {'gate': 'red'}]}]
        self.plan['gates']['red']['cap'] = 0.4
        self.manifest.write_text(json.dumps(self.plan))
        with self.assertRaises(RuntimeError):
            self.make(self._paint(FakeEden({'to-red': RED}), (32, 18))).run()
        failed = json.loads((self.dir / 'out' / 'r1.json').read_text())
        self.assertEqual(failed['status'], 'FAILED')
        self.assertNotIn('scale', failed)

        self.plan['gates']['red']['scale'] = 'fit'
        self.manifest.write_text(json.dumps(self.plan))
        (self.dir / 'out' / 'r1.json').unlink()
        (self.dir / 'out' / 'STOP-INPUT').unlink()
        self.assertEqual(self.make(self._paint(FakeEden({'to-red': RED}), (32, 18))).run(), 'DONE')
        receipt = json.loads((self.dir / 'out' / 'r1.json').read_text())
        self.assertEqual(receipt['status'], 'PASS')
        self.assertEqual(receipt['scale'][0]['scale'], 'fit')
        self.assertEqual(receipt['scale'][0]['frame'], [32, 18])
        self.assertEqual(receipt['scale'][0]['reference'], [64, 36])
        self.assertEqual(receipt['results'][1]['scale']['scale'], 'fit')

        # The plan can opt in, and a different aspect is still compared at its own size.
        self.plan['gates']['red'].pop('scale')
        self.plan['scale'] = 'fit'
        self.manifest.write_text(json.dumps(self.plan))
        (self.dir / 'out' / 'r1.json').unlink()
        self.assertEqual(self.make(self._paint(FakeEden({'to-red': RED}), (32, 18))).run(), 'DONE')
        self.plan.pop('scale')
        self.plan['gates']['red']['scale'] = 'fit'
        self.manifest.write_text(json.dumps(self.plan))
        (self.dir / 'out' / 'r1.json').unlink()
        with self.assertRaises(RuntimeError):
            self.make(self._paint(FakeEden({'to-red': RED}), (40, 30))).run()
        mismatched = json.loads((self.dir / 'out' / 'r1.json').read_text())
        self.assertEqual(mismatched['status'], 'FAILED')
        self.assertNotIn('scale', mismatched)

    def test_receipt_warns_when_a_passing_hook_checked_nothing(self):
        (self.dir / 'always.py').write_text('def check(path, gate):\n    return True\n')
        self.plan['gates']['empty'] = {
            'kind': 'hook', 'module': 'always.py', 'function': 'check', 'cap': 1, 'interval': 0.01}
        self.plan['rows'] = [
            {'id': 'bare', 'start': 'blue', 'end': 'blue', 'steps': [{'gate': 'empty'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(self.make(FakeEden({})).run(), 'DONE')
        receipt = json.loads((self.dir / 'out' / 'bare.json').read_text())
        self.assertEqual(receipt['status'], 'PASS')
        self.assertIn('no screen or log evidence', receipt['warnings'][0])

        (self.dir / 'always.py').write_text('def check(path, gate):\n    return {"screen": "blue"}\n')
        (self.dir / 'out' / 'bare.json').unlink()
        self.assertEqual(self.make(FakeEden({})).run(), 'DONE')
        receipt = json.loads((self.dir / 'out' / 'bare.json').read_text())
        self.assertEqual(receipt['status'], 'PASS')
        self.assertNotIn('warnings', receipt)
        self.assertEqual(receipt['results'][0]['hook_evidence'], {'screen': 'blue'})

    def test_failure_kinds_early_stop_and_start_state(self):
        settings = self.dir / 'settings.json'
        settings.write_text('{"menu": {"x": 1}}\n')
        self.instances['a']['settings'] = str(settings)
        self.plan['gates']['saved'] = {'kind': 'json', 'source': 'settings', 'expect': {'menu.x': 2},
                                       'cap': 0.25, 'asserts': 'card'}
        self.plan['rows'] = [{'id': 'r1', 'start': 'blue', 'end': 'red', 'steps': [
            {'play': 'to-red', 'unchecked': True}, {'gate': 'red'}, {'gate': 'saved'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        with self.assertRaises(RuntimeError):
            self.make(FakeEden({'to-red': RED}, log_path=self.log)).run()
        receipt = json.loads((self.dir / 'out' / 'r1.json').read_text())
        self.assertEqual(receipt['failure_kind'], 'product-suspect')
        self.assertEqual(receipt['failure_cause'], 'card:saved')
        summary = json.loads((self.dir / 'out' / 'summary.json').read_text())
        self.assertEqual(summary['by_kind']['product-suspect'], ['r1'])

        self.plan.pop('gates')
        self.plan['gates'] = {'gone': {'kind': 'log', 'source': 'diag_log', 'pattern': 'missing', 'cap': 0.2}}
        self.plan['rows'] = [{'id': 'r1', 'start': 'blue', 'end': 'blue', 'steps': [{'gate': 'gone'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        shutil.rmtree(self.dir / 'out')
        with self.assertRaises(RuntimeError):
            self.make(FakeEden({})).run()
        missing = json.loads((self.dir / 'out' / 'r1.json').read_text())
        self.assertEqual(missing['failure_kind'], 'harness-suspect')
        self.assertEqual(missing['failure_cause'], 'missing-evidence:gone')

        Image.new('RGB', (64, 36), RED).save(self.dir / 'refs' / 'red.png')
        self.plan['gates'] = {
            'red': {'kind': 'image', 'reference': 'refs/red.png', 'sha256': sha(self.dir / 'refs' / 'red.png'),
                    'roi': [0, 0, 64, 36], 'cap': 0.3, 'interval': 0.05},
            'saved': {'kind': 'json', 'source': 'settings', 'expect': {'menu.x': 2}, 'cap': 0.25, 'asserts': 'card'},
        }
        self.plan['start'] = 'red'
        self.plan['early_stop'] = 2
        self.plan['on_failure'] = 'continue'
        self.plan['recover'] = []
        self.plan['rows'] = [
            {'id': 'r1', 'start': 'red', 'end': 'red', 'steps': [{'gate': 'red'}, {'gate': 'saved'}]},
            {'id': 'r2', 'start': 'red', 'end': 'red', 'start_gate': 'red', 'steps': [{'gate': 'red'}, {'gate': 'saved'}]},
            {'id': 'r3', 'start': 'red', 'end': 'red', 'steps': [{'gate': 'red'}, {'gate': 'saved'}]},
        ]
        self.manifest.write_text(json.dumps(self.plan))
        shutil.rmtree(self.dir / 'out')
        eden = FakeEden({})
        eden.colour = RED
        with self.assertRaises(runner.harness.EarlyStop):
            self.make(eden).run()
        stopped = json.loads((self.dir / 'out' / 'summary.json').read_text())
        self.assertTrue(stopped['result'].startswith('probable shared cause:'))
        self.assertEqual(stopped['early_stop']['count'], 2)
        self.assertEqual([row['status'] for row in stopped['rows']], ['fail', 'fail', 'not run'])
        self.assertEqual(stopped['by_cause']['card:saved'], ['r1', 'r2'])

        self.plan['rows'][0]['start'] = 'elsewhere'
        self.plan['early_stop'] = 3
        self.manifest.write_text(json.dumps(self.plan))
        shutil.rmtree(self.dir / 'out')
        with self.assertRaises(runner.StopRun) as caught:
            self.make(FakeEden({})).run()
        self.assertIn('starts on elsewhere', str(caught.exception))
        mismatched = json.loads((self.dir / 'out' / 'summary.json').read_text())
        self.assertEqual(mismatched['rows'][0]['failure_kind'], 'harness-suspect')
        self.assertIn('start-state:elsewhere', mismatched['rows'][0]['failure_cause'])

    def test_fixture_replay_runs_gates_and_step_hooks_without_eden(self):
        settings = self.dir / 'settings.json'
        settings.write_text('{"menu": {"x": 1}}\n')
        self.instances['a']['settings'] = str(settings)
        (self.dir / 'steps.py').write_text(
            'def check_card(ctx):\n'
            '    with open(ctx.instance["settings"]) as handle:\n'
            '        text = handle.read()\n'
            '    if \'"x": 1\' not in text:\n'
            '        raise RuntimeError("card")\n'
            '    return "card"\n')
        self.plan['capture_fixtures'] = True
        self.plan['rows'] = [{'id': 'r1', 'start': 'blue', 'end': 'red', 'steps': [
            {'play': 'to-red', 'unchecked': True}, {'gate': 'red'}, {'run': 'steps.py:check_card'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(self.make(FakeEden({'to-red': RED})).run(), 'DONE')
        fixture = self.dir / 'out' / 'fixtures' / 'r1'
        self.assertTrue((fixture / 'fixture.json').is_file())
        report = runner.harness.replay_fixture(self.manifest, fixture, self.dir / 'replay-out', runner.Runner)
        self.assertTrue(report['ok'], report)
        self.assertEqual([item['name'] for item in report['checks']], ['red', 'steps.py:check_card'])
        card = fixture / 'a-settings.json'
        card.write_text('{"menu": {"x": 0}}\n')
        refused = runner.harness.replay_fixture(self.manifest, fixture, self.dir / 'replay-out-2', runner.Runner)
        self.assertFalse(refused['ok'])
        self.assertIn('card', refused['checks'][1]['error'])

    def test_self_test_reports_each_declared_recovery(self):
        menu, popup = (20, 180, 40), (180, 180, 20)
        Image.new('RGB', (64, 36), BLUE).save(self.dir / 'refs' / 'blue.png')
        Image.new('RGB', (64, 36), menu).save(self.dir / 'refs' / 'menu.png')
        Image.new('RGB', (64, 36), popup).save(self.dir / 'refs' / 'popup.png')
        for name in ('to-menu', 'to-popup', 'to-blue'):
            source = self.dir / 'segments' / 'to-red.edenmacro.json'
            target = self.dir / 'segments' / f'{name}.edenmacro.json'
            target.write_bytes(source.read_bytes())
            self.plan['segments'][name] = {'file': f'segments/{name}.edenmacro.json', 'sha256': sha(target)}
        image = {'kind': 'image', 'roi': [0, 0, 64, 36], 'cap': 0.4, 'interval': 0.05}
        self.plan['gates']['blue'] = {**image, 'reference': 'refs/blue.png', 'sha256': sha(self.dir / 'refs' / 'blue.png')}
        self.plan['gates']['menu'] = {**image, 'reference': 'refs/menu.png', 'sha256': sha(self.dir / 'refs' / 'menu.png')}
        self.plan['gates']['popup'] = {**image, 'reference': 'refs/popup.png', 'sha256': sha(self.dir / 'refs' / 'popup.png')}
        self.plan['recover'] = [{'play': 'to-blue', 'unchecked': True}]
        self.plan['screen_classes'] = [
            {'class': 'menu', 'reach': [{'play': 'to-menu', 'unchecked': True}], 'gate': 'menu'},
            {'class': 'pop-up', 'reach': [{'play': 'to-popup', 'unchecked': True}], 'gate': 'popup'},
        ]
        self.plan['rows'] = [{'id': 'r1', 'start': 'blue', 'end': 'blue', 'start_gate': 'blue',
                              'steps': [{'shot': 'stay'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        eden = FakeEden({'to-menu': menu, 'to-popup': popup, 'to-blue': BLUE})
        eden.colour = BLUE
        report = self.make(eden).self_test()
        self.assertTrue(report['ok'], report)
        classes = [item.get('class') for item in report['recoveries'] if item['check'] == 'recovery']
        self.assertEqual(classes, ['menu', 'pop-up'])
        eden.screens['to-blue'] = menu
        broken = self.make(eden).self_test()
        self.assertFalse(broken['ok'])
        self.assertTrue(any(not item['ok'] and item['check'] == 'recovery' for item in broken['recoveries']))

    def test_dry_run_rejects_a_bad_early_stop_and_screen_class(self):
        self.plan['early_stop'] = True
        self.plan['screen_classes'] = [{'class': 'menu'}]
        self.manifest.write_text(json.dumps(self.plan))
        report = runner.dry_run(self.manifest)
        self.assertTrue(any('early_stop' in item for item in report['errors']))
        self.assertTrue(any('screen class' in item for item in report['errors']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
