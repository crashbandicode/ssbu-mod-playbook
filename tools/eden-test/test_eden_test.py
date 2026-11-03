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
        (self.rows / 'rows.json').write_text('{"rows": []}')

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
        (self.rows / 'rows.json').write_text('{"rows": [], "note": "fail-once"}')
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
        (self.rows / 'rows.json').write_text('{"rows": [], "note": "fail-once"}')
        eden_test.main(['new', *self.args()])
        run = self.dir / 'runs/lr-14'
        eden_test.main(['up', str(run)])
        self.assertEqual(eden_test.main(['run', str(run)]), 3)
        self.assertEqual(eden_test.main(['run', str(run), '--retry']), 0)
        self.assertEqual(json.loads((run / 'out/r2.json').read_text())['status'], 'PASS')


if __name__ == '__main__':
    unittest.main(verbosity=2)
