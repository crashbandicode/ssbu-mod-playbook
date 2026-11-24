#!/usr/bin/env python3
"""Offline tests for eden_test.py: fake lifecycle/runner/harvest scripts, temp run folders, no Eden."""
import hashlib
import importlib.util
import inspect
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eden_test  # noqa: E402

TRACE_REQUIRED = (
    "this base is a trace run: pass --elf <the build's libtraining_rooms.so> and at least one "
    "--trace-point <module!symbol> (the lifecycle needs 1..256 trace points); the two the drill "
    "blocks use are libtraining_rooms.nro!edge_juggle_attempt and libtraining_rooms.nro!planner_chose"
)

# ib-practice-publisher/combined-yoga-main-1/rows/rows.json:932-966, cb-boot-and-bridge.
REVIEWER_BOOT_STEPS = [
    {'run': 'routes:_setting', 'args': {'key': 'core.use_gdbstub', 'value': False}},
    {'run': 'routes:_setting', 'args': {'key': 'core.network_interface', 'value': 'lo'}},
    {'run': 'routes:_setting', 'args': {'key': 'core.lan_online', 'value': False}},
    {'run': 'routes:configure_boot'},
    {'run': 'routes:load_game'},
    {'run': 'routes:bridge_on'},
    {'run': 'routes:to_main'},
]

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


class TileTakeRowsTests(unittest.TestCase):
    def test_the_tile_take_rows_resolve_to_library_steps(self):
        folder = Path(__file__).resolve().parent
        with mock.patch.object(sys, 'path', [str(folder.parent / 'eden-runner'), *sys.path]):
            import routes

        plan = json.loads((folder / 'example' / 'rows-tile-take.json').read_text())
        self.assertEqual([row['id'] for row in plan['rows']],
                         ['tk00', 'tk01', 'tk02', 'tk03', 'tk04', 'tk04b', 'tk05', 'tk06'])
        hold = next(row for row in plan['rows'] if row['id'] == 'tk04b')
        self.assertEqual(hold['steps'], [
            {'run': 'routes:take_noop', 'args': {'holding': 'p1', 'hold': True}},
        ])
        for row in plan['rows']:
            for step in row['steps']:
                with self.subTest(row=row['id'], step=step['run']):
                    module, name = step['run'].split(':', 1)
                    self.assertEqual(module, 'routes')
                    function = getattr(routes, name, None)
                    self.assertTrue(callable(function), step['run'])
                    inspect.signature(function).bind(object(), **step.get('args', {}))


class TargetNroTests(unittest.TestCase):
    def test_check_run_requires_the_target_nro_and_embedded_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / 'run'
            rows = run / 'rows'
            rows.mkdir(parents=True)
            commit = '12345678' + 'f' * 32
            sha256 = 'a' * 64
            (rows / 'rows.json').write_text(json.dumps(
                {'target': {'plugin': {'commit': commit, 'nro_sha256': sha256}}, 'rows': []}))
            config = {'instances': [{'plugins': []}]}
            (run / 'config.json').write_text(json.dumps(config))
            self.assertIn(f'target NRO {sha256[:12]}… is not staged in this run',
                          eden_test.check_run(run, skip_setup_plan=True))
            nro = run / 'training.nro'
            nro.write_bytes(b'no embedded revision')
            config['instances'][0]['plugins'] = [{'path': str(nro), 'sha256': sha256}]
            (run / 'config.json').write_text(json.dumps(config))
            self.assertIn(f'target NRO {nro.name} does not carry commit {commit[:8]}',
                          eden_test.check_run(run, skip_setup_plan=True))
            nro.write_bytes(b'binary ' + commit[:8].encode() + b' payload')
            self.assertEqual(eden_test.check_run(run, skip_setup_plan=True), [])


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

    def comparison(self, reviewed, pinned, allowed):
        self.reviewed.write_text(json.dumps(reviewed))
        self.fixture.write_text(json.dumps(pinned))
        self.item['reviewed']['sha256'] = sha(self.reviewed)
        self.item['differs_only'] = allowed
        self.config['instances'][0]['files'][0]['sha256'] = sha(self.fixture)
        (self.run / 'config.json').write_text(json.dumps(self.config))
        self.plan()
        problems = eden_test.check_run(self.run)
        evidence = json.loads((self.run / 'setup-fixtures.json').read_text())
        return problems, evidence[0]

    def test_literal_dot_key_requires_its_own_component_allowance(self):
        reviewed = {'allowed': {'x': 1}}
        pinned = {'allowed': {'x': 1}, 'allowed.x': 9}
        problems, result = self.comparison(reviewed, pinned, ['allowed'])
        self.assertIn('["allowed.x"]', ' '.join(problems))
        self.assertEqual(result['differing_paths'], [['allowed.x']])
        self.assertFalse(result['ok'])
        problems, result = self.comparison(reviewed, pinned, [['allowed.x']])
        self.assertEqual(problems, [])
        self.assertTrue(result['ok'])

    def test_list_index_allowances_accept_string_and_component_forms(self):
        reviewed, pinned = {'menu': [1, 2, 3, 4]}, {'menu': [1, 2, 3, 9]}
        for allowed in (['menu.3'], [['menu', 3]]):
            with self.subTest(allowed=allowed):
                problems, result = self.comparison(reviewed, pinned, allowed)
                self.assertEqual(problems, [])
                self.assertEqual(result['differing_paths'], [['menu', 3]])
                self.assertTrue(result['ok'])

    def test_numeric_dict_key_is_not_a_list_index(self):
        reviewed, pinned = {'menu': {'3': 1}}, {'menu': {'3': 9}}
        problems, result = self.comparison(reviewed, pinned, ['menu.3'])
        self.assertTrue(problems)
        self.assertEqual(result['differing_paths'], [['menu', '3']])
        self.assertEqual(self.comparison(reviewed, pinned, [['menu', '3']])[0], [])

    def test_invalid_component_allowances_are_refused(self):
        for allowed in ([['menu', True]], [['menu', {}]], [3], 'menu'):
            with self.subTest(allowed=allowed):
                problems, _ = self.comparison({'menu': 1}, {'menu': 2}, allowed)
                self.assertIn('differs_only', ' '.join(problems))

    def test_empty_allowance_entries_are_refused_and_authorise_nothing(self):
        reviewed = {'cpu': 'Mario', 'menu': {'setting': 1}}
        pinned = {'cpu': 'Kirby', 'menu': {'setting': 2}, 'added': True}
        for allowed in ([[]], [''], ['menu.'], ['.menu'], ['menu..setting'], [['menu', '']]):
            with self.subTest(allowed=allowed):
                problems, evidence = self.comparison(reviewed, pinned, allowed)
                self.assertIn('differs_only entries must name at least one key', ' '.join(problems))
                self.assertIn('3 differing paths', ' '.join(problems))
                self.assertEqual(evidence['differing_paths'], [['added'], ['cpu'], ['menu', 'setting']])
                self.assertFalse(evidence['ok'])

    def test_empty_allowance_list_requires_exact_match(self):
        original = {'cpu': 'Mario', 'menu': {'setting': 1}}
        self.assertEqual(self.comparison(original, original, [])[0], [])
        problems, evidence = self.comparison(original, {'cpu': 'Kirby', 'menu': {'setting': 1}}, [])
        self.assertIn('cpu', ' '.join(problems))
        self.assertFalse(evidence['ok'])

    def test_valid_allowance_still_applies_beside_an_invalid_one(self):
        problems, evidence = self.comparison({'cpu': 'Mario', 'menu': {'setting': 1}},
                                             {'cpu': 'Kirby', 'menu': {'setting': 2}}, [[], 'menu'])
        self.assertIn('differs_only entries must name at least one key', ' '.join(problems))
        self.assertIn('1 differing paths: cpu', ' '.join(problems))
        self.assertFalse(evidence['ok'])

    def test_fixture_diff_nested_lists_types_and_root(self):
        self.assertEqual(eden_test.fixture_diff(
            {'nest': {'a': 1}, 'list': [0, {'x': 2}], 'old': 1},
            {'nest': {'a': 3}, 'list': [0, {'x': 4}], 'new': 1}),
            [['list', 1, 'x'], ['nest', 'a'], ['new'], ['old']])
        self.assertEqual(eden_test.fixture_diff({'list': [1, 2]}, {'list': [1]}), [['list']])
        self.assertEqual(eden_test.fixture_diff({'value': 1}, {'value': True}), [['value']])
        self.assertEqual(eden_test.fixture_diff([1], {'one': 1}), [[]])
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
        self.assertEqual(evidence[0]['differing_paths'], [['last_page'], ['menu', 'a', 0], ['menu', 'b', 1]])
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


class IntervalAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        (self.run / 'rows').mkdir()
        (self.run / 'out').mkdir()
        self.rows = [{'id': 'r1', 'start': 'training', 'end': 'training'}]
        self.set_rows()
        self.set_timing(up_start=0, up_done=5, down_start=25, down_done=30)

    def set_rows(self):
        (self.run / 'rows/rows.json').write_text(json.dumps({'rows': self.rows}))

    def set_timing(self, **timing):
        (self.run / 'timing.json').write_text(json.dumps(timing))

    def receipt(self, name, value):
        (self.run / 'out' / name).write_text(json.dumps(value))

    def journal(self, values):
        (self.run / 'out/attempts.jsonl').write_text(''.join(json.dumps(value) + '\n' for value in values))

    def test_superseded_receipt_and_current_journal_are_both_counted(self):
        self.set_timing(up_start=0, up_done=5, down_start=26, down_done=31)
        self.rows = [{'id': 'new-name', 'start': 'training', 'end': 'training'}]
        self.set_rows()
        self.receipt('old-name.json', {'id': 'old-name', 'role': 'test', 'start': 'training',
                                       'end': 'training', 'started': 5, 'ended': 15})
        self.journal([{'id': 'new-name', 'role': 'test', 'started': 15, 'ended': 25}])
        self.receipt('summary.json', {'started': 6, 'start': 'x', 'end': 'x'})
        self.receipt('not-a-receipt.json', {'id': 'different', 'start': 'x', 'end': 'x', 'started': 6, 'ended': 7})
        split = eden_test.time_split(self.run)
        self.assertEqual((split['window'], split['test'], split['attempts'], split['unclassified']), (31, 20, 2, 1))
        self.assertEqual(split['attempt_ids_not_in_manifest'], ['old-name'])

    def test_old_receipt_role_uses_its_manifest_snapshot_not_current_rows(self):
        snapshot_dir = self.run / 'out/manifests'
        snapshot_dir.mkdir()
        old = {'rows': [{'id': 'r1', 'start': 'menu', 'end': 'training', 'role': 'test'}]}
        pending = snapshot_dir / 'pending'
        pending.write_text(json.dumps(old))
        old_sha = sha(pending)
        pending.rename(snapshot_dir / f'{old_sha}.json')
        self.rows[0]['role'] = 'setup'
        self.set_rows()
        self.receipt('r1.FAILED.old.json', {'id': 'r1', 'start': 'menu', 'end': 'training',
                                          'started': 5, 'ended': 15, 'manifest_sha256': old_sha})
        split = eden_test.time_split(self.run)
        self.assertEqual((split['setup'], split['test']), (0, 10))
        self.assertEqual((split['roles_from_manifest'], split['roles_defaulted']), (1, 0))

    def test_manifest_replacement_cannot_pair_one_version_with_another_hash(self):
        rows_path = self.run / 'rows/rows.json'
        original_rows = {'rows': [{'id': 'r1', 'start': 'training', 'end': 'training', 'role': 'test'}]}
        replacement_rows = {'rows': [{'id': 'r1', 'start': 'training', 'end': 'training', 'role': 'setup'}]}
        data_a, data_b = (json.dumps(value).encode() for value in (original_rows, replacement_rows))
        hash_a, hash_b = (hashlib.sha256(data).hexdigest() for data in (data_a, data_b))
        snapshots = self.run / 'out/manifests'
        snapshots.mkdir()
        (snapshots / f'{hash_b}.json').write_bytes(data_b)
        original_read_bytes = Path.read_bytes
        original_sha256 = eden_test.sha256
        staging = rows_path.with_name('replacement.json')

        def replace_manifest():
            staging.write_bytes(data_b)
            staging.replace(rows_path)

        def replaced_sha256(path):
            if path == rows_path:
                replace_manifest()
            return original_sha256(path)

        for pinned, expected in ((hash_b, (10, 0)), (hash_a, (0, 10))):
            with self.subTest(pinned=pinned):
                rows_path.write_bytes(data_a)
                self.receipt('r1.json', {'id': 'r1', 'start': 'training', 'end': 'training',
                                         'started': 5, 'ended': 15, 'manifest_sha256': pinned})
                reads = []

                def interleaved_read(path):
                    data = original_read_bytes(path)
                    if path == rows_path:
                        reads.append(path)
                        replace_manifest()
                    return data

                with mock.patch.object(Path, 'read_bytes', interleaved_read), \
                        mock.patch.object(eden_test, 'sha256', side_effect=replaced_sha256) as digest:
                    split = eden_test.time_split(self.run)
                self.assertEqual((split['setup'], split['test']), expected)
                self.assertEqual((split['roles_from_manifest'], split['roles_defaulted']), (1, 0))
                self.assertFalse(any(call.args[0] == rows_path for call in digest.call_args_list))
                self.assertEqual(reads, [rows_path])
                self.assertEqual(rows_path.read_bytes(), data_b)

    def test_missing_manifest_keeps_journal_attempt_without_a_current_hash(self):
        (self.run / 'rows/rows.json').unlink()
        self.journal([{'id': 'r1', 'role': 'test', 'started': 5, 'ended': 15}])
        split = eden_test.time_split(self.run)
        self.assertEqual((split['test'], split['attempts'], split['roles_from_manifest']), (10, 1, 0))
        self.assertEqual(split['attempt_ids_not_in_manifest'], ['r1'])

    def test_old_receipt_without_snapshot_defaults_from_own_screens(self):
        self.rows[0]['role'] = 'test'
        self.set_rows()
        self.receipt('r1.json', {'id': 'r1', 'start': 'menu', 'end': 'training',
                                 'started': 5, 'ended': 15, 'manifest_sha256': 'f' * 64})
        split = eden_test.time_split(self.run)
        self.assertEqual((split['setup'], split['test']), (10, 0))
        self.assertEqual((split['roles_from_manifest'], split['roles_defaulted']), (0, 1))

    def test_tampered_snapshot_is_not_used_as_role_provenance(self):
        snapshot_dir = self.run / 'out/manifests'
        snapshot_dir.mkdir()
        (snapshot_dir / ('f' * 64 + '.json')).write_text(json.dumps(
            {'rows': [{'id': 'r1', 'role': 'test'}]}))
        self.receipt('r1.json', {'id': 'r1', 'start': 'menu', 'end': 'training',
                                 'started': 5, 'ended': 15, 'manifest_sha256': 'f' * 64})
        split = eden_test.time_split(self.run)
        self.assertEqual((split['setup'], split['test'], split['roles_from_manifest'], split['roles_defaulted']),
                         (10, 0, 0, 1))

    def test_journal_without_own_role_or_screens_is_unroled_even_if_current_row_has_role(self):
        self.rows[0]['role'] = 'setup'
        self.set_rows()
        self.journal([{'id': 'r1', 'started': 5, 'ended': 15}])
        split = eden_test.time_split(self.run)
        self.assertEqual((split['unroled'], split['setup']), (10, 0))
        self.assertEqual((split['roles_from_manifest'], split['attempts_unroled']), (0, 1))

    def test_superseded_row_attempt_remains_in_the_journal_total(self):
        self.set_timing(up_start=0, up_done=5, down_start=26, down_done=31)
        self.journal([{'id': 'old-name', 'role': 'test', 'started': 5, 'ended': 15},
                      {'id': 'r1', 'role': 'test', 'started': 15, 'ended': 25}])
        split = eden_test.time_split(self.run)
        self.assertEqual((split['window'], split['test'], split['attempts'], split['unclassified']), (31, 20, 2, 1))

    def test_role_source_counts_defaulted_journal_attempts(self):
        self.rows.append({'id': 'r2', 'start': 'training', 'end': 'training'})
        self.set_rows()
        self.journal([{'id': 'r1', 'role': 'test', 'role_source': 'default', 'started': 5, 'ended': 15},
                      {'id': 'r2', 'role': 'test', 'role_source': 'default', 'started': 15, 'ended': 25}])
        split = eden_test.time_split(self.run)
        self.assertEqual((split['test'], split['attempts'], split['roles_defaulted']), (20, 2, 2))

    def test_old_receipt_uses_explicit_same_screen_setup_role(self):
        self.rows[0]['role'] = 'setup'
        self.set_rows()
        self.receipt('r1.json', {'id': 'r1', 'start': 'training', 'end': 'training',
                                 'manifest_sha256': sha(self.run / 'rows/rows.json'), 'started': 5, 'ended': 15})
        split = eden_test.time_split(self.run)
        self.assertEqual((split['setup'], split['test'], split['roles_defaulted'], split['roles_from_manifest']),
                         (10, 0, 0, 1))

    def test_missing_endpoint_still_sweeps_overlapping_attempts(self):
        self.set_timing(up_start=0, up_done=2, down_start=20)
        self.rows.append({'id': 'r2', 'start': 'training', 'end': 'training'})
        self.set_rows()
        self.journal([{'id': 'r1', 'role': 'test', 'started': 3, 'ended': 13},
                      {'id': 'r2', 'role': 'test', 'started': 5, 'ended': 11}])
        split = eden_test.time_split(self.run)
        self.assertEqual((split['window'], split['unclassified'], split['test'], split['overlap']), (None, None, 10, 6))
        self.assertTrue(split['incomplete'])

    def test_superseded_attempt_without_role_is_unroled(self):
        self.set_timing(up_start=0, up_done=5, down_start=25)
        self.journal([{'id': 'old-name', 'started': 5, 'ended': 15}])
        split = eden_test.time_split(self.run)
        self.assertEqual((split['attempts'], split['test_share']), (1, 0))
        self.assertEqual((split['attempts_unroled'], split['unroled']), (1, 10))
        config = {'label': 'fake', 'rows': {'sha256': sha(self.run / 'rows/rows.json')},
                  'instances': [{'app': {'path': '/fake', 'sha256': '0' * 64}}]}
        eden_test.results_md(self.run, config)
        line = next(line for line in (self.run / 'RESULTS.md').read_text().splitlines() if line.startswith('- Timing:'))
        self.assertIn('test 0 s, unroled 10 s, unclassified ? (incomplete timing)', line)

    def test_duplicate_receipt_supplies_missing_attempt_role(self):
        self.journal([{'id': 'r1', 'started': 5, 'ended': 15}])
        self.receipt('r1.json', {'id': 'r1', 'role': 'setup', 'role_source': 'explicit',
                                 'start': 'training', 'end': 'training', 'started': 5, 'ended': 15})
        split = eden_test.time_split(self.run)
        self.assertEqual((split['attempts'], split['setup'], split['test'], split['roles_defaulted']), (1, 10, 0, 0))

    def test_two_journal_attempts_and_latest_receipt_deduplicate(self):
        failed = {'id': 'r1', 'role': 'test', 'started': 5, 'ended': 15, 'status': 'FAILED'}
        passed = {'id': 'r1', 'role': 'test', 'started': 15, 'ended': 25, 'status': 'PASS'}
        self.journal([failed, passed])
        self.receipt('r1.json', {**passed, 'start': 'training', 'end': 'training'})
        split = eden_test.time_split(self.run)
        self.assertEqual(split, {'window': 30, 'up': 5, 'setup': 0, 'test': 20, 'unroled': 0, 'down': 5,
                                 'unclassified': 0, 'incomplete': False, 'overlap': 0, 'attempts': 2,
                                 'attempts_skipped': 0, 'attempts_unroled': 0, 'roles_defaulted': 0,
                                 'roles_from_manifest': 0, 'attempt_ids_not_in_manifest': [],
                                 'test_share': 0.667})

    def test_externally_retained_attempt_is_counted(self):
        self.receipt('r1.FAILED.before-fix.json',
                     {'id': 'r1', 'role': 'test', 'start': 'training', 'end': 'training',
                      'started': 5, 'ended': 15, 'status': 'FAILED'})
        self.receipt('r1.json', {'id': 'r1', 'role': 'test', 'start': 'training', 'end': 'training',
                                 'started': 15, 'ended': 25, 'status': 'PASS'})
        self.receipt('other.FAILED.outside.json', {'id': 'other', 'role': 'test', 'start': 'training',
                                                   'end': 'training', 'started': 5, 'ended': 25})
        split = eden_test.time_split(self.run)
        self.assertEqual((split['test'], split['attempts'], split['unclassified']), (20, 3, 0))
        self.assertEqual(split['attempt_ids_not_in_manifest'], ['other'])

    def test_runner_window_gap_is_unclassified(self):
        self.set_timing(up_start=0, up_done=5, down_start=55, down_done=60, runs=[[10, 15], [45, 50]])
        self.journal([{'id': 'r1', 'role': 'test', 'started': 10, 'ended': 15},
                      {'id': 'r1', 'role': 'test', 'started': 45, 'ended': 50}])
        split = eden_test.time_split(self.run)
        self.assertEqual((split['window'], split['test'], split['unclassified']), (60, 10, 40))

    def test_overlap_goes_to_the_interval_that_started_first(self):
        self.set_timing(up_start=0, up_done=5, down_start=20, down_done=25)
        self.rows.append({'id': 'r2', 'start': 'menu', 'end': 'training'})
        self.set_rows()
        self.journal([{'id': 'r1', 'role': 'test', 'started': 5, 'ended': 15},
                      {'id': 'r2', 'role': 'setup', 'started': 10, 'ended': 20}])
        split = eden_test.time_split(self.run)
        self.assertEqual((split['up'], split['test'], split['setup'], split['down'], split['overlap'],
                          split['unclassified']), (5, 10, 5, 5, 5, 0))

    def test_clipping_and_three_way_overlap_count_wall_time_once(self):
        self.set_timing(up_start=10, up_done=12, down_start=25, down_done=30)
        self.rows.extend([{'id': 'r2', 'start': 'menu', 'end': 'training'},
                          {'id': 'r3', 'start': 'menu', 'end': 'training'}])
        self.set_rows()
        self.journal([{'id': 'r1', 'role': 'test', 'started': 5, 'ended': 15},
                      {'id': 'r2', 'role': 'setup', 'started': 11, 'ended': 20},
                      {'id': 'r3', 'role': 'setup', 'started': 12, 'ended': 14}])
        split = eden_test.time_split(self.run)
        self.assertEqual((split['window'], split['up'], split['test'], split['setup'], split['down'],
                          split['overlap'], split['unclassified']), (20, 0, 5, 5, 5, 5, 5))

    def test_missing_window_defaults_role_and_skips_unfinished_attempt(self):
        self.set_timing(up_start=0, up_done=5, down_start=20)
        self.journal([{'id': 'r1', 'start': 'training', 'end': 'training', 'started': 5, 'ended': 15},
                      {'id': 'r1', 'started': 20}])
        self.receipt('r1.json', {'id': 'r1', 'start': 'training', 'end': 'training', 'started': 5, 'ended': 15})
        split = eden_test.time_split(self.run)
        self.assertEqual((split['window'], split['unclassified'], split['up'], split['test'], split['down']),
                         (None, None, 5, 10, 0))
        self.assertEqual((split['attempts'], split['attempts_skipped'], split['roles_defaulted']), (1, 1, 1))
        config = {'label': 'fake', 'rows': {'sha256': sha(self.run / 'rows/rows.json')},
                  'instances': [{'app': {'path': '/fake', 'sha256': '0' * 64}}]}
        eden_test.results_md(self.run, config)
        line = next(line for line in (self.run / 'RESULTS.md').read_text().splitlines() if line.startswith('- Timing:'))
        self.assertIn('down 0 s;', line)
        self.assertIn('; classified: up+setup+down 5 s, test 10 s, unclassified ? (incomplete timing) '
                      '(1 roles defaulted)', line)

    def test_invalid_duplicate_is_skipped_once(self):
        invalid = {'id': 'r1', 'started': 'bad', 'ended': 15}
        self.journal([invalid])
        self.receipt('r1.json', invalid)
        split = eden_test.time_split(self.run)
        self.assertEqual((split['attempts'], split['attempts_skipped']), (0, 1))

    def test_valid_duplicate_can_replace_incomplete_attempt(self):
        self.journal([{'id': 'r1', 'started': 5, 'ended': 4}])
        self.receipt('r1.json', {'id': 'r1', 'role': 'test', 'start': 'training', 'end': 'training',
                                 'started': 5, 'ended': 15})
        split = eden_test.time_split(self.run)
        self.assertEqual((split['attempts'], split['attempts_skipped'], split['test']), (1, 0, 10))


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
            ('test', {'status': 'PASS', 'started': 20, 'ended': 40,
                      'manifest_sha256': sha(self.run / 'rows/rows.json')}),
            ('unfinished', {'status': 'FAILED', 'started': 50, 'role': 'setup'}),
        ):
            row = next(row for row in self.rows if row['id'] == name)
            receipt.update(id=name, start=row['start'], end=row['end'])
            (self.run / 'out' / f'{name}.json').write_text(json.dumps(receipt))

    def test_split_uses_receipts_fallback_and_skips_unfinished(self):
        self.assertEqual(eden_test.time_split(self.run),
                         {'window': 74, 'up': 10, 'setup': 27, 'test': 0, 'unroled': 0, 'down': 5,
                          'unclassified': 32, 'incomplete': False, 'overlap': 1, 'attempts': 2,
                          'attempts_skipped': 1, 'attempts_unroled': 0, 'roles_defaulted': 0,
                          'roles_from_manifest': 1, 'attempt_ids_not_in_manifest': [], 'test_share': 0})

    def test_missing_timing_and_receipts_are_zero(self):
        (self.run / 'timing.json').write_text('{}')
        (self.run / 'rows/rows.json').write_text('[]')
        for receipt in (self.run / 'out').glob('*.json'):
            receipt.unlink()
        self.assertEqual(eden_test.time_split(self.run),
                         {'window': None, 'up': 0, 'setup': 0, 'test': 0, 'unroled': 0, 'down': 0,
                          'unclassified': None, 'incomplete': True, 'overlap': 0, 'attempts': 0,
                          'attempts_skipped': 0, 'attempts_unroled': 0, 'roles_defaulted': 0,
                          'roles_from_manifest': 0, 'attempt_ids_not_in_manifest': [], 'test_share': 0})

    def test_receipt_role_overrides_the_row_default(self):
        path = self.run / 'out/setup.json'
        receipt = json.loads(path.read_text())
        receipt['role'] = 'test'
        path.write_text(json.dumps(receipt))
        split = eden_test.time_split(self.run)
        self.assertEqual((split['setup'], split['test'], split['unclassified'], split['test_share']), (20, 7, 32, 0.167))

    def test_partial_timing_leaves_uncovered_time_unclassified(self):
        (self.run / 'timing.json').write_text(json.dumps(
            {'up_start': 10, 'runs': [[30, 35]], 'down_done': 70}))
        split = eden_test.time_split(self.run)
        self.assertEqual((split['up'], split['down'], split['unclassified'], split['setup'], split['test_share']),
                         (0, 0, 32, 28, 0))

    def test_results_timing_line_includes_the_split(self):
        config = {'label': 'fake', 'rows': {'sha256': sha(self.run / 'rows/rows.json')},
                  'instances': [{'app': {'path': '/fake', 'sha256': '0' * 64}}]}
        eden_test.results_md(self.run, config)
        timing = next(line for line in (self.run / 'RESULTS.md').read_text().splitlines() if line.startswith('- Timing:'))
        self.assertIn('classified: up+setup+down 42 s, test 0 s, unclassified 32 s', timing)


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


class FreeDisplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sockets = Path(self.tmp.name)
        patcher = mock.patch.object(eden_test, 'X11_SOCKET_DIR', self.sockets, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_preferred_free_and_occupied(self):
        self.assertEqual(eden_test.free_display(':298'), ':298')
        (self.sockets / 'X298').touch()
        self.assertEqual(eden_test.free_display(':298'), ':299')

    def test_skips_forbidden_and_taken_displays(self):
        (self.sockets / 'X60').touch()
        self.assertEqual(eden_test.free_display(':60'), ':62')
        self.assertEqual(eden_test.free_display(':60', taken=(':62',)), ':63')

    def test_dangling_socket_alias_is_not_free(self):
        (self.sockets / 'X183').symlink_to('missing')
        self.assertEqual(eden_test.free_display(':183'), ':184')

    def test_exhaustion_and_display_number_boundary(self):
        for number in range(200, 240):
            (self.sockets / f'X{number}').touch()
        with self.assertRaises(SystemExit) as caught:
            eden_test.free_display(':200')
        self.assertEqual(str(caught.exception), 'no free X display from :200 upward (40 tried)')
        (self.sockets / 'X999').touch()
        with self.assertRaises(SystemExit) as caught:
            eden_test.free_display(':999')
        self.assertEqual(str(caught.exception), 'no free X display from :999 upward (40 tried)')


class HostInterfaceTests(unittest.TestCase):
    def test_flags_address_and_usable_interface_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, flags in (('lo', '0x9'), ('down0', '0x0'),
                                ('ipless0', '0x1'), ('wifi0', '0x1001')):
                folder = root / name
                folder.mkdir()
                (folder / 'flags').write_text(flags)
            with mock.patch.object(eden_test, 'NET_SYSFS', root, create=True), \
                 mock.patch.object(eden_test, 'socket', create=True), \
                 mock.patch.object(eden_test, 'fcntl', create=True) as fcntl:
                def ioctl(fd, request, data):
                    if data.startswith(b'ipless0'):
                        raise OSError('no IPv4')
                    return b'\0' * 256
                fcntl.ioctl.side_effect = ioctl
                self.assertEqual(eden_test.host_interface_problem('missing0'), 'no such interface')
                self.assertEqual(eden_test.host_interface_problem('../wifi0'), 'no such interface')
                self.assertEqual(eden_test.host_interface_problem('down0'), 'is down')
                self.assertEqual(eden_test.host_interface_problem('lo'), 'is loopback')
                self.assertEqual(eden_test.host_interface_problem('ipless0'), 'has no IPv4 address')
                self.assertIsNone(eden_test.host_interface_problem('wifi0'))
                self.assertEqual(eden_test.usable_interfaces(), ['wifi0'])


class EdenTestTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='eden-test-test-'))
        for name, body in (('lifecycle.py', FAKE_LIFECYCLE), ('runner.py', FAKE_RUNNER), ('harvest.py', FAKE_HARVEST)):
            (self.dir / name).write_text(body)
        self.saved = (eden_test.LIFECYCLE, eden_test.RUNNER, eden_test.HARVEST, eden_test.RUNS)
        eden_test.LIFECYCLE, eden_test.RUNNER, eden_test.HARVEST = (self.dir / 'lifecycle.py', self.dir / 'runner.py',
                                                                     self.dir / 'harvest.py')
        eden_test.RUNS = self.dir / 'runs'
        self.lineups = self.dir / 'card-lineups'
        self.lineups.mkdir()
        card_patch = mock.patch.object(eden_test, 'CARD_LINEUPS', self.lineups, create=True)
        card_patch.start()
        self.addCleanup(card_patch.stop)
        app = self.dir / 'Eden.AppImage'
        app.write_bytes(b'eden')
        nro = self.dir / 'plugin.nro'
        nro.write_bytes(b'nro')
        self.previous = self.dir / 'prev-config.json'
        self.previous.write_text(json.dumps({
            'owner': 'OpenCode x/y', 'label': 'lr-13', 'host': 'yoga', 'state_dir': '/old/state', 'work_root': '/old/work',
            'network_interface': 'test0',
            'runner': {'path': '/old/runner.py', 'sha256': '0' * 64},
            'instances': [{'key': 'a', 'app': {'path': str(app), 'sha256': sha(app)},
                           'plugins': [{'path': str(nro), 'sha256': sha(nro)}], 'game': '/games/x.xci'}]}))
        self.rows = self.dir / 'rows-src'
        self.rows.mkdir()
        (self.rows / 'rows.json').write_text('{"setup": [{"state": "Training", "via": "segment", "ref": "s1"}], "rows":[]}')
        interface_check = mock.patch.object(eden_test, 'host_interface_problem', return_value=None, create=True)
        self.interface_check = interface_check.start()
        self.addCleanup(interface_check.stop)

    def tearDown(self):
        eden_test.LIFECYCLE, eden_test.RUNNER, eden_test.HARVEST, eden_test.RUNS = self.saved
        shutil.rmtree(self.dir)

    def args(self, *extra):
        return ['--from', str(self.previous), '--label', 'lr-14', '--rows', str(self.rows), *extra]

    def go_args(self, label='go-1', rows=None, nro=None, host='yoga', owner='Claude devin-test/go-1',
                lineup=False):
        return ['go', '--host', host, '--label', label, '--nro', str(nro or self.dir / 'plugin.nro'),
                '--rows', str(rows or self.rows), *(['--owner', owner] if owner is not None else []),
                *([] if lineup else ['--no-card-lineup', 'test without the card lineup']),
                '--from', str(self.previous)]

    def go_new_args(self, label='go-1', rows=None, no_lobby=False):
        return types.SimpleNamespace(from_config=str(self.previous), host='yoga', label=label,
                                     owner='Claude devin-test/go-1', nro=[str(self.dir / 'plugin.nro')],
                                     rows=str(rows or self.rows), elf=None, trace_point=[],
                                     network_interface=None, no_lobby=no_lobby, card_lineup=None,
                                     card_lineup_origin=None, no_card_lineup='test without the card lineup')

    def make_lineup(self, folder, plugins):
        folder.mkdir(parents=True)
        for name, data in plugins.items():
            (folder / name).write_bytes(data)
        (folder / 'SHA256SUMS.txt').write_text(''.join(
            f'{sha(folder / name)}  {name}\n' for name in sorted(plugins)))
        return folder

    def run_go_without_up(self, args):
        output = io.StringIO()
        with mock.patch.object(eden_test, 'cmd_up') as up, \
             mock.patch.object(eden_test, 'cmd_run', return_value=0), redirect_stdout(output):
            result = eden_test.main(args)
        return result, output.getvalue(), up

    def assert_lineup_refused(self, args, label):
        with mock.patch.object(eden_test, 'cmd_up') as up, mock.patch.object(eden_test, 'sh') as sh, \
             mock.patch.object(eden_test.shutil, 'copy2') as copy:
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(args)
        up.assert_not_called()
        sh.assert_not_called()
        copy.assert_not_called()
        self.assertFalse((eden_test.RUNS / label).exists())
        return str(caught.exception)

    def test_card_lineup_default_uses_newest_stamp_only(self):
        self.make_lineup(self.lineups / '20260101-1000Z/plugins', {'old.nro': b'old'})
        newest = self.make_lineup(self.lineups / '20261231-2359Z/plugins', {'new.nro': b'new'})
        (self.lineups / 'not-a-stamp').mkdir()
        (self.lineups / '20270101-0000Z').write_text('a file is not a stamp directory')
        result, output, up = self.run_go_without_up(self.go_args(lineup=True))
        self.assertEqual(result, 0)
        up.assert_called_once()
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertEqual(config['card_lineup'], {
            'staged': True, 'dir': str(newest.resolve()), 'origin': str(newest.resolve()),
            'sums_sha256': sha(newest / 'SHA256SUMS.txt'), 'files': 1})
        self.assertIn('Staged plugins [a]:', output)
        self.assertIn(f'card lineup {newest} (1 files verified)', output)

    def test_card_lineup_default_refuses_missing_newest_plugins_or_no_stamps(self):
        for label, older in (('no-stamps', False), ('missing-newest', True)):
            with self.subTest(label=label):
                if older:
                    self.make_lineup(self.lineups / '20260101-1000Z/plugins', {'old.nro': b'old'})
                    (self.lineups / '20261231-2359Z').mkdir()
                message = self.assert_lineup_refused(self.go_args(label=label, lineup=True), label)
                if older:
                    self.assertEqual(
                        message, f'newest card lineup {self.lineups / "20261231-2359Z"} has no plugins folder '
                        '(older stamps are not used); pass --card-lineup <dir> or --no-card-lineup <reason>')
                else:
                    self.assertEqual(
                        message, f'no card lineup under {self.lineups} (no <YYYYMMDD-HHMM>Z folder; '
                        'on an Eden host that folder does not exist: run go from the workspace machine or copy '
                        'a lineup and pass --card-lineup <dir>); or pass --no-card-lineup <reason>')

    def test_card_lineup_validation_refuses_all_bad_inputs_before_staging(self):
        for kind in ('missing-folder', 'not-directory', 'mismatch', 'missing-file', 'unlisted-nro',
                     'subfolder', 'stray-file', 'missing-sums', 'empty-sums', 'malformed',
                     'duplicate', 'path-separator', 'backslash', 'not-nro', 'symlink', 'symlink-sums'):
            with self.subTest(kind=kind):
                folder = self.dir / f'bad-{kind}'
                if kind == 'not-directory':
                    folder.write_text('not a directory')
                elif kind != 'missing-folder':
                    self.make_lineup(folder, {'one.nro': b'one'})
                    sums = folder / 'SHA256SUMS.txt'
                    if kind == 'mismatch':
                        (folder / 'one.nro').write_bytes(b'wrong')
                    elif kind == 'missing-file':
                        (folder / 'one.nro').unlink()
                    elif kind == 'unlisted-nro':
                        (folder / 'extra.nro').write_bytes(b'extra')
                    elif kind == 'subfolder':
                        (folder / 'extra').mkdir()
                    elif kind == 'stray-file':
                        (folder / 'notes.txt').write_text('extra')
                    elif kind == 'missing-sums':
                        sums.unlink()
                    elif kind == 'empty-sums':
                        sums.write_text('')
                    elif kind == 'malformed':
                        sums.write_text('garbage\n')
                    elif kind == 'duplicate':
                        sums.write_text(sums.read_text() * 2)
                    elif kind == 'path-separator':
                        sums.write_text(f'{"0" * 64}  nested/one.nro\n')
                    elif kind == 'backslash':
                        sums.write_text(f'{"0" * 64}  nested\\one.nro\n')
                    elif kind == 'not-nro':
                        sums.write_text(f'{"0" * 64}  other.txt\n')
                    elif kind == 'symlink':
                        (folder / 'one.nro').unlink()
                        (folder / 'one.nro').symlink_to(self.dir / 'plugin.nro')
                    elif kind == 'symlink-sums':
                        target = self.dir / 'original-sums.txt'
                        sums.replace(target)
                        sums.symlink_to(target)
                label = f'refuse-{kind}'
                message = self.assert_lineup_refused(
                    self.go_args(label=label, lineup=True) + ['--card-lineup', str(folder)], label)
                self.assertTrue(message, kind)
                if kind == 'mismatch':
                    self.assertIn(sha(folder / 'one.nro')[:12], message)
                    self.assertIn(hashlib.sha256(b'one').hexdigest()[:12], message)
                if kind == 'symlink-sums':
                    self.assertIn('SHA256SUMS.txt', message)
                    self.assertIn('symlink', message)

    def test_card_lineup_validation_collects_simultaneous_problems(self):
        folder = self.make_lineup(self.dir / 'bad-both', {'one.nro': b'one'})
        (folder / 'one.nro').write_bytes(b'wrong')
        (folder / 'unlisted.nro').write_bytes(b'extra')
        message = self.assert_lineup_refused(
            self.go_args(lineup=True) + ['--card-lineup', str(folder)], 'go-1')
        self.assertIn('one.nro', message)
        self.assertIn('unlisted.nro', message)
        self.assertIn(sha(folder / 'one.nro')[:12], message)

    def test_card_lineup_parser_accepts_one_or_two_spaces_and_binary_marker(self):
        folder = self.make_lineup(self.dir / 'valid', {'z.nro': b'z', 'a.nro': b'a'})
        sums = folder / 'SHA256SUMS.txt'
        sums.write_text(f'{sha(folder / "z.nro")} *z.nro\n\n{sha(folder / "a.nro")} a.nro\n')
        self.assertEqual(eden_test.read_card_lineup(folder), {
            'dir': str(folder.resolve()), 'sums_sha256': sha(sums),
            'plugins': [{'name': name, 'sha256': sha(folder / name)} for name in ('a.nro', 'z.nro')]})

    def test_card_lineup_copied_file_is_rehashed_before_up(self):
        card = self.make_lineup(self.dir / 'copied-card', {'one.nro': b'one'})
        real_copy = shutil.copy2
        def corrupt_card_copy(source, target, *args, **kwargs):
            result = real_copy(source, target, *args, **kwargs)
            if Path(source) == card / 'one.nro':
                Path(target).write_bytes(b'corrupted copy')
            return result
        with mock.patch.object(eden_test.shutil, 'copy2', side_effect=corrupt_card_copy), \
             mock.patch.object(eden_test, 'cmd_up') as up:
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(self.go_args(lineup=True) + ['--card-lineup', str(card)])
        self.assertIn('one.nro', str(caught.exception))
        up.assert_not_called()

    def test_card_lineup_origin_is_distinct_from_verified_directory(self):
        card = self.make_lineup(self.dir / 'remote-card', {'one.nro': b'one'})
        self.run_go_without_up(self.go_args(lineup=True) + [
            '--card-lineup', str(card), '--card-lineup-origin', '/original/card'])
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertEqual(config['card_lineup']['dir'], str(card.resolve()))
        self.assertEqual(config['card_lineup']['origin'], '/original/card')

    def test_card_lineup_rejects_duplicate_nro_names_before_copying(self):
        card = self.make_lineup(self.dir / 'duplicate-card', {'one.nro': b'one'})
        first = self.dir / 'plugin.nro'
        second_dir = self.dir / 'second'
        second_dir.mkdir()
        second = second_dir / first.name
        second.write_bytes(b'second')
        message = self.assert_lineup_refused(
            self.go_args(nro=first, lineup=True) + ['--card-lineup', str(card), '--nro', str(second)],
            'go-1')
        self.assertIn('each --nro must have a distinct file name', message)

    def test_card_lineup_rejects_unsafe_folder_path_before_read_or_transport(self):
        for host in ('yoga', 'xps'):
            for index, suffix in enumerate(("has'apostrophe", 'has\nnewline', 'has\rcarriage-return')):
                label = f'unsafe-{host}-{index}'
                folder = self.dir / suffix
                with self.subTest(host=host, suffix=suffix), \
                     mock.patch.object(eden_test, 'read_card_lineup', create=True) as read:
                    message = self.assert_lineup_refused(
                        self.go_args(label=label, host=host, lineup=True) + [
                            '--card-lineup', str(folder), '--ssh', 'xps-host'],
                        label)
                    read.assert_not_called()
                    self.assertIn(str(folder), message)
                    self.assertIn('without apostrophes or line breaks', message)

    def card_fixture(self, two_instances=False):
        card = self.make_lineup(self.dir / 'card', {
            'libtraining_rooms.nro': b'card training', 'libssbu_tile_cursor.nro': b'card tile',
            'libother.nro': b'card other'})
        base_dir = self.dir / 'base-plugins'
        base_dir.mkdir()
        base = json.loads(self.previous.read_text())
        pins = []
        for name, data in (('libtraining_rooms.nro', b'old training'),
                           ('libssbu_hw_bridge.nro', b'bridge'), ('libssbu_tile_cursor.nro', b'old tile')):
            path = base_dir / name
            path.write_bytes(data)
            pins.append({'name': name, 'path': str(path), 'sha256': sha(path), 'extra': 'preserved'})
        base['instances'][0]['plugins'] = pins
        if two_instances:
            second = json.loads(json.dumps(base['instances'][0]))
            second['key'] = 'b'
            base['instances'].append(second)
        self.previous.write_text(json.dumps(base))
        build = self.dir / 'build-card-test'
        build.mkdir()
        new = build / 'libtraining_rooms.nro'
        new.write_bytes(b'new training')
        return card, pins, new

    def test_card_lineup_full_staging_precedence_receipt_and_source_unchanged(self):
        card, base, new = self.card_fixture()
        before = {p.name: p.read_bytes() for p in card.iterdir()}
        result, output, _ = self.run_go_without_up(
            self.go_args(nro=new, lineup=True) + ['--card-lineup', str(card)])
        self.assertEqual(result, 0)
        run = eden_test.RUNS / 'go-1'
        config = json.loads((run / 'config.json').read_text())
        pins = config['instances'][0]['plugins']
        self.assertEqual([p['name'] for p in pins], [
            'libother.nro', 'libssbu_tile_cursor.nro', 'libtraining_rooms.nro', 'libssbu_hw_bridge.nro'])
        by_name = {p['name']: p for p in pins}
        for name in ('libother.nro', 'libssbu_tile_cursor.nro'):
            self.assertEqual(by_name[name], {'name': name, 'path': str(run / 'inputs/card-lineup' / name),
                                            'sha256': sha(card / name)})
        self.assertEqual(by_name['libtraining_rooms.nro']['path'], str(run / 'inputs' / new.name))
        self.assertEqual(by_name['libtraining_rooms.nro']['sha256'], sha(new))
        self.assertEqual(by_name['libssbu_hw_bridge.nro'], base[1])
        for pin in pins:
            self.assertEqual(sha(pin['path']), pin['sha256'])
        self.assertEqual(config['staged_plugins']['a'], [
            {'name': 'libother.nro', 'sha256': sha(card / 'libother.nro'), 'source': 'card'},
            {'name': 'libssbu_tile_cursor.nro', 'sha256': sha(card / 'libssbu_tile_cursor.nro'), 'source': 'card'},
            {'name': new.name, 'sha256': sha(new), 'source': 'nro',
             'replaces': {'source': 'card', 'sha256': sha(card / new.name)}},
            {'name': 'libssbu_hw_bridge.nro', 'sha256': base[1]['sha256'], 'source': 'base'}])
        self.assertEqual(config['superseded_base_pins'], [
            {'instance': 'a', 'name': base[i]['name'], 'base_path': base[i]['path'],
             'base_sha256': base[i]['sha256'], 'card_sha256': sha(card / base[i]['name'])}
            for i in (0, 2)])
        self.assertEqual(config['plugin_swaps'], [{
            'from': str(run / 'inputs/card-lineup' / new.name), 'from_sha256': sha(card / new.name),
            'to': str(run / 'inputs' / new.name)}])
        self.assertEqual({p.name: p.read_bytes() for p in card.iterdir()}, before)
        self.assertEqual((run / 'inputs/card-lineup/SHA256SUMS.txt').read_bytes(),
                         (card / 'SHA256SUMS.txt').read_bytes())
        self.assertIn('--nro replaced: libtraining_rooms.nro', output)
        self.assertIn('libssbu_hw_bridge.nro (base)', output)

    def test_card_lineup_added_nro_does_not_drop_card_files(self):
        card, _, _ = self.card_fixture()
        new = self.dir / 'new.nro'
        new.write_bytes(b'added')
        self.run_go_without_up(self.go_args(nro=new, lineup=True) + ['--card-lineup', str(card)])
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertEqual({p['name'] for p in config['staged_plugins']['a']},
                         {p.name for p in card.glob('*.nro')} | {'libssbu_hw_bridge.nro', 'new.nro'})
        self.assertEqual(config['staged_plugins']['a'][-1],
                         {'name': 'new.nro', 'sha256': sha(new), 'source': 'nro'})

    def test_card_lineup_is_staged_for_both_instances(self):
        card, _, new = self.card_fixture(two_instances=True)
        _, output, _ = self.run_go_without_up(
            self.go_args(nro=new, lineup=True) + ['--card-lineup', str(card)])
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertEqual(config['staged_plugins']['a'], config['staged_plugins']['b'])
        self.assertEqual(len(config['superseded_base_pins']), 4)
        self.assertIn('Staged plugins [a]:', output)
        self.assertIn('Staged plugins [b]:', output)

    def test_card_lineup_opt_out_retains_base_and_records_reason(self):
        _, output, _ = self.run_go_without_up(self.go_args())
        run = eden_test.RUNS / 'go-1'
        config = json.loads((run / 'config.json').read_text())
        self.assertEqual(config['card_lineup'], {'staged': False, 'reason': 'test without the card lineup'})
        self.assertEqual([p.get('name', Path(p['path']).name) for p in config['instances'][0]['plugins']],
                         ['plugin.nro'])
        self.assertEqual(config['staged_plugins']['a'], [{
            'name': 'plugin.nro', 'sha256': sha(self.dir / 'plugin.nro'), 'source': 'nro',
            'replaces': {'source': 'base', 'sha256': sha(self.dir / 'plugin.nro')}}])
        self.assertNotIn('superseded_base_pins', config)
        self.assertIn('card lineup NOT STAGED, reason: test without the card lineup', output)
        self.assertIn('- Card lineup: NOT STAGED — reason: test without the card lineup',
                      (run / 'REPORT.md').read_text())

    def test_card_lineup_opt_out_reason_and_mutual_exclusion_refuse_before_staging(self):
        for index, reason in enumerate(('', '   ', "cannot use operator's lineup", 'two\nlines', 'two\rlines')):
            label = f'bad-reason-{index}'
            with self.subTest(reason=reason):
                message = self.assert_lineup_refused(
                    self.go_args(label=label, lineup=True) + ['--no-card-lineup', reason], label)
                self.assertEqual(message, '--no-card-lineup needs a reason on one line without apostrophes; '
                                 f'got: {reason!r}')
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(eden_test, 'cmd_up') as up:
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(self.go_args() + ['--card-lineup', str(self.dir)])
        self.assertEqual(caught.exception.code, 2)
        up.assert_not_called()
        self.assertFalse((eden_test.RUNS / 'go-1').exists())

    def test_card_lineup_remote_verified_then_rsynced_before_ssh_with_origin(self):
        folder = self.make_lineup(self.lineups / '20261231-2359Z/plugins', {'one.nro': b'one'})
        stage = '/home/intpa/eden-runs/_eden-test/stage/go-1'
        token = 'd' * 32
        def fake_sh(command, log=None):
            if command[0] == 'scp' and command[1].endswith('/go-result.json'):
                Path(command[2]).write_text(json.dumps(
                    {'token': token, 'code': 0, 'counts': {'PASS': 0, 'FAIL': 0, 'NOT RUN': 0}}))
            return 0
        with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex=token)), \
             mock.patch.object(eden_test, 'sh', side_effect=fake_sh) as sh:
            self.assertEqual(eden_test.main(self.go_args(host='xps', lineup=True) + ['--ssh', 'xps-host']), 0)
        commands = [call.args[0] for call in sh.call_args_list]
        self.assertEqual(commands[1], ['rsync', '-a', '--delete', '--mkpath',
                                       str(folder.resolve()) + '/', f'xps-host:{stage}/card-lineup/'])
        ssh = next(c for c in commands if c[0] == 'ssh')
        self.assertLess(commands.index(commands[1]), commands.index(ssh))
        self.assertEqual(ssh[ssh.index('--card-lineup'):ssh.index('--card-lineup') + 4],
                         ['--card-lineup', f'{stage}/card-lineup', '--card-lineup-origin', str(folder.resolve())])

    def test_card_lineup_remote_invalid_refuses_before_any_transport(self):
        folder = self.make_lineup(self.lineups / '20261231-2359Z/plugins', {'one.nro': b'one'})
        (folder / 'one.nro').write_bytes(b'changed')
        message = self.assert_lineup_refused(
            self.go_args(host='xps', lineup=True) + ['--ssh', 'xps-host'], 'go-1')
        self.assertIn('one.nro', message)

    def test_card_lineup_remote_opt_out_has_reason_and_no_card_copy(self):
        with mock.patch.object(eden_test, 'sh', return_value=0) as sh:
            with self.assertRaisesRegex(SystemExit, 'remote go produced no result'):
                eden_test.main(self.go_args(host='xps') + ['--ssh', 'xps-host'])
        commands = [call.args[0] for call in sh.call_args_list]
        self.assertFalse(any(c[0] == 'rsync' and c[-1].endswith('/card-lineup/') for c in commands))
        ssh = next(c for c in commands if c[0] == 'ssh')
        self.assertEqual(ssh[ssh.index('--no-card-lineup') + 1], "'test without the card lineup'")

    def test_go_allocates_and_records_distinct_displays(self):
        base = json.loads(self.previous.read_text())
        base['display_helper'] = {'path': '/helper', 'sha256': '0' * 64}
        base['instances'][0]['display'] = ':60'
        second = json.loads(json.dumps(base['instances'][0]))
        second['key'] = 'b'
        base['instances'].append(second)
        self.previous.write_text(json.dumps(base))
        sockets = self.dir / 'sockets'
        sockets.mkdir()
        (sockets / 'X60').touch()
        with mock.patch.object(eden_test, 'X11_SOCKET_DIR', sockets, create=True), \
             mock.patch.object(eden_test, 'cmd_up'), \
             mock.patch.object(eden_test, 'cmd_run', return_value=0):
            self.assertEqual(eden_test.main(self.go_args()), 0)
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertEqual([item['display'] for item in config['instances']], [':62', ':63'])
        self.assertEqual(config['display_choice'], [
            {'key': 'a', 'base': ':60', 'chosen': ':62', 'reason': 'free display chosen (helper provisions it)'},
            {'key': 'b', 'base': ':60', 'chosen': ':63', 'reason': 'free display chosen (helper provisions it)'}])

    def test_go_without_display_helper_preserves_existing_display_and_lifecycle_readiness(self):
        base = json.loads(self.previous.read_text())
        base['instances'][0]['display'] = ':200'
        self.previous.write_text(json.dumps(base))
        sockets = self.dir / 'sockets'
        sockets.mkdir()
        (sockets / 'X200').touch()
        lifecycle_path = Path(__file__).resolve().parent.parent / 'eden-lifecycle' / 'lifecycle.py'
        spec = importlib.util.spec_from_file_location('lifecycle_for_display_test', lifecycle_path)
        lifecycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lifecycle)
        with mock.patch.object(eden_test, 'X11_SOCKET_DIR', sockets), \
             mock.patch.object(eden_test, 'cmd_up'), \
             mock.patch.object(eden_test, 'cmd_run', return_value=0):
            self.assertEqual(eden_test.main(self.go_args()), 0)
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        with mock.patch.object(lifecycle, 'Path', side_effect=lambda path: sockets / Path(path).name):
            original = lifecycle.Host().display_ready(base['instances'][0], base)
            selected = lifecycle.Host().display_ready(config['instances'][0], config)
        self.assertTrue(original['ready'], original['detail'])
        self.assertTrue(selected['ready'], selected['detail'])
        self.assertEqual(config['instances'][0]['display'], ':200')
        self.assertEqual(config['display_choice'], [
            {'key': 'a', 'base': ':200', 'chosen': ':200', 'reason': 'kept: the base has no display_helper'}])

    def test_go_does_not_add_display_to_an_instance_without_one(self):
        with mock.patch.object(eden_test, 'cmd_up'), mock.patch.object(eden_test, 'cmd_run', return_value=0):
            self.assertEqual(eden_test.main(self.go_args()), 0)
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertNotIn('display', config['instances'][0])
        self.assertEqual(config['display_choice'], [])

    def test_go_display_exhaustion_refuses_before_any_copy_or_up(self):
        base = json.loads(self.previous.read_text())
        base['display_helper'] = {'path': '/helper', 'sha256': '0' * 64}
        base['instances'][0]['display'] = ':200'
        self.previous.write_text(json.dumps(base))
        sockets = self.dir / 'sockets'
        sockets.mkdir()
        for number in range(200, 240):
            (sockets / f'X{number}').touch()
        with mock.patch.object(eden_test, 'X11_SOCKET_DIR', sockets, create=True), \
             mock.patch.object(eden_test.shutil, 'copy2') as copy, \
             mock.patch.object(eden_test, 'sh') as sh, \
             mock.patch.object(eden_test, 'lifecycle') as lifecycle, \
             mock.patch.object(eden_test, 'cmd_up') as up:
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(self.go_args())
            self.assertEqual(str(caught.exception), 'no free X display from :200 upward (40 tried)')
            copy.assert_not_called()
            sh.assert_not_called()
            lifecycle.assert_not_called()
            up.assert_not_called()
            self.assertFalse((eden_test.RUNS / 'go-1').exists())

    def test_go_refuses_missing_down_and_loopback_before_staging(self):
        for index, (interface, problem, description) in enumerate((
            (None, None, 'the base names none'),
            ('missing0', 'no such interface', '"missing0" no such interface'),
            ('down0', 'is down', '"down0" is down'),
            ('lo', 'is loopback', '"lo" is loopback'),
        )):
            label = f'network-refuse-{index}'
            base = json.loads(self.previous.read_text())
            if interface is None:
                base.pop('network_interface', None)
            else:
                base['network_interface'] = interface
            self.previous.write_text(json.dumps(base))
            with self.subTest(interface=interface), \
                 mock.patch.object(eden_test, 'host_interface_problem', return_value=problem, create=True), \
                 mock.patch.object(eden_test, 'usable_interfaces', return_value=['wifi0', 'eth0'], create=True), \
                 mock.patch.object(eden_test.shutil, 'copy2') as copy, \
                 mock.patch.object(eden_test, 'sh') as sh, \
                 mock.patch.object(eden_test, '_go_sh') as remote, \
                 mock.patch.object(eden_test, 'lifecycle') as lifecycle, \
                 mock.patch.object(eden_test, 'cmd_up') as up:
                with self.assertRaises(SystemExit) as caught:
                    eden_test.main(self.go_args(label=label))
                self.assertEqual(
                    str(caught.exception),
                    f'lobby rows need a usable host network interface: {description}; usable here: wifi0, eth0. '
                    f'Set "network_interface" in {self.previous} or pass --network-interface; '
                    'pass --no-lobby only for a run without lobby rows')
                copy.assert_not_called()
                sh.assert_not_called()
                remote.assert_not_called()
                lifecycle.assert_not_called()
                up.assert_not_called()
                self.assertFalse((eden_test.RUNS / label).exists())

    def test_go_records_usable_interface_and_override(self):
        for label, extra, expected in (('network-base', [], 'test0'),
                                       ('network-override', ['--network-interface', 'wifi0'], 'wifi0')):
            with self.subTest(label=label), \
                 mock.patch.object(eden_test, 'cmd_up'), \
                 mock.patch.object(eden_test, 'cmd_run', return_value=0):
                self.assertEqual(eden_test.main(self.go_args(label=label) + extra), 0)
            config = json.loads((eden_test.RUNS / label / 'config.json').read_text())
            self.assertEqual(config['network_interface'], expected)
            self.assertEqual(config['network_check'], {
                'interface': expected, 'usable': True, 'problem': None, 'no_lobby': False, 'row_overrides': []})

    def test_no_lobby_allows_and_records_unusable_network(self):
        base = json.loads(self.previous.read_text())
        base.pop('network_interface')
        self.previous.write_text(json.dumps(base))
        with mock.patch.object(eden_test, 'cmd_up'), mock.patch.object(eden_test, 'cmd_run', return_value=0):
            self.assertEqual(eden_test.main(self.go_args() + ['--no-lobby']), 0)
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertIsNone(config['network_interface'])
        self.assertEqual(config['network_check'], {
            'interface': None, 'usable': False, 'problem': 'the base names none', 'no_lobby': True, 'row_overrides': []})

    def test_no_lobby_retains_named_interface_failure(self):
        with mock.patch.object(eden_test, 'host_interface_problem', return_value='is down'), \
             mock.patch.object(eden_test, 'cmd_up'), mock.patch.object(eden_test, 'cmd_run', return_value=0):
            self.assertEqual(eden_test.main(self.go_args() + ['--no-lobby']), 0)
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertEqual(config['network_check'], {
            'interface': 'test0', 'usable': False, 'problem': 'is down', 'no_lobby': True, 'row_overrides': []})

    def test_go_refuses_reviewers_original_boot_steps_before_staging(self):
        example = Path(__file__).resolve().parent / 'example' / 'rows.json'
        plan = json.loads(example.read_text())
        plan['rows'][0]['steps'] = REVIEWER_BOOT_STEPS
        (self.rows / 'rows.json').write_text(json.dumps(plan))
        with mock.patch.object(eden_test.shutil, 'copy2') as copy, \
             mock.patch.object(eden_test, 'sh') as sh, \
             mock.patch.object(eden_test, '_go_sh') as remote, \
             mock.patch.object(eden_test, 'lifecycle') as lifecycle, \
             mock.patch.object(eden_test, 'cmd_up') as up:
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(self.go_args())
            self.assertEqual(
                str(caught.exception),
                'rows set core.network_interface to "lo" in row r00 step 2 (routes:_setting), but this run\'s '
                'checked interface is "test0": remove that step and use {"run": "routes:host_network"} '
                '(or pass --network-interface <value> if that interface is meant; "lo" is never usable: '
                'Eden does not list loopback)')
            copy.assert_not_called()
            sh.assert_not_called()
            remote.assert_not_called()
            lifecycle.assert_not_called()
            up.assert_not_called()
            self.assertFalse((eden_test.RUNS / 'go-1').exists())

    def test_go_accepts_example_and_matching_host_network_interface(self):
        example = Path(__file__).resolve().parent / 'example'
        plan = json.loads((example / 'rows.json').read_text())
        self.assertEqual(eden_test.go_new(self.go_new_args(label='example', rows=example)), eden_test.RUNS / 'example')
        plan['rows'][0]['steps'][1]['args'] = {'interface': 'test0'}
        (self.rows / 'rows.json').write_text(json.dumps(plan))
        self.assertEqual(eden_test.go_new(self.go_new_args(label='matching')), eden_test.RUNS / 'matching')
        for label in ('example', 'matching'):
            config = json.loads((eden_test.RUNS / label / 'config.json').read_text())
            self.assertEqual(config['network_check']['row_overrides'], [])

    def test_go_refuses_explicit_host_network_override(self):
        plan = {'rows': [{'id': 'r1', 'steps': [
            {'run': 'routes:host_network', 'args': {'interface': 'other0'}}]}]}
        (self.rows / 'rows.json').write_text(json.dumps(plan))
        with mock.patch.object(eden_test.shutil, 'copy2') as copy:
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(self.go_args())
        self.assertIn('to "other0" in row r1 step 1 (routes:host_network)', str(caught.exception))
        copy.assert_not_called()
        self.assertFalse((eden_test.RUNS / 'go-1').exists())

    def test_go_records_all_conflicts_including_gif_and_recover_for_no_lobby(self):
        bad = lambda value: {'run': 'routes:_setting', 'args': {'key': 'core.network_interface', 'value': value}}
        plan = {'recover': [bad('plan0')], 'rows': [
            {'id': 'r1', 'steps': [bad('other0'), {'gif': {'name': 'clip', 'steps': [bad('gif0')]}}],
             'recover': [bad('recover0')]}]}
        (self.rows / 'rows.json').write_text(json.dumps(plan))
        with mock.patch.object(eden_test.shutil, 'copy2') as copy:
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(self.go_args())
            copy.assert_not_called()
        for location in ('row r1 step 1', 'row r1 step 2 gif step 1',
                         'row r1 step recover 1', 'row <plan recover> step 1'):
            self.assertIn(location, str(caught.exception))
        self.assertEqual(eden_test.go_new(self.go_new_args(no_lobby=True)), eden_test.RUNS / 'go-1')
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertEqual(config['network_check']['row_overrides'], [
            {'row': 'r1', 'step': 1, 'run': 'routes:_setting', 'value': 'other0'},
            {'row': 'r1', 'step': '2 gif step 1', 'run': 'routes:_setting', 'value': 'gif0'},
            {'row': 'r1', 'step': 'recover 1', 'run': 'routes:_setting', 'value': 'recover0'},
            {'row': '<plan recover>', 'step': 1, 'run': 'routes:_setting', 'value': 'plan0'}])
        self.assertEqual(json.loads((eden_test.RUNS / 'go-1/rows/rows.json').read_text()), plan)

    def test_go_requires_owner_before_dispatch(self):
        error = io.StringIO()
        with mock.patch.object(eden_test, 'cmd_go') as go, redirect_stderr(error):
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(self.go_args(owner=None))
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('--owner', error.getvalue())
        go.assert_not_called()

    def test_go_rejects_bad_owner_before_local_or_remote_staging(self):
        for host in ('yoga', 'xps'):
            for owner in ('eden-test/go-1', 'Claude', 'Claude multiple words', ''):
                with self.subTest(host=host, owner=owner), \
                     mock.patch.object(eden_test, 'go_new', side_effect=SystemExit('go_new called')) as new, \
                     mock.patch.object(eden_test, '_go_remote', side_effect=SystemExit('remote called')) as remote, \
                     mock.patch.object(eden_test, 'sh') as sh, \
                     mock.patch.object(eden_test, 'lifecycle') as lifecycle:
                    with self.assertRaises(SystemExit) as caught:
                        eden_test.main(self.go_args(host=host, owner=owner))
                    self.assertEqual(
                        str(caught.exception),
                        '--owner must be "<Engine> <session>/<stream>" (OpenCode|Codex|Claude|Cursor), '
                        f'e.g. "Claude devin-<lane>/<label>"; got: {owner!r}')
                    new.assert_not_called()
                    remote.assert_not_called()
                    sh.assert_not_called()
                    lifecycle.assert_not_called()
                    self.assertFalse((self.dir / 'runs/go-1').exists())

    def test_go_local_copies_pins_port_trace_and_reports_without_down(self):
        source = self.dir / 'profile/user/config'
        source.mkdir(parents=True)
        (source / 'qt-config.ini').write_text('[Debug]\nuse_gdbstub=false\n')
        port = self.dir / 'old-port.txt'
        port.write_text('6979\n')
        base = json.loads(self.previous.read_text())
        base.update(rows={'path': 'old'}, authorization='old')
        item = base['instances'][0]
        item.update(source=str(source.parent.parent), trace={'unmapped_access': 'log'},
                    files=[{'path': str(port), 'sha256': sha(port),
                            'to': 'user/sdmc/ultimate/ssbu-hw-bridge/port.txt'}])
        self.previous.write_text(json.dumps(base))
        build = self.dir / 'new-build'
        build.mkdir()
        new = build / 'plugin.nro'
        new.write_bytes(b'new plugin build')
        extra = build / 'extra.nro'
        extra.write_bytes(b'an additional plugin')
        elf = build / 'plugin.so'
        elf.write_bytes(b'symbols')
        (self.rows / 'rows.json').write_text(json.dumps({'rows': [
            {'id': 'r1', 'steps': []}, {'id': 'r2', 'steps': []}]}))
        (self.rows / 'relative-link').symlink_to('rows.json')
        output = io.StringIO()
        fake_routes = types.SimpleNamespace(free_port=lambda: 6987)
        argv = self.go_args(nro=new) + ['--nro', str(extra), '--elf', str(elf),
                                      '--trace-point', 'plugin.nro!attempt']
        with mock.patch.dict(sys.modules, {'routes': fake_routes}), \
             mock.patch.object(eden_test, 'cmd_down') as down, redirect_stdout(output):
            self.assertEqual(eden_test.main(argv), 0)
        down.assert_not_called()
        run = self.dir / 'runs/go-1'
        config = json.loads((run / 'config.json').read_text())
        self.assertEqual((config['owner'], config['label'], config['defer_load']),
                         ('Claude devin-test/go-1', 'go-1', True))
        self.assertNotIn('authorization', config)
        self.assertEqual(config['state_dir'], str(run / 'state'))
        self.assertEqual(config['work_root'], str(run / 'work'))
        pins = config['instances'][0]['plugins']
        self.assertEqual((pins[0]['path'], pins[0]['sha256']), (str(run / 'inputs/plugin.nro'), sha(new)))
        self.assertEqual(pins[1], {'name': 'extra.nro', 'path': str(run / 'inputs/extra.nro'),
                                   'sha256': sha(extra)})
        self.assertEqual(config['plugin_swaps'], [{'from': str(self.dir / 'plugin.nro'),
                                                   'from_sha256': sha(self.dir / 'plugin.nro'),
                                                   'to': str(run / 'inputs/plugin.nro')}])
        self.assertEqual((run / 'inputs/port.txt').read_text(), '6987\n')
        pin = config['instances'][0]['files'][0]
        self.assertEqual((pin['path'], pin['sha256']), (str(run / 'inputs/port.txt'), sha(run / 'inputs/port.txt')))
        self.assertEqual(config['bridge_port'], 6987)
        self.assertIn('GDB stub off', config['stub_rule'])
        self.assertEqual(config['instances'][0]['trace']['symbols'],
                         [{'module': 'plugin.nro', 'elf': str(run / 'inputs/plugin.so')}])
        self.assertEqual(config['instances'][0]['trace']['points'], [
            {'target': 'plugin.nro!attempt', 'mode': 'records', 'registers': ['x0', 'x1', 'x2', 'x3']}])
        self.assertEqual(config['rows']['sha256'], sha(run / 'rows/rows.json'))
        self.assertEqual(config['runner']['sha256'], sha(eden_test.RUNNER))
        self.assertTrue((run / 'rows/relative-link').is_symlink())
        self.assertIn('2 PASS, 0 FAIL, 0 NOT RUN', (run / 'REPORT.md').read_text())
        self.assertIn('2 PASS, 0 FAIL, 0 NOT RUN', output.getvalue())
        self.assertEqual(json.loads((run / 'go-result.json').read_text()),
                         {'token': None, 'code': 0, 'counts': {'PASS': 2, 'FAIL': 0, 'NOT RUN': 0}})
        self.assertFalse((run / 'lifecycle-down.log').exists())

    def test_go_file_rows_owner_and_no_trace_rule(self):
        rowfile = self.rows / 'rows.json'
        rowfile.write_text(json.dumps({'rows': [{'id': 'r1', 'steps': []}, {'id': 'r2', 'steps': []}]}))
        with mock.patch.object(eden_test, 'cmd_down') as down:
            code = eden_test.main(self.go_args(rows=rowfile) + ['--owner', 'Claude test/owner'])
        self.assertEqual(code, 0)
        down.assert_not_called()
        run = self.dir / 'runs/go-1'
        config = json.loads((run / 'config.json').read_text())
        self.assertEqual(config['owner'], 'Claude test/owner')
        self.assertEqual(config['stub_rule'], 'no trace section: stub left to the rows (routes:configure_boot)')
        self.assertEqual((run / 'rows/rows.json').read_bytes(), rowfile.read_bytes())
        self.assertNotIn('bridge_port', config)

    def test_go_local_records_invocation_token(self):
        token = 'd' * 32
        with mock.patch.object(eden_test, 'cmd_up'), mock.patch.object(eden_test, 'cmd_run', return_value=0):
            self.assertEqual(eden_test.main(self.go_args() + ['--token', token]), 0)
        result = json.loads((self.dir / 'runs/go-1/go-result.json').read_text())
        self.assertEqual(result['token'], token)

    def test_go_default_base_updates_every_instances_plugin_and_port(self):
        base = json.loads(self.previous.read_text())
        port = self.dir / 'base-port.txt'
        port.write_text('6979\n')
        base['instances'][0]['files'] = [{'path': str(port), 'sha256': sha(port),
                                        'to': 'user/sdmc/ultimate/ssbu-hw-bridge/port.txt'}]
        second = json.loads(json.dumps(base['instances'][0]))
        second['key'] = 'b'
        base['instances'].append(second)
        default = eden_test.RUNS / '_eden-test/base.json'
        default.parent.mkdir(parents=True)
        default.write_text(json.dumps(base))
        free = mock.Mock(return_value=6991)
        fake_routes = types.SimpleNamespace(free_port=free)
        argv = self.go_args()[:-2]
        with mock.patch.dict(sys.modules, {'routes': fake_routes}), \
             mock.patch.object(eden_test, 'cmd_up') as up, \
             mock.patch.object(eden_test, 'cmd_run', return_value=0):
            self.assertEqual(eden_test.main(argv), 0)
        up.assert_called_once_with(self.dir / 'runs/go-1', skip_setup_plan=True, allow_volatile_pins=True)
        free.assert_called_once_with()
        config = json.loads((self.dir / 'runs/go-1/config.json').read_text())
        self.assertEqual(len(config['plugin_swaps']), 2)
        for item in config['instances']:
            self.assertEqual(item['plugins'][0]['path'], str(self.dir / 'runs/go-1/inputs/plugin.nro'))
            self.assertEqual(item['files'][0]['path'], str(self.dir / 'runs/go-1/inputs/port.txt'))
        self.assertEqual(config['bridge_port'], 6991)

    def test_go_row_failure_and_not_run_return_three(self):
        for label, spec, expected in (
            ('failed', {'rows': [{'id': 'r1'}, {'id': 'r2'}], 'note': 'fail-once'}, '1 FAIL, 0 NOT RUN'),
            ('unrun', {'rows': [{'id': 'r1'}, {'id': 'r2'}, {'id': 'r3'}]}, '0 FAIL, 1 NOT RUN'),
        ):
            with self.subTest(label=label):
                (self.rows / 'rows.json').write_text(json.dumps(spec))
                with mock.patch.object(eden_test, 'cmd_down') as down:
                    self.assertEqual(eden_test.main(self.go_args(label=label)), 3)
                down.assert_not_called()
                self.assertIn(expected, (self.dir / f'runs/{label}/REPORT.md').read_text())
                self.assertEqual(json.loads((self.dir / f'runs/{label}/go-result.json').read_text())['code'], 3)

    def test_go_trace_stub_refusal_precedes_lifecycle(self):
        source = self.dir / 'source/user/config'
        source.mkdir(parents=True)
        (source / 'qt-config.ini').write_text('use_gdbstub=true\n')
        base = json.loads(self.previous.read_text())
        base['instances'][0].update(source=str(source.parent.parent), trace={'unmapped_access': 'log'})
        self.previous.write_text(json.dumps(base))
        elf = self.dir / 'plugin.so'
        elf.write_bytes(b'elf')
        with mock.patch.object(eden_test, 'lifecycle') as lifecycle:
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(self.go_args() + ['--elf', str(elf), '--trace-point', 'plugin.nro!attempt'])
        lifecycle.assert_not_called()
        self.assertIn(f'the source profile {source / "qt-config.ini"} has use_gdbstub=true', str(caught.exception))

    def test_go_trace_base_requires_elf_and_trace_points_before_copy(self):
        source = self.dir / 'profile'
        source.mkdir()
        base = json.loads(self.previous.read_text())
        base['instances'][0].update(source=str(source), trace={'unmapped_access': 'log'})
        self.previous.write_text(json.dumps(base))
        elf = self.dir / 'plugin.so'
        elf.write_bytes(b'elf')
        for index, options in enumerate(([], ['--elf', str(elf)], ['--trace-point', 'plugin.nro!attempt'])):
            label = f'missing-trace-{index}'
            with self.subTest(options=options), \
                 mock.patch.object(eden_test, 'cmd_up', side_effect=SystemExit('unexpected Up')) as up, \
                 mock.patch.object(eden_test, 'sh') as sh, mock.patch.object(eden_test, 'lifecycle') as lifecycle:
                with self.assertRaises(SystemExit) as caught:
                    eden_test.main(self.go_args(label=label) + options)
                self.assertEqual(str(caught.exception), TRACE_REQUIRED)
                up.assert_not_called()
                sh.assert_not_called()
                lifecycle.assert_not_called()
                self.assertFalse((self.dir / 'runs' / label).exists())

    def test_go_new_direct_rejects_trace_base_before_copy(self):
        base = json.loads(self.previous.read_text())
        base['instances'][0].update(source=str(self.dir), trace={'unmapped_access': 'log'})
        self.previous.write_text(json.dumps(base))
        args = types.SimpleNamespace(from_config=str(self.previous), host='yoga', label='direct-1',
                                     owner='Claude test/direct', nro=[str(self.dir / 'plugin.nro')],
                                     rows=str(self.rows), elf=None, trace_point=[], card_lineup=None,
                                     card_lineup_origin=None, no_card_lineup='test without the card lineup')
        with self.assertRaises(SystemExit) as caught:
            eden_test.go_new(args)
        self.assertEqual(str(caught.exception), TRACE_REQUIRED)
        self.assertFalse((self.dir / 'runs/direct-1').exists())

    def test_go_remote_surfaces_trace_refusal_without_report_copy(self):
        output = io.StringIO()

        def fake_sh(command, log=None):
            if command[0] == 'ssh':
                print(TRACE_REQUIRED)
                return 1
            if command[0] == 'scp':
                return 1
            return 0

        with chdir(self.dir), mock.patch.object(eden_test, 'sh', side_effect=fake_sh) as sh, \
             redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, 'remote go produced no result for this call'):
                eden_test.main(self.go_args(host='xps') + ['--ssh', 'mapped-xps'])
        self.assertIn(TRACE_REQUIRED, output.getvalue())
        self.assertFalse(any(c.args[0][0] == 'scp' and c.args[0][1].endswith('/REPORT.md')
                             for c in sh.call_args_list))
        self.assertFalse((self.dir / 'REPORT-go-1.md').exists())

    def test_go_trace_point_requires_elf_and_elf_requires_matching_nro(self):
        elf = self.dir / 'other.so'
        elf.write_bytes(b'elf')
        with mock.patch.object(eden_test, 'lifecycle') as lifecycle:
            with self.assertRaisesRegex(SystemExit, '--trace-point.*--elf'):
                eden_test.main(self.go_args() + ['--trace-point', 'plugin.nro!attempt'])
            with self.assertRaises(SystemExit) as caught:
                eden_test.main(self.go_args() + ['--elf', str(elf)])
        self.assertIn('other.so', str(caught.exception))
        self.assertIn('plugin.nro', str(caught.exception))
        lifecycle.assert_not_called()

    def test_go_reports_even_when_runner_raises(self):
        (self.rows / 'rows.json').write_text('{"rows":[{"id":"r1"}]}')
        output = io.StringIO()
        with mock.patch.object(eden_test, 'cmd_up'), \
             mock.patch.object(eden_test, 'cmd_run', side_effect=RuntimeError('runner broke')), \
             mock.patch.object(eden_test, 'cmd_down') as down, redirect_stdout(output):
            self.assertEqual(eden_test.main(self.go_args()), 3)
        down.assert_not_called()
        self.assertTrue((self.dir / 'runs/go-1/REPORT.md').is_file())
        self.assertIn('runner broke', output.getvalue())

    def test_go_failed_up_after_start_prints_down_without_calling_it(self):
        def fail_up(run, **kwargs):
            eden_test.stamp(run, 'up_start')
            raise SystemExit('lifecycle failed')

        output = io.StringIO()
        with mock.patch.object(eden_test, 'cmd_up', side_effect=fail_up), \
             mock.patch.object(eden_test, 'cmd_down') as down, redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, 'lifecycle failed'):
                eden_test.main(self.go_args())
        down.assert_not_called()
        self.assertIn(f'down {self.dir / "runs/go-1"}', output.getvalue())

    def test_go_host_mismatch_without_remote_mapping_names_host_and_map(self):
        with self.assertRaises(SystemExit) as caught:
            eden_test.main(self.go_args(host='xps'))
        self.assertIn('xps', str(caught.exception))
        self.assertIn('hosts.json', str(caught.exception))

    def test_go_remote_stages_exact_commands_and_copies_report_on_exit_three(self):
        build = self.dir / 'build'
        build.mkdir()
        nro, second, elf = build / 'plugin.nro', build / 'extra.nro', build / 'plugin.so'
        for path in (nro, second, elf):
            path.write_bytes(b'fixture')
        host, label = 'xps-host', 'remote-1'
        stage = f'/home/intpa/eden-runs/_eden-test/stage/{label}'
        argv = self.go_args(label=label, nro=nro, host='xps') + [
            '--nro', str(second), '--elf', str(elf), '--trace-point', 'plugin.nro!attempt',
            '--owner', 'Claude devin-test/remote-1', '--ssh', host,
            '--network-interface', 'wlp114s0', '--no-lobby']
        output = io.StringIO()
        token = 'a' * 32
        def fake_sh(command, log=None):
            if command[0] == 'ssh':
                return 1
            if command[0] == 'scp':
                if command[1].endswith('/go-result.json'):
                    Path(command[2]).write_text(json.dumps(
                        {'token': token, 'code': 3, 'counts': {'PASS': 3, 'FAIL': 1, 'NOT RUN': 16}}))
                else:
                    Path(command[2]).write_text('report')
            return 0

        with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex=token)), \
             mock.patch.object(eden_test, 'sh', side_effect=fake_sh) as sh, \
             redirect_stdout(output):
            self.assertEqual(eden_test.main(argv), 3)
        expected = [
            ['rsync', '-a', '--delete', '--mkpath', str(eden_test.PLAYBOOK / 'tools') + '/', f'{host}:{stage}/tools/'],
            ['rsync', '-a', '--mkpath', str(nro), f'{host}:{stage}/nro/'],
            ['rsync', '-a', '--mkpath', str(second), f'{host}:{stage}/nro/'],
            ['rsync', '-a', '--mkpath', str(elf), f'{host}:{stage}/elf/'],
            ['rsync', '-a', '--mkpath', str(self.rows) + '/', f'{host}:{stage}/rows/'],
            ['ssh', '-o', 'BatchMode=yes', host, '/usr/bin/python3', '-I', '-B',
             f'{stage}/tools/eden-test/eden_test.py', 'go', '--host', 'xps', '--label', label,
             '--nro', f'{stage}/nro/plugin.nro', '--nro', f'{stage}/nro/extra.nro',
             '--rows', f'{stage}/rows', '--no-card-lineup', "'test without the card lineup'",
             '--elf', f'{stage}/elf/plugin.so',
             '--trace-point', 'plugin.nro!attempt', '--network-interface', 'wlp114s0', '--no-lobby',
             '--owner', "'Claude devin-test/remote-1'", '--token', token],
            ['scp', f'{host}:/home/intpa/eden-runs/{label}/go-result.json', mock.ANY],
            ['scp', f'{host}:/home/intpa/eden-runs/{label}/REPORT.md', f'./REPORT-{label}.md'],
        ]
        self.assertEqual([call.args[0] for call in sh.call_args_list], expected)
        self.interface_check.assert_not_called()
        temp = Path(sh.call_args_list[-2].args[0][-1])
        self.assertTrue(temp.is_absolute())
        self.assertFalse(temp.exists())
        self.assertIn(f'REPORT-{label}.md', output.getvalue())
        self.assertIn(f'run /home/intpa/eden-runs/{label} --rows <id>', output.getvalue())
        self.assertIn(f'down /home/intpa/eden-runs/{label}', output.getvalue())

    def test_go_remote_uses_host_map_and_file_rows(self):
        hosts = eden_test.RUNS / '_eden-test/hosts.json'
        hosts.parent.mkdir(parents=True)
        hosts.write_text(json.dumps({'xps': 'mapped-xps'}))
        rowfile = self.rows / 'rows.json'
        def fake_sh(command, log=None):
            if command[0] == 'scp' and command[1].endswith('/go-result.json'):
                Path(command[2]).write_text(json.dumps(
                    {'token': 'b' * 32, 'code': 0, 'counts': {'PASS': 2, 'FAIL': 0, 'NOT RUN': 0}}))
            return 0

        with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex='b' * 32)), \
             mock.patch.object(eden_test, 'sh', side_effect=fake_sh) as sh:
            self.assertEqual(eden_test.main(self.go_args(host='xps', rows=rowfile)), 0)
        commands = [call.args[0] for call in sh.call_args_list]
        self.assertIn(['rsync', '-a', '--mkpath', str(rowfile),
                       'mapped-xps:/home/intpa/eden-runs/_eden-test/stage/go-1/rows/rows.json'], commands)
        self.assertEqual(commands[-3][0:4], ['ssh', '-o', 'BatchMode=yes', 'mapped-xps'])

    def test_go_remote_missing_result_refuses_without_copying_report(self):
        output = io.StringIO()

        def fake_sh(command, log=None):
            if command[0] == 'ssh' or command[0] == 'scp' and command[1].endswith('/go-result.json'):
                return 1
            if command[0] == 'scp':
                Path(command[2]).write_text('report')
            return 0

        with chdir(self.dir), mock.patch.object(eden_test, 'sh', side_effect=fake_sh) as sh, redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, 'remote go produced no result for this call \\(ssh exit 1\\)'):
                eden_test.main(self.go_args(host='xps') + ['--ssh', 'mapped-xps'])
        self.assertFalse(any(c.args[0][0] == 'scp' and c.args[0][1].endswith('/REPORT.md')
                             for c in sh.call_args_list))
        self.assertNotIn('Down:', output.getvalue())

    def test_go_remote_rejects_old_green_result_for_reused_label(self):
        for old_token in (None, 'old-invocation'):
            with self.subTest(old_token=old_token):
                output = io.StringIO()

                def fake_sh(command, log=None):
                    if command[0] == 'ssh':
                        return 1
                    if command[0] == 'scp':
                        if command[1].endswith('/go-result.json'):
                            old = {'code': 0, 'counts': {'PASS': 2, 'FAIL': 0, 'NOT RUN': 0}}
                            if old_token is not None:
                                old['token'] = old_token
                            Path(command[2]).write_text(json.dumps(old))
                        else:
                            Path(command[2]).write_text('old report')
                    return 0

                with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex='c' * 32)), \
                     mock.patch.object(eden_test, 'sh', side_effect=fake_sh) as sh, redirect_stdout(output):
                    with self.assertRaisesRegex(SystemExit, 'remote go produced no result for this call \\(ssh exit 1\\)'):
                        eden_test.main(self.go_args(host='xps') + ['--ssh', 'mapped-xps'])
                self.assertFalse(any(c.args[0][0] == 'scp' and c.args[0][1].endswith('/REPORT.md')
                                     for c in sh.call_args_list))
                self.assertNotIn('Report:', output.getvalue())
                self.assertNotIn('Down:', output.getvalue())

    def test_go_remote_staging_failure_never_invokes_ssh(self):
        with mock.patch.object(eden_test, 'sh', return_value=9) as sh:
            with self.assertRaisesRegex(SystemExit, 'staging failed'):
                eden_test.main(self.go_args(host='xps') + ['--ssh', 'mapped-xps'])
        self.assertEqual(sh.call_count, 1)
        self.assertEqual(sh.call_args.args[0][0], 'rsync')

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
        self.assertEqual(comparison['differing_paths'], [['last_page']])

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


FLOWN = Path('/home/intpa/smash-training-lobby/artifacts/phase23/test-setup-speed/blocks')


class ReportTests(unittest.TestCase):
    """`report`: per row PASS / FAIL / NOT RUN with the reason and the proof, read from the run folder only."""

    def setUp(self):
        self.run = Path(tempfile.mkdtemp(prefix='eden-test-report-'))
        self.addCleanup(shutil.rmtree, self.run)
        out = self.run / 'out'
        (self.run / 'rows').mkdir()
        (out / 'r1').mkdir(parents=True)
        (self.run / 'state').mkdir()
        rows = [{'id': f'r{i}', 'start': 't', 'end': 't', 'steps': [{'run': f'routes:block{i}'}]} for i in range(4)]
        (self.run / 'rows/rows.json').write_text(json.dumps({'rows': rows}))
        line = '[Bridge] roster entry=1 kind=83 nana=false ev=106 frame=2595 wall=379783903'
        (out / 'r0.json').write_text(json.dumps({
            'id': 'r0', 'status': 'PASS', 'started': 10, 'ended': 12, 'role': 'setup',
            'results': [{'run': 'routes:block0', 'value': {'block': 'block0', 'verified': {'kind': 83}, 'line': line}}]}))
        (out / 'r1/shot-FAIL.png').write_bytes(b'png!')
        (out / 'r1.json').write_text(json.dumps({
            'id': 'r1', 'status': 'FAILED', 'started': 12, 'ended': 13, 'role': 'test', 'results': [],
            'error': "RuntimeError('[Bridge] req set result=refused reason=no_backup frame=2627 wall=379784461')",
            'evidence_paths': ['/home/intpa/eden-runs/x/out/r1/shot-FAIL.png']}))
        (out / 'r3.json').write_text(json.dumps({'id': 'r3', 'status': 'RAN-UNVERIFIED', 'started': 13, 'ended': 14,
                                                 'results': []}))
        (out / 'attempts.jsonl').write_text(''.join(json.dumps({'id': i, 'status': s}) + '\n' for i, s in (
            ('r0', 'FAILED'), ('r0', 'PASS'), ('r1', 'FAILED'), ('r3', 'RAN-UNVERIFIED'))))
        profile = self.run / 'work/profiles/profile-a'
        plugins = profile / eden_test.PLUGINS
        plugins.mkdir(parents=True)
        (plugins / 'libx.nro').write_bytes(b'the build under test')
        (plugins / 'stray.nro').write_bytes(b'not pinned')
        (self.run / 'state/instances.json').write_text(json.dumps({'a': {'profile': str(profile)}}))
        self.pin = hashlib.sha256(b'the build under test').hexdigest()
        self.config = {'label': 'lab-1', 'host': 'yoga',
                       'instances': [{'key': 'a', 'plugins': [{'name': 'libx.nro', 'path': '/in/libx.nro',
                                                               'sha256': self.pin}]}]}
        (self.run / 'config.json').write_text(json.dumps(self.config))
        (self.run / 'timing.json').write_text(json.dumps({'up_start': 1, 'up_done': 9, 'run_start': 10, 'run_end': 14}))

    def rows_of(self, text):
        return {line.split('|')[1].strip(): line for line in text.splitlines() if line.startswith('| r')}

    def test_each_row_is_pass_fail_or_not_run_with_reason_and_proof(self):
        text = eden_test.report_md(self.run)
        rows = self.rows_of(text)
        self.assertEqual(sorted(rows), ['r0', 'r1', 'r2', 'r3'])
        self.assertIn('| PASS |', rows['r0'])
        self.assertIn('kind', rows['r0'])                         # the reason: what the block verified
        self.assertIn('roster entry=1 kind=83', rows['r0'])       # the proof: the raw line in the receipt
        self.assertIn('earlier attempts: FAILED', rows['r0'])     # a pass after a failed attempt says so
        self.assertIn('| FAIL |', rows['r1'])
        self.assertIn('reason=no_backup frame=2627', rows['r1'])
        self.assertIn('out/r1/shot-FAIL.png (4 bytes)', rows['r1'])
        self.assertIn('| NOT RUN |', rows['r2'])
        self.assertIn('| FAIL |', rows['r3'])                     # ran but unverified is never a PASS
        self.assertIn('RAN-UNVERIFIED', rows['r3'])
        for row in ('r1', 'r2', 'r3'):
            self.assertNotIn('PASS', rows[row])
        self.assertIn('1 PASS, 2 FAIL, 1 NOT RUN of 4', text)

    def test_the_nro_hashes_at_the_top_are_of_the_files_in_the_profile(self):
        top = eden_test.report_md(self.run).split('| Row |')[0]
        self.assertIn(f'libx.nro `{self.pin}` in the profile; matches the config pin', top)
        stray = hashlib.sha256(b'not pinned').hexdigest()
        self.assertIn(f'stray.nro `{stray}` in the profile; NOT in the config', top)
        (self.run / 'work/profiles/profile-a' / eden_test.PLUGINS / 'libx.nro').write_bytes(b'another build')
        top = eden_test.report_md(self.run).split('| Row |')[0]
        self.assertIn('DIFFERS from the config pin', top)

    def test_a_pruned_profile_is_said_and_the_pins_are_shown_as_pins(self):
        shutil.rmtree(self.run / 'work')
        top = eden_test.report_md(self.run).split('| Row |')[0]
        self.assertIn(f'libx.nro `{self.pin}` as pinned in config.json (the profile is gone', top)

    def test_card_lineup_report_sources_in_profile_and_after_pruning(self):
        folder = self.run / 'work/profiles/profile-a' / eden_test.PLUGINS
        specs = [
            ('card.nro', b'card', 'card', None, '; from the card lineup'),
            ('base.nro', b'base', 'base', None, '; from the base (not on the card)'),
            ('replace-card.nro', b'new card', 'nro', {'source': 'card', 'sha256': 'a' * 64},
             f'; --nro, replaces the card file {"a" * 64}'),
            ('replace-base.nro', b'new base', 'nro', {'source': 'base', 'sha256': 'b' * 64},
             f'; --nro, replaces the base pin {"b" * 64}'),
            ('added.nro', b'added', 'nro', None, '; --nro, added (no card or base file of this name)'),
        ]
        pins, staged = [], []
        for name, data, source, replaces, _ in specs:
            path = folder / name
            path.write_bytes(data)
            pins.append({'name': name, 'path': str(path), 'sha256': sha(path)})
            entry = {'name': name, 'sha256': sha(path), 'source': source}
            if replaces:
                entry['replaces'] = replaces
            staged.append(entry)
        self.config['instances'][0]['plugins'] = pins
        self.config['staged_plugins'] = {'a': staged}
        self.config['card_lineup'] = {'staged': True, 'dir': '/remote/card', 'origin': '/surface/card',
                                      'sums_sha256': 'c' * 64, 'files': 2}
        (self.run / 'config.json').write_text(json.dumps(self.config))
        for pruned in (False, True):
            if pruned:
                shutil.rmtree(self.run / 'work')
            with self.subTest(pruned=pruned):
                top = eden_test.report_md(self.run).split('| Row |')[0]
                self.assertIn('- Card lineup: /surface/card (SHA256SUMS cccccccccccc…, 2 files verified)', top)
                self.assertIn('replace-card.nro (card aaaaaaaaaaaa… → ', top)
                self.assertIn('base.nro (base)', top)
                self.assertIn('added.nro (--nro)', top)
                for pin, spec in zip(pins, specs):
                    line = next(line for line in top.splitlines() if line.startswith(f'  - {pin["name"]} '))
                    self.assertIn(pin['sha256'], line)
                    self.assertIn(spec[4], line)
        self.assertIn('- Card lineup:', top[:top.index('- NROs:')])

    def test_opt_out_report_says_from_base_without_claiming_card_membership(self):
        self.config['card_lineup'] = {'staged': False, 'reason': 'test without the card lineup'}
        self.config['staged_plugins'] = {'a': [{'name': 'libx.nro', 'sha256': self.pin, 'source': 'base'}]}
        (self.run / 'config.json').write_text(json.dumps(self.config))
        for pruned in (False, True):
            if pruned:
                shutil.rmtree(self.run / 'work')
            with self.subTest(pruned=pruned):
                report = eden_test.report_md(self.run)
                line = next(line for line in report.splitlines() if line.startswith('  - libx.nro '))
                self.assertIn(self.pin, line)
                self.assertTrue(line.endswith('; from the base'), line)
                self.assertNotIn('not on the card', report)
                self.assertIn('- Card lineup: NOT STAGED — reason: test without the card lineup', report)

    def test_card_lineup_report_old_config_says_not_recorded_without_source_suffixes(self):
        top = eden_test.report_md(self.run).split('| Row |')[0]
        self.assertIn('- Card lineup: not recorded (run made before the card-lineup default)', top)
        self.assertNotIn('; from the card lineup', top)
        self.assertNotIn('; from the base', top)

    def test_up_or_down_is_read_from_the_run_and_the_next_commands_are_printed(self):
        text = eden_test.report_md(self.run)
        self.assertIn('EDEN UP on yoga', text)
        self.assertIn(f'run {self.run} --rows <id>', text)
        self.assertIn(f'down {self.run}', text)
        timing = json.loads((self.run / 'timing.json').read_text())
        (self.run / 'timing.json').write_text(json.dumps(dict(timing, down_start=20, down_done=30)))
        (self.run / 'state/lifecycle.json').write_text(json.dumps({'released': True, 'cleanup_failures': []}))
        text = eden_test.report_md(self.run)
        self.assertNotIn('EDEN UP', text)
        self.assertIn('DOWN', text)
        self.assertIn('released=True', text)

    def test_report_command_writes_the_file(self):
        self.assertEqual(eden_test.main(['report', str(self.run)]), 0)
        self.assertIn('1 PASS, 2 FAIL, 1 NOT RUN of 4', (self.run / 'REPORT.md').read_text())

    @unittest.skipUnless((FLOWN / 'proof-tss-blocks-83ee-1/yoga/out').is_dir(), 'flown run -1 not on this machine')
    def test_flown_run_1_whose_runner_crashed_never_shows_r01_as_pass(self):
        text = eden_test.report_md(FLOWN / 'proof-tss-blocks-83ee-1/yoga')
        rows = self.rows_of(text)
        self.assertIn('| PASS |', rows['r00'])
        self.assertIn('| FAIL |', rows['r01'])  # its blocks ran, the runner crashed before the row receipt
        self.assertIn('attempted without a verdict', rows['r01'])
        self.assertIn('out/r01/routes-reach-training.json', rows['r01'])
        self.assertEqual([r for r, line in rows.items() if 'PASS' in line], ['r00'])
        self.assertIn('1 PASS, 1 FAIL, 17 NOT RUN of 19', text)
        top = text.split('| Row |')[0]
        self.assertIn('Runner: 1 call(s) ended: FAILED', top)  # the crash is said, not hidden
        self.assertIn('Object of type set is not JSON serializable', top)

    def test_a_row_whose_steps_ran_without_a_row_receipt_is_a_fail_and_the_runner_end_is_shown(self):
        (self.run / 'out/r2').mkdir()
        (self.run / 'out/r2/routes-block2.json').write_text('{}')
        (self.run / 'runner.log').write_text(
            '$ runner run\n{"row": "r1", "status": "FAILED", "seconds": 1.0}\n'
            '{"status": "FAILED", "error": "TypeError(\'boom\')", "stop_input": "x"}\n\n'
            '$ runner run --rows r0\n{"status": "DONE", "out": "x", "failed": 0}\n')
        text = eden_test.report_md(self.run)
        row = self.rows_of(text)['r2']
        self.assertIn('| FAIL |', row)
        self.assertIn('attempted without a verdict', row)
        self.assertIn('out/r2/routes-block2.json', row)
        self.assertIn("Runner: 2 call(s) ended: FAILED `TypeError('boom')`; DONE", text)
        self.assertIn('1 PASS, 3 FAIL, 0 NOT RUN of 4', text)

    def test_recovery_files_alone_do_not_mark_the_row_attempted(self):
        recovery = self.run / 'out/r2/recovery'
        recovery.mkdir(parents=True)
        (recovery / 'routes-tr-idle.json').write_text('{}')
        row = self.rows_of(eden_test.report_md(self.run))['r2']
        self.assertIn('| NOT RUN |', row)
        self.assertIn('no receipt out/r2.json; a recovery to this row ran (out/r2/recovery/)', row)
        self.assertNotIn('attempted without a verdict', row)
        (recovery.parent / 'actual-row-output.json').write_text('{}')
        row = self.rows_of(eden_test.report_md(self.run))['r2']
        self.assertIn('| FAIL |', row)
        self.assertIn('attempted without a verdict', row)

    @unittest.skipUnless((FLOWN / 'proof-tss-blocks-83ee-2/yoga/out').is_dir(), 'flown run -2 not on this machine')
    def test_flown_run_2_shows_three_passes_the_refusal_and_fifteen_unrun(self):
        text = eden_test.report_md(FLOWN / 'proof-tss-blocks-83ee-2/yoga')
        rows = self.rows_of(text)
        self.assertEqual([r for r, line in rows.items() if '| PASS |' in line], ['r00', 'r01', 'r02'])
        self.assertIn('| FAIL |', rows['r03'])
        self.assertIn('req set result=refused reason=no_backup frame=2627 wall=379784461', rows['r03'])
        self.assertEqual(sum('| NOT RUN |' in line for line in rows.values()), 15)
        self.assertIn('3 PASS, 1 FAIL, 15 NOT RUN of 19', text)
        self.assertIn('DOWN', text)


class RerunTests(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp(prefix='eden-test-rerun-'))
        self.addCleanup(shutil.rmtree, self.run)
        (self.run / 'out/r1').mkdir(parents=True)
        (self.run / 'out/r1/sub.json').write_text('{}')
        (self.run / 'out/r1.json').write_text(json.dumps({'id': 'r1', 'status': 'PASS'}))
        (self.run / 'out/STOP-INPUT').write_text('r0')

    def test_named_rows_are_set_aside_so_a_passed_row_runs_again(self):
        with mock.patch.object(eden_test, 'sh', return_value=0) as sh, mock.patch.object(eden_test, 'cmd_pace'):
            eden_test.cmd_run(self.run, rows='r1,r9')
        argv = sh.call_args[0][0]
        self.assertEqual(argv[argv.index('--rows') + 1], 'r1,r9')
        out = self.run / 'out'
        self.assertFalse((out / 'r1.json').exists())
        self.assertEqual(json.loads((out / 'r1.rerun-1.json').read_text())['status'], 'PASS')
        self.assertTrue((out / 'r1.rerun-1/sub.json').exists())
        self.assertFalse((out / 'STOP-INPUT').exists())  # a named re-run is a retry
        (out / 'r1.json').write_text(json.dumps({'id': 'r1', 'status': 'FAILED'}))
        with mock.patch.object(eden_test, 'sh', return_value=0), mock.patch.object(eden_test, 'cmd_pace'):
            eden_test.cmd_run(self.run, rows='r1')
        self.assertEqual(json.loads((out / 'r1.rerun-2.json').read_text())['status'], 'FAILED')

    def test_a_plain_run_sets_nothing_aside(self):
        with mock.patch.object(eden_test, 'sh', return_value=0) as sh, mock.patch.object(eden_test, 'cmd_pace'):
            eden_test.cmd_run(self.run)
        self.assertNotIn('--rows', sh.call_args[0][0])
        self.assertTrue((self.run / 'out/r1.json').exists())

    def test_down_stops_only_this_runs_bridge_capture(self):
        import subprocess
        import time
        folder = self.run / 'out/bridge-run'
        folder.mkdir()
        script = self.run / 'fake_capture.py'
        script.write_text('import time\ntime.sleep(60)\n')
        mine = subprocess.Popen([sys.executable, str(script), 'capture-bridge', str(folder)])
        other = subprocess.Popen([sys.executable, str(script), 'something-else', str(folder)])
        self.addCleanup(lambda: [p.kill() for p in (mine, other) if p.poll() is None])
        (folder / 'capture.json').write_text(json.dumps({'pid': other.pid}))
        self.assertFalse(eden_test.stop_capture(self.run)['stopped'])  # not a capture of this run: left alone
        self.assertIsNone(other.poll())
        (folder / 'capture.json').write_text(json.dumps({'pid': mine.pid}))
        self.assertTrue(eden_test.stop_capture(self.run)['stopped'])
        mine.wait(timeout=10)
        time.sleep(0.1)
        self.assertFalse(eden_test.stop_capture(self.run)['stopped'])  # already gone

if __name__ == '__main__':
    unittest.main(verbosity=2)
