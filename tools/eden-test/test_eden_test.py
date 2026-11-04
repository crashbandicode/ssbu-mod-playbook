#!/usr/bin/env python3
"""Offline tests for eden_test.py: fake lifecycle/runner/harvest scripts, temp run folders, no Eden."""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eden_test  # noqa: E402

FAKE_LIFECYCLE = '''
import json, sys
from pathlib import Path
mode, config = sys.argv[1], json.loads(Path(sys.argv[2]).read_text())
state = Path(config['state_dir']); state.mkdir(parents=True, exist_ok=True)
if mode == 'up':
    (state / 'instances.json').write_text(json.dumps({'a': {'socket': 'fake'}}))
    (state / 'lifecycle.json').write_text(json.dumps({'phase': 'up', 'instances': [{'key': 'a', 'unit': 'none.service'}]}))
else:
    j = json.loads((state / 'lifecycle.json').read_text()); j.update(phase='down', released=True, cleanup_failures=[])
    (state / 'lifecycle.json').write_text(json.dumps(j))
print(mode, 'ok')
'''
FAKE_RUNNER = '''
import json, sys
from pathlib import Path
mode = sys.argv[1]; out = Path(sys.argv[sys.argv.index('--out') + 1]); out.mkdir(parents=True, exist_ok=True)
if mode == 'dry-run':
    print('dry ok'); sys.exit(0)
(out / 'r1.json').write_text(json.dumps({'status': 'PASS'}))
fail = (out / 'STOP-INPUT').exists() is False and Path(sys.argv[2]).read_text().count('fail-once')
if fail and not list(out.glob('STOP-INPUT.*')):
    (out / 'r2.json').write_text(json.dumps({'status': 'FAILED', 'error': 'gate x did not pass'}))
    (out / 'STOP-INPUT').write_text('r2'); sys.exit(3)
(out / 'r2.json').write_text(json.dumps({'status': 'PASS'}))
'''
FAKE_HARVEST = 'print(\'{"segments": [], "gates": []}\')\n'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class EdenTestTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='eden-test-test-'))
        for name, body in (('lifecycle.py', FAKE_LIFECYCLE), ('runner.py', FAKE_RUNNER), ('harvest.py', FAKE_HARVEST)):
            (self.dir / name).write_text(body)
        self.saved = (eden_test.LIFECYCLE, eden_test.RUNNER, eden_test.HARVEST, eden_test.RUNS)
        eden_test.LIFECYCLE, eden_test.RUNNER, eden_test.HARVEST = (self.dir / 'lifecycle.py', self.dir / 'runner.py',
                                                                     self.dir / 'harvest.py')
        eden_test.RUNS = self.dir / 'runs'
        app = self.dir / 'Eden.AppImage'
        app.write_bytes(b'eden')
        nro = self.dir / 'plugin.nro'
        nro.write_bytes(b'nro')
        self.previous = self.dir / 'prev-config.json'
        self.previous.write_text(json.dumps({
            'owner': 'OpenCode x/y', 'label': 'lr-13', 'host': 'yoga', 'state_dir': '/old/state', 'work_root': '/old/work',
            'runner': {'path': '/old/runner.py', 'sha256': '0' * 64},
            'instances': [{'key': 'a', 'app': {'path': str(app), 'sha256': sha(app)},
                           'plugins': [{'path': str(nro), 'sha256': sha(nro)}], 'game': '/games/x.xci'}]}))
        self.rows = self.dir / 'rows-src'
        self.rows.mkdir()
        (self.rows / 'rows.json').write_text('{"setup": [{"state": "Training", "via": "segment", "ref": "s1"}], "rows":[]}')

    def tearDown(self):
        eden_test.LIFECYCLE, eden_test.RUNNER, eden_test.HARVEST, eden_test.RUNS = self.saved
        shutil.rmtree(self.dir)

    def args(self, *extra):
        return ['--from', str(self.previous), '--label', 'lr-14', '--rows', str(self.rows), *extra]

    def test_new_makes_fresh_paths_and_keeps_pins(self):
        eden_test.main(['new', *self.args()])
        config = json.loads((self.dir / 'runs/lr-14/config.json').read_text())
        self.assertEqual((config['label'], config['state_dir']), ('lr-14', str(self.dir / 'runs/lr-14/state')))
        self.assertEqual(config['runner']['path'], str(eden_test.RUNNER))  # never the old staged copy
        self.assertEqual(config['rows']['sha256'], sha(self.dir / 'runs/lr-14/rows/rows.json'))
        with self.assertRaises(SystemExit):  # labels are never reused
            eden_test.main(['new', *self.args()])

    def test_new_refuses_a_changed_pin(self):
        (self.dir / 'plugin.nro').write_bytes(b'different')
        with self.assertRaises(SystemExit) as caught:
            eden_test.main(['new', *self.args()])
        self.assertIn('pin changed', str(caught.exception))

    def test_plugin_swaps_the_same_named_pin_and_records_it(self):
        rc9 = self.dir / 'rc9'
        rc9.mkdir()
        (rc9 / 'plugin.nro').write_bytes(b'rc9 build')
        eden_test.main(['new', *self.args('--plugin', str(rc9 / 'plugin.nro'))])
        config = json.loads((self.dir / 'runs/lr-14/config.json').read_text())
        pin = config['instances'][0]['plugins'][0]
        self.assertEqual((pin['path'], pin['sha256']), (str(rc9 / 'plugin.nro'), sha(rc9 / 'plugin.nro')))
        self.assertEqual(config['plugin_swaps'][0]['from'], str(self.dir / 'plugin.nro'))
        with self.assertRaises(SystemExit) as caught:  # an unknown file name is refused, not added
            eden_test.main(['new', '--from', str(self.previous), '--label', 'lr-15', '--rows', str(self.rows),
                            '--plugin', str(self.dir / 'Eden.AppImage')])
        self.assertIn('no pinned plugin', str(caught.exception))

    def test_classify_exit_flags_core_dumps_and_the_cap(self):
        self.assertTrue(eden_test.classify_exit({'Result': 'timeout', 'ExecMainCode': '3', 'ExecMainStatus': '11'})
                        .startswith('CRASH (core dumped, signal 11); the runtime cap fired'))  # p4za
        self.assertTrue(eden_test.classify_exit({'Result': 'signal', 'ExecMainCode': '2', 'ExecMainStatus': '9'})
                        .startswith('forced stop (SIGKILL), not a crash'))
        self.assertTrue(eden_test.classify_exit({'Result': 'core-dump', 'ExecMainCode': '3', 'ExecMainStatus': '7'})
                        .startswith('CRASH (core dumped, signal 7)'))  # lr-12 SIGBUS

    def test_explicit_defer_load_leaves_boot_setup_to_rows(self):  # pkg4
        previous = json.loads(self.previous.read_text())
        previous['defer_load'] = False
        self.previous.write_text(json.dumps(previous))
        eden_test.main(['new', *self.args('--defer-load')])
        config = json.loads((self.dir / 'runs/lr-14/config.json').read_text())
        self.assertTrue(config['defer_load'])

    def test_new_rebases_pinned_qt_without_changing_original_or_other_pins(self):
        qt = self.dir / 'old-qt.ini'
        original = '[Data Storage]\nnand_directory=/old/private/nand\nsdmc_directory=/old/private/sdmc\n[Network]\nport=24872\n'
        qt.write_text(original)
        previous = json.loads(self.previous.read_text())
        previous['instances'][0]['files'] = [{'path':str(qt), 'sha256':sha(qt), 'to':'user/config/qt-config.ini'}]
        self.previous.write_text(json.dumps(previous))
        eden_test.main(['new', *self.args()])
        config = json.loads((self.dir / 'runs/lr-14/config.json').read_text())
        pin = config['instances'][0]['files'][0]
        derived = Path(pin['path'])
        self.assertEqual(qt.read_text(), original)
        self.assertEqual(sha(derived), pin['sha256'])
        self.assertIn('port=24872', derived.read_text())
        self.assertIn('nand_directory=' + str(self.dir / 'runs/lr-14/work/profiles/profile-a/user/nand'), derived.read_text())
        self.assertIn('sdmc_directory=' + str(self.dir / 'runs/lr-14/work/profiles/profile-a/user/sdmc'), derived.read_text())
        self.assertEqual(config['instances'][0]['plugins'], previous['instances'][0]['plugins'])

    def test_qt_rebase_still_refuses_changed_pin_before_derivation(self):
        qt = self.dir / 'old-qt.ini'
        qt.write_text('nand_directory=/x\nsdmc_directory=/y\n')
        previous = json.loads(self.previous.read_text())
        previous['instances'][0]['files'] = [{'path':str(qt),'sha256':'0'*64,'to':'user/config/qt-config.ini'}]
        self.previous.write_text(json.dumps(previous))
        with self.assertRaises(SystemExit):
            eden_test.main(['new', *self.args()])

    def test_all_runs_down_even_after_a_failed_row_and_writes_results(self):
        (self.rows / 'rows.json').write_text('{"setup": [{"state": "Training", "via": "segment", "ref": "s1"}], "rows":[{"id": "r1"}, {"id": "r2"}], "note": "fail-once"}')
        eden_test.main(['all', *self.args()])
        run = self.dir / 'runs/lr-14'
        journal = json.loads((run / 'state/lifecycle.json').read_text())
        self.assertTrue(journal['released'])
        results = (run / 'RESULTS.md').read_text()
        self.assertIn('1 PASS, 1 FAILED', results)
        self.assertIn('FAILED r2: gate x did not pass', results)
        self.assertLessEqual(len(results.splitlines()), 20)
        timing = json.loads((run / 'timing.json').read_text())
        self.assertTrue({'new', 'up_start', 'up_done', 'run_start', 'run_end', 'down_start', 'down_done'} <= set(timing))

    def test_retry_clears_stop_input_and_reruns_on_the_same_boot(self):
        (self.rows / 'rows.json').write_text('{"setup": [{"state": "Training", "via": "segment", "ref": "s1"}], "rows":[], "note": "fail-once"}')
        eden_test.main(['new', *self.args()])
        run = self.dir / 'runs/lr-14'
        eden_test.main(['up', str(run)])
        self.assertEqual(eden_test.main(['run', str(run)]), 3)
        self.assertEqual(eden_test.main(['run', str(run), '--retry']), 0)
        self.assertEqual(json.loads((run / 'out/r2.json').read_text())['status'], 'PASS')

    def test_results_count_only_rows_and_add_up_retries(self):
        (self.rows / 'rows.json').write_text('{"setup": [{"state": "Training", "via": "segment", "ref": "s1"}], "rows":[{"id": "r1"}, {"id": "r2"}, {"id": "r3"}], "note": "fail-once"}')
        eden_test.main(['new', *self.args()])
        run = self.dir / 'runs/lr-14'
        eden_test.main(['up', str(run)])
        eden_test.main(['run', str(run)])
        (run / 'out/r2.json').rename(run / 'out/r2.FAILED.first-try.json')  # an agent keeps the failed attempt
        (run / 'out/project-state.json').write_text('{"phase": "whatever"}')  # not a row receipt
        (run / 'rows/rows.json').write_text('{"setup": [{"state": "Training", "via": "segment", "ref": "s1"}], "rows":[{"id": "r1"}, {"id": "r2"}, {"id": "r3"}], "note": "fixed"}')
        eden_test.main(['run', str(run), '--retry'])
        eden_test.HARVEST.write_text('print(\'{\\n  "segments": [1, 2],\\n  "gates": []\\n}\')\n')  # multi-line JSON
        eden_test.main(['down', str(run)])
        results = (run / 'RESULTS.md').read_text()
        self.assertIn('2 PASS, 0 FAILED, 1 unrun; 1 earlier failed attempts retained', results)
        self.assertIn('unrun: r3', results)
        self.assertIn('changed during the boot', results)
        self.assertIn('Harvest: segments 2, gates 0', results)
        self.assertIn('over 2 run call(s)', results)
        self.assertLessEqual(len(results.splitlines()), 20)
        self.assertEqual(len(json.loads((run / 'timing.json').read_text())['runs']), 2)

    def test_up_refuses_a_missing_or_vague_setup_plan(self):
        (self.rows / 'rows.json').write_text('{"rows": [{"id": "r1"}]}')
        eden_test.main(['new', *self.args()])
        run = self.dir / 'runs/lr-14'
        with self.assertRaises(SystemExit) as caught:
            eden_test.main(['up', str(run)])
        self.assertIn('no "setup" list', str(caught.exception))
        self.assertEqual(eden_test.setup_plan_problems({'setup': [{'state': 'CPU Kirby', 'via': 'by hand'}]}),
                         ['setup[0] needs "state", "via" (fixture/segment/record/hook) and "ref"'])
        eden_test.main(['up', str(run), '--skip-setup-plan'])  # re-running recorded rows stays possible
        self.assertTrue((run / 'state/instances.json').exists())

    def test_pace_counts_hand_driven_inputs_not_runner_or_recorded_ones(self):
        (self.rows / 'rows.json').write_text('{"setup": [{"state": "Training", "via": "segment", "ref": "s1"}], '
                                             '"rows": [{"id": "r1"}, {"id": "r2"}]}')
        eden_test.main(['new', *self.args()])
        run = self.dir / 'runs/lr-14'
        eden_test.main(['up', str(run)])
        control = run / 'state/run-a/control.jsonl'
        control.parent.mkdir(parents=True, exist_ok=True)
        req = lambda m: json.dumps({'event': 'request', 'method': m, 'time': '2026-11-04T02:00:00Z'})
        lines = [req('macro.record_start')] + [req('input.button')] * 30 + [req('macro.stop')]  # recording: fine
        lines += [req('input.button')] * 50  # outside recordings: 5 of them are the runner's hook presses
        control.write_text('\n'.join(lines) + '\n')
        (run / 'out').mkdir(exist_ok=True)
        (run / 'out/actions.jsonl').write_text('\n'.join(json.dumps({'method': 'input.button'}) for _ in range(5)) + '\n')
        report = eden_test.pace_report(run)
        self.assertEqual((report['manual_inputs'], report['runner_inputs']), (45, 5))
        self.assertIn('45 hand-driven inputs (budget 40)', report['over'][0])
        self.assertEqual(eden_test.main(['pace', str(run)]), 2)
        up = json.loads((run / 'timing.json').read_text())['up_done']
        control.write_text(req('macro.play') + '\n')
        late = eden_test.pace_report(run, now=up + 16 * 60)  # no row passed 16 minutes after Up
        self.assertEqual(len(late['over']), 1)
        self.assertIn('without a newly passed row', late['over'][0])

    def test_pace_stops_near_the_runtime_cap(self):
        (self.rows / 'rows.json').write_text('{"setup": [{"state": "Training", "via": "segment", "ref": "s1"}], '
                                             '"rows": [{"id": "r1"}]}')
        eden_test.main(['new', *self.args()])
        run = self.dir / 'runs/lr-14'
        config = json.loads((run / 'config.json').read_text())
        config['runtime'] = 1200
        (run / 'config.json').write_text(json.dumps(config))
        eden_test.main(['up', str(run)])
        up = json.loads((run / 'timing.json').read_text())['up_done']
        early = eden_test.pace_report(run, now=up + 10 * 60)
        self.assertEqual((early['minutes_to_cap'], early['over']), (10.0, []))
        late = eden_test.pace_report(run, now=up + 16 * 60)  # 4 minutes left
        self.assertTrue(any('to the runtime cap' in reason for reason in late['over']))

    def test_down_uses_the_up_time_config_and_keeps_an_edited_one_aside(self):
        eden_test.main(['new', *self.args()])
        run = self.dir / 'runs/lr-14'
        eden_test.main(['up', str(run), '--skip-setup-plan'])
        up_config = json.loads((run / 'config.json').read_text())
        journal = json.loads((run / 'state/lifecycle.json').read_text())
        journal['config'] = up_config
        (run / 'state/lifecycle.json').write_text(json.dumps(journal))
        (run / 'config.json').write_text(json.dumps(dict(up_config, owner='edited after Up')))
        aside = eden_test.restore_up_config(run)
        self.assertEqual(json.loads((run / 'config.json').read_text()), up_config)
        self.assertEqual(json.loads(aside.read_text())['owner'], 'edited after Up')
        self.assertIsNone(eden_test.restore_up_config(run))  # nothing to do the second time


if __name__ == '__main__':
    unittest.main(verbosity=2)
