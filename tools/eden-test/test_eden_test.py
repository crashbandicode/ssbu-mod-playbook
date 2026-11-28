#!/usr/bin/env python3
"""Offline tests for eden_test.py: fake lifecycle/runner/harvest scripts, temp run folders, no Eden."""
import base64
import hashlib
import importlib.util
import inspect
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path, PureWindowsPath
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eden_test  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'eden-runner'))
import test_routes as runner_tests  # noqa: E402
import test_setup_calls as setup_tests  # noqa: E402


class FakeMux:
    def __init__(self, host, **_kwargs):
        self.host = host
        self.rider_args = [*eden_test._lifecycle_module.SSH_OPTIONS, '-o', 'ControlMaster=no',
                           '-o', 'ControlPath=/tmp/fake-eden-ssh/%C', '-o', 'ProxyCommand=/bin/false']

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def ensure(self):
        pass

    def _control(self, operation):
        return subprocess.CompletedProcess(['ssh', '-O', operation], 0, '', '')


def setUpModule():
    mux = mock.patch.object(eden_test, 'SshMux', FakeMux)
    mux.start()
    unittest.addModuleCleanup(mux.stop)

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
if mode in ('dry-run', 'up-check'):
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


class TargetStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.writable_directories)
        self.root = Path(self.tmp.name)
        self.runs = self.root / 'runs'
        self.repo = self.root / 'producer'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'tracked.txt').write_text('old\n')
        self.git('add', 'tracked.txt')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=f@f', 'commit', '-qm', 'parent')
        self.parent = self.git('rev-parse', 'HEAD').stdout.strip()
        (self.repo / 'tracked.txt').write_text('new\n')
        self.git('add', 'tracked.txt')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=f@f', 'commit', '-qm', 'child')
        self.commit = self.git('rev-parse', 'HEAD').stdout.strip()

    def git(self, *args):
        return subprocess.run(['git', '--no-optional-locks', '-C', str(self.repo), *args],
                              check=True, capture_output=True, text=True)

    def writable_directories(self):
        for directory, names, _ in os.walk(self.root, topdown=True, followlinks=False):
            path = Path(directory)
            path.chmod(path.stat().st_mode | 0o700)
            names[:] = [name for name in names if not (path / name).is_symlink()]

    def repository_fingerprint(self):
        paths = []
        for path in sorted((self.repo / '.git').rglob('*')):
            info = path.lstat()
            paths.append((str(path.relative_to(self.repo)), info.st_size, info.st_mtime_ns))
        return (paths, self.git('status', '--porcelain', '--untracked-files=all', '--ignored').stdout)

    def test_pack_is_one_commit_and_never_modifies_producer(self):
        before = self.repository_fingerprint()
        packed = eden_test.pack_target(self.repo, self.commit, self.root / 'packs')
        self.assertGreater(packed['bytes'], 0)
        self.assertEqual(packed['pack_sha256'], sha(packed['pack_file']))
        self.assertEqual(self.repository_fingerprint(), before)
        result = eden_test.stage_target(self.commit, 'run-01', packed['pack_file'], runs=self.runs)
        self.assertEqual(result['cache'], 'built')
        cache = self.runs / '_eden-test' / 'target-cache' / self.commit
        parent = subprocess.run(['git', '-C', str(cache), 'cat-file', '-e', self.parent],
                                capture_output=True)
        self.assertNotEqual(parent.returncode, 0)
        self.assertEqual(self.repository_fingerprint(), before)

    def test_cache_reuse_is_hardlinked_readonly_and_unstage_leaves_cache(self):
        packed = eden_test.pack_target(self.repo, self.commit, self.root / 'packs')
        first = eden_test.stage_target(self.commit, 'run-01', packed['pack_file'], runs=self.runs)
        second = eden_test.stage_target(self.commit, 'run-02', runs=self.runs)
        self.assertEqual((first['cache'], second['cache']), ('built', 'reused'))
        cache = self.runs / '_eden-test' / 'target-cache' / self.commit
        staged = Path(second['staged'])
        self.assertEqual((cache / 'tracked.txt').stat().st_ino, (staged / 'tracked.txt').stat().st_ino)
        for tree in (cache, staged):
            for directory, names, files in os.walk(tree):
                for path in (Path(directory), *(Path(directory) / name for name in [*names, *files])):
                    if not path.is_symlink():
                        self.assertEqual(path.stat().st_mode & 0o222, 0, str(path))
        with self.assertRaises(PermissionError):
            (staged / 'tracked.txt').write_text('not permitted')
        mode = (cache / 'tracked.txt').stat().st_mode
        eden_test.unstage_targets('run-02', runs=self.runs)
        self.assertFalse(staged.exists())
        self.assertTrue(cache.exists())
        self.assertEqual((cache / 'tracked.txt').stat().st_mode, mode)
        self.assertTrue(eden_test.probe_target(self.commit, runs=self.runs))
        self.assertEqual(eden_test.stage_target(self.commit, 'run-01', runs=self.runs)['cache'], 'reused')

    def test_windows_remove_target_files_without_chmod(self):
        root = self.root / 'stage'
        root.mkdir()
        cache = self.root / 'cache'
        cache.write_text('cache contents')
        linked = root / 'linked'
        os.link(cache, linked)
        cache.chmod(0o444)
        (root / 'plain').write_text('other')
        symlink = root / 'link'
        symlink.symlink_to(cache)
        removed = []
        chmod = Path.chmod
        def checked_chmod(path, *args, **kwargs):
            self.assertTrue(path.is_dir(), f'file chmod: {path}')
            return chmod(path, *args, **kwargs)
        def remove(path):
            removed.append(Path(path).name)
            os.unlink(path)
        with mock.patch.object(eden_test.host_os, 'WINDOWS', True), \
                mock.patch.object(eden_test.host_os, 'remove_file', side_effect=remove), \
                mock.patch.object(Path, 'chmod', checked_chmod):
            eden_test._remove_target_tree(root)
        self.assertCountEqual(removed, ['linked', 'plain', 'link'])
        self.assertFalse(root.exists())
        self.assertEqual(cache.read_text(), 'cache contents')
        self.assertEqual(cache.stat().st_mode & 0o222, 0)

    def test_tampered_cache_is_rejected_and_rebuilt(self):
        packed = eden_test.pack_target(self.repo, self.commit, self.root / 'packs')
        eden_test.stage_target(self.commit, 'run-01', packed['pack_file'], runs=self.runs)
        cache_parent = self.runs / '_eden-test' / 'target-cache'
        cache = cache_parent / self.commit
        cache.chmod(cache.stat().st_mode | 0o700)
        (cache / 'unknown.txt').write_text('tampered')
        self.assertFalse(eden_test.probe_target(self.commit, runs=self.runs))
        result = eden_test.stage_target(self.commit, 'run-02', packed['pack_file'], runs=self.runs)
        self.assertEqual(result['cache'], 'built')
        rejected = list(cache_parent.glob('.rejected-' + self.commit + '-*'))
        self.assertEqual(len(rejected), 1)
        self.assertEqual((rejected[0] / 'unknown.txt').read_text(), 'tampered')
        self.assertTrue(eden_test.probe_target(self.commit, runs=self.runs))

    def test_missing_and_wrong_pack_leave_no_cache(self):
        with self.assertRaisesRegex(ValueError, 'no valid cache or pack'):
            eden_test.stage_target(self.commit, 'run-01', runs=self.runs)
        old = eden_test.pack_target(self.repo, self.parent, self.root / 'packs')
        with self.assertRaises((ValueError, subprocess.SubprocessError)):
            eden_test.stage_target(self.commit, 'run-01', old['pack_file'], runs=self.runs)
        cache_parent = self.runs / '_eden-test' / 'target-cache'
        self.assertFalse((cache_parent / self.commit).exists())
        self.assertEqual(list(cache_parent.glob('.tmp-*')), [])

    def test_tampered_cache_without_pack_is_preserved_as_rejected(self):
        packed = eden_test.pack_target(self.repo, self.commit, self.root / 'packs')
        eden_test.stage_target(self.commit, 'run-01', packed['pack_file'], runs=self.runs)
        parent = self.runs / '_eden-test' / 'target-cache'
        cache = parent / self.commit
        cache.chmod(cache.stat().st_mode | 0o700)
        (cache / 'unknown.txt').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'no valid cache or pack'):
            eden_test.stage_target(self.commit, 'run-02', runs=self.runs)
        self.assertFalse(cache.exists())
        self.assertEqual(len(list(parent.glob('.rejected-' + self.commit + '-*'))), 1)

    def test_cache_is_reused_by_a_fresh_process(self):
        packed = eden_test.pack_target(self.repo, self.commit, self.root / 'packs')
        program = ('import json,sys; sys.path.insert(0,sys.argv[1]); import eden_test; '
                   'print(json.dumps(eden_test.stage_target(sys.argv[3],sys.argv[4],'
                   'None if sys.argv[5] == "-" else sys.argv[5],runs=sys.argv[2])))')
        def call(label, pack):
            result = subprocess.run([sys.executable, '-I', '-B', '-c', program,
                                     str(Path(eden_test.__file__).parent), str(self.runs), self.commit, label, pack],
                                    capture_output=True, text=True, check=True, timeout=60)
            return json.loads(result.stdout)
        first = call('run-01', packed['pack_file'])
        second = call('run-02', '-')
        self.assertEqual((first['cache'], second['cache']), ('built', 'reused'))
        self.assertEqual((Path(first['staged']) / 'tracked.txt').stat().st_ino,
                         (Path(second['staged']) / 'tracked.txt').stat().st_ino)

    def test_checkout_does_not_run_inherited_smudge_filters(self):
        (self.repo / '.gitattributes').write_text('tracked.txt filter=refuse\n')
        self.git('add', '.gitattributes')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=f@f', 'commit', '-qm', 'attributes')
        commit = self.git('rev-parse', 'HEAD').stdout.strip()
        packed = eden_test.pack_target(self.repo, commit, self.root / 'packs')
        gitconfig = self.root / 'global.gitconfig'
        gitconfig.write_text('[filter "refuse"]\n    smudge = /bin/false\n    required = true\n')
        with mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(gitconfig)}):
            receipt = eden_test.stage_target(commit, 'run-01', packed['pack_file'], runs=self.runs)
        self.assertEqual((Path(receipt['staged']) / 'tracked.txt').read_text(), 'new\n')

    def test_changed_existing_staged_tree_is_never_replaced(self):
        packed = eden_test.pack_target(self.repo, self.commit, self.root / 'packs')
        result = eden_test.stage_target(self.commit, 'run-01', packed['pack_file'], runs=self.runs)
        staged = Path(result['staged'])
        staged.chmod(staged.stat().st_mode | 0o700)
        (staged / 'extra.txt').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'staged tree .* changed after staging'):
            eden_test.stage_target(self.commit, 'run-01', packed['pack_file'], runs=self.runs)
        self.assertEqual((staged / 'extra.txt').read_text(), 'tampered')

    def test_stage_and_unstage_cli_use_injected_runs(self):
        packed = eden_test.pack_target(self.repo, self.commit, self.root / 'packs')
        output = io.StringIO()
        with mock.patch.object(eden_test, 'RUNS', self.runs), redirect_stdout(output):
            self.assertEqual(eden_test.main(['stage-target', '--commit', self.commit, '--label', 'run-01',
                                             '--probe']), 0)
            self.assertEqual(output.getvalue(), json.dumps({'commit': self.commit, 'cached': False}) + '\n')
            output.seek(0)
            output.truncate()
            self.assertEqual(eden_test.main(['stage-target', '--commit', self.commit, '--label', 'run-01',
                                             '--pack', packed['pack_file']]), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt['cache'], 'built')
            output.seek(0)
            output.truncate()
            self.assertEqual(eden_test.main(['stage-target', '--commit', self.commit, '--label', 'run-01',
                                             '--probe']), 0)
            self.assertEqual(output.getvalue(), json.dumps({'commit': self.commit, 'cached': True}) + '\n')
            self.assertEqual(eden_test.main(['unstage-targets', '--label', 'run-01']), 0)
        self.assertFalse(Path(receipt['staged']).exists())
        self.assertTrue((self.runs / '_eden-test' / 'target-cache' / self.commit).exists())

    def test_stage_and_unstage_refuse_symlinked_run_directory(self):
        foreign = self.root / 'foreign'
        (foreign / 'targets').mkdir(parents=True)
        keep = foreign / 'targets' / 'keep.txt'
        keep.write_text('not ours')
        stage = self.runs / '_eden-test' / 'stage'
        stage.mkdir(parents=True)
        (stage / 'run-01').symlink_to(foreign, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink in target directory'):
            eden_test.unstage_targets('run-01', runs=self.runs)
        with self.assertRaisesRegex(ValueError, 'symlink in target directory'):
            eden_test.stage_target(self.commit, 'run-01', runs=self.runs)
        self.assertEqual(keep.read_text(), 'not ours')

    def test_probe_refuses_an_incoming_pack_symlink(self):
        foreign = self.root / 'foreign.pack'
        foreign.write_text('not ours')
        incoming = self.runs / '_eden-test' / 'stage' / 'run-01' / 'targets' / '.incoming'
        incoming.mkdir(parents=True)
        pack = incoming / (self.commit + '.pack')
        pack.symlink_to(foreign)
        with mock.patch.object(eden_test, 'RUNS', self.runs):
            with self.assertRaisesRegex(SystemExit, 'symlink in target pack'):
                eden_test.main(['stage-target', '--commit', self.commit, '--label', 'run-01', '--probe'])
        with self.assertRaisesRegex(ValueError, 'symlink in target pack'):
            eden_test.stage_target(self.commit, 'run-01', pack, runs=self.runs)
        self.assertEqual(foreign.read_text(), 'not ours')


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
        self.assertIn('up-check', sh.call_args[0][0])
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
    def test_windows_interfaces_and_refusals(self):
        interfaces = [
            {'name': 'Loopback Pseudo-Interface 1', 'up': True, 'loopback': True, 'ipv4': True},
            {'name': 'Ethernet down', 'up': False, 'loopback': False, 'ipv4': True},
            {'name': 'No IPv4', 'up': True, 'loopback': False, 'ipv4': False},
            {'name': 'Very long Wi-Fi interface name', 'up': True, 'loopback': False, 'ipv4': True},
            {'name': 'Ethernet', 'up': True, 'loopback': False, 'ipv4': True},
        ]
        with mock.patch.object(eden_test.host_os, 'WINDOWS', True), \
                mock.patch.object(eden_test.host_os, 'windows_interfaces', return_value=interfaces) as query:
            for name, expected in ((None, 'no such interface'), ('', 'no such interface'),
                                   ('missing', 'no such interface'),
                                   ('Loopback Pseudo-Interface 1', 'is loopback'),
                                   ('Ethernet down', 'is down'), ('No IPv4', 'has no IPv4 address'),
                                   ('Very long Wi-Fi interface name', None)):
                with self.subTest(name=name):
                    self.assertEqual(eden_test.host_interface_problem(name), expected)
            self.assertEqual(eden_test.usable_interfaces(), ['Ethernet', 'Very long Wi-Fi interface name'])
            query.side_effect = OSError('adapter query failed')
            with self.assertRaisesRegex(OSError, 'adapter query failed'):
                eden_test.host_interface_problem('Ethernet')
            with self.assertRaisesRegex(OSError, 'adapter query failed'):
                eden_test.usable_interfaces()

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
        shared = eden_test.RUNS / '_eden-test/base.json'  # these fixtures run as the yoga host
        shared.parent.mkdir(parents=True)
        shared.write_text(self.previous.read_text())
        interface_check = mock.patch.object(eden_test, 'host_interface_problem', return_value=None, create=True)
        self.interface_check = interface_check.start()
        self.addCleanup(interface_check.stop)
        # Down's cleanup reads the ledger and this machine's processes; these fixtures have neither.
        self.open_claims = []
        claims = mock.patch.object(eden_test, '_open_claims', side_effect=lambda run, config: list(self.open_claims), create=True)
        claims.start()
        self.addCleanup(claims.stop)
        self.process_reason = None
        processes = mock.patch.object(eden_test, '_process_reason', side_effect=lambda paths, proc=None: self.process_reason, create=True)
        processes.start()
        self.addCleanup(processes.stop)

    def tearDown(self):
        eden_test.LIFECYCLE, eden_test.RUNNER, eden_test.HARVEST, eden_test.RUNS = self.saved
        subprocess.run(['chmod', '-R', 'u+rwx', str(self.dir)], check=False)
        shutil.rmtree(self.dir)

    def pruning_lifecycle(self, fail_prune=False):
        """A lifecycle whose Up makes the run's copies and whose Down removes them unless told --keep-state."""
        source = FAKE_LIFECYCLE.replace(
            "    (state / 'instances.json')",
            "    work = Path(config['work_root']) / 'profiles' / 'profile-a'; work.mkdir(parents=True, exist_ok=True)\n"
            "    (work / 'save.bin').write_text('profile'); (state / 'snapshot-a').mkdir(exist_ok=True)\n"
            "    (state / 'snapshot-a' / 'f').write_text('snapshot')\n"
            "    (state / 'instances.json')")
        source = source.replace(
            "    elif '--evidence-dir' in sys.argv:\n",
            "    elif '--evidence-dir' in sys.argv:\n"
            "        import shutil\n"
            + ("        (state / 'lifecycle.json').write_text(json.dumps(j)); sys.exit(7)\n" if fail_prune else '')
            + "        shutil.rmtree(config['work_root'], ignore_errors=True); shutil.rmtree(state / 'snapshot-a', ignore_errors=True)\n")
        self.assertIn('snapshot-a', source)
        self.assertIn('shutil.rmtree', source)
        eden_test.LIFECYCLE.write_text(source)

    def up_run(self, label='lr-14'):
        eden_test.main(['new', '--from', str(self.previous), '--label', label, '--rows', str(self.rows)])
        run = self.dir / 'runs' / label
        eden_test.main(['up', str(run)])
        return run

    def cleanup(self, run):
        return json.loads((run / 'out' / 'down-cleanup.json').read_text())

    def args(self, *extra):
        return ['--from', str(self.previous), '--label', 'lr-14', '--rows', str(self.rows), *extra]

    def go_args(self, label='go-1', rows=None, nro=None, host='yoga', owner='Claude devin-test/go-1',
                lineup=False):
        return ['go', '--host', host, '--label', label, '--nro', str(nro or self.dir / 'plugin.nro'),
                '--rows', str(rows or self.rows), *(['--owner', owner] if owner is not None else []),
                *([] if lineup else ['--no-card-lineup', 'test without the card lineup']),
                *(['--from', str(self.previous)] if host == 'yoga' else [])]

    def remote_args(self):
        """Arguments of a go that is sent to another host: no local base."""
        args = self.go_new_args()
        args.from_config = None
        return args

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

    def test_go_eden_settings_and_invalid_inputs(self):
        result, _, _ = self.run_go_without_up(self.go_args() +
                                               ['--eden-setting', 'core.resolution_setup=3',
                                                '--eden-setting', 'core.volume=-1'])
        self.assertEqual(result, 0)
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertEqual(config['eden_settings']['core.resolution_setup'], 3)
        self.assertEqual(config['eden_settings']['core.volume'], -1)
        for value in ('bad=3', 'core.volume=x', 'core.volume=1.5'):
            with mock.patch.object(eden_test, 'go_new') as new, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    eden_test.main(self.go_args(label='invalid') + ['--eden-setting', value])
                new.assert_not_called()

    def test_go_prepare_runs_checks_without_up(self):
        for label, problems, dry, preflight, expected in (
                ('prepared', [], 0, 0, 0), ('bad-check', ['bad pin'], 0, 0, 1),
                ('bad-dry', [], 1, 0, 1), ('bad-preflight', [], 0, 1, 1)):
            output = io.StringIO()
            with mock.patch.object(eden_test, 'check_run', return_value=problems) as check, \
                    mock.patch.object(eden_test, 'sh', return_value=dry) as sh, \
                    mock.patch.object(eden_test, 'lifecycle', return_value=preflight) as life, \
                    mock.patch.object(eden_test, 'cmd_up') as up, redirect_stdout(output):
                self.assertEqual(eden_test.main(self.go_args(label=label) + ['--prepare']), expected)
            run = eden_test.RUNS / label
            check.assert_called_once_with(run, skip_setup_plan=True, allow_volatile_pins=True)
            self.assertIn('up-check', sh.call_args.args[0])
            life.assert_called_once_with(run, 'preflight')
            up.assert_not_called()
            self.assertIn(f'Prepared: {run}', output.getvalue())

    def test_go_records_sorted_unique_staged_targets_in_run_config(self):
        first, second = 'a' * 40, 'b' * 40
        args = self.go_args() + ['--staged-target', second, '--staged-target', first,
                                 '--staged-target', second]
        result, _, _ = self.run_go_without_up(args)
        self.assertEqual(result, 0)
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertEqual(config['staged_targets'], [first, second])

    def test_local_go_without_staged_target_clears_inherited_requirement(self):
        previous = json.loads(self.previous.read_text())
        previous['staged_targets'] = ['a' * 40]
        self.previous.write_text(json.dumps(previous))
        result, _, _ = self.run_go_without_up(self.go_args())
        self.assertEqual(result, 0)
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertNotIn('staged_targets', config)

    def test_invalid_staged_target_is_refused_by_go_cli(self):
        for value in ('a' * 39, 'A' * 40, 'g' * 40):
            with self.subTest(value=value), redirect_stderr(io.StringIO()), \
                    mock.patch.object(eden_test, 'cmd_up') as up:
                with self.assertRaises(SystemExit) as caught:
                    eden_test.main(self.go_args() + ['--staged-target', value])
                self.assertEqual(caught.exception.code, 2)
                up.assert_not_called()
                self.assertFalse((eden_test.RUNS / 'go-1').exists())

    def test_new_from_config_copies_staged_target_requirement(self):
        previous = json.loads(self.previous.read_text())
        previous['staged_targets'] = ['a' * 40]
        self.previous.write_text(json.dumps(previous))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(eden_test.main(['new', *self.args()]), 0)
        config = json.loads((eden_test.RUNS / 'lr-14/config.json').read_text())
        self.assertEqual(config['staged_targets'], ['a' * 40])

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
            if command[0] == 'scp' and command[-2].endswith('/go-result.json'):
                Path(command[-1]).write_text(json.dumps(
                    {'token': token, 'code': 0, 'counts': {'PASS': 0, 'FAIL': 0, 'NOT RUN': 0}}))
            return 0
        with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex=token)), \
             mock.patch.object(eden_test, 'sh', side_effect=fake_sh) as sh:
            self.assertEqual(eden_test.main(self.go_args(host='xps', lineup=True) + ['--ssh', 'xps-host']), 0)
        commands = [call.args[0] for call in sh.call_args_list]
        self.assertEqual(commands[1][-2:], [str(folder.resolve()) + '/', f'xps-host:{stage}/card-lineup/'])
        self.assertIn('ProxyCommand=/bin/false', commands[1][2])
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

    def test_go_defers_display_allocation_until_lifecycle_claim(self):
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
        self.assertEqual([item['display'] for item in config['instances']], [':60', ':60'])
        self.assertEqual(config['display_choice'], [
            {'key': 'a', 'base': ':60', 'chosen': ':60', 'reason': 'allocated under the host claim lock by eden-lifecycle'},
            {'key': 'b', 'base': ':60', 'chosen': ':60', 'reason': 'allocated under the host claim lock by eden-lifecycle'}])

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
            {'key': 'a', 'base': ':200', 'chosen': ':200', 'reason': 'allocated under the host claim lock by eden-lifecycle'}])

    def test_go_does_not_add_display_to_an_instance_without_one(self):
        with mock.patch.object(eden_test, 'cmd_up'), mock.patch.object(eden_test, 'cmd_run', return_value=0):
            self.assertEqual(eden_test.main(self.go_args()), 0)
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertNotIn('display', config['instances'][0])
        self.assertEqual(config['display_choice'], [])

    def test_go_defers_display_conflict_resolution_until_lifecycle_claim(self):
        base = json.loads(self.previous.read_text())
        base['display_helper'] = {'path': '/helper', 'sha256': '0' * 64}
        base['instances'][0]['display'] = ':200'
        self.previous.write_text(json.dumps(base))
        sockets = self.dir / 'sockets'
        sockets.mkdir()
        for number in range(200, 240):
            (sockets / f'X{number}').touch()
        with mock.patch.object(eden_test, 'X11_SOCKET_DIR', sockets, create=True), \
             mock.patch.object(eden_test, 'cmd_up') as up, \
             mock.patch.object(eden_test, 'cmd_run', return_value=0):
            self.assertEqual(eden_test.main(self.go_args()), 0)
        up.assert_called_once()
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertEqual(config['instances'][0]['display'], ':200')

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
        self.assertEqual((run / 'inputs/port.txt').read_text(), '0\n')
        pin = config['instances'][0]['files'][0]
        self.assertEqual((pin['path'], pin['sha256']), (str(run / 'inputs/port.txt'), sha(run / 'inputs/port.txt')))
        self.assertEqual(config['bridge_port'], 'allocated atomically at lifecycle claim')
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
        shared = eden_test.RUNS / '_eden-test/base.json'  # a go with a token was sent to this host: it has the base
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text(self.previous.read_text())
        with mock.patch.object(eden_test, 'cmd_up'), mock.patch.object(eden_test, 'cmd_run', return_value=0):
            self.assertEqual(eden_test.main(self.go_args()[:-2] + ['--token', token]), 0)
        result = json.loads((self.dir / 'runs/go-1/go-result.json').read_text())
        self.assertEqual(result['token'], token)

    def test_go_default_base_updates_every_instances_plugin_and_port(self):
        base = json.loads(self.previous.read_text())
        notify = {'file': '/home/intpa/eden-run-notify/NOTIFY.md', 'to': 'tr-primary-2'}
        base['notify'] = notify
        port = self.dir / 'base-port.txt'
        port.write_text('6979\n')
        base['instances'][0]['files'] = [{'path': str(port), 'sha256': sha(port),
                                        'to': 'user/sdmc/ultimate/ssbu-hw-bridge/port.txt'}]
        second = json.loads(json.dumps(base['instances'][0]))
        second['key'] = 'b'
        base['instances'].append(second)
        default = eden_test.RUNS / '_eden-test/base.json'
        default.parent.mkdir(parents=True, exist_ok=True)
        default.write_text(json.dumps(base))
        free = mock.Mock(return_value=6991)
        fake_routes = types.SimpleNamespace(free_port=free)
        argv = self.go_args()[:-2]
        with mock.patch.dict(sys.modules, {'routes': fake_routes}), \
             mock.patch.object(eden_test, 'cmd_up') as up, \
             mock.patch.object(eden_test, 'cmd_run', return_value=0):
            self.assertEqual(eden_test.main(argv), 0)
        up.assert_called_once_with(self.dir / 'runs/go-1', skip_setup_plan=True, allow_volatile_pins=True)
        free.assert_not_called()
        config = json.loads((self.dir / 'runs/go-1/config.json').read_text())
        self.assertEqual(config['notify'], notify)
        self.assertEqual(len(config['plugin_swaps']), 2)
        for item in config['instances']:
            self.assertEqual(item['plugins'][0]['path'], str(self.dir / 'runs/go-1/inputs/plugin.nro'))
            self.assertEqual(item['files'][0]['path'], str(self.dir / 'runs/go-1/inputs/port.txt'))
        self.assertEqual((self.dir / 'runs/go-1/inputs/port.txt').read_text(), '0\n')
        self.assertEqual(config['bridge_port'], 'allocated atomically at lifecycle claim')

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
        self.assertFalse(any(c.args[0][0] == 'scp' and c.args[0][-2].endswith('/REPORT.md')
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

    HOST_STAGE = '/home/intpa/eden-runs/_eden-test/stage/go-1'

    def remote_go(self, *extra):
        """A go for xps sent over a fake transport: (its exit code or SystemExit, the commands it ran)."""
        token = 'e' * 32

        def fake_sh(command, log=None):
            if command[0] == 'scp' and command[-2].endswith('/go-result.json'):
                Path(command[-1]).write_text(json.dumps(
                    {'token': token, 'code': 0, 'counts': {'PASS': 0, 'FAIL': 0, 'NOT RUN': 0}}))
            return 0

        with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex=token)), \
             mock.patch.object(eden_test, 'sh', side_effect=fake_sh) as sh, redirect_stdout(io.StringIO()):
            try:
                result = eden_test.main(self.go_args(host='xps') + ['--ssh', 'xps-host', *extra])
            except SystemExit as stop:
                result = stop
        return result, [call.args[0] for call in sh.call_args_list]

    def test_remote_go_forwards_from_as_a_path_on_the_host(self):
        base = f'{self.HOST_STAGE}/base.stub-on.json'
        result, commands = self.remote_go('--from', base)
        self.assertEqual(result, 0)
        go = next(c for c in commands if c[0] == 'ssh')
        self.assertEqual(go.count('--from'), 1)
        self.assertEqual(go[go.index('--from') + 1], base)
        self.assertEqual(go[go.index('--label') + 1], 'go-1')
        copies = [c for c in commands if c[0] in ('rsync', 'scp')]
        self.assertFalse([c for c in copies if any('base.stub-on.json' in str(part) for part in c)],
                         'the base is a file on the host; nothing is copied for it')

    def test_remote_go_without_from_sends_no_base(self):
        result, commands = self.remote_go()
        self.assertEqual(result, 0)
        go = next(c for c in commands if c[0] == 'ssh')
        self.assertNotIn('--from', go)

    def test_remote_go_refuses_a_from_outside_this_runs_stage_folder_before_any_transport(self):
        stage = self.HOST_STAGE
        for value in ('/tmp/base.json', str(self.previous), 'base.stub-on.json',
                      f'{stage}/../go-2/base.json', '/home/intpa/eden-runs/_eden-test/stage/other-run/base.json',
                      '/home/intpa/eden-runs/_eden-test/base.json', f'{stage}/sub/base.json', f'{stage}/base.txt',
                      f'{stage}/.hidden.json', f"{stage}/it's.json", f'{stage}/'):
            with self.subTest(value=value):
                result, commands = self.remote_go('--from', value)
                self.assertIsInstance(result, SystemExit)
                self.assertIn('--from', str(result))
                self.assertIn(f'{stage}/', str(result))
                self.assertEqual(commands, [])

    def test_a_local_copy_that_names_the_host_does_not_make_this_machine_the_host(self):
        copy = self.dir / 'copy-of-the-xps-base.json'
        base = json.loads(self.previous.read_text())
        base['host'] = 'xps'
        copy.write_text(json.dumps(base))
        hosts = eden_test.RUNS / '_eden-test/hosts.json'
        for route in ('--ssh', 'hosts.json'):
            if route == 'hosts.json':
                hosts.parent.mkdir(parents=True, exist_ok=True)
                hosts.write_text(json.dumps({'xps': 'mapped-xps'}))
            argv = self.go_args(host='xps') + ['--from', str(copy)] + (['--ssh', 'xps-host'] if route == '--ssh' else [])
            with self.subTest(route=route), \
                 mock.patch.object(eden_test, 'go_new', side_effect=SystemExit('go_new called')) as new, \
                 mock.patch.object(eden_test, 'cmd_up') as up, mock.patch.object(eden_test, 'sh') as sh:
                with self.assertRaises(SystemExit) as caught:
                    eden_test.main(argv)
                new.assert_not_called()
                up.assert_not_called()
                sh.assert_not_called()
                self.assertIn('on the host', str(caught.exception))

    def host_bases(self, label='go-1'):
        """Make this machine the yoga host: a shared base that is a trace base, and in the run's own stage folder
        the same base without its trace section. Returns (shared, owned)."""
        shared = eden_test.RUNS / '_eden-test/base.json'
        base = json.loads(self.previous.read_text())
        base['instances'][0].update(source=str(self.dir), trace={'unmapped_access': 'log'})
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text(json.dumps(base))
        del base['instances'][0]['trace']
        owned = shared.parent / 'stage' / label / 'base.stub-on.json'
        owned.parent.mkdir(parents=True)
        owned.write_text(json.dumps(base))
        return shared, owned

    def received(self, *extra, label='go-1'):
        """A go as the host receives it (it carries the caller's token): (result or SystemExit, output, mocks)."""
        argv = self.go_args(label=label)[:-2] + ['--token', 'f' * 32, *extra]
        output = io.StringIO()
        with mock.patch.object(eden_test, '_go_remote', side_effect=SystemExit('sent on to another host')) as remote, \
             mock.patch.object(eden_test, 'sh') as sh, mock.patch.object(eden_test, 'cmd_up') as up, \
             mock.patch.object(eden_test, 'cmd_run', return_value=0), redirect_stdout(output):
            try:
                result = eden_test.main(argv)
            except SystemExit as stop:
                result = stop
        return result, output.getvalue(), types.SimpleNamespace(remote=remote, sh=sh, up=up)

    def assert_refused_here(self, result, mocks, *words, label='go-1'):
        self.assertIsInstance(result, SystemExit)
        for word in words:
            self.assertIn(word, str(result))
        mocks.remote.assert_not_called()
        mocks.sh.assert_not_called()
        mocks.up.assert_not_called()
        self.assertFalse((eden_test.RUNS / label).exists())

    def test_received_go_uses_the_owned_base_and_leaves_the_shared_base_alone(self):
        shared, owned = self.host_bases()
        before = shared.read_bytes()
        result, _, mocks = self.received()
        self.assertEqual(str(result), TRACE_REQUIRED)  # the shared base is a trace base: no ELF, no go
        self.assertFalse((eden_test.RUNS / 'go-1').exists())
        result, _, mocks = self.received('--from', str(owned))
        self.assertEqual(result, 0)
        mocks.remote.assert_not_called()
        mocks.up.assert_called_once()
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertFalse(any('trace' in item for item in config['instances']))
        self.assertTrue(config['stub_rule'].startswith('no trace section'))
        self.assertEqual(config['instances'][0]['app'], json.loads(before)['instances'][0]['app'])
        self.assertEqual(json.loads((eden_test.RUNS / 'go-1/go-result.json').read_text())['token'], 'f' * 32)
        self.assertEqual(shared.read_bytes(), before)

    def test_received_go_refuses_a_mistyped_base_here_and_sends_nothing_on(self):
        _, owned = self.host_bases()
        missing = owned.with_name('base.stub-off.json')
        result, _, mocks = self.received('--from', str(missing))
        self.assert_refused_here(result, mocks, str(missing))

    def test_received_go_refuses_a_base_for_another_host(self):
        _, owned = self.host_bases()
        base = json.loads(owned.read_text())
        base['host'] = 'xps'
        owned.write_text(json.dumps(base))
        result, _, mocks = self.received('--from', str(owned))
        self.assert_refused_here(result, mocks, str(owned), "'xps'")

    def test_received_go_refuses_a_base_outside_its_own_stage_folder(self):
        shared, owned = self.host_bases()
        other = shared.parent / 'stage' / 'other-run' / 'base.stub-on.json'
        nested = owned.parent / 'sub' / 'base.stub-on.json'
        for path in (other, nested):
            path.parent.mkdir(parents=True)
            path.write_text(owned.read_text())
        loose = self.dir / 'loose-base.json'
        loose.write_text(owned.read_text())
        for index, path in enumerate((other, nested, loose, f'{owned.parent}/../go-1/base.stub-on.json')):
            label = 'go-1'
            with self.subTest(path=str(path)):
                result, _, mocks = self.received('--from', str(path), label=label)
                self.assert_refused_here(result, mocks, '--from', str(owned.parent), label=label)

    def test_received_go_refuses_a_symlink_as_its_base(self):
        _, owned = self.host_bases()
        real = self.dir / 'real-base.json'
        real.write_text(owned.read_text())
        owned.unlink()
        owned.symlink_to(real)
        result, _, mocks = self.received('--from', str(owned))
        self.assert_refused_here(result, mocks, str(owned))
        owned.unlink()
        folder = owned.parent
        folder.rmdir()
        elsewhere = self.dir / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'base.stub-on.json').write_text(real.read_text())
        folder.symlink_to(elsewhere)
        result, _, mocks = self.received('--from', str(owned))
        self.assert_refused_here(result, mocks, 'symlink')

    def test_received_go_refuses_a_base_that_pins_another_eden(self):
        shared, owned = self.host_bases()
        other = self.dir / 'Other.AppImage'
        other.write_bytes(b'another eden')
        base = json.loads(owned.read_text())
        base['instances'][0]['app'] = {'path': str(other), 'sha256': sha(other)}
        owned.write_text(json.dumps(base))
        result, _, mocks = self.received('--from', str(owned))
        self.assert_refused_here(result, mocks, 'AppImage', sha(other)[:12], str(shared))

    def test_received_go_without_this_hosts_base_is_refused_here(self):
        (eden_test.RUNS / '_eden-test/base.json').unlink()
        result, _, mocks = self.received()
        self.assert_refused_here(result, mocks, 'base.json', 'missing')
        shared, _ = self.host_bases()
        base = json.loads(shared.read_text())
        base['host'] = 'xps'
        shared.write_text(json.dumps(base))
        result, _, mocks = self.received()
        self.assert_refused_here(result, mocks, 'base.json', "'xps'")

    def test_a_go_typed_on_the_host_keeps_taking_any_local_base(self):
        shared, owned = self.host_bases()
        loose = self.dir / 'loose-base.json'
        loose.write_text(owned.read_text())
        argv = self.go_args()[:-2] + ['--from', str(loose)]
        with mock.patch.object(eden_test, '_go_remote', side_effect=SystemExit('sent on to another host')) as remote:
            result, _, up = self.run_go_without_up(argv)
        self.assertEqual(result, 0)
        remote.assert_not_called()
        up.assert_called_once()
        config = json.loads((eden_test.RUNS / 'go-1/config.json').read_text())
        self.assertFalse(any('trace' in item for item in config['instances']))

    def shared_base(self, host):
        """Give this machine a shared base that says which host it is."""
        shared = eden_test.RUNS / '_eden-test/base.json'
        base = json.loads(self.previous.read_text())
        base['host'] = host
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text(json.dumps(base))
        return shared

    def typed_go(self, argv):
        """A go typed on this machine: (result or SystemExit, mocks). Nothing real is started."""
        with mock.patch.object(eden_test, 'go_new', side_effect=SystemExit('go_new called')) as new, \
             mock.patch.object(eden_test, '_go_remote', side_effect=SystemExit('sent to the host')) as remote, \
             mock.patch.object(eden_test, 'cmd_up') as up, mock.patch.object(eden_test, 'sh') as sh, \
             redirect_stdout(io.StringIO()):
            try:
                result = eden_test.main(argv)
            except SystemExit as stop:
                result = stop
        return result, types.SimpleNamespace(new=new, remote=remote, up=up, sh=sh)

    def test_a_copy_naming_another_host_never_starts_a_go_on_a_machine_whose_base_says_otherwise(self):
        # rv-fusion-g4, 2026-11-24 08:01Z: this machine's shared base says surface; the --from copy says yoga.
        shared = self.shared_base('surface')
        hosts = eden_test.RUNS / '_eden-test/hosts.json'
        for name, write in (('missing', None), ('malformed', '{"yoga": '), ('not an object', '["yoga"]'),
                            ('without this host', '{"xps": "xps-host"}'), ('entry not a name', '{"yoga": 7}')):
            hosts.unlink(missing_ok=True)
            if write is not None:
                hosts.write_text(write)
            for extra in ([], ['--from', str(self.previous)]):
                with self.subTest(hosts_json=name, extra=extra):
                    result, mocks = self.typed_go(self.go_args()[:-2] + extra)
                    self.assertIsInstance(result, SystemExit)
                    mocks.new.assert_not_called()
                    mocks.remote.assert_not_called()
                    mocks.up.assert_not_called()
                    mocks.sh.assert_not_called()
                    text = str(result)
                    self.assertIn("'yoga'", text)
                    self.assertIn('nothing was started', text)
                    if name in ('missing', 'without this host'):
                        self.assertIn(str(shared), text)
                        self.assertIn("'surface'", text)
                    else:
                        self.assertIn(str(hosts), text)

    def test_a_route_to_the_host_never_opens_a_local_from(self):
        self.shared_base('surface')
        local = self.dir / 'broken-local-copy.json'
        local.write_text('{"host": "yoga", ')
        hosts = eden_test.RUNS / '_eden-test/hosts.json'
        for route in ('--ssh', 'hosts.json'):
            hosts.unlink(missing_ok=True)
            if route == 'hosts.json':
                hosts.write_text(json.dumps({'yoga': 'tr-yoga'}))
            argv = self.go_args()[:-2] + ['--from', str(local)] + (['--ssh', 'tr-yoga'] if route == '--ssh' else [])
            with self.subTest(route=route):
                result, mocks = self.typed_go(argv)
                self.assertEqual(str(result), 'sent to the host')  # the remote path then checks --from as a host path
                mocks.remote.assert_called_once()
                mocks.new.assert_not_called()
        hosts.unlink(missing_ok=True)
        result, commands = self.remote_go('--from', f'{self.HOST_STAGE}/base.stub-on.json')
        self.assertEqual(result, 0)
        result, commands = self.remote_go('--from', str(local))
        self.assertIsInstance(result, SystemExit)
        self.assertIn('on the host', str(result))
        self.assertEqual(commands, [])

    def test_the_host_itself_takes_any_local_from_also_with_an_ssh_name(self):
        self.shared_base('yoga')
        loose = self.dir / 'loose-base.json'
        loose.write_text(self.previous.read_text())
        hosts = eden_test.RUNS / '_eden-test/hosts.json'
        hosts.write_text('{"yoga": ')  # the host does not need the map to run itself
        for extra in ([], ['--ssh', 'tr-yoga']):
            with self.subTest(extra=extra):
                result, mocks = self.typed_go(self.go_args()[:-2] + ['--from', str(loose), *extra])
                self.assertEqual(str(result), 'go_new called')
                mocks.remote.assert_not_called()
                result, mocks = self.typed_go(self.go_args()[:-2] + extra)
                self.assertEqual(str(result), 'go_new called')
                mocks.remote.assert_not_called()

    def test_an_unreadable_shared_base_stops_a_typed_go_by_name(self):
        shared = eden_test.RUNS / '_eden-test/base.json'
        for body in ('{"host": ', '{"instances": []}', '[]'):
            shared.write_text(body)
            for extra in ([], ['--from', str(self.previous)], ['--ssh', 'tr-yoga']):
                with self.subTest(body=body, extra=extra):
                    result, mocks = self.typed_go(self.go_args()[:-2] + extra)
                    self.assertIsInstance(result, SystemExit)
                    self.assertIn(str(shared), str(result))
                    self.assertIn('nothing was started', str(result))
                    mocks.new.assert_not_called()
                    mocks.remote.assert_not_called()

    def test_a_machine_without_a_shared_base_is_not_a_host_whatever_the_go_names(self):
        # rv-fusion-g4, 2026-11-24 08:09Z: no shared base and no route; the copy names yoga. The Surface is such a
        # machine. A file the go names is no evidence of which machine this is.
        shared = eden_test.RUNS / '_eden-test/base.json'
        shared.unlink()
        broken = self.dir / 'broken.json'
        broken.write_text('{"host": ')
        for extra in ([], ['--from', str(self.previous)], ['--from', str(broken)],
                      ['--from', str(self.dir / 'no-such-file.json')]):
            with self.subTest(extra=extra):
                result, mocks = self.typed_go(self.go_args()[:-2] + extra)
                self.assertIsInstance(result, SystemExit)
                mocks.new.assert_not_called()
                mocks.remote.assert_not_called()
                mocks.up.assert_not_called()
                mocks.sh.assert_not_called()
                text = str(result)
                self.assertIn(str(shared), text)
                self.assertIn("'yoga'", text)
                self.assertIn('--ssh', text)
                self.assertIn('nothing was started', text)
        self.assertFalse((eden_test.RUNS / 'go-1').exists())
        result, mocks = self.typed_go(self.go_args()[:-2] + ['--from', str(broken), '--ssh', 'tr-yoga'])
        self.assertEqual(str(result), 'sent to the host')  # with a route the go is sent; the file is not opened
        mocks.new.assert_not_called()
        shared.write_text(self.previous.read_text())
        result, mocks = self.typed_go(self.go_args())
        self.assertEqual(str(result), 'go_new called')  # the host itself, with its shared base back
        mocks.remote.assert_not_called()

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
                if command[-2].endswith('/go-result.json'):
                    Path(command[-1]).write_text(json.dumps(
                        {'token': token, 'code': 3, 'counts': {'PASS': 3, 'FAIL': 1, 'NOT RUN': 16}}))
                else:
                    Path(command[-1]).write_text('report')
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
        commands = [call.args[0] for call in sh.call_args_list]
        normalized = []
        for command in commands:
            self.assertIn('ProxyCommand=/bin/false', ' '.join(command))
            if command[0] == 'rsync':
                normalized.append([command[0], *command[3:]])
            elif command[0] == 'scp':
                normalized.append([command[0], *command[-2:]])
            else:
                normalized.append([command[0], '-o', 'BatchMode=yes', *command[command.index(host):]])
        self.assertEqual(normalized, expected)
        self.interface_check.assert_not_called()
        temp = Path(sh.call_args_list[-2].args[0][-1])
        self.assertTrue(temp.is_absolute())
        self.assertFalse(temp.exists())
        self.assertIn(f'REPORT-{label}.md', output.getvalue())
        self.assertIn(f'run /home/intpa/eden-runs/{label} --rows <id>', output.getvalue())
        self.assertIn(f'down /home/intpa/eden-runs/{label}', output.getvalue())

    def test_go_remote_ships_named_targets_before_go_and_reuses_cache(self):
        repo = self.dir / 'source'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)
        git('init', '-q')
        (repo / 'source.txt').write_text('plugin')
        git('add', 'source.txt')
        git('-c', 'user.name=fixture', '-c', 'user.email=f@f', 'commit', '-qm', 'plugin')
        plugin = git('rev-parse', 'HEAD').stdout.strip()
        (repo / 'source.txt').write_text('bridge')
        git('add', 'source.txt')
        git('-c', 'user.name=fixture', '-c', 'user.email=f@f', 'commit', '-qm', 'bridge')
        bridge = git('rev-parse', 'HEAD').stdout.strip()
        plan = {'target': {'plugin': {'tree': str(repo), 'commit': plugin, 'nro_sha256': 'a' * 64},
                           'bridge': {'tree': str(repo), 'commit': bridge},
                           'repos': {'same': {'tree': str(repo), 'commit': plugin}}}, 'rows': []}
        rowfile = self.rows / 'rows.json'
        rowfile.write_text(json.dumps(plan))
        original_rows = rowfile.read_bytes()
        events = []
        pack_files = {}
        original_run = subprocess.run
        original_pack = eden_test.pack_target
        class RecordingMux(FakeMux):
            def ensure(self):
                events.append(('ensure', None))
        def pack(tree, commit, directory):
            events.append(('pack', commit))
            packed = original_pack(tree, commit, directory)
            pack_files[commit] = Path(packed['pack_file'])
            return packed
        def run(command, **kwargs):
            if command[0] == 'rsync' and command[-1].endswith('.pack'):
                self.assertIn('ProxyCommand=/bin/false', ' '.join(command))
                self.assertIn('--partial', command)
                self.assertIn('--stats', command)
                self.assertEqual(events[-1][0], 'ensure')
                self.assertEqual(events[-2], ('pack', plugin))
                self.assertTrue(pack_files[plugin].is_file())
                events.append(('pack-rsync', command[-1]))
                return subprocess.CompletedProcess(command, 0, 'Total bytes sent: 1,234\n', '')
            if command[0] != 'ssh':
                return original_run(command, **kwargs)
            self.assertIn('ProxyCommand=/bin/false', command)
            commit = command[command.index('--commit') + 1]
            probe = '--probe' in command
            events.append(('probe' if probe else 'stage', commit))
            if probe:
                if commit == bridge:
                    self.assertFalse(pack_files[plugin].exists())
                receipt = {'commit': commit, 'cached': commit != plugin}
                return subprocess.CompletedProcess(command, 0, json.dumps(receipt) + '\n', '')
            self.assertEqual('--pack' in command, commit == plugin)
            if commit == plugin:
                self.assertTrue(pack_files[plugin].is_file())
            result = {'commit': commit, 'cache': 'built' if commit == plugin else 'reused'}
            return subprocess.CompletedProcess(command, 0, json.dumps(result) + '\n', '')
        def fake_sh(command, log=None):
            self.assertIn('ProxyCommand=/bin/false', ' '.join(command))
            if command[0] == 'rsync':
                kind = 'pack-rsync' if command[-1].endswith('.pack') else 'rsync'
                events.append((kind, command[-1]))
                if kind == 'pack-rsync':
                    self.assertIn('--partial', command)
            elif command[0] == 'ssh':
                flags = [command[index + 1] for index, value in enumerate(command)
                         if value == '--staged-target']
                self.assertEqual(flags, sorted([plugin, bridge]))
                self.assertEqual(sum(kind == 'stage' for kind, _ in events), 2)
                events.append(('go', None))
            elif command[0] == 'scp' and command[-2].endswith('/go-result.json'):
                Path(command[-1]).write_text(json.dumps(
                    {'token': 'b' * 32, 'code': 0, 'counts': {'PASS': 0, 'FAIL': 0, 'NOT RUN': 0}}))
            elif command[0] == 'scp':
                Path(command[-1]).write_text('report')
            return 0
        output = io.StringIO()
        with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex='b' * 32)), \
                mock.patch.object(eden_test, 'SshMux', RecordingMux), \
                mock.patch.object(eden_test, 'pack_target', side_effect=pack), \
                mock.patch.object(eden_test.subprocess, 'run', side_effect=run), \
                mock.patch.object(eden_test, 'sh', side_effect=fake_sh), redirect_stdout(output):
            self.assertEqual(eden_test.main(self.go_args(host='xps', rows=rowfile) + ['--ssh', 'mapped-xps']), 0)
        kinds = [event[0] for event in events]
        self.assertEqual([event[0] for event in events if event[0] in ('probe', 'pack', 'pack-rsync', 'stage')],
                         ['probe', 'pack', 'pack-rsync', 'stage', 'probe', 'stage'])
        self.assertLess(kinds.index('stage'), kinds.index('go'))
        self.assertEqual(sum(event[0] == 'pack-rsync' for event in events), 1)
        self.assertEqual([event for event in events if event[0] == 'pack'], [('pack', plugin)])
        self.assertIn('1234 bytes shipped', output.getvalue())
        self.assertIn(f'target bridge {bridge[:12]}: cache reused, 0 bytes shipped', output.getvalue())
        self.assertIn(f'target repos.same {plugin[:12]}: cache reused, 0 bytes shipped', output.getvalue())
        self.assertEqual(rowfile.read_bytes(), original_rows)

    def test_go_remote_cache_hit_never_packs_or_transfers_a_pack(self):
        repo = self.dir / 'cached-source'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)
        git('init', '-q')
        (repo / 'file').write_text('fixture')
        git('add', 'file')
        git('-c', 'user.name=fixture', '-c', 'user.email=f@f', 'commit', '-qm', 'fixture')
        commit = git('rev-parse', 'HEAD').stdout.strip()
        (self.rows / 'rows.json').write_text(json.dumps(
            {'target': {'plugin': {'tree': str(repo), 'commit': commit, 'nro_sha256': 'a' * 64}}, 'rows': []}))
        def remote(host, mux, argv, timeout):
            if '--probe' in argv:
                return 0, 'notice\n' + json.dumps({'commit': commit, 'cached': True}) + '\n', ''
            return 0, json.dumps({'commit': commit, 'cache': 'reused'}), ''
        def sh(command):
            if command[0] == 'scp' and command[-2].endswith('/go-result.json'):
                Path(command[-1]).write_text(json.dumps(
                    {'token': 'd' * 32, 'code': 0, 'counts': {'PASS': 0, 'FAIL': 0, 'NOT RUN': 0}}))
            elif command[0] == 'scp':
                Path(command[-1]).write_text('report')
            return 0
        output = io.StringIO()
        with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex='d' * 32)), \
                mock.patch.object(eden_test, '_go_sh', side_effect=sh), \
                mock.patch.object(eden_test, '_go_target_ssh', side_effect=remote), \
                mock.patch.object(eden_test, 'pack_target') as pack, \
                mock.patch.object(eden_test, '_go_target_rsync') as rsync, redirect_stdout(output):
            self.assertEqual(eden_test._go_remote_with_mux(
                self.remote_args(), None, 'fake-host', FakeMux('fake-host')), 0)
        pack.assert_not_called()
        rsync.assert_not_called()
        self.assertIn(f'target plugin {commit[:12]}: cache reused, 0 bytes shipped', output.getvalue())

    def test_powershell_login_shell_maps_nonzero_to_one_without_losing_cache_miss(self):
        commit = 'a' * 40
        targets = [{'label': 'plugin', 'tree': str(self.dir), 'commit': commit}]
        probe_results = []
        def mapped_exit(code):
            return 0 if code == 0 else 1
        self.assertEqual(mapped_exit(4), 1)
        def remote(host, mux, argv, timeout):
            if '--probe' in argv:
                output = io.StringIO()
                with redirect_stdout(output):
                    code = eden_test.main(['stage-target', '--commit', commit, '--label', 'go-1', '--probe'])
                probe_results.append((code, output.getvalue()))
                return mapped_exit(code), output.getvalue(), ''
            return 0, json.dumps({'commit': commit, 'cache': 'built'}), ''
        def pack(tree, target_commit, directory):
            path = Path(directory) / 'target.pack'
            path.write_bytes(b'pack')
            return {'commit': target_commit, 'pack_file': str(path)}
        def sh(command):
            if command[0] == 'scp' and command[-2].endswith('/go-result.json'):
                Path(command[-1]).write_text(json.dumps(
                    {'token': 'e' * 32, 'code': 0, 'counts': {'PASS': 0, 'FAIL': 0, 'NOT RUN': 0}}))
            elif command[0] == 'scp':
                Path(command[-1]).write_text('report')
            return 0
        with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex='e' * 32)), \
                mock.patch.object(eden_test, '_go_sh', side_effect=sh), \
                mock.patch.object(eden_test, '_go_target_ssh', side_effect=remote), \
                mock.patch.object(eden_test, 'pack_target', side_effect=pack) as packed, \
                mock.patch.object(eden_test, '_go_target_rsync', return_value=(0, 4)) as rsync, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(eden_test._go_remote_with_mux(
                self.remote_args(), None, 'fake-host', FakeMux('fake-host'), targets, self.dir), 0)
        self.assertEqual(probe_results, [(0, json.dumps({'commit': commit, 'cached': False}) + '\n')])
        packed.assert_called_once()
        rsync.assert_called_once()

    def test_probe_failures_reject_nonzero_and_invalid_receipts_with_stderr(self):
        commit = 'a' * 40
        valid = json.dumps({'commit': commit, 'cached': False})
        cases = [(1, valid), (0, 'garbage'), (0, ''),
                 (0, json.dumps({'commit': 'b' * 40, 'cached': False})),
                 (0, json.dumps({'commit': commit, 'cached': 0})),
                 (0, json.dumps({'commit': commit})), (0, '[]')]
        targets = [{'label': 'plugin', 'tree': str(self.dir), 'commit': commit}]
        for code, output in cases:
            with self.subTest(code=code, output=output):
                response = subprocess.CompletedProcess(['ssh'], code, output, 'remote detail\n')
                with mock.patch.object(eden_test, '_go_sh', return_value=0), \
                        mock.patch.object(eden_test.subprocess, 'run', return_value=response), \
                        mock.patch.object(eden_test, 'pack_target') as pack, \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaisesRegex(SystemExit, 'target plugin cache probe failed: remote detail'):
                        eden_test._go_remote_with_mux(
                            self.remote_args(), None, 'fake-host', FakeMux('fake-host'), targets, self.dir)
                pack.assert_not_called()

    def test_real_target_probe_transport_diagnoses_once_and_does_not_retry(self):
        commit = 'a' * 40
        targets = [{'label': 'plugin', 'tree': str(self.dir), 'commit': commit}]
        diagnosis = {'line': 'ssh diag named', 'phase': 'session', 'transient': False}
        for code in (255, 1):
            with self.subTest(code=code):
                receipt = eden_test.RUNS / '_eden-test/ssh-diag/go-1/ssh-diag.md'
                receipt.unlink(missing_ok=True)
                response = subprocess.CompletedProcess(['ssh'], code, '', 'connection reset')
                with mock.patch.object(eden_test, '_go_sh', return_value=0), \
                        mock.patch.object(eden_test.subprocess, 'run', return_value=response) as probe, \
                        mock.patch.object(eden_test._lifecycle_module, 'ssh_diagnosis', return_value=diagnosis) as diag, \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        eden_test._go_remote_with_mux(
                            self.remote_args(), None, 'fake-host', FakeMux('fake-host'), targets, self.dir)
                self.assertIn('target plugin cache probe failed: connection reset', str(error.exception))
                if code == 255:
                    self.assertEqual(str(error.exception),
                                     'target plugin cache probe failed: connection reset; ssh diag named; '
                                     f'ssh-diag receipt: {receipt}')
                self.assertEqual('ssh diag named' in str(error.exception), code == 255)
                self.assertEqual(probe.call_count, 1)
                self.assertEqual(diag.call_count, int(code == 255))
                self.assertEqual(receipt.exists(), code == 255)
                if receipt.exists():
                    self.assertEqual(len(receipt.read_text().splitlines()), 1)
                    self.assertIn(' ssh target probe : ssh diag named', receipt.read_text())
                    self.assertFalse(diag.call_args.kwargs['allow_retry'])

    def test_target_does_not_duplicate_master_failure_receipt(self):
        receipt = self.dir / 'ssh-diag.md'
        diagnosis = {'line': 'ssh diag master failed', 'phase': 'auth', 'cause': 'not established',
                     'evidence': 'fixture', 'transient': False}
        life = eden_test._lifecycle_module
        class FailedMux(FakeMux):
            def ensure(self):
                recorded = life.ssh_record(diagnosis, 'ssh', 'master', self_dir)
                raise life.LedgerTransport([recorded], 'SSH master failed')
        self_dir = self.dir
        with mock.patch.object(eden_test.subprocess, 'run') as run:
            with self.assertRaises(life.LedgerTransport) as error:
                eden_test._go_target_ssh('fake-host', FailedMux('fake-host'), ['probe'], 10)
        run.assert_not_called()
        self.assertIn(f'ssh-diag receipt: {receipt}', str(error.exception))
        self.assertEqual(len(receipt.read_text().splitlines()), 1)

    def test_go_rider_transfers_and_remote_go_distinguish_transport_from_application(self):
        diagnosis = {'line': 'ssh diag named', 'phase': 'session', 'transient': False}
        token = 'f' * 32
        for stage, transport in (('rsync', True), ('rsync', False), ('go', True),
                                 ('result', True), ('result', False), ('report', True), ('report', False)):
            with self.subTest(stage=stage, transport=transport):
                receipt = eden_test.RUNS / '_eden-test/ssh-diag/go-1/ssh-diag.md'
                receipt.unlink(missing_ok=True)
                calls = []
                commands = []
                def sh(command):
                    kind = command[0]
                    what = ('result' if kind == 'scp' and command[-2].endswith('/go-result.json') else
                            'report' if kind == 'scp' else 'go' if kind == 'ssh' else 'rsync')
                    calls.append(what)
                    commands.append(command)
                    if what == stage:
                        code = 255 if transport else 23 if what == 'rsync' else 1
                        return eden_test._ExitCode(code, 'Connection reset' if transport else 'application failure')
                    if what == 'result':
                        Path(command[-1]).write_text(json.dumps({'token': token, 'code': 0, 'counts': {}}))
                    elif what == 'report':
                        Path(command[-1]).write_text('report')
                    return 0
                with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex=token)), \
                        mock.patch.object(eden_test, '_go_sh', side_effect=sh), \
                        mock.patch.object(eden_test._lifecycle_module, 'ssh_diagnosis', return_value=diagnosis) as diag, \
                        redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        eden_test._go_remote_with_mux(
                            self.remote_args(), None, 'fake-host', FakeMux('fake-host'), [], self.dir)
                self.assertEqual(calls.count(stage), 1)
                self.assertEqual(diag.call_count, int(transport))
                self.assertEqual('ssh diag named' in str(error.exception), transport)
                self.assertEqual(receipt.exists(), transport)
                original = {'rsync': f'go staging failed ({255 if transport else 23}): {shlex.join(commands[-1])}',
                            'result': 'remote go produced no result for this call (ssh exit 0); '
                                      'nothing was copied; see the output above',
                            'report': f'copying remote report failed ({255 if transport else 1})',
                            'go': 'remote go failed (ssh exit 255); the remote go may be running; '
                                  'check the host before starting another'}[stage]
                self.assertEqual(str(error.exception), original +
                                 (f'; ssh diag named; ssh-diag receipt: {receipt}' if transport else ''))
                if transport:
                    self.assertEqual(len(receipt.read_text().splitlines()), 1)
                if stage == 'go':
                    self.assertIn('the remote go may be running; check the host before starting another',
                                  str(error.exception))
                    self.assertNotIn('result', calls)

    def test_target_pack_rsync_transport_is_diagnosed_without_replay(self):
        commit = 'a' * 40
        targets = [{'label': 'plugin', 'tree': str(self.dir), 'commit': commit}]
        diagnosis = {'line': 'ssh diag named', 'phase': 'session', 'transient': False}
        for code in (10, 23, 124):
            with self.subTest(code=code):
                receipt = eden_test.RUNS / '_eden-test/ssh-diag/go-1/ssh-diag.md'
                receipt.unlink(missing_ok=True)
                def pack(*_args):
                    path = self.dir / 'temporary.pack'
                    path.write_text('fixture')
                    return {'pack_file': str(path)}
                with mock.patch.object(eden_test, '_go_sh', return_value=0), \
                        mock.patch.object(eden_test, '_go_target_ssh',
                                          return_value=(0, json.dumps({'commit': commit, 'cached': False}), '')), \
                        mock.patch.object(eden_test, 'pack_target', side_effect=pack), \
                        mock.patch.object(eden_test, '_go_target_rsync',
                                          return_value=(eden_test._ExitCode(code, timed_out=code == 124), None)) as transfer, \
                        mock.patch.object(eden_test._lifecycle_module, 'ssh_diagnosis', return_value=diagnosis) as diag:
                    with self.assertRaises(SystemExit) as error:
                        eden_test._go_remote_with_mux(
                            self.remote_args(), None, 'fake-host', FakeMux('fake-host'), targets, self.dir)
                transfer.assert_called_once()
                self.assertEqual(diag.call_count, int(code in (10, 124)))
                self.assertEqual('ssh diag named' in str(error.exception), code in (10, 124))
                self.assertEqual(receipt.exists(), code in (10, 124))
                if code != 124:
                    original = f'target plugin pack rsync failed ({code}): {shlex.join(transfer.call_args.args[0])}'
                    self.assertEqual(str(error.exception), original +
                                     (f'; ssh diag named; ssh-diag receipt: {receipt}' if code == 10 else ''))
                if code == 124:
                    self.assertTrue(diag.call_args.kwargs['attempt_timed_out'])

    def test_target_staging_ssh_transport_is_diagnosed_without_replay(self):
        commit = 'a' * 40
        targets = [{'label': 'plugin', 'tree': str(self.dir), 'commit': commit}]
        diagnosis = {'line': 'ssh diag named', 'phase': 'session', 'transient': False}
        for code in (255, 1):
            with self.subTest(code=code):
                receipt = eden_test.RUNS / '_eden-test/ssh-diag/go-1/ssh-diag.md'
                receipt.unlink(missing_ok=True)
                responses = [subprocess.CompletedProcess([], 0, json.dumps({'commit': commit, 'cached': True}), ''),
                             subprocess.CompletedProcess([], code, '', 'failed')]
                with mock.patch.object(eden_test, '_go_sh', return_value=0), \
                        mock.patch.object(eden_test.subprocess, 'run', side_effect=responses) as ssh, \
                        mock.patch.object(eden_test._lifecycle_module, 'ssh_diagnosis', return_value=diagnosis) as diag, \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        eden_test._go_remote_with_mux(
                            self.remote_args(), None, 'fake-host', FakeMux('fake-host'), targets, self.dir)
                self.assertEqual(ssh.call_count, 2)
                self.assertEqual(diag.call_count, int(code == 255))
                self.assertEqual('ssh diag named' in str(error.exception), code == 255)
                self.assertEqual(receipt.exists(), code == 255)
                self.assertEqual(str(error.exception), f'target plugin staging failed ({code})' +
                                 (f'; ssh diag named; ssh-diag receipt: {receipt}' if code == 255 else ''))

    def test_target_pack_rsync_timeout_preserves_original_detail_and_diagnosis(self):
        commit = 'a' * 40
        targets = [{'label': 'plugin', 'tree': str(self.dir), 'commit': commit}]
        diagnosis = {'line': 'ssh diag named', 'phase': 'session', 'transient': False}
        calls = []
        def pack(*_args):
            path = self.dir / 'temporary.pack'
            path.write_text('fixture')
            return {'pack_file': str(path)}
        def timeout(command, **kwargs):
            calls.append((command, kwargs))
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        with mock.patch.object(eden_test, '_go_sh', return_value=0), \
                mock.patch.object(eden_test, '_go_target_ssh',
                                  return_value=(0, json.dumps({'commit': commit, 'cached': False}), '')), \
                mock.patch.object(eden_test, 'pack_target', side_effect=pack), \
                mock.patch.object(eden_test.subprocess, 'run', side_effect=timeout), \
                mock.patch.object(eden_test._lifecycle_module, 'ssh_diagnosis', return_value=diagnosis) as diag:
            with self.assertRaises(SystemExit) as error:
                eden_test._go_remote_with_mux(
                    self.remote_args(), None, 'fake-host', FakeMux('fake-host'), targets, self.dir)
        self.assertEqual(len(calls), 1)
        previous = f'target pack rsync failed: {subprocess.TimeoutExpired(calls[0][0], 3600)}'
        receipt = eden_test.RUNS / '_eden-test/ssh-diag/go-1/ssh-diag.md'
        self.assertEqual(str(error.exception), previous + f'; ssh diag named; ssh-diag receipt: {receipt}')
        diag.assert_called_once()
        self.assertTrue(diag.call_args.kwargs['attempt_timed_out'])
        self.assertEqual(len(receipt.read_text().splitlines()), 1)

    def test_remote_go_dead_master_refuses_even_with_mapped_application_exit(self):
        class DeadMux(FakeMux):
            def _control(self, operation):
                return subprocess.CompletedProcess([], 255, '', 'master dead')
        calls = []
        def sh(command):
            calls.append(command[0])
            return 1 if command[0] == 'ssh' else 0
        diagnosis = {'line': 'ssh diag named', 'phase': 'unknown', 'transient': False}
        with mock.patch.object(eden_test, '_go_sh', side_effect=sh), \
                mock.patch.object(eden_test._lifecycle_module, 'ssh_diagnosis', return_value=diagnosis) as diag:
            with self.assertRaisesRegex(SystemExit, 'remote go may be running') as error:
                eden_test._go_remote_with_mux(
                    self.remote_args(), None, 'fake-host', DeadMux('fake-host'), [], self.dir)
        self.assertEqual(calls.count('ssh'), 1)
        self.assertNotIn('scp', calls)
        diag.assert_called_once()
        self.assertIn('ssh diag named', str(error.exception))
        self.assertTrue((eden_test.RUNS / '_eden-test/ssh-diag/go-1/ssh-diag.md').is_file())

    def test_target_ssh_quotes_each_remote_argument_for_powershell(self):
        response = subprocess.CompletedProcess(['ssh'], 0, 'ok\n', '')
        args = ['/usr/bin/python3', '/tmp/a b/file.py', "a'b", '--probe']
        with mock.patch.object(eden_test.subprocess, 'run', return_value=response) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(eden_test._go_target_ssh('host', FakeMux('host'), args, 10), (0, 'ok\n', ''))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('host') + 1:],
                         ['/usr/bin/python3', "'/tmp/a b/file.py'", "'a''b'", '--probe'])

    def test_target_ssh_timeout_is_diagnosed_once(self):
        mux = FakeMux('fake-host')
        mux.receipt_dir = self.dir / 'receipts'
        diagnosis = {'line': 'ssh diag named', 'phase': 'session', 'transient': False}
        with mock.patch.object(eden_test.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 10)) as run, \
                mock.patch.object(eden_test._lifecycle_module, 'ssh_diagnosis', return_value=diagnosis) as diag:
            with self.assertRaisesRegex(SystemExit, 'target staging SSH failed:.*ssh diag named'):
                eden_test._go_target_ssh('fake-host', mux, ['probe'], 10)
        run.assert_called_once()
        diag.assert_called_once()
        self.assertTrue(diag.call_args.kwargs['attempt_timed_out'])
        self.assertEqual(len((mux.receipt_dir / 'ssh-diag.md').read_text().splitlines()), 1)

    def test_go_remote_missing_commit_stops_before_mux_or_remote_command(self):
        repo = self.dir / 'source'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, capture_output=True)
        (self.rows / 'rows.json').write_text(json.dumps(
            {'target': {'plugin': {'tree': str(repo), 'commit': 'a' * 40, 'nro_sha256': 'b' * 64}}, 'rows': []}))
        with mock.patch.object(eden_test, 'SshMux') as mux, mock.patch.object(eden_test, 'sh') as sh, \
                mock.patch.object(eden_test, 'pack_target') as pack:
            with self.assertRaisesRegex(SystemExit, 'target plugin:'):
                eden_test.main(self.go_args(host='xps') + ['--ssh', 'mapped-xps'])
        mux.assert_not_called()
        sh.assert_not_called()
        pack.assert_not_called()

    def test_go_remote_validates_all_targets_before_packing_or_remote_commands(self):
        repo = self.dir / 'source'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)
        git('init', '-q')
        (repo / 'file').write_text('fixture')
        git('add', 'file')
        git('-c', 'user.name=fixture', '-c', 'user.email=f@f', 'commit', '-qm', 'fixture')
        commit = git('rev-parse', 'HEAD').stdout.strip()
        (self.rows / 'rows.json').write_text(json.dumps({
            'target': {'plugin': {'tree': str(repo), 'commit': commit, 'nro_sha256': 'b' * 64},
                       'bridge': {'tree': str(repo), 'commit': 'a' * 40}}, 'rows': []}))
        with mock.patch.object(eden_test, 'SshMux') as mux, mock.patch.object(eden_test, 'sh') as sh, \
                mock.patch.object(eden_test, 'pack_target') as pack:
            with self.assertRaisesRegex(SystemExit, 'target bridge:'):
                eden_test.main(self.go_args(host='xps') + ['--ssh', 'mapped-xps'])
        mux.assert_not_called()
        sh.assert_not_called()
        pack.assert_not_called()

    def test_go_target_pack_rsync_failure_reports_exit_before_go(self):
        commit = 'a' * 40
        args = self.remote_args()
        targets = [{'label': 'plugin', 'tree': str(self.dir), 'commit': commit}]
        pack_file = self.dir / 'pack'
        def pack(*_args):
            pack_file.write_text('pack')
            return {'pack_file': str(pack_file), 'bytes': 20}
        with mock.patch.object(eden_test, '_go_sh', return_value=0) as sh, \
                mock.patch.object(eden_test, '_go_target_ssh',
                                  return_value=(0, json.dumps({'commit': commit, 'cached': False}), '')), \
                mock.patch.object(eden_test, 'pack_target', side_effect=pack), \
                mock.patch.object(eden_test, '_go_target_rsync', return_value=(23, None)):
            with self.assertRaisesRegex(SystemExit, r'target plugin pack rsync failed \(23\)'):
                eden_test._go_remote_with_mux(args, None, 'fake-host', FakeMux('fake-host'), targets, self.dir)
        self.assertEqual(sh.call_count, 1)
        self.assertFalse(pack_file.exists())

    def test_go_target_pack_is_removed_when_staging_fails(self):
        commit = 'a' * 40
        pack_file = self.dir / 'pack'
        targets = [{'label': 'plugin', 'tree': str(self.dir), 'commit': commit}]
        def pack(*_args):
            pack_file.write_text('pack')
            return {'pack_file': str(pack_file), 'bytes': 20}
        def remote(host, mux, argv, timeout):
            if '--probe' in argv:
                return 0, json.dumps({'commit': commit, 'cached': False}), ''
            self.assertTrue(pack_file.exists())
            return 1, '', 'stage failed'
        with mock.patch.object(eden_test, '_go_sh', return_value=0), \
                mock.patch.object(eden_test, '_go_target_ssh', side_effect=remote), \
                mock.patch.object(eden_test, 'pack_target', side_effect=pack), \
                mock.patch.object(eden_test, '_go_target_rsync', return_value=(0, 20)):
            with self.assertRaisesRegex(SystemExit, r'target plugin staging failed \(1\)'):
                eden_test._go_remote_with_mux(
                    self.remote_args(), None, 'fake-host', FakeMux('fake-host'), targets, self.dir)
        self.assertFalse(pack_file.exists())

    def test_go_remote_with_mux_packs_targets_when_called_directly(self):
        repo = self.dir / 'direct-source'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)
        git('init', '-q')
        (repo / 'file').write_text('fixture')
        git('add', 'file')
        git('-c', 'user.name=fixture', '-c', 'user.email=f@f', 'commit', '-qm', 'fixture')
        commit = git('rev-parse', 'HEAD').stdout.strip()
        (self.rows / 'rows.json').write_text(json.dumps(
            {'target': {'plugin': {'tree': str(repo), 'commit': commit, 'nro_sha256': 'a' * 64}}, 'rows': []}))
        args = self.remote_args()
        def remote(host, mux, argv, timeout):
            if '--probe' in argv:
                return 0, json.dumps({'commit': commit, 'cached': False}), ''
            return 0, json.dumps({'commit': commit, 'cache': 'built'}), ''
        def sh(command):
            if command[0] == 'scp' and command[-2].endswith('/go-result.json'):
                Path(command[-1]).write_text(json.dumps(
                    {'token': 'c' * 32, 'code': 0, 'counts': {'PASS': 0, 'FAIL': 0, 'NOT RUN': 0}}))
            elif command[0] == 'scp':
                Path(command[-1]).write_text('report')
            return 0
        with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex='c' * 32)), \
                mock.patch.object(eden_test, '_go_sh', side_effect=sh), \
                mock.patch.object(eden_test, '_go_target_ssh', side_effect=remote), \
                mock.patch.object(eden_test, '_go_target_rsync', return_value=(0, 45)) as rsync, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(eden_test._go_remote_with_mux(args, None, 'fake-host', FakeMux('fake-host')), 0)
        pack_path = Path(rsync.call_args.args[0][-2])
        self.assertFalse(pack_path.exists())

    def test_go_remote_reports_a_master_retry_once(self):
        life = eden_test._lifecycle_module
        starts = []
        diagnosis = {'line': 'ssh diag fake: packet loss', 'phase': 'kex', 'cause': 'packet loss',
                     'evidence': 'fixture', 'transient': True}
        class Process:
            pid = 123456
            def __init__(self, status):
                self.status = status
            def wait(self, timeout=None):
                return self.status
            def poll(self):
                return self.status
        def popen(argv, **_kwargs):
            starts.append(argv)
            return Process(0 if len(starts) > 1 else 255)
        def run(argv, **_kwargs):
            if '-G' in argv:
                template = next(option for option in argv if option.startswith('ControlPath='))
                return subprocess.CompletedProcess(argv, 0, 'controlpath ' +
                                                   template.removeprefix('ControlPath=').replace('%C', 'fixture') +
                                                   '\n', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        def mux(host, **kwargs):
            return life.SshMux(host, run=run, popen=popen, **kwargs)
        captured = io.StringIO()
        with tempfile.TemporaryDirectory(prefix='es-', dir='/tmp') as runtime, \
                mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': runtime}), \
                mock.patch.object(eden_test, '_go_route', return_value='fake-host'), \
                mock.patch.object(eden_test, '_go_remote_base', return_value=None), \
                mock.patch.object(eden_test, '_go_targets', return_value=[]), \
                mock.patch.object(eden_test, 'SshMux', side_effect=mux), \
                mock.patch.object(life, 'ssh_diagnosis', return_value=diagnosis), \
                mock.patch.object(eden_test, '_go_remote_with_mux', return_value=0) as operation, \
                redirect_stderr(captured):
            self.assertEqual(eden_test._go_remote(self.remote_args(), None), 0)
        receipt = eden_test.RUNS / '_eden-test/ssh-diag/go-1/ssh-diag.md'
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(receipt.read_text().splitlines()), 1)
        self.assertEqual(captured.getvalue().count(
            f'ssh retried once: packet loss; ssh-diag receipt: {receipt}'), 1)
        operation.assert_called_once()

    def test_go_remote_uses_host_map_and_file_rows(self):
        hosts = eden_test.RUNS / '_eden-test/hosts.json'
        hosts.parent.mkdir(parents=True, exist_ok=True)
        hosts.write_text(json.dumps({'xps': 'mapped-xps'}))
        rowfile = self.rows / 'rows.json'
        def fake_sh(command, log=None):
            if command[0] == 'scp' and command[-2].endswith('/go-result.json'):
                Path(command[-1]).write_text(json.dumps(
                    {'token': 'b' * 32, 'code': 0, 'counts': {'PASS': 2, 'FAIL': 0, 'NOT RUN': 0}}))
            return 0

        with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex='b' * 32)), \
             mock.patch.object(eden_test, 'sh', side_effect=fake_sh) as sh:
            self.assertEqual(eden_test.main(self.go_args(host='xps', rows=rowfile)), 0)
        commands = [call.args[0] for call in sh.call_args_list]
        self.assertTrue(any(c[0] == 'rsync' and c[-2:] == [str(rowfile),
                       'mapped-xps:/home/intpa/eden-runs/_eden-test/stage/go-1/rows/rows.json'] for c in commands))
        self.assertIn('mapped-xps', commands[-3])
        self.assertIn('ProxyCommand=/bin/false', commands[-3])

    def test_go_remote_missing_result_refuses_without_copying_report(self):
        output = io.StringIO()

        def fake_sh(command, log=None):
            if command[0] == 'ssh' or command[0] == 'scp' and command[-2].endswith('/go-result.json'):
                return 1
            if command[0] == 'scp':
                Path(command[-1]).write_text('report')
            return 0

        with chdir(self.dir), mock.patch.object(eden_test, 'sh', side_effect=fake_sh) as sh, redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, 'remote go produced no result for this call \\(ssh exit 1\\)'):
                eden_test.main(self.go_args(host='xps') + ['--ssh', 'mapped-xps'])
        self.assertFalse(any(c.args[0][0] == 'scp' and c.args[0][-2].endswith('/REPORT.md')
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
                        if command[-2].endswith('/go-result.json'):
                            old = {'code': 0, 'counts': {'PASS': 2, 'FAIL': 0, 'NOT RUN': 0}}
                            if old_token is not None:
                                old['token'] = old_token
                            Path(command[-1]).write_text(json.dumps(old))
                        else:
                            Path(command[-1]).write_text('old report')
                    return 0

                with chdir(self.dir), mock.patch('uuid.uuid4', return_value=types.SimpleNamespace(hex='c' * 32)), \
                     mock.patch.object(eden_test, 'sh', side_effect=fake_sh) as sh, redirect_stdout(output):
                    with self.assertRaisesRegex(SystemExit, 'remote go produced no result for this call \\(ssh exit 1\\)'):
                        eden_test.main(self.go_args(host='xps') + ['--ssh', 'mapped-xps'])
                self.assertFalse(any(c.args[0][0] == 'scp' and c.args[0][-2].endswith('/REPORT.md')
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

    def test_down_pending_release_returns_zero_and_reports_retry_unit(self):
        for unit in ('eden-settle-deadbeef.service', None):
            with self.subTest(unit=unit):
                label = 'pending-unit' if unit else 'pending-no-unit'
                args = ['--from', str(self.previous), '--label', label, '--rows', str(self.rows)]
                eden_test.main(['new', *args])
                run = self.dir / 'runs' / label
                eden_test.main(['up', str(run)])
                source = FAKE_LIFECYCLE.replace(
                    "j.update(phase='down', released=True, cleanup_failures=[])",
                    f"j.update(phase='down', released=False, release_pending=True, "
                    f"settle_unit={unit!r}, cleanup_failures=[])")
                source = source.replace("elif '--evidence-dir' in sys.argv:", "elif False and '--evidence-dir' in sys.argv:")
                eden_test.LIFECYCLE.write_text(source)
                (run / 'state' / 'RELEASE-PENDING.md').write_text(
                    'old line\nrelease pending since 03:00:00Z: ssh diag fixture\n')
                output = io.StringIO()
                with redirect_stdout(output):
                    code = eden_test.main(['down', str(run)])
                self.assertEqual(code, 0)
                self.assertIn('Down complete; the ledger release is pending: '
                              'release pending since 03:00:00Z: ssh diag fixture; settle unit '
                              + (unit or 'not started') + '; running down again completes it', output.getvalue())
                self.assertNotIn('the claim stays held', output.getvalue())
                self.assertIn('released=False, release pending', (run / 'RESULTS.md').read_text())
                eden_test.LIFECYCLE.write_text(FAKE_LIFECYCLE)

    def test_down_real_cleanup_failure_retains_held_claim_message(self):
        eden_test.main(['new', *self.args()])
        run = self.dir / 'runs/lr-14'
        eden_test.main(['up', str(run)])
        with mock.patch.object(eden_test, 'lifecycle', return_value=1):
            with self.assertRaisesRegex(SystemExit, 'the claim stays held'):
                eden_test.main(['down', str(run)])

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

    # --- Down cleanup: the run's own copies go, anything another run may need stays and is listed -------------------

    def stage_copy(self, label='lr-14'):
        stage = eden_test.RUNS / '_eden-test' / 'stage' / label
        (stage / 'tools' / 'eden-test').mkdir(parents=True)
        (stage / 'tools' / 'eden-test' / 'eden_test.py').write_text('staged tool')
        (stage / 'base.probe.json').write_text('{}')  # a file a lane put there by hand
        return stage

    def test_down_removes_work_and_the_stage_copy_and_keeps_out_and_state(self):
        self.pruning_lifecycle()
        run = self.up_run()
        stage = self.stage_copy()
        other_stage = self.stage_copy('other-label')
        eden_test.main(['down', str(run)])
        self.assertFalse((run / 'work').exists())
        self.assertFalse((run / 'state/snapshot-a').exists())
        self.assertFalse(stage.exists())
        self.assertTrue(other_stage.is_dir())
        self.assertTrue((eden_test.RUNS / '_eden-test/base.json').is_file())
        # The receipts stay where they are: the journal in state/, the run's files in out/.
        self.assertTrue(json.loads((run / 'state/lifecycle.json').read_text())['released'])
        self.assertEqual((run / 'out/post-run-a/run.log').read_text(), 'run bytes')
        log = (run / 'lifecycle-down.log').read_text()
        self.assertEqual(log.count('$ '), 2)
        self.assertLess(log.index('--keep-state'), log.rindex('$ '))  # the first call prunes nothing
        self.assertNotIn('--keep-state', log[log.rindex('$ '):])
        record = self.cleanup(run)
        self.assertIsNone(record['skipped'])
        self.assertEqual(record['kept'], [])
        self.assertEqual([item['what'] for item in record['removed']], ['work/', 'state/snapshot-a/', 'stage copy'])
        self.assertTrue(all(item['bytes'] > 0 for item in record['removed']))
        results = (run / 'RESULTS.md').read_text()
        self.assertIn('- Cleanup: removed work/, state/snapshot-a/, stage copy (0.0 GB); receipt out/down-cleanup.json', results)
        self.assertIn('- Prune: 2.0 GB freed', results)
        self.assertLessEqual(len(results.splitlines()), 20)
        eden_test.main(['down', str(run)])  # a repeated Down finds its journal and removes nothing new
        self.assertEqual(self.cleanup(run)['removed'], [])
        eden_test.main(['report', str(run)])

    def test_down_does_not_prune_work_that_another_run_pins(self):
        self.pruning_lifecycle()
        run = self.up_run()
        stage = self.stage_copy()
        pinned = run / 'work/profiles/profile-a/save.bin'
        second = json.loads(self.previous.read_text())
        second['instances'][0]['files'] = [{'path': str(pinned), 'sha256': sha(pinned), 'to': 'user/save.bin'}]
        other = eden_test.RUNS / 'second-run'
        other.mkdir()
        (other / 'config.json').write_text(json.dumps(second))
        eden_test.main(['down', str(run)])
        self.assertEqual(pinned.read_text(), 'profile')  # never pruned: the check ran before the lifecycle's prune
        self.assertTrue((run / 'state/snapshot-a/f').is_file())
        self.assertTrue(stage.is_dir())
        self.assertEqual((run / 'lifecycle-down.log').read_text().count('$ '), 1)
        self.assertTrue(json.loads((run / 'state/lifecycle.json').read_text())['released'])
        record = self.cleanup(run)
        self.assertEqual(record['removed'], [])
        kept = {item['what']: item['reason'] for item in record['kept']}
        self.assertIn(f'{other / "config.json"} names {pinned}', kept['work/'])
        self.assertIn('Down has to run again from it', kept['stage copy'])
        self.assertIn('KEPT work/', (run / 'RESULTS.md').read_text())
        # The pin is gone: the next Down prunes and removes the stage copy.
        (other / 'config.json').unlink()
        eden_test.main(['down', str(run)])
        self.assertFalse((run / 'work').exists())
        self.assertFalse(stage.exists())

    def test_down_keeps_a_copy_under_a_folder_another_run_names(self):
        self.pruning_lifecycle()
        run = self.up_run()
        other = eden_test.RUNS / 'second-run'
        other.mkdir()
        (other / 'config.json').write_text(json.dumps({'source': str(run)}))  # an ancestor of every copy
        eden_test.main(['down', str(run)])
        self.assertTrue((run / 'work/profiles/profile-a/save.bin').is_file())
        self.assertIn('names ' + str(run), self.cleanup(run)['kept'][0]['reason'])
        (other / 'config.json').write_text(json.dumps({'source': str(run) + '-2/work'}))  # another run's name: no pin
        eden_test.main(['down', str(run)])
        self.assertFalse((run / 'work').exists())

    def test_down_leaves_a_shared_input_package_of_a_second_run(self):
        self.pruning_lifecycle()
        package = eden_test.RUNS / '_shared-inputs'
        package.mkdir()
        shared = package / 'plugin.nro'
        shared.write_bytes(b'shared nro')
        base = json.loads(self.previous.read_text())
        base['instances'][0]['plugins'] = [{'path': str(shared), 'sha256': sha(shared)}]
        self.previous.write_text(json.dumps(base))
        first, second = self.up_run('first-run'), self.up_run('second-run')
        stage = self.stage_copy('first-run')
        eden_test.main(['down', str(first)])
        self.assertFalse((first / 'work').exists())
        self.assertFalse(stage.exists())
        self.assertEqual(shared.read_bytes(), b'shared nro')
        self.assertTrue((second / 'work/profiles/profile-a/save.bin').is_file())  # the second run is still Up
        self.assertTrue((second / 'state/snapshot-a/f').is_file())
        self.assertEqual(self.cleanup(first)['kept'], [])
        self.assertFalse(any('_shared-inputs' in item['path'] for item in self.cleanup(first)['removed']))
        eden_test.main(['check', str(second), '--skip-setup-plan'])  # its pins still verify

    def test_down_keeps_a_stage_copy_another_run_pins_and_lists_it(self):
        self.pruning_lifecycle()
        run = self.up_run()
        stage = self.stage_copy()
        other = eden_test.RUNS / 'second-run'
        other.mkdir()
        (other / 'config.json').write_text(json.dumps({'from': str(stage / 'base.probe.json')}))
        eden_test.main(['down', str(run)])
        self.assertFalse((run / 'work').exists())  # nothing names the run's own copies
        self.assertTrue((stage / 'base.probe.json').is_file())
        self.assertTrue((stage / 'tools/eden-test/eden_test.py').is_file())
        kept = self.cleanup(run)['kept']
        self.assertEqual([item['what'] for item in kept], ['stage copy'])
        self.assertIn(str(other / 'config.json'), kept[0]['reason'])
        self.assertIn('KEPT stage copy', (run / 'RESULTS.md').read_text())

    def test_down_keeps_everything_when_it_cannot_read_another_runs_config(self):
        self.pruning_lifecycle()
        run = self.up_run()
        stage = self.stage_copy()
        other = eden_test.RUNS / 'second-run'
        other.mkdir()
        for broken in ('not json', None):
            with self.subTest(broken=broken):
                if broken is None:
                    (other / 'config.json').unlink()
                    (other / 'config.json').symlink_to(other / 'missing.json')  # present, but it cannot be read
                else:
                    (other / 'config.json').write_text(broken)
                eden_test.main(['down', str(run)])
                self.assertTrue((run / 'work/profiles/profile-a/save.bin').is_file())
                self.assertTrue(stage.is_dir())
                record = self.cleanup(run)
                self.assertEqual(record['removed'], [])
                self.assertIn(f'cannot read {other / "config.json"}', record['kept'][0]['reason'])
        (other / 'config.json').unlink()
        os.chmod(other, 0)  # a run folder that cannot be listed
        try:
            eden_test.main(['down', str(run)])
            self.assertTrue((run / 'work').is_dir())
            self.assertIn('cannot read', self.cleanup(run)['kept'][0]['reason'])
        finally:
            os.chmod(other, 0o755)

    def test_down_keeps_paths_named_in_keep_file_or_used_by_a_process(self):
        self.pruning_lifecycle()
        run = self.up_run()
        stage = self.stage_copy()
        keep = eden_test.RUNS / '.keep'
        keep.write_text('# kept for the review\nlr-14\n')
        eden_test.main(['down', str(run)])
        self.assertTrue((run / 'work').is_dir())
        self.assertIn(f'{keep} lists lr-14', self.cleanup(run)['kept'][0]['reason'])
        keep.unlink()
        keep.mkdir()  # present, but not a readable list
        eden_test.main(['down', str(run)])
        self.assertTrue((run / 'work').is_dir())
        self.assertIn(f'cannot read {keep}', self.cleanup(run)['kept'][0]['reason'])
        keep.rmdir()
        self.process_reason = 'process 4242 (sleep) uses the path'
        eden_test.main(['down', str(run)])
        self.assertTrue((run / 'work').is_dir())
        self.assertTrue(stage.is_dir())
        self.assertEqual(self.cleanup(run)['kept'][0]['reason'], 'process 4242 (sleep) uses the path')
        self.process_reason = None
        eden_test.main(['down', str(run)])
        self.assertFalse((run / 'work').exists())
        self.assertFalse(stage.exists())

    def test_down_keeps_everything_for_an_unmatched_or_unreadable_ledger(self):
        self.pruning_lifecycle()
        run = self.up_run()
        stage = self.stage_copy()
        journal = json.loads((run / 'state/lifecycle.json').read_text())
        journal['token'] = 'a' * 32
        (run / 'state/lifecycle.json').write_text(json.dumps(journal))
        own = f'2026-11-25T22:00:00+00:00 CLAIMED: OpenCode x/y host=yoga lifecycle={"a" * 32} Eden lifecycle admitted'
        foreign = '2026-11-25T22:01:00+00:00 CLAIMED: Claude other/elsewhere-01 host=yoga lifecycle=' + 'b' * 32
        self.open_claims = [own]  # the ledger still shows this run: not a clean Down
        eden_test.main(['down', str(run)])
        self.assertTrue((run / 'work').is_dir())
        self.assertIn("still shows this run's claim open", self.cleanup(run)['skipped'])
        self.open_claims = [foreign]  # a run whose folder is not here: nothing is known of what it uses
        eden_test.main(['down', str(run)])
        self.assertTrue((run / 'work').is_dir())
        self.assertTrue(stage.is_dir())
        self.assertIn('an open ledger claim is not matched to a run here', self.cleanup(run)['kept'][0]['reason'])
        other = eden_test.RUNS / 'elsewhere-01'  # the same claim, with its run readable here and not naming us
        other.mkdir()
        (other / 'config.json').write_text(json.dumps({'label': 'elsewhere-01'}))
        with mock.patch.object(eden_test, '_open_claims', side_effect=OSError('ledger unreachable')):
            eden_test.main(['down', str(run)])
            self.assertTrue((run / 'work').is_dir())
            self.assertIn('cannot read the ledger', self.cleanup(run)['kept'][0]['reason'])
        eden_test.main(['down', str(run)])
        self.assertFalse((run / 'work').exists())
        self.assertFalse(stage.exists())

    def test_claim_or_raw_pin_appearing_during_prune_keeps_remaining_files(self):
        self.pruning_lifecycle()
        for label in ('late-claim', 'late-pin'):
            with self.subTest(label=label):
                self.open_claims.clear()
                run = self.up_run(label)
                stage = self.stage_copy(label)
                raw = run / 'out/r1/hit.1.capture.gif'
                raw.parent.mkdir(parents=True)
                raw.write_bytes(b'raw')
                clip = raw.with_name('hit.gif')
                clip.write_bytes(b'published')
                (run / 'out/r1.json').write_text(json.dumps({'gif': {
                    'capture': raw.name, 'path': str(clip), 'sha256': sha(clip)}}))
                real_lifecycle = eden_test.lifecycle

                def finish_prune(*args):
                    code = real_lifecycle(*args)
                    if '--keep-state' not in args:
                        if label == 'late-claim':
                            self.open_claims.append('now CLAIMED: Claude new/upcoming host=yoga lifecycle=new')
                        else:
                            other = eden_test.RUNS / 'next-consumer'
                            other.mkdir()
                            (other / 'config.json').write_text(json.dumps({'input': str(raw)}))
                    return code

                with mock.patch.object(eden_test, 'lifecycle', side_effect=finish_prune):
                    eden_test.main(['down', str(run)])
                self.assertFalse((run / 'work').exists(), 'the earlier prune legitimately finished')
                self.assertTrue(raw.is_file(), 'capture removed using pre-prune claim/config data')
                self.assertEqual(stage.is_dir(), label == 'late-claim')
                self.assertTrue(any(item['path'] == str(raw) for item in self.cleanup(run)['kept']))

    def test_linked_peer_keeps_work_before_prune(self):
        self.pruning_lifecycle()
        run = self.up_run()
        stage = self.stage_copy()
        pinned = run / 'work/profiles/profile-a/save.bin'
        archived = self.dir / 'archived-peer'
        archived.mkdir()
        (archived / 'config.json').write_text(json.dumps({'input': str(pinned)}))
        peer = eden_test.RUNS / 'peer'
        peer.symlink_to(archived, target_is_directory=True)

        eden_test.main(['down', str(run)])
        self.assertTrue(pinned.is_file(), 'the linked peer still needs the work input')
        self.assertTrue(stage.is_dir())
        self.assertEqual((run / 'lifecycle-down.log').read_text().count('$ '), 1)
        self.assertIn(str(peer), self.cleanup(run)['kept'][0]['reason'])

    def test_other_stage_base_pins_work_and_an_uninspected_stage_keeps_it(self):
        self.pruning_lifecycle()
        for kind in ('plain', 'linked', 'dangling'):
            with self.subTest(kind=kind):
                run = self.up_run(kind)
                own_stage = self.stage_copy(kind)
                pinned = run / 'work/profiles/profile-a/save.bin'
                peer = own_stage.parent / f'peer-{kind}'
                other = peer if kind == 'plain' else self.dir / f'archived-{kind}'
                if kind != 'dangling':
                    other.mkdir()
                    (other / 'base.json').write_text(json.dumps({'input': str(pinned)}))
                if kind != 'plain':
                    peer.symlink_to(other, target_is_directory=True)
                try:
                    eden_test.main(['down', str(run)])
                    self.assertTrue(pinned.is_file(), f'{kind} peer stage did not protect work')
                    self.assertTrue(own_stage.is_dir())
                    self.assertEqual((run / 'lifecycle-down.log').read_text().count('$ '), 1)
                    reason = self.cleanup(run)['kept'][0]['reason']
                    self.assertIn(str(peer / 'base.json' if kind == 'plain' else peer), reason)
                    if kind != 'plain':
                        self.assertIn('unknown', reason)
                finally:
                    if peer.is_symlink():
                        peer.unlink()

    def test_dangling_peer_blocks_prune_even_without_visible_copies(self):
        self.pruning_lifecycle()
        run = self.up_run()
        stage = self.stage_copy()
        shutil.rmtree(run / 'work')
        shutil.rmtree(run / 'state/snapshot-a')
        peer = eden_test.RUNS / 'peer'
        peer.symlink_to(self.dir / 'missing-peer', target_is_directory=True)
        eden_test.main(['down', str(run)])
        self.assertEqual((run / 'lifecycle-down.log').read_text().count('$ '), 1)
        self.assertTrue(stage.is_dir())
        self.assertIn(str(peer), self.cleanup(run)['kept'][0]['reason'])

    def test_peer_appearing_between_removals_keeps_remaining_raw_or_stage(self):
        self.pruning_lifecycle()
        for when in ('second-raw', 'stage'):
            with self.subTest(when=when):
                run = self.up_run(when)
                stage = self.stage_copy(when)
                row = run / 'out/r1'
                row.mkdir(parents=True)
                clip = row / 'published.gif'
                clip.write_bytes(b'published')
                raws = [row / f'{n}.capture.gif' for n in (1, 2)]
                for raw in raws:
                    raw.write_bytes(b'raw')
                (run / 'out/r1.json').write_text(json.dumps({'steps': [
                    {'gif': {'capture': raw.name, 'path': str(clip), 'sha256': sha(clip)}} for raw in raws]}))
                archived = self.dir / f'archived-{when}'
                archived.mkdir()
                (archived / 'config.json').write_text('{}')  # even a readable target is not inspected
                peer = eden_test.RUNS / f'peer-{when}'
                captures = eden_test.raw_captures

                def publish_peer(run):
                    for raw, reason in captures(run):
                        if raw == raws[1] and when == 'second-raw':
                            peer.symlink_to(archived, target_is_directory=True)
                        yield raw, reason
                    if when == 'stage':
                        peer.symlink_to(archived, target_is_directory=True)

                with mock.patch.object(eden_test, 'raw_captures', side_effect=publish_peer):
                    eden_test.main(['down', str(run)])
                self.assertFalse((run / 'work').exists())
                self.assertFalse(raws[0].exists(), 'the first removal predates the link')
                self.assertEqual(raws[1].is_file(), when == 'second-raw')
                self.assertTrue(stage.is_dir())
                self.assertTrue(all(str(peer) in item['reason'] for item in self.cleanup(run)['kept']))
                peer.unlink()

    def test_down_removes_nothing_more_after_a_failed_prune(self):
        for copies in (True, False):
            with self.subTest(copies=copies):
                label = 'with-copies' if copies else 'no-copies'
                self.pruning_lifecycle()
                run = self.up_run(label)
                stage = self.stage_copy(label)
                if not copies:  # a run with nothing left to prune still needs the lifecycle's own word
                    shutil.rmtree(run / 'work')
                    shutil.rmtree(run / 'state/snapshot-a')
                raw = run / 'out/r1/hit.1.capture.gif'
                raw.parent.mkdir(parents=True)
                raw.write_bytes(b'raw')
                clip = run / 'out/r1/hit.gif'
                clip.write_bytes(b'clip')
                (run / 'out/r1.json').write_text(json.dumps({'status': 'PASS', 'steps': [
                    {'gif': {'capture': raw.name, 'path': str(clip), 'sha256': sha(clip)}}]}))
                self.pruning_lifecycle(fail_prune=True)
                eden_test.main(['down', str(run)])
                self.assertTrue(raw.is_file())
                self.assertTrue(stage.is_dir())
                self.assertEqual((run / 'work').is_dir(), copies)
                record = self.cleanup(run)
                self.assertEqual(record['removed'], [])
                reasons = {item['what']: item['reason'] for item in record['kept']}
                self.assertIn('the lifecycle did not prune (exit 7)', reasons['raw capture r1/hit.1.capture.gif'])
                self.assertIn('the lifecycle did not prune (exit 7)', reasons['stage copy'])

    def test_down_removes_a_raw_capture_only_when_its_own_row_binds_it_to_a_verified_clip(self):
        self.pruning_lifecycle()
        run = self.up_run()
        out = run / 'out'
        outside = self.dir / 'repo/docs/media'
        outside.mkdir(parents=True)

        def capture(row, name, clip=None, digest=None, receipt=True):
            raw = out / row / f'{name}.123.capture.gif'
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b'raw ' + name.encode())
            if receipt:
                info = {'capture': raw.name, 'path': str(clip), 'sha256': digest or (sha(clip) if clip.is_file() else '0' * 64)}
                rows = json.loads((out / f'{row}.json').read_text()) if (out / f'{row}.json').exists() else {'steps': []}
                rows['steps'].append({'gif': info})
                (out / f'{row}.json').write_text(json.dumps(rows))
            return raw

        published = outside / 'hit.gif'
        published.write_bytes(b'published')
        bound = capture('r1', 'hit', published)
        row_clip = out / 'r1/menu.gif'
        row_clip.write_bytes(b'menu clip')
        bound_in_row = capture('r1', 'menu', row_clip)
        missing = capture('r2', 'gone', outside / 'gone.gif')
        changed = capture('r2', 'changed', published, digest='1' * 64)
        unbound = capture('r3', 'unbound', receipt=False)
        (out / 'r3.json').write_text(json.dumps({'steps': [{'gif': {'path': str(published), 'sha256': sha(published)}}]}))
        # Row r4's receipt names the capture file of row r5: another row's good clip proves nothing.
        other_row = capture('r5', 'other', receipt=False)
        (out / 'r4').mkdir()
        (out / 'r4.json').write_text(json.dumps({'steps': [
            {'gif': {'capture': other_row.name, 'path': str(published), 'sha256': sha(published)}}]}))
        linked_clip_dir = self.dir / 'repo/linked'
        linked_clip_dir.symlink_to(outside)
        via_link = capture('r6', 'vialink', linked_clip_dir / 'hit.gif')
        real_row = self.dir / 'elsewhere-row'
        real_row.mkdir()
        (real_row / 'x.123.capture.gif').write_bytes(b'raw x')
        (out / 'r7').symlink_to(real_row)  # a row folder that is a link
        (out / 'r7.json').write_text(json.dumps({'steps': [
            {'gif': {'capture': 'x.123.capture.gif', 'path': str(published), 'sha256': sha(published)}}]}))
        eden_test.main(['down', str(run)])
        self.assertFalse(bound.exists())
        self.assertFalse(bound_in_row.exists())
        self.assertEqual(published.read_bytes(), b'published')
        self.assertEqual(row_clip.read_bytes(), b'menu clip')
        for raw in (missing, changed, unbound, other_row, via_link, real_row / 'x.123.capture.gif'):
            self.assertTrue(raw.is_file(), raw)
        record = self.cleanup(run)
        removed = [item['what'] for item in record['removed']]
        self.assertIn('raw capture r1/hit.123.capture.gif', removed)
        self.assertIn('raw capture r1/menu.123.capture.gif', removed)
        reasons = {item['what']: item['reason'] for item in record['kept']}
        self.assertIn('missing or differs', reasons['raw capture r2/gone.123.capture.gif'])
        self.assertIn('missing or differs', reasons['raw capture r2/changed.123.capture.gif'])
        self.assertIn('no receipt of its own row', reasons['raw capture r3/unbound.123.capture.gif'])
        self.assertIn('no receipt of its own row', reasons['raw capture r5/other.123.capture.gif'])
        self.assertIn('missing or differs', reasons['raw capture r6/vialink.123.capture.gif'])
        self.assertIn('not a plain file in a plain row folder', reasons['raw capture r7/x.123.capture.gif'])

    def test_down_cleanup_opens_read_only_stage_trees_without_following_links(self):
        self.pruning_lifecycle()
        run = self.up_run()
        stage = self.stage_copy()
        outside = self.dir / 'outside-cache'
        outside.mkdir()
        (outside / 'kept.txt').write_text('cache')
        target = stage / 'targets' / ('a' * 40)
        (target / 'src').mkdir(parents=True)
        (target / 'src/lib.rs').write_text('fn main() {}')
        (target / 'cache-link').symlink_to(outside)
        os.chmod(target / 'src/lib.rs', 0o444)
        os.chmod(target / 'src', 0o555)
        os.chmod(target, 0o555)
        os.chmod(outside, 0o555)
        eden_test.main(['down', str(run)])
        self.assertFalse(stage.exists())
        self.assertEqual((outside / 'kept.txt').read_text(), 'cache')
        self.assertEqual(outside.stat().st_mode & 0o777, 0o555)  # nothing was changed through the link
        os.chmod(outside, 0o755)
        # A stage folder that is itself a link is not this run's folder.
        run2 = self.up_run('linked-stage')
        real = self.dir / 'real-stage'
        real.mkdir()
        (real / 'f').write_text('x')
        (eden_test.RUNS / '_eden-test/stage').mkdir(exist_ok=True)
        (eden_test.RUNS / '_eden-test/stage/linked-stage').symlink_to(real)
        eden_test.main(['down', str(run2)])
        self.assertEqual((real / 'f').read_text(), 'x')
        self.assertIn('not removed: symlink in target directory', self.cleanup(run2)['kept'][-1]['reason'])

    def test_down_keep_state_and_pending_release_clean_nothing(self):
        self.pruning_lifecycle()
        run = self.up_run()
        stage = self.stage_copy()
        eden_test.main(['down', str(run), '--keep-state'])
        self.assertTrue((run / 'work/profiles/profile-a/save.bin').is_file())
        self.assertTrue(stage.is_dir())
        self.assertEqual(self.cleanup(run)['skipped'], '--keep-state')
        self.assertEqual((run / 'lifecycle-down.log').read_text().count('$ '), 1)
        self.assertIn('- Cleanup: not run: --keep-state', (run / 'RESULTS.md').read_text())
        journal = json.loads((run / 'state/lifecycle.json').read_text())
        for change, why in (({'released': False, 'release_pending': True}, 'the ledger release is pending'),
                            ({'cleanup_failures': [{'key': 'a'}]}, 'cleanup failures'),
                            ({'released': False, 'claimed': True}, 'the claim is not released')):
            with self.subTest(change=change):
                self.assertIn(why, eden_test._unclean_down({**journal, **change}))
        self.assertIsNone(eden_test._unclean_down(journal))
        self.assertIn('does not say down', eden_test._unclean_down({**journal, 'phase': 'up'}))
        with mock.patch.object(eden_test, 'lifecycle', return_value=1):
            with self.assertRaisesRegex(SystemExit, 'the claim stays held'):
                eden_test.main(['down', str(run)])
        self.assertTrue((run / 'work').is_dir())
        self.assertTrue(stage.is_dir())
        self.assertEqual(self.cleanup(run)['skipped'], 'lifecycle down failed (1)')


class DownProcessTests(unittest.TestCase):
    """_process_reason on a modelled /proc: which processes keep a path at Down."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='eden-down-proc-'))
        self.proc = self.dir / 'proc'
        self.path = self.dir / 'runs/lr-14/work'
        self.path.mkdir(parents=True)
        self.addCleanup(lambda: (subprocess.run(['chmod', '-R', 'u+rwx', str(self.dir)], check=False),
                                 shutil.rmtree(self.dir)))

    def process(self, pid, name='worker', cmdline='worker\0--flag\0', cwd='/tmp', exe='/usr/bin/sleep', fds=('/dev/null',),
                state='S (sleeping)', parent=1):
        folder = self.proc / str(pid)
        (folder / 'fd').mkdir(parents=True)
        (folder / 'status').write_text(f'Name:\t{name}\nState:\t{state}\nPPid:\t{parent}\n')
        if cmdline is not None:
            (folder / 'cmdline').write_text(cmdline)
        if cwd:
            (folder / 'cwd').symlink_to(cwd)
        if exe:
            (folder / 'exe').symlink_to(exe)
        for number, target in enumerate(fds):
            (folder / 'fd' / str(number)).symlink_to(target)
        return folder

    def reason(self):
        return eden_test._process_reason([self.path], self.proc)

    def test_readable_processes_elsewhere_keep_nothing(self):
        self.process(101)
        self.process(102, state='Z (zombie)', cmdline='', cwd=None, exe=None, fds=())
        self.assertIsNone(self.reason())

    def test_a_process_in_the_path_keeps_it(self):
        for change in ({'cwd': str(self.path)}, {'fds': ('/dev/null', str(self.path / 'profiles/save.bin'))},
                       {'cmdline': f'tail\0-f\0{self.path}/log\0'}):
            with self.subTest(change=change):
                folder = self.process(110, **change)
                self.assertIn('process 110 (worker) uses', self.reason())
                shutil.rmtree(folder)
        self.process(111, cmdline=f'tail\0{self.path}-2/log\0')  # another folder whose name starts the same
        self.assertIsNone(self.reason())

    def test_a_process_that_cannot_be_inspected_keeps_it(self):
        folder = self.process(120)
        os.chmod(folder / 'fd', 0)
        self.assertEqual(self.reason(), 'process 120 (worker) cannot be inspected')
        os.chmod(folder / 'fd', 0o755)
        (folder / 'cmdline').unlink()  # its links are readable, its command line is not
        self.assertEqual(self.reason(), 'process 120 (worker) cannot be inspected')
        shutil.rmtree(folder)
        folder = self.process(121, name='ssh-agent', cmdline='/usr/bin/ssh-agent\0-D\0', cwd=None, exe=None, fds=())
        os.chmod(folder / 'fd', 0)  # a listed name, but its fd folder is not root-owned here
        self.assertEqual(self.reason(), 'process 121 (ssh-agent) cannot be inspected')

    def test_this_down_and_its_ancestors_are_not_counted(self):
        self.process(os.getpid(), cwd=str(self.path), parent=os.getppid())
        self.process(os.getppid(), cwd=str(self.path), parent=1)
        self.assertIsNone(self.reason())


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


class ReportStallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='eden-report-stall-')
        self.addCleanup(tmp.cleanup)
        self.run = Path(tmp.name)
        (self.run / 'rows').mkdir()
        (self.run / 'out' / 'r1').mkdir(parents=True)
        (self.run / 'state').mkdir()
        self.row = {'id': 'r1', 'steps': [{'run': 'routes:wireless_room'}]}
        (self.run / 'rows' / 'rows.json').write_text(json.dumps({'rows': [self.row]}))
        self.config = {
            'label': 'report-control', 'host': 'fake',
            'rows': {'sha256': sha(self.run / 'rows' / 'rows.json')},
            'instances': [{'key': 'a', 'app': {'path': '/fake', 'sha256': '0' * 64}}],
        }
        (self.run / 'config.json').write_text(json.dumps(self.config))
        (self.run / 'timing.json').write_text(json.dumps({'up_start': 1, 'up_done': 2}))

    def wireless_value(self, lost):
        fixture = runner_tests.WirelessRoomTests()
        fixture.setUp()
        try:
            fixture.io.form_screen = 'ldn.create'
            fixture.io.lost_stage_sends = lost
            with mock.patch.object(runner_tests.routes, '_stall_utc', return_value='2026-11-24T07:14:18+00:00'):
                value = fixture.run_room()
            path = fixture.row / 'stall-events.jsonl'
            journal = path.read_text() if path.exists() else ''
            return value, journal
        finally:
            fixture.doCleanups()

    def write_receipt(self, value, status='PASS', journal=''):
        receipt = {'id': 'r1', 'status': status, 'results': [
            {'run': 'routes:wireless_room', 'value': value}]}
        if status != 'PASS':
            receipt['error'] = 'route stopped'
        (self.run / 'out' / 'r1.json').write_text(json.dumps(receipt))
        if journal:
            (self.run / 'out' / 'r1' / 'stall-events.jsonl').write_text(journal)
        return receipt

    def report_row(self):
        return next(line for line in eden_test.report_md(self.run).splitlines() if line.startswith('| r1 |'))

    def summary(self):
        with redirect_stdout(io.StringIO()):
            eden_test.results_md(self.run, self.config)
        return (self.run / 'RESULTS.md').read_text()

    def test_report_clean_pass_stays_pass(self):
        value, journal = self.wireless_value(0)
        self.assertEqual(value['result'], 'PASS')
        self.assertEqual(value['stalls'], [])
        self.write_receipt(value, journal=journal)
        row = self.report_row()
        self.assertIn('| PASS |', row)
        self.assertNotIn('stall', row.lower())
        self.assertIn('1 PASS, 0 FAIL, 0 NOT RUN of 1', eden_test.report_md(self.run))
        self.assertNotIn('stall', self.summary().lower())

    def test_report_ignores_uncommitted_gate_candidates(self):
        folder = self.run / 'out' / 'r1'
        (folder / '.gate-candidate-unfinished.png').write_bytes(b'partial')
        self.assertEqual(eden_test.row_report(self.run, self.row, {})[0], 'NOT RUN')
        self.write_receipt({}, status='PASS')
        result, _, proof = eden_test.row_report(self.run, self.row, {})
        self.assertEqual(result, 'PASS')
        self.assertFalse(any('.gate-candidate-' in entry for entry in proof))
        (folder / 'committed.png').write_bytes(b'picture')
        self.assertTrue(any('committed.png' in entry for entry in eden_test.row_report(self.run, self.row, {})[2]))

    def test_clean_results_match_base_byte_for_byte(self):
        value, journal = self.wireless_value(0)
        self.write_receipt(value, journal=journal)
        expected = (
            '# report-control — 2026-11-24\n'
            '- Eden: /fake (000000000000…)\n'
            '- Plugins: \n'
            '- Rows: 1 PASS, 0 FAILED, 0 unrun; 0 earlier failed attempts retained (receipts in `out/`); '
            'final rows.json 4f87ae625307…; 0 runner manifests in `out/manifests/`\n'
            '- Down: cleanup_failures=None, released=None;\n'
            '- Prune: not run: work/ and state/ stay until a clean Down\n'
            '- Harvest: not run\n'
            '- Timing: up 1 s, rows 0 s over 1 run call(s), down 0 s; '
            'classified: up+setup+down 1 s, test 0 s, unclassified ? (incomplete timing)\n'
            '- Next: \n'
        )
        with mock.patch.object(eden_test.time, 'strftime', return_value='2026-11-24'):
            self.assertEqual(self.summary().encode(), expected.encode())

    def test_report_one_lost_input_shows_pass_with_1_stall(self):
        value, journal = self.wireless_value(1)
        self.assertEqual(value['result'], 'PASS with 1 stall')
        event = value['stalls'][0]
        self.assertEqual((event['kind'], event['step'], event['screen']), ('resend', 'stage-a', 'stage_select'))
        value['phases'] = {'stage': {'stalls': [dict(event)]}}
        receipt = self.write_receipt(value, journal=journal)
        row = self.report_row()
        self.assertIn('| PASS with 1 stall |', row)
        self.assertIn('stalls: 1:', row)
        self.assertIn('resend', row)
        self.assertIn('stage-a', row)
        self.assertIn('stage_select', row)
        self.assertEqual(len(eden_test.row_stalls(self.run, 'r1', receipt)), 1)
        self.assertEqual(eden_test.row_report(self.run, self.row, {})[0], 'PASS')
        self.assertEqual(len(eden_test.report_rows(self.run)[0]), 4)
        self.assertEqual(len(eden_test.report_rows_with_stalls(self.run)[0]), 5)
        self.assertEqual(eden_test.report_counts(self.run), {'PASS': 1, 'FAIL': 0, 'NOT RUN': 0})
        self.assertIn('(1 row passed with stalls, 1 stall in those rows)', eden_test.report_md(self.run))
        summary = self.summary()
        self.assertIn('PASS with 1 stall', summary)
        self.assertIn('(1 row passed with stalls, 1 stall in those rows)', summary)

    def test_report_two_distinct_stalls(self):
        value, journal = self.wireless_value(1)
        event = dict(value['stalls'][0], step='css-hand', send=2)
        value['stalls'].append(event)
        self.write_receipt(value, journal=journal)
        self.assertIn('| PASS with 2 stalls |', self.report_row())
        self.assertIn('css-hand', self.report_row())
        suffix = '(1 row passed with stalls, 2 stalls in those rows)'
        self.assertIn(suffix, eden_test.report_md(self.run))
        self.assertIn(suffix, self.summary())

    def test_report_stopped_row_is_never_relabelled_pass(self):
        value, journal = self.wireless_value(1)
        value['result'] = 'STOPPED with 1 stall'
        self.write_receipt(value, status='FAILED', journal=journal)
        row = self.report_row()
        self.assertIn('| FAIL |', row)
        self.assertNotIn('PASS', row)
        self.assertIn('stalls: 1:', row)
        self.assertEqual(eden_test.report_counts(self.run), {'PASS': 0, 'FAIL': 1, 'NOT RUN': 0})
        summary = self.summary()
        self.assertIn('FAILED r1', summary)
        self.assertNotIn('PASS with 1 stall', summary)

    def test_report_stall_only_in_the_jsonl_copy_still_counts(self):
        value, journal = self.wireless_value(1)
        value.pop('stalls')
        value.pop('result')
        self.write_receipt(value, journal=journal + '{bad json\n')
        row = self.report_row()
        self.assertIn('| PASS with 1 stall |', row)
        self.assertIn('unreadable', row)
        self.assertIn('stage-a', row)

    def test_shown_result(self):
        for result, stalls, shown in (
            ('PASS', [], 'PASS'), ('PASS', [{'step': 'stage-a'}], 'PASS with 1 stall'),
            ('FAIL', [{'step': 'stage-a'}], 'FAIL'), ('NOT RUN', [{'step': 'stage-a'}], 'NOT RUN'),
        ):
            with self.subTest(result=result, stalls=stalls):
                self.assertEqual(eden_test.shown_result(result, stalls), shown)

    def test_stall_details_are_not_clipped_by_the_reason_limit(self):
        value, _ = self.wireless_value(1)
        event = value['stalls'][0]
        value['stalls'] = [dict(event, step=f'step-{i:02d}') for i in range(30)]
        self.write_receipt(value)
        row = self.report_row()
        self.assertIn('| PASS with 30 stalls |', row)
        self.assertIn('step-29', row)

    def test_row_stalls_keep_first_seen_order_and_merge_the_block(self):
        value, journal = self.wireless_value(1)
        first = value['stalls'][0]
        second = dict(first, step='place')
        value['phases'] = {'phase': {'stalls': [dict(first), second]}}
        receipt = self.write_receipt(value, journal=journal)
        events = eden_test.row_stalls(self.run, 'r1', receipt)
        self.assertEqual([event['step'] for event in events], ['stage-a', 'place'])
        self.assertEqual(events[0]['block'], 'wireless_room')
        self.assertNotIn('block', first)

    def test_summary_limits_stall_rows_and_omits_clean_rows(self):
        value, journal = self.wireless_value(1)
        rows = ([{'id': f'r{i}', 'steps': self.row['steps']} for i in range(26)]
                + [{'id': f'fail{i}', 'steps': self.row['steps']} for i in range(6)]
                + [{'id': 'unrun', 'steps': self.row['steps']}])
        (self.run / 'rows' / 'rows.json').write_text(json.dumps({'rows': rows}))
        self.config['rows']['sha256'] = sha(self.run / 'rows' / 'rows.json')
        receipt = {'status': 'PASS', 'results': [{'run': 'routes:wireless_room', 'value': value}]}
        for row in rows[:25]:
            (self.run / 'out' / f'{row["id"]}.json').write_text(json.dumps(receipt))
        clean, _ = self.wireless_value(0)
        (self.run / 'out' / 'r25.json').write_text(json.dumps({
            'status': 'PASS', 'results': [{'run': 'routes:wireless_room', 'value': clean}]}))
        for i in range(6):
            (self.run / 'out' / f'fail{i}.json').write_text(json.dumps({'status': 'FAILED', 'error': 'stopped'}))
        summary = self.summary()
        self.assertIn('(25 rows passed with stalls, 25 stalls in those rows)', summary)
        for i in range(6):
            self.assertIn(f'  - PASS with 1 stall r{i}\n', summary)
        self.assertNotIn('  - PASS with 1 stall r6\n', summary)
        self.assertNotIn('  - PASS r25\n', summary)
        self.assertIn('  - and 19 more rows passed with stalls (see REPORT.md)\n', summary)
        self.assertEqual(len(summary.splitlines()), 23)

    def test_total_excludes_stalls_on_a_failed_row(self):
        value, journal = self.wireless_value(1)
        self.write_receipt(value, journal=journal)
        rows = [self.row, {'id': 'r2', 'steps': self.row['steps']}]
        (self.run / 'rows' / 'rows.json').write_text(json.dumps({'rows': rows}))
        self.config['rows']['sha256'] = sha(self.run / 'rows' / 'rows.json')
        failed_value = dict(value, stalls=[value['stalls'][0], dict(value['stalls'][0], utc='later')])
        stopped = {'status': 'FAILED', 'error': 'route stopped', 'results': [
            {'run': 'routes:wireless_room', 'value': failed_value}]}
        (self.run / 'out' / 'r2.json').write_text(json.dumps(stopped))
        report = eden_test.report_md(self.run)
        self.assertIn('(1 row passed with stalls, 1 stall in those rows)', report)
        self.assertIn('| r1 | wireless_room | PASS with 1 stall |', report)
        self.assertIn('| r2 | wireless_room | FAIL | stalls: 2:', report)
        self.assertIn('(1 row passed with stalls, 1 stall in those rows)', self.summary())

    def test_only_failed_stalls_do_not_add_passed_stall_totals(self):
        value, journal = self.wireless_value(1)
        self.write_receipt(value, status='FAILED', journal=journal)
        self.assertNotIn('passed with stalls', eden_test.report_md(self.run))
        self.assertNotIn('passed with stalls', self.summary())
        self.assertIn('| FAIL | stalls: 1:', self.report_row())

    def test_distinct_utc_events_count_twice_and_identical_events_once(self):
        value, _ = self.wireless_value(1)
        first = value['stalls'][0]
        second = dict(first, utc='2026-11-24T07:14:19+00:00')
        value['stalls'] = [first, second, dict(first)]
        receipt = self.write_receipt(value)
        events = eden_test.row_stalls(self.run, 'r1', receipt)
        self.assertEqual(len(events), 2)
        self.assertEqual([event['utc'] for event in events], [first['utc'], second['utc']])

    def assert_writer_event_identity(self, value, journal, block):
        event, = value['stalls']
        line, = [json.loads(text) for text in journal.splitlines()]
        self.assertEqual(line['block'], block)
        canonical = lambda item: json.dumps({key: val for key, val in item.items() if key != 'block'},
                                            sort_keys=True, default=str)
        self.assertEqual(canonical(event), canonical(line))
        self.assertEqual(line, {**event, 'block': block})
        receipt = self.write_receipt(value, journal=journal)
        self.assertEqual(len(eden_test.row_stalls(self.run, 'r1', receipt)), 1)

    def test_wireless_writer_event_matches_its_jsonl_copy(self):
        value, journal = self.wireless_value(1)
        self.assertEqual(value['stalls'][0]['kind'], 'resend')
        self.assert_writer_event_identity(value, journal, 'wireless_room')

    def test_tr_input_writer_event_matches_its_jsonl_copy(self):
        fixture = setup_tests.ReachTrainingClosedLoopTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        menu = setup_tests.retrying_menu(fixture.ctx, lambda: fixture.sleep(1))

        def inner(metrics):
            before = menu.read('tr.menu', 2)
            runner_tests.routes._tr_input(menu, before, 'tap-a1', fixture.io.now + 20)
            return {'block': 'inner', **metrics}

        with mock.patch.object(runner_tests.routes.time, 'monotonic', side_effect=lambda: fixture.io.now):
            value = runner_tests.routes._block(fixture.ctx, 'inner', 'inner', {}, inner)
        journal = (fixture.row / 'stall-events.jsonl').read_text()
        self.assertEqual(value['stalls'][0]['kind'], 'resend')
        self.assert_writer_event_identity(value, journal, 'tr_input')

    def test_reach_training_writer_event_matches_its_jsonl_copy(self):
        fixture = setup_tests.ReachTrainingClosedLoopTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.io.failures['pick_stage'] = 1
        value = fixture.invoke()
        journal = (fixture.row / 'stall-events.jsonl').read_text()
        self.assertEqual(value['stalls'][0]['kind'], 'phase-retry')
        self.assert_writer_event_identity(value, journal, 'reach_training')


class RerunTests(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp(prefix='eden-test-rerun-'))
        self.addCleanup(shutil.rmtree, self.run)
        (self.run / 'out/r1').mkdir(parents=True)
        (self.run / 'out/r1/sub.json').write_text('{}')
        (self.run / 'out/r1.json').write_text(json.dumps({'id': 'r1', 'status': 'PASS'}))
        (self.run / 'out/STOP-INPUT').write_text('r0')

    def test_bad_admission_preserves_stop_and_previous_receipt_and_row_folder(self):
        before_receipt = (self.run / 'out/r1.json').read_bytes()
        before_child = (self.run / 'out/r1/sub.json').read_bytes()
        with mock.patch.object(eden_test, 'sh', return_value=1) as sh, mock.patch.object(eden_test, 'cmd_pace') as pace:
            self.assertEqual(eden_test.cmd_run(self.run, retry=True, rows='r1'), 1)
        self.assertTrue((self.run / 'out/STOP-INPUT').is_file())
        self.assertEqual((self.run / 'out/STOP-INPUT').read_text(), 'r0')
        self.assertEqual((self.run / 'out/r1.json').read_bytes(), before_receipt)
        self.assertEqual((self.run / 'out/r1/sub.json').read_bytes(), before_child)
        self.assertFalse(list((self.run / 'out').glob('*.rerun-*')))
        self.assertEqual(sh.call_count, 1)
        self.assertIn('up-check', sh.call_args[0][0])
        pace.assert_not_called()

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

    def test_stop_capture_windows_identity(self):
        folder = self.run / 'out/bridge-run'
        folder.mkdir()
        (folder / 'capture.json').write_text(json.dumps({'pid': 42}))
        with mock.patch.object(eden_test.host_os, 'WINDOWS', True), \
                mock.patch.object(eden_test.host_os, 'argv_has') as has, \
                mock.patch.object(eden_test.os, 'kill') as kill:
            for value, expected in (
                    (None, {'pid': 42, 'stopped': False, 'why': 'not running'}),
                    (False, {'pid': 42, 'stopped': False, 'why': "that pid is not this run's capture"}),
                    (True, {'pid': 42, 'stopped': True})):
                has.return_value = value
                self.assertEqual(eden_test.stop_capture(self.run), expected)
                has.assert_called_with(42, 'capture-bridge', str(folder))
            kill.assert_called_once_with(42, signal.SIGTERM)

    def test_down_stops_every_instances_bridge_capture(self):
        import subprocess
        script = self.run / 'fake_capture.py'
        script.write_text('import time\ntime.sleep(60)\n')
        captures = []
        for name in ('bridge-run-a', 'bridge-run-b'):
            folder = self.run / 'out' / name
            folder.mkdir()
            process = subprocess.Popen([sys.executable, str(script), 'capture-bridge', str(folder)])
            self.addCleanup(lambda p=process: p.poll() is None and p.kill())
            (folder / 'capture.json').write_text(json.dumps({'pid': process.pid}))
            captures.append(process)
        results = eden_test.stop_captures(self.run)
        self.assertEqual([(r['pid'], r['stopped']) for r in results], [(p.pid, True) for p in captures])
        for process in captures:
            process.wait(timeout=10)
        self.assertEqual(eden_test.stop_captures(self.run / 'nowhere'), [])

class LaneControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / 'run'
        self.run.mkdir()
        self.runs_patch = mock.patch.object(eden_test, 'RUNS', self.root)
        self.runs_patch.start()
        self.addCleanup(self.runs_patch.stop)

    def test_fly_down_only_after_successful_up(self):
        counts = {'PASS': 1, 'FAIL': 0, 'NOT RUN': 0}
        for run_result, down, expected in ((0, False, 0), (0, True, 0), (3, True, 3),
                                           (RuntimeError('row failed'), True, 3)):
            with mock.patch.object(eden_test, 'cmd_up') as up, \
                    mock.patch.object(eden_test, 'cmd_run', side_effect=run_result if isinstance(run_result, Exception) else None,
                                      return_value=run_result), \
                    mock.patch.object(eden_test, 'cmd_report') as report, \
                    mock.patch.object(eden_test, 'report_counts', return_value=counts), \
                    mock.patch.object(eden_test, 'cmd_down') as finish, redirect_stdout(io.StringIO()):
                self.assertEqual(eden_test.cmd_fly(self.run, down=down), expected)
                up.assert_called_once_with(self.run, skip_setup_plan=True, allow_volatile_pins=True)
                report.assert_called_once_with(self.run)
                self.assertEqual(finish.call_count, int(down))
        with mock.patch.object(eden_test, 'cmd_up', side_effect=SystemExit('up failed')), \
                mock.patch.object(eden_test, 'cmd_down') as finish:
            with self.assertRaisesRegex(SystemExit, 'up failed'):
                eden_test.cmd_fly(self.run, down=True)
            finish.assert_not_called()
        with mock.patch.object(eden_test, 'cmd_up'), mock.patch.object(eden_test, 'cmd_run', return_value=0), \
                mock.patch.object(eden_test, 'cmd_report'), mock.patch.object(eden_test, 'report_counts', return_value=counts), \
                mock.patch.object(eden_test, 'cmd_down', side_effect=RuntimeError('down failed')), redirect_stdout(io.StringIO()):
            self.assertEqual(eden_test.cmd_fly(self.run, down=True, token='token'), 1)
        self.assertEqual(json.loads((self.run / 'go-result.json').read_text())['code'], 1)

    def test_detach_argv_and_active_refusal(self):
        argv = ['remote', '--host', 'xps', '--ssh', 'peer', '--label', 'demo', '--detach', 'fly', '--down']
        inactive = subprocess.CompletedProcess([], 3, 'inactive\n', '')
        launched = subprocess.CompletedProcess([], 0, '', '')
        with mock.patch.object(eden_test.host_os, 'WINDOWS', False), \
                mock.patch.object(eden_test.subprocess, 'run', side_effect=[inactive, launched]) as run, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(eden_test.main(argv), 0)
        command = run.call_args_list[1].args[0]
        log = self.root / '_eden-test/go/demo/go.log'
        self.assertEqual(command, ['systemd-run', '--user', '--unit', 'eden-go-demo', '--quiet', '--collect',
            '--expand-environment=no', '-p', 'MemoryMax=2G', '-p', 'TasksMax=512', '-p', 'RuntimeMaxSec=14400',
            '-p', 'KillMode=mixed', '-p', 'TimeoutStopSec=600', f'--working-directory={Path.cwd()}',
            '/usr/bin/python3', '-I', '-B', str(Path(eden_test.__file__).resolve()),
            'remote', '--host', 'xps', '--ssh', 'peer', '--label', 'demo', 'fly', '--down', '--go-log', str(log)])
        self.assertIn('eden_test.py peek --label demo', output.getvalue())
        self.assertIn('eden_test.py stop --host xps --ssh peer --label demo', output.getvalue())
        with mock.patch.object(eden_test.subprocess, 'run',
                               return_value=subprocess.CompletedProcess([], 0, 'active\n', '')) as run:
            with self.assertRaisesRegex(SystemExit, 'already active'):
                eden_test.main(argv)
            self.assertEqual(run.call_count, 1)
        with mock.patch.object(eden_test.host_os, 'WINDOWS', True):
            with self.assertRaisesRegex(SystemExit, 'Linux controller'):
                eden_test.main(argv)

    def test_go_log_records_running_and_ended(self):
        log = self.root / 'go/go.log'
        seen = []
        def remote(args):
            seen.append(json.loads((log.parent / 'status.json').read_text()))
            print('running command')
            return 3
        with mock.patch.object(eden_test, 'cmd_remote', side_effect=remote):
            self.assertEqual(eden_test.main(['remote', '--host', 'xps', '--label', 'demo',
                                             'fly', '--down', '--go-log', str(log)]), 3)
        self.assertEqual(seen[0]['phase'], 'running')
        self.assertEqual((seen[0]['label'], seen[0]['host']), ('demo', 'xps'))
        status = json.loads((log.parent / 'status.json').read_text())
        self.assertEqual((status['phase'], status['exit']), ('ended', 3))
        self.assertIn('ended', status)
        self.assertIn('running command', log.read_text())

    def test_go_log_passes_log_file_to_child(self):
        log = self.root / 'go/go.log'
        def child(argv, **kwargs):
            self.assertEqual(kwargs['stdout'].name, str(log))
            self.assertEqual(kwargs['stderr'], subprocess.STDOUT)
            kwargs['stdout'].write('child output\n')
            kwargs['stdout'].flush()
            return subprocess.CompletedProcess(argv, 0)
        def remote(args):
            return eden_test.sh(['child'], self.root / 'runner.log')
        with mock.patch.object(eden_test, 'cmd_remote', side_effect=remote), \
                mock.patch.object(eden_test.subprocess, 'run', side_effect=child):
            self.assertEqual(eden_test.main(['remote', '--host', 'xps', '--label', 'demo',
                                             'report', '--go-log', str(log)]), 0)
        self.assertIn('child output', log.read_text())
        self.assertIn('child output', (self.root / 'runner.log').read_text())

    def test_go_log_records_failure_status(self):
        log = self.root / 'go-fail/go.log'
        with mock.patch.object(eden_test, 'cmd_remote', side_effect=SystemExit('remote refused')):
            with self.assertRaisesRegex(SystemExit, 'remote refused'):
                eden_test.main(['remote', '--host', 'xps', '--label', 'demo',
                                'report', '--go-log', str(log)])
        self.assertEqual(json.loads((log.parent / 'status.json').read_text())['exit'], 1)
        self.assertIn('remote refused', log.read_text())
        with mock.patch.object(eden_test, 'cmd_remote', side_effect=RuntimeError('unexpected failure')):
            with self.assertRaisesRegex(RuntimeError, 'unexpected failure'):
                eden_test.main(['remote', '--host', 'xps', '--label', 'demo',
                                'report', '--go-log', str(log)])
        self.assertIn('RuntimeError: unexpected failure', log.read_text())

    def test_peek_reads_status_and_last_40_lines(self):
        folder = self.root / '_eden-test/go/demo'
        folder.mkdir(parents=True)
        (folder / 'status.json').write_text('{"phase":"running"}')
        (folder / 'go.log').write_text(''.join(f'line-{i}\n' for i in range(45)))
        done = subprocess.CompletedProcess([], 0, 'ActiveState=active\nSubState=running\nResult=success\n', '')
        with mock.patch.object(eden_test.subprocess, 'run', return_value=done) as run, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(eden_test.main(['peek', '--label', 'demo']), 0)
        self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'show', 'eden-go-demo', '-p',
                                                 'ActiveState', '-p', 'SubState', '-p', 'Result'])
        self.assertIn('line-5\n', out.getvalue())
        self.assertNotIn('line-4\n', out.getvalue())
        self.assertIn('ActiveState=active', out.getvalue())

    def test_stop_per_host(self):
        for host in ('xps', 'ally'):
            with mock.patch.object(eden_test, '_go_sh', return_value=0) as sh, redirect_stdout(io.StringIO()):
                self.assertEqual(eden_test.main(['stop', '--host', host, '--ssh', 'peer', '--label', 'demo']), 0)
            command = sh.call_args.args[0]
            if host == 'xps':
                self.assertEqual(command[-2:], ['/usr/bin/touch', '/home/intpa/eden-runs/demo/out/STOP'])
            else:
                script = base64.b64decode(command[-1].rsplit(' ', 1)[-1]).decode('utf-16le')
                self.assertEqual(script, 'New-Item -ItemType File -Force -Path '
                                         'C:/home/intpa/eden-runs/demo/out/STOP | Out-Null')

    def test_collect_linux_and_windows_and_reject_escape(self):
        output = self.root / 'dest'
        with mock.patch.object(eden_test, '_go_sh', return_value=0) as sh, redirect_stdout(io.StringIO()):
            self.assertEqual(eden_test.main(['collect', '--host', 'xps', '--ssh', 'peer', '--label', 'linux',
                                             '--to', str(output)]), 0)
        self.assertEqual(len(sh.call_args_list), len(eden_test._COLLECT_NAMES))
        self.assertTrue(all('--exclude=*.mkv' in c.args[0] and '--exclude=*.capture.gif' in c.args[0]
                            for c in sh.call_args_list))
        calls = []
        def remote(command):
            calls.append(command)
            if command[0] == 'scp':
                with tarfile.open(command[-1], 'w:gz') as tar:
                    data = b'config'
                    info = tarfile.TarInfo('config.json')
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            return 0
        with mock.patch.object(eden_test, '_go_sh', side_effect=remote), redirect_stdout(io.StringIO()):
            self.assertEqual(eden_test.main(['collect', '--host', 'ally', '--ssh', 'peer', '--label', 'win',
                                             '--to', str(output)]), 0)
        self.assertEqual((output / 'win/config.json').read_bytes(), b'config')
        self.assertEqual([c[0] for c in calls], ['ssh', 'scp', 'ssh'])
        script = base64.b64decode(calls[0][-1].rsplit(' ', 1)[-1]).decode('utf-16le')
        self.assertIn("$run='C:/home/intpa/eden-runs/win';", script)
        self.assertIn("--exclude='*.mkv'", script)
        self.assertIn("--exclude='*.capture.gif'", script)
        self.assertIn("'config.json','lifecycle-*.log'", script)
        calls.clear()
        with mock.patch.object(eden_test, '_go_sh', side_effect=remote), redirect_stdout(io.StringIO()):
            self.assertEqual(eden_test.main(['collect', '--host', 'ally', '--ssh', 'peer', '--label', 'films',
                                             '--to', str(output), '--films']), 0)
        script = base64.b64decode(calls[0][-1].rsplit(' ', 1)[-1]).decode('utf-16le')
        self.assertIn("$run='C:/home/intpa/eden-runs/films';", script)
        self.assertNotIn('--exclude=', script)
        bad = self.root / 'bad.tar.gz'
        with tarfile.open(bad, 'w:gz') as tar:
            item = tarfile.TarInfo('../escape')
            item.size = 1
            tar.addfile(item, io.BytesIO(b'x'))
        with self.assertRaisesRegex(ValueError, 'unsafe collect member'):
            eden_test._extract_collect(bad, output / 'win')
        with self.assertRaisesRegex(SystemExit, 'destination already exists'):
            eden_test.main(['collect', '--host', 'ally', '--ssh', 'peer', '--label', 'win', '--to', str(output)])


class WindowsControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.tools = self.dir / 'tools'
        self.tools.mkdir()
        (self.tools / 'tool.py').write_text('tool')
        self.rows = self.dir / 'rows'
        self.rows.mkdir()
        (self.rows / 'rows.json').write_text('{}')
        (self.rows / 'hook.py').write_text('hook')
        self.lineup = self.dir / 'lineup'
        self.lineup.mkdir()
        (self.lineup / 'card.nro').write_text('card')
        self.nro = self.dir / 'plugin.nro'
        self.nro.write_text('nro')
        self.elf = self.dir / 'plugin.so'
        self.elf.write_text('elf')
        self.args = types.SimpleNamespace(host='ally', label='demo', from_config=None, nro=[str(self.nro)],
            elf=str(self.elf), rows=str(self.rows), no_card_lineup='reason', trace_point=[],
            network_interface=None, no_lobby=False, owner='Claude devin/demo', ssh='ally-machine')
        self.mux = FakeMux('ally-machine')

    def decode(self, command):
        return base64.b64decode(command[-1].rsplit(' ', 1)[-1]).decode('utf-16le')

    def test_windows_remote_command_and_length(self):
        python = r'C:\Program Files\Python312\python.exe'
        command = eden_test._remote_command('host', self.mux, [python, '-u', 'a b', "it's"], True)
        self.assertEqual(command[:2], ['ssh', '-T'])
        self.assertEqual(self.decode(command),
                         "$env:EDEN_TEST_RELAY='1'; & 'C:\\Program Files\\Python312\\python.exe' -u "
                         "'a b' 'it''s'; exit $LASTEXITCODE")
        self.assertIn('-NoLogo -NoProfile -NonInteractive -EncodedCommand', command[-1])
        with self.assertRaisesRegex(SystemExit, r'remote command too long for the Windows host: \d+ characters'):
            eden_test._remote_command('host', self.mux, [python, 'x' * 6000], True)

    def test_windows_tar_layout(self):
        archive = self.dir / 'stage.tar.gz'
        with mock.patch.object(eden_test, 'PLAYBOOK', self.dir):
            eden_test._stage_tar(self.args, {'dir': str(self.lineup)}, archive)
        with tarfile.open(archive) as tar:
            self.assertTrue({'tools/tool.py', 'card-lineup/card.nro', 'nro/plugin.nro',
                             'elf/plugin.so', 'rows/rows.json', 'rows/hook.py'} <= set(tar.getnames()))
        self.args.rows = str(self.rows / 'rows.json')
        with mock.patch.object(eden_test, 'PLAYBOOK', self.dir):
            eden_test._stage_tar(self.args, None, archive)
        with tarfile.open(archive) as tar:
            self.assertIn('rows/rows.json', tar.getnames())
            self.assertNotIn('rows/hook.py', tar.getnames())

    def test_relay_dispatch_and_refusal(self):
        config = {'host': 'ally', 'label': 'demo'}
        requests = [
            {'id': 'a', 'kind': 'lifecycle', 'mode': 'up', 'config': config},
            {'id': 'b', 'kind': 'claims', 'config': config},
            {'id': 'c', 'kind': 'lifecycle', 'mode': 'down', 'config': {**config, 'label': 'other'}},
            {'id': 'd', 'kind': 'lifecycle', 'mode': 'status', 'config': config},
        ]
        process = types.SimpleNamespace(stdout=io.StringIO('ordinary\n' + ''.join(
            eden_test.RELAY_PREFIX + json.dumps(r) + '\n' for r in requests)), stdin=io.StringIO(),
            wait=lambda: 7)
        forward = mock.Mock(side_effect=[{'ok': True}, RuntimeError('backend refused')])
        claims = mock.Mock(return_value=['line'])
        popen = mock.Mock(return_value=process)
        output = io.StringIO()
        with redirect_stdout(output):
            result = eden_test._relay_run(['ssh', 'host'], 'host', self.args, popen, forward, claims)
        self.assertEqual(result, 7)
        self.assertTrue(output.getvalue().startswith('ordinary\n'))
        replies = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(replies, [{'id': 'a', 'ok': True, 'result': {'ok': True}},
                                   {'id': 'b', 'ok': True, 'result': ['line']},
                                   {'id': 'c', 'ok': False, 'error': 'relay: foreign run'},
                                   {'id': 'd', 'ok': False, 'error': 'backend refused'}])
        forward.assert_any_call('host', 'up', config, True, keep_state=False, evidence_dir=None)
        forward.assert_any_call('host', 'status', config, False, keep_state=False, evidence_dir=None)
        claims.assert_called_once_with(config)
        self.assertEqual(popen.call_args.kwargs, {'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE,
            'stderr': subprocess.STDOUT, 'text': True, 'encoding': 'utf-8', 'errors': 'replace'})

    def test_windows_staging_and_fetch_commands(self):
        token = 'd' * 32
        commands = []
        relay_commands = []
        root = 'C:/home/intpa/eden-runs'
        stage = root + '/_eden-test/stage/demo'
        def sh(command):
            commands.append(command)
            if command[0] == 'scp' and command[-1].endswith('/stage.tar.gz'):
                with tarfile.open(command[-2]) as tar:
                    self.assertIn('tools/tool.py', tar.getnames())
                    self.assertIn('rows/hook.py', tar.getnames())
            if command[0] == 'scp' and command[-2].endswith('/go-result.json'):
                Path(command[-1]).write_text(json.dumps({'token': token, 'code': 0, 'counts': {}}))
            return 0
        def relay(command, host, args):
            relay_commands.append(command)
            return 0
        with chdir(self.dir), mock.patch.object(eden_test, 'PLAYBOOK', self.dir), \
                mock.patch.object(eden_test, '_go_sh', side_effect=sh), \
                mock.patch.object(eden_test, '_relay_run', side_effect=relay), \
                mock.patch.object(eden_test.uuid, 'uuid4', return_value=types.SimpleNamespace(hex=token)), \
                mock.patch.object(eden_test._lifecycle_module, 'ssh_master_dead', return_value=False), \
                redirect_stdout(io.StringIO()):
            code = eden_test._go_remote_with_mux(self.args, None, 'ally-machine', self.mux, [], self.dir)
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in commands], ['ssh', 'scp', 'ssh', 'scp', 'scp'])
        self.assertEqual(self.decode(commands[0]),
                         f'New-Item -ItemType Directory -Force -Path {stage} | Out-Null')
        self.assertIn(f'tar.exe -xzf {stage}/stage.tar.gz -C {stage}', self.decode(commands[2]))
        self.assertEqual(commands[1][-1], f'ally-machine:{stage}/stage.tar.gz')
        self.assertEqual(commands[3][-2], f'ally-machine:{root}/demo/go-result.json')
        self.assertEqual(commands[4][-2], f'ally-machine:{root}/demo/REPORT.md')
        self.assertIn(f"{stage}/tools/eden-test/eden_test.py go --host ally --label demo", self.decode(relay_commands[0]))

    def test_windows_target_pack_uses_scp_and_windows_helper(self):
        commit, token = 'a' * 40, 'b' * 32
        stage = 'C:/home/intpa/eden-runs/_eden-test/stage/demo'
        commands = []
        def sh(command):
            commands.append(command)
            if command[0] == 'scp' and command[-2].endswith('/go-result.json'):
                Path(command[-1]).write_text(json.dumps({'token': token, 'code': 0, 'counts': {}}))
            return 0
        def remote(host, mux, argv, timeout, windows=False):
            self.assertTrue(windows)
            self.assertEqual(argv[:5], [r'C:\Program Files\Python312\python.exe', '-u', '-X', 'utf8', '-B'])
            receipt = {'commit': commit, 'cached': False} if '--probe' in argv else {'commit': commit, 'cache': 'built'}
            if '--probe' not in argv:
                self.assertEqual(argv[-2:], ['--pack', f'{stage}/targets/.incoming/{commit}.pack'])
            return 0, json.dumps(receipt), ''
        def pack(tree, wanted, folder):
            path = Path(folder) / 'fixture.pack'
            path.write_bytes(b'pack')
            return {'pack_file': str(path)}
        targets = [{'tree': str(self.dir), 'commit': commit, 'label': 'plugin'}]
        with chdir(self.dir), mock.patch.object(eden_test, 'PLAYBOOK', self.dir), \
                mock.patch.object(eden_test, '_go_sh', side_effect=sh), \
                mock.patch.object(eden_test, '_relay_run', return_value=0), \
                mock.patch.object(eden_test, '_go_target_ssh', side_effect=remote) as helper, \
                mock.patch.object(eden_test, 'pack_target', side_effect=pack), \
                mock.patch.object(eden_test.uuid, 'uuid4', return_value=types.SimpleNamespace(hex=token)), \
                mock.patch.object(eden_test._lifecycle_module, 'ssh_master_dead', return_value=False), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(eden_test._go_remote_with_mux(self.args, None, 'ally-machine',
                                                           self.mux, targets, self.dir), 0)
        self.assertEqual(helper.call_count, 2)
        pack_scp = [c for c in commands if c[0] == 'scp' and c[-1].endswith('.pack')]
        self.assertEqual(len(pack_scp), 1)
        self.assertEqual(pack_scp[0][-1], f'ally-machine:{stage}/targets/.incoming/{commit}.pack')
        self.assertTrue(any(c[0] == 'ssh' and '/targets/.incoming' in self.decode(c) for c in commands))

    def test_remote_prepare_forwards_options_without_fetching_report(self):
        self.args.prepare = True
        self.args.down = True
        self.args.eden_setting = [('core.volume', 0), ('core.resolution_setup', 3)]
        with mock.patch.object(eden_test, 'PLAYBOOK', self.dir), \
                mock.patch.object(eden_test, '_go_sh', return_value=0) as sh, \
                mock.patch.object(eden_test, '_relay_run', return_value=0) as relay, \
                mock.patch.object(eden_test._lifecycle_module, 'ssh_master_dead', return_value=False), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(eden_test._go_remote_with_mux(self.args, None, 'ally-machine',
                                                           self.mux, [], self.dir), 0)
        self.assertEqual([c.args[0][0] for c in sh.call_args_list], ['ssh', 'scp', 'ssh'])
        script = self.decode(relay.call_args.args[0])
        self.assertIn('--prepare --down --eden-setting core.volume=0 --eden-setting core.resolution_setup=3', script)
        self.assertIn('eden_test.py remote --host ally --label demo fly --down', out.getvalue())

    def test_windows_remote_base(self):
        self.args.from_config = 'C:/home/intpa/eden-runs/_eden-test/stage/demo/base.json'
        self.assertEqual(eden_test._go_remote_base(self.args), self.args.from_config)
        self.args.from_config = '/home/intpa/eden-runs/_eden-test/stage/demo/base.json'
        with self.assertRaises(SystemExit):
            eden_test._go_remote_base(self.args)

    def test_remote_command_per_host(self):
        for host in ('ally', 'xps'):
            args = types.SimpleNamespace(host=host, label='demo', ssh='target', remote_mode='run',
                                         remote_args=['--rows', 'r1'])
            with mock.patch.object(eden_test, '_go_sh', return_value=7) as sh, \
                    mock.patch.object(eden_test, '_relay_run', return_value=8) as relay:
                self.assertEqual(eden_test.cmd_remote(args), 8 if host == 'ally' else 7)
            if host == 'ally':
                script = self.decode(relay.call_args.args[0])
                self.assertIn('C:/home/intpa/eden-runs/demo --rows r1', script)
            else:
                command = sh.call_args.args[0]
                self.assertEqual(command[-8:], ['/usr/bin/python3', '-I', '-B',
                    '/home/intpa/eden-runs/_eden-test/stage/demo/tools/eden-test/eden_test.py',
                    'run', '/home/intpa/eden-runs/demo', '--rows', 'r1'])


class WindowsHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        (self.run / 'config.json').write_text(json.dumps({'host': 'ally', 'label': 'demo'}))

    def test_python_per_os(self):
        with mock.patch.object(eden_test.host_os, 'WINDOWS', False):
            self.assertEqual(eden_test._python(), [sys.executable, '-B', '-I'])
        with mock.patch.object(eden_test.host_os, 'WINDOWS', True):
            self.assertEqual(eden_test._python(), [sys.executable, '-B', '-X', 'utf8'])

    def test_windows_down_honors_path_pins(self):
        root = PureWindowsPath('C:/home/intpa/eden-runs/run-one')
        work = root / 'work'
        peer = self.run / 'peer.json'
        peer.write_text(json.dumps({'source': root.as_posix().upper()}))
        with mock.patch.object(eden_test.host_os, 'WINDOWS', True), \
                mock.patch.object(eden_test, '_pinning_files', return_value=[peer]), \
                mock.patch.object(eden_test, '_process_reason', return_value=None):
            guard = eden_test._DownGuard(self.run, self.run, [])
            self.assertIn('peer.json names', guard.reason(work) or '')
            (self.run / '.keep').write_text(work.as_posix().upper() + '\n')
            peer.write_text(json.dumps({'source': (root.parent / 'run-one-more').as_posix()}))
            guard = eden_test._DownGuard(self.run, self.run, [])
            self.assertIn('.keep lists', guard.reason(work) or '')
            self.assertIsNone(guard.reason(root / 'work-two'))

    @unittest.skipUnless(os.name == 'nt', 'live Windows file sharing')
    def test_windows_down_keeps_a_file_open_without_a_path_in_argv(self):
        path = self.run / 'work'
        path.mkdir()
        leaf = path / 'profile.dat'
        leaf.write_bytes(b'x')
        with mock.patch.object(eden_test.host_os, 'windows_processes', return_value=[]):
            with leaf.open('rb'):
                self.assertIsNotNone(eden_test._process_reason([path]))
            self.assertIsNone(eden_test._process_reason([path]))

    def test_report_uses_controller_follow_up_commands(self):
        with mock.patch.object(eden_test, 'report_rows_with_stalls', return_value=[]), \
                mock.patch.object(eden_test, 'time_split', return_value={'setup': 0, 'test': 0}), \
                mock.patch.object(eden_test, 'card_lineup_report', return_value='lineup'), \
                mock.patch.object(eden_test, 'nro_lines', return_value=[]):
            report = eden_test.report_md(self.run)
        self.assertIn('eden_test.py remote --host ally --label demo down', report)
        self.assertIn('eden_test.py remote --host ally --label demo run --rows <id>', report)

    def test_relay_requires_opt_in_and_checks_reply_id(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, 'served by the Linux controller'):
                eden_test._relay({'kind': 'claims'})
        with mock.patch.dict(os.environ, {'EDEN_TEST_RELAY': '1'}), \
                mock.patch.object(eden_test.uuid, 'uuid4', return_value=types.SimpleNamespace(hex='one')):
            for text, valid in (('{"id":"one","ok":true}\n', True), ('{"id":"other"}\n', False),
                                ('[]\n', False), ('not json\n', False), ('', False)):
                out = io.StringIO()
                with mock.patch.object(sys, 'stdin', io.StringIO(text)), redirect_stdout(out):
                    if valid:
                        self.assertTrue(eden_test._relay({'kind': 'claims'})['ok'])
                    else:
                        with self.assertRaisesRegex(SystemExit, '^relay: invalid reply$'):
                            eden_test._relay({'kind': 'claims'})
                lines = out.getvalue().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith(eden_test.RELAY_PREFIX))
                self.assertEqual(json.loads(lines[0][len(eden_test.RELAY_PREFIX):]), {'kind': 'claims', 'id': 'one'})

    def test_lifecycle_relay(self):
        config = {'host': 'ally', 'label': 'demo'}
        cases = [('up', (), {'ok': True, 'result': {}}, 0),
                 ('up', (), {'ok': False, 'error': 'refused'}, 1),
                 ('preflight', (), {'ok': True, 'result': {'ok': False}}, 1),
                 ('down', ('--keep-state', '--evidence-dir', 'evidence'), {'ok': True, 'result': {}}, 0)]
        with mock.patch.object(eden_test.host_os, 'WINDOWS', True), \
                mock.patch.object(eden_test, '_relay') as relay, redirect_stdout(io.StringIO()):
            for mode, extra, reply, code in cases:
                relay.return_value = reply
                result = eden_test.lifecycle(self.run, mode, *extra)
                self.assertEqual(result, code)
                if reply.get('error'):
                    self.assertEqual(result.stderr, reply['error'])
                relay.assert_called_with({'kind': 'lifecycle', 'mode': mode, 'config': config,
                                          'keep_state': '--keep-state' in extra,
                                          'evidence_dir': 'evidence' if mode == 'down' else None})
                log = (self.run / f'lifecycle-{mode}.log').read_text()
                self.assertIn(f'$ relay lifecycle {mode}\n{json.dumps(reply, indent=2)}\n', log)

    def test_open_claims_relay(self):
        config = {'host': 'ally'}
        with mock.patch.object(eden_test.host_os, 'WINDOWS', True), \
                mock.patch.object(eden_test, '_relay', return_value={'ok': True, 'result': ['one']}) as relay:
            self.assertEqual(eden_test._open_claims(self.run, config), ['one'])
            relay.assert_called_once_with({'kind': 'claims', 'config': config})
            relay.return_value = {'ok': False, 'error': 'unavailable'}
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                eden_test._open_claims(self.run, config)

    def test_windows_unit_results(self):
        state = self.run / 'state'
        state.mkdir()
        (state / 'lifecycle.json').write_text(json.dumps({'instances': [{'key': x} for x in ('a', 'b', 'c')]}))
        for key, value in (('a', True), ('b', False)):
            folder = state / f'run-{key}'
            folder.mkdir()
            (folder / 'run.json').write_text(json.dumps({'owned_pids_stopped': {'job_terminated': value}}))
        with mock.patch.object(eden_test.host_os, 'WINDOWS', True):
            self.assertEqual(eden_test.unit_results(self.run), ['a: Windows Job terminated by Down',
                'b: the Job was already gone at Down', 'c: no stop record'])

    def test_windows_process_reason(self):
        path = Path('C:/home/intpa/eden-runs/r1/work')
        processes = [
            {'pid': 50, 'ppid': 49, 'name': 'me', 'command': str(path), 'exe': None},
            {'pid': 49, 'ppid': 1, 'name': 'parent', 'command': str(path), 'exe': None},
            {'pid': 40, 'ppid': 1, 'name': 'other', 'command': r'C:\home\intpa\eden-runs\r10\work', 'exe': None},
            {'pid': 41, 'ppid': 1, 'name': 'hidden', 'command': None, 'exe': None},
        ]
        with mock.patch.object(eden_test.host_os, 'WINDOWS', True), \
                mock.patch.object(eden_test.os, 'getpid', return_value=50), \
                mock.patch.object(eden_test.host_os, 'windows_processes', return_value=processes) as get:
            self.assertIsNone(eden_test._process_reason([path]))
            processes.append({'pid': 42, 'ppid': 1, 'name': 'owner',
                              'command': r'OPEN C:\HOME\INTPA\EDEN-RUNS\R1\WORK\x', 'exe': None})
            self.assertEqual(eden_test._process_reason([path]), f'process 42 (owner) uses {path}')
            processes[-1]['command'] = None
            processes[-1]['exe'] = r'C:\home\intpa\eden-runs\r1\work\eden.exe'
            self.assertEqual(eden_test._process_reason([path]), f'process 42 (owner) uses {path}')
            get.side_effect = OSError('process list unavailable')
            with self.assertRaisesRegex(OSError, 'process list unavailable'):
                eden_test._process_reason([path])


if __name__ == '__main__':
    unittest.main(verbosity=2)
