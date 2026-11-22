#!/usr/bin/env python3
"""Offline tests for eden_test.py: fake lifecycle/runner/harvest scripts, temp run folders, no Eden."""
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    if '--keep-state' in sys.argv:
        j['prune'] = {'kept': 'now'}
    elif '--evidence-dir' in sys.argv:
        out = Path(sys.argv[sys.argv.index('--evidence-dir') + 1]); (out / 'post-run-a').mkdir(parents=True, exist_ok=True)
        (out / 'post-run-a/run.log').write_text('run bytes')
        j['prune'] = {'done': 'now', 'evidence': str(out), 'evidence_saved': 'now', 'copied_files': 1, 'listed_files': 0,
                      'bytes': 3 * 10 ** 9, 'unique_bytes': 2 * 10 ** 9, 'disk_free_before': 10 ** 11,
                      'disk_free_after': 10 ** 11 + 2 * 10 ** 9, 'skipped': [], 'left': []}
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


class SetupFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / 'run'
        self.rows = self.run / 'rows'
        self.rows.mkdir(parents=True)
        self.reviewed = self.rows / 'reviewed.json'
        self.fixture = self.rows / 'fixture.json'
        self.reviewed.write_text(json.dumps({'last_page': 2, 'menu': {'a': [0, 0], 'b': [5, 6]}}))
        self.fixture.write_text(json.dumps({'menu': {'a': [1, 0], 'b': [5, 8]}}))
        self.item = {'state': 'Training', 'via': 'fixture', 'ref': 'settings', 'to': 'TrainingRooms/settings.json',
                     'reviewed': {'path': 'reviewed.json', 'sha256': sha(self.reviewed)}}
        self.config = {'instances': [{'key': 'a', 'files': [{'to': self.item['to'], 'path': str(self.fixture),
                                                            'sha256': sha(self.fixture)}]}]}
        (self.run / 'config.json').write_text(json.dumps(self.config))

    def plan(self):
        (self.rows / 'rows.json').write_text(json.dumps({'setup': [self.item], 'rows': []}))

    def test_fixture_diff_nested_lists_types_and_root(self):
        self.assertEqual(eden_test.fixture_diff(
            {'nest': {'a': 1}, 'list': [0, {'x': 2}], 'old': 1},
            {'nest': {'a': 3}, 'list': [0, {'x': 4}], 'new': 1}),
            ['list.1.x', 'nest.a', 'new', 'old'])
        self.assertEqual(eden_test.fixture_diff({'list': [1, 2]}, {'list': [1]}), ['list'])
        self.assertEqual(eden_test.fixture_diff({'value': 1}, {'value': True}), ['value'])
        self.assertEqual(eden_test.fixture_diff([1], {'one': 1}), [''])
        self.assertEqual(eden_test.fixture_diff({'x': [1, 2]}, {'x': [1, 2]}), [])

    def test_up_refuses_differences_and_writes_evidence(self):
        self.plan()
        with mock.patch.object(eden_test, 'sh') as sh:
            with self.assertRaises(SystemExit) as caught:
                eden_test.cmd_up(self.run)
        self.assertIn('last_page', str(caught.exception))
        self.assertIn('Training', str(caught.exception))
        self.assertIn('3', str(caught.exception))
        sh.assert_not_called()
        evidence = json.loads((self.run / 'setup-fixtures.json').read_text())
        self.assertEqual(evidence[0]['differing_paths'], ['last_page', 'menu.a.0', 'menu.b.1'])
        self.assertFalse(evidence[0]['ok'])

    def test_skip_setup_plan_does_not_skip_reviewed_fixture(self):
        self.plan()
        with mock.patch.object(eden_test, 'sh') as sh:
            with self.assertRaises(SystemExit) as caught:
                eden_test.cmd_up(self.run, skip_setup_plan=True)
        self.assertIn('last_page', str(caught.exception))
        sh.assert_not_called()

    def test_allowed_exact_and_parent_paths_pass(self):
        self.item['differs_only'] = ['last_page', 'menu.a', 'menu.b.1']
        self.plan()
        with mock.patch.object(eden_test, 'sh', return_value=0), \
                mock.patch.object(eden_test, 'lifecycle', return_value=0):
            eden_test.cmd_up(self.run)
        evidence = json.loads((self.run / 'setup-fixtures.json').read_text())
        self.assertTrue(evidence[0]['ok'])
        self.assertEqual(evidence[0]['reviewed'], self.item['reviewed'])
        self.assertEqual(evidence[0]['pin']['path'], str(self.fixture))

    def test_wrong_reviewed_hash_is_refused(self):
        self.item['reviewed']['sha256'] = '0' * 64
        problems = eden_test.fixture_problems(self.item, self.rows, self.config)
        self.assertTrue(any('sha256' in p for p in problems))

    def test_missing_fixture_is_refused(self):
        self.config['instances'][0]['files'][0]['path'] = str(self.rows / 'missing.json')
        self.assertTrue(eden_test.fixture_problems(self.item, self.rows, self.config))

    def test_invalid_json_is_refused(self):
        self.fixture.write_text('{')
        self.assertTrue(eden_test.fixture_problems(self.item, self.rows, self.config))

    def test_null_fixture_root_is_compared(self):
        self.fixture.write_text('null')
        self.assertIn('<root>', ' '.join(eden_test.fixture_problems(self.item, self.rows, self.config)))

    def test_review_without_to_is_refused(self):
        del self.item['to']
        self.assertTrue(eden_test.fixture_problems(self.item, self.rows, self.config))

    def test_unreviewed_item_is_untouched(self):
        del self.item['reviewed']
        self.assertEqual(eden_test.fixture_problems(self.item, self.rows, self.config), [])
        self.assertEqual(eden_test.setup_plan_problems({'setup': [self.item]}), [])
        self.assertEqual(eden_test.setup_plan_problems({'setup': [self.item]}, self.rows, self.config), [])


class PinnedFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / 'runs' / 'run'
        self.rows = self.run / 'rows'
        self.rows.mkdir(parents=True)
        self.reviewed = self.rows / 'reviewed.json'
        self.reviewed.write_text('{"last_page": 2}')
        self.clean = self.rows / 'clean.json'
        self.clean.write_text(self.reviewed.read_text())
        self.actual = Path(self.tmp.name) / 'actual.json'
        self.actual.write_text('{"last_page": 9}')
        self.destination = 'user/sdmc/ultimate/TrainingRooms/settings.json'
        self.item = {'state': 'Training', 'via': 'fixture', 'ref': 'settings', 'to': self.destination,
                     'reviewed': {'path': 'reviewed.json', 'sha256': sha(self.reviewed)}}
        self.config = {'instances': [{'key': 'a', 'files': [
            {'to': './' + self.destination, 'path': str(self.actual), 'sha256': sha(self.actual)}]}]}

    def save(self):
        (self.rows / 'rows.json').write_text(json.dumps({'setup': [self.item], 'rows': []}))
        (self.run / 'config.json').write_text(json.dumps(self.config))

    def check(self, *flags):
        self.save()
        return eden_test.main(['check', str(self.run), *flags])

    def test_up_refuses_clean_declaration_when_installed_pin_differs(self):
        self.item['file'] = 'clean.json'
        self.save()
        with mock.patch.object(eden_test, 'sh', return_value=0) as sh, \
                mock.patch.object(eden_test, 'lifecycle', return_value=0):
            with self.assertRaises(SystemExit) as caught:
                eden_test.cmd_up(self.run)
        self.assertIn('last_page', str(caught.exception))
        sh.assert_not_called()

    def test_matching_pin_passes_check_and_writes_comparison(self):
        self.config['instances'][0]['files'][0].update(path=str(self.clean), sha256=sha(self.clean))
        self.assertEqual(self.check(), 0)
        entry = json.loads((self.run / 'setup-fixtures.json').read_text())[0]
        self.assertEqual((entry['state'], entry['instance'], entry['to']), ('Training', 'a', self.destination))
        self.assertEqual((entry['pin']['path'], entry['pin']['sha256']), (str(self.clean), sha(self.clean)))
        self.assertEqual((entry['reviewed']['path'], entry['reviewed']['sha256']),
                         ('reviewed.json', sha(self.reviewed)))
        self.assertEqual(entry['differing_paths'], [])
        self.assertTrue(entry['ok'])

    def test_multiple_instances_and_explicit_instance_selection(self):
        self.config['instances'][0]['files'][0].update(path=str(self.clean), sha256=sha(self.clean))
        self.config['instances'].append({'key': 'b', 'files': [
            {'to': self.destination, 'path': str(self.actual), 'sha256': sha(self.actual)}]})
        self.assertEqual(self.check(), 2)
        entries = json.loads((self.run / 'setup-fixtures.json').read_text())
        self.assertEqual([(e['instance'], e['ok']) for e in entries], [('a', True), ('b', False)])
        self.item['instance'] = 'a'
        self.assertEqual(self.check(), 0)
        self.item['instance'] = 'b'
        self.assertEqual(self.check(), 2)

    def test_per_instance_results_remain_independent(self):
        self.config['instances'].append({'key': 'b', 'files': [
            {'to': self.destination, 'path': str(self.clean), 'sha256': sha(self.clean)}]})
        self.assertEqual(self.check(), 2)
        entries = json.loads((self.run / 'setup-fixtures.json').read_text())
        self.assertEqual([(e['instance'], e['ok']) for e in entries], [('a', False), ('b', True)])

    def test_last_pin_is_the_one_installed_for_a_duplicate_destination(self):
        self.config['instances'][0]['files'].append(
            {'to': self.destination, 'path': str(self.clean), 'sha256': sha(self.clean)})
        self.assertEqual(self.check(), 0)
        entry = json.loads((self.run / 'setup-fixtures.json').read_text())[0]
        self.assertEqual(entry['pin']['path'], str(self.clean))

    def test_bad_pin_hash_and_missing_pin_are_refused(self):
        self.config['instances'][0]['files'][0]['sha256'] = '0' * 64
        self.assertEqual(self.check(), 2)
        entry = json.loads((self.run / 'setup-fixtures.json').read_text())[0]
        self.assertFalse(entry['ok'])
        self.item['instance'] = 'a'
        self.config['instances'][0]['files'] = []
        self.assertEqual(self.check(), 2)
        self.item['instance'] = 'missing'
        self.assertEqual(self.check(), 2)

    def test_legacy_file_and_missing_to_are_refused(self):
        self.config['instances'][0]['files'][0].update(path=str(self.clean), sha256=sha(self.clean))
        self.item['file'] = 'clean.json'
        self.save()
        with mock.patch.object(eden_test, 'sh', return_value=0) as sh, \
                mock.patch.object(eden_test, 'lifecycle', return_value=0):
            with self.assertRaises(SystemExit) as caught:
                eden_test.cmd_up(self.run)
        self.assertIn('to', str(caught.exception))
        sh.assert_not_called()
        del self.item['file']
        del self.item['to']
        self.assertEqual(self.check(), 2)

    def test_skip_setup_plan_does_not_skip_pin_comparison(self):
        self.assertEqual(self.check('--skip-setup-plan'), 2)


class TimeSplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        (self.run / 'rows').mkdir()
        (self.run / 'out').mkdir()
        self.rows = [
            {'id': 'setup', 'start': 'menu', 'end': 'training'},
            {'id': 'test', 'start': 'training', 'end': 'training', 'role': 'setup'},
            {'id': 'unfinished', 'start': 'training', 'end': 'training', 'role': 'setup'},
        ]
        (self.run / 'rows/rows.json').write_text(json.dumps(self.rows))
        (self.run / 'timing.json').write_text(json.dumps(
            {'up_start': 1, 'up_done': 11, 'runs': [[12, 62]], 'down_start': 70, 'down_done': 75}))
        for name, receipt in (
            ('setup', {'status': 'PASS', 'started': 10, 'ended': 18, 'role': 'setup'}),
            ('test', {'status': 'PASS', 'started': 20, 'ended': 40}),
            ('unfinished', {'status': 'FAILED', 'started': 50, 'role': 'setup'}),
        ):
            (self.run / 'out' / f'{name}.json').write_text(json.dumps(receipt))

    def test_split_uses_receipts_fallback_and_skips_unfinished(self):
        self.assertEqual(eden_test.time_split(self.run),
                         {'up': 10, 'setup': 8, 'test': 20, 'between_rows': 22,
                          'down': 5, 'harness': 45, 'test_share': 0.308})

    def test_missing_timing_and_receipts_are_zero(self):
        (self.run / 'timing.json').write_text('{}')
        (self.run / 'rows/rows.json').write_text('[]')
        self.assertEqual(eden_test.time_split(self.run),
                         {'up': 0, 'setup': 0, 'test': 0, 'between_rows': 0,
                          'down': 0, 'harness': 0, 'test_share': 0})

    def test_receipt_role_overrides_the_row_default(self):
        path = self.run / 'out/setup.json'
        receipt = json.loads(path.read_text())
        receipt['role'] = 'test'
        path.write_text(json.dumps(receipt))
        split = eden_test.time_split(self.run)
        self.assertEqual((split['setup'], split['test'], split['harness'], split['test_share']), (0, 28, 37, 0.431))

    def test_partial_timing_and_between_rows_floor(self):
        (self.run / 'timing.json').write_text(json.dumps(
            {'up_start': 10, 'runs': [[30, 35]], 'down_done': 70}))
        split = eden_test.time_split(self.run)
        self.assertEqual((split['up'], split['down'], split['between_rows'], split['harness'], split['test_share']),
                         (0, 0, 0, 8, 0.714))

    def test_results_timing_line_includes_the_split(self):
        config = {'label': 'fake', 'rows': {'sha256': sha(self.run / 'rows/rows.json')},
                  'instances': [{'app': {'path': '/fake', 'sha256': '0' * 64}}]}
        eden_test.results_md(self.run, config)
        timing = next(line for line in (self.run / 'RESULTS.md').read_text().splitlines() if line.startswith('- Timing:'))
        self.assertIn('harness 45 s, test 20 s (30.8 %)', timing)


class VolatilePinsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'runs'
        self.run = self.root / 'new'
        (self.run / 'rows').mkdir(parents=True)
        (self.run / 'rows/rows.json').write_text(json.dumps({
            'setup': [{'state': 'Training', 'via': 'segment', 'ref': 's1'}], 'rows': []}))
        self.pin = str(self.root / 'old' / 'plugins' / 'plugin.nro')
        (self.run / 'config.json').write_text(json.dumps(
            {'instances': [{'plugins': [{'path': self.pin, 'sha256': 'a' * 64}]}]}))

    def test_only_sibling_run_paths_are_listed(self):
        second = str(self.root / 'z-run' / 'data.json')
        pin = lambda value: {'path': value, 'sha256': 'a' * 64}
        config = {'nested': [{'plugin': pin(self.pin)}, pin(second), pin(self.pin)],
                  'shared': pin(str(self.root / '_shared-inputs' / 'plugin.nro')),
                  'own': pin(str(self.run / 'inputs' / 'settings.json')),
                  'relative': pin('old/plugins/plugin.nro'),
                  'outside': pin(str(self.root.parent / 'runs-other' / 'old' / 'plugin.nro')),
                  'folder': pin(str(self.root / 'old')),
                  'shared_folder': pin(str(self.root / '_shared-inputs')),
                  'own_folder': pin(str(self.run)),
                  'root': pin(str(self.root)),
                  'normalized': pin(str(self.root / 'old' / '..' / '_shared-inputs' / 'settings.json'))}
        self.assertEqual(eden_test.volatile_pins(config, self.run),
                         [(str(self.root / 'old'), 'old'), (self.pin, 'old'), (second, 'z-run')])

    def test_up_refuses_before_dry_run(self):
        with mock.patch.object(eden_test, 'sh') as sh:
            with self.assertRaises(SystemExit) as caught:
                eden_test.cmd_up(self.run)
        self.assertIn(self.pin, str(caught.exception))
        self.assertIn(str(self.root / '_<name>'), str(caught.exception))
        self.assertIn('janitor', str(caught.exception))
        sh.assert_not_called()
        self.assertEqual(json.loads((self.run / 'setup-fixtures.json').read_text()), [])

    def test_allow_flag_reaches_dry_run(self):
        with mock.patch.object(eden_test, 'sh', return_value=1) as sh, \
                mock.patch.object(eden_test, 'lifecycle') as lifecycle:
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(['up', str(self.run), '--allow-volatile-pins'])
        self.assertIn('dry run failed', str(caught.exception))
        sh.assert_called_once()
        lifecycle.assert_not_called()


class ActivePinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'runs'
        self.run = self.root / 'new'
        (self.run / 'rows').mkdir(parents=True)
        (self.run / 'rows/rows.json').write_text(json.dumps(
            {'setup': [{'state': 'Training', 'via': 'segment', 'ref': 's1'}], 'rows': []}))
        self.old = str(self.root / 'old' / 'plugins' / 'plugin.nro')
        self.shared = str(self.root / '_shared' / 'plugin.nro')
        self.config = {'plugin_swaps': [{'from': self.old, 'from_sha256': '1' * 64, 'to': self.shared}],
                       'notes': str(self.root / 'notes' / 'history'),
                       'instances': [{'key': 'a', 'plugins': [{'path': self.shared, 'sha256': '2' * 64}]}]}

    def save(self):
        (self.run / 'config.json').write_text(json.dumps(self.config))

    def test_historical_swap_path_does_not_block_up(self):
        self.save()
        with mock.patch.object(eden_test, 'sh', return_value=1) as sh:
            with self.assertRaises(SystemExit) as caught:
                eden_test.cmd_up(self.run)
        self.assertIn('dry run failed', str(caught.exception))
        sh.assert_called_once()

    def test_active_pin_source_and_game_are_listed_by_up(self):
        self.config['instances'][0].update(source=str(self.root / 'profile-run'),
                                           game=str(self.root / 'game-run' / 'game.xci'))
        pin = str(self.root / 'pin-run' / 'plugin.nro')
        self.config['instances'][0]['plugins'].append({'path': pin, 'sha256': 'a' * 64})
        self.save()
        with mock.patch.object(eden_test, 'sh') as sh:
            with self.assertRaises(SystemExit) as caught:
                eden_test.cmd_up(self.run)
        for path in (str(self.root / 'profile-run'), str(self.root / 'game-run' / 'game.xci'), pin):
            self.assertIn(path, str(caught.exception))
        self.assertNotIn(self.old, str(caught.exception))
        sh.assert_not_called()

    def test_pin_walk_matches_lifecycle(self):
        lifecycle_path = Path(__file__).resolve().parent.parent / 'eden-lifecycle' / 'lifecycle.py'
        spec = importlib.util.spec_from_file_location('lifecycle_for_pin_test', lifecycle_path)
        lifecycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lifecycle)
        sample = {'pin': {'path': self.old, 'sha256': 'a' * 64, 'nested': [
            {'path': self.shared, 'sha256': 'b' * 64}]}, 'bad': {'path': '/x', 'sha256': 'A' * 64},
            'plugin_swaps': self.config['plugin_swaps']}
        self.assertEqual([p['path'] for p in eden_test.iter_pin_nodes(sample)],
                         [p['path'] for p in lifecycle.iter_pin_nodes(sample)])

    def test_all_flags_reach_dry_run(self):
        self.config['instances'][0]['source'] = str(self.root / 'old')
        self.save()
        (self.run / 'rows/rows.json').write_text('{"rows": []}')
        argv = ['all', '--from', 'unused', '--label', 'unused', '--rows', 'unused',
                '--skip-setup-plan', '--allow-volatile-pins']
        with mock.patch.object(eden_test, 'cmd_new', return_value=self.run), \
                mock.patch.object(eden_test, 'sh', return_value=1) as sh, \
                mock.patch.object(eden_test, 'cmd_down') as down:
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(argv)
        self.assertIn('dry run failed', str(caught.exception))
        sh.assert_called_once()
        down.assert_not_called()


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

    def test_new_stages_and_up_checks_the_configured_file_pin(self):
        actual = self.dir / 'actual.json'
        actual.write_text('{"last_page": 9}')
        (self.rows / 'reviewed.json').write_text('{"last_page": 2}')
        (self.rows / 'clean.json').write_text('{"last_page": 2}')
        destination = 'user/sdmc/ultimate/TrainingRooms/settings.json'
        previous = json.loads(self.previous.read_text())
        previous['instances'][0]['files'] = [{'path': str(actual), 'sha256': sha(actual), 'to': destination}]
        self.previous.write_text(json.dumps(previous))
        item = {'state': 'Training', 'via': 'fixture', 'ref': 'settings', 'to': destination, 'file': 'clean.json',
                'reviewed': {'path': 'reviewed.json', 'sha256': sha(self.rows / 'reviewed.json')}}
        (self.rows / 'rows.json').write_text(json.dumps({'setup': [item], 'rows': []}))
        eden_test.main(['new', *self.args()])
        run = self.dir / 'runs/lr-14'
        with mock.patch.object(eden_test, 'sh', return_value=0) as sh, \
                mock.patch.object(eden_test, 'lifecycle', return_value=0):
            with self.assertRaises(SystemExit) as caught:
                eden_test.cmd_up(run)
        self.assertIn('last_page', str(caught.exception))
        sh.assert_not_called()
        comparison = json.loads((run / 'setup-fixtures.json').read_text())[0]
        self.assertEqual(comparison['pin']['path'], json.loads((run / 'config.json').read_text())
                         ['instances'][0]['files'][0]['path'])
        self.assertEqual(comparison['differing_paths'], ['last_page'])

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
        self.assertIn('split', timing)
        # Down hands the lifecycle the run's out/ for the files the run wrote, and RESULTS.md gives the prune's bytes.
        self.assertIn(f'--evidence-dir {run / "out"}', (run / 'lifecycle-down.log').read_text())
        self.assertEqual((run / 'out/post-run-a/run.log').read_text(), 'run bytes')
        self.assertIn('- Prune: 2.0 GB freed of 3.0 GB of copies (df +2.0 GB); 1 files the run wrote in out/post-run-*/, '
                      '0 listed only', results)

    def test_down_keep_state_skips_the_prune_and_says_so(self):
        eden_test.main(['new', *self.args()])
        run = self.dir / 'runs/lr-14'
        eden_test.main(['up', str(run)])
        eden_test.main(['down', str(run), '--keep-state'])
        self.assertIn('--keep-state', (run / 'lifecycle-down.log').read_text())
        self.assertFalse((run / 'out/post-run-a').exists())
        self.assertIn('- Prune: kept (--keep-state)', (run / 'RESULTS.md').read_text())
        self.assertEqual(eden_test.prune_summary(run, {}), 'not run: work/ and state/ stay until a clean Down')
        failed = {'evidence': str(run / 'out'), 'evidence_saved': 'now', 'error': "OSError('busy')"}
        self.assertEqual(eden_test.prune_summary(run, {'prune': failed}),
                         "FAILED: OSError('busy'); the run's files are in out/post-run-*/")

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
