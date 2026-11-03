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
                return {'ok': True, 'result': {'state': 'recording'}}
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
                {'id': 'r1', 'start': 'blue', 'end': 'red', 'steps': [{'play': 'to-red'}, {'gate': 'red'}]},
                {'id': 'r2', 'start': 'red', 'end': 'red-paused', 'steps': [{'play': 'pause'}, {'gate': 'paused'}]},
                {'id': 'r3', 'start': 'red-paused', 'end': 'blue', 'steps': [{'play': 'to-blue'}, {'shot': 'end'}]},
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
            {'play': 'to-red', 'on': 'b'},
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

    def test_dry_run_catches_change_gate_before_mark_and_diff_without_copy(self):
        self.plan['gates']['hud-changed'] = {'kind': 'change', 'mark': 'hud', 'roi': [0, 0, 64, 36], 'cap': 3}
        self.plan['rows'] = [{'id': 'c1', 'start': 'blue', 'end': 'red', 'steps': [
            {'gate': 'hud-changed'}, {'json_diff': {'source': 'settings', 'before': 'before'}}]}]
        self.manifest.write_text(json.dumps(self.plan))
        errors = ' | '.join(runner.dry_run(self.manifest)['errors'])
        self.assertIn('before its mark', errors)
        self.assertIn('has no earlier copy', errors)

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
