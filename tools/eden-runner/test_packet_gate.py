#!/usr/bin/env python3
"""Offline packet gate tests with a temporary committed source tree."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'eden-test'))
import packet_gate  # noqa: E402
import eden_test  # noqa: E402

PRACTICE_STATE = r'''
writeln!(out, "phase={phase}");
("wizard.step", fields.wizard_step);
writeln!(out, "wizard.x={x}\nwizard.y={y}");
pub const CPU_ENTRIES: usize = 8;
pub fn snapshot(game: Option<Game>, fields: Fields, thinks: [u64; CPU_ENTRIES], out: &mut String) {
    for (key, value) in [
        ("step", fields.step),
        ("wizard.selection", fields.wizard_selection),
    ] {
        if !value.is_empty() {
            let _ = writeln!(out, "{key}={value}");
        }
    }
    for (key, value) in [("score", fields.score), ("attempt.frames", fields.attempt_frames)] {
        if let Some(value) = value {
            let _ = writeln!(out, "{key}={value}");
        }
    }
    for (entry, count) in thinks.into_iter().enumerate().skip(1) {
        let _ = writeln!(out, "cpu.{entry}.ai_thinks={count}");
    }
}
pub fn lanes(slots: &[u64], name: &str, out: &mut String) {
    for (slot, value) in slots.iter().enumerate() {
        let _ = writeln!(out, "slot.{slot}.value={value}");
    }
    let _ = writeln!(out, "named.{name}.value=1");
    for lane in 2..=3 {
        let _ = writeln!(out, "lane.{lane}.frames={}", 0);
    }
    for (group, kind) in [("player", "left"), ("partner", "right")] {
        let _ = writeln!(out, "{group}.{kind}=1");
    }
    for half in ["top", "bottom"] {
        for edge in ["in", "out"] {
            let _ = writeln!(out, "{half}.{edge}=1");
        }
    }
    for (rank, medal) in ["gold", "silver"].into_iter().enumerate() {
        let _ = writeln!(out, "medal.{rank}.{medal}=1");
    }
}
'''

# Bridge 95abb28, src/native_ui/mod.rs (excerpt, see tools/PROVENANCE.md): the first bridge whose state-block file
# has a `#[cfg(test)]` item in the middle, a test-only fn inside `impl Sample`, in front of the real writer.
UI_95ABB28 = (Path(__file__).resolve().parent / 'test-data/bridge-ui/95abb28/src/native_ui/mod.rs').read_text()


class PacketGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / 'rows'
        self.base.mkdir()
        self.tree = Path(self.tmp.name) / 'source'
        self.tree.mkdir()
        files = {
            'src/model/bridge_state.rs': r'''
writeln!(out, "ack={}", ack);
push_str(out, "page", view.page);
out.push_str("strip.aims=\"");
for key in ["zone.bins", "zone.y", "zone.left", "zone.right"] {}
write!(out, "rows={rows}");
#[cfg(test)]
mod tests {
    fn line(out: &mut String, n: u32) {
        writeln!(out, "test.only={}", n);
    }
}
''',
            'src/model/record_place.rs': 'writeln!(out, "recording.page.role={}", role);\n',
            'src/model/practice_state.rs': PRACTICE_STATE,
            'src/model/bridge_events.rs': r'''
write!(text, "fed p1={} cpu={} run.p1={} run.cpu={} preview={} hides={}", a, b, c, d, e, f);
write!(text, "hold dir={dir} polls={polls} fires={fires} jump={}");
write!(text, "room.panel row={row} index={index}");
write!(text, "room.panel applied partners={partners} behavior={behavior}");
write!(text, "clip ctx={} total={} in={} out={}");
for side in ["p1", "cpu"] {
    write!(text, "seat owner.{side}={}", 1);
}
''',
            'src/proto2.rs': ('writeln!(out, "screen={}", screen);\n'
                              'writeln!(out, "ui.focus={}", focus);\n'),
        }
        for name, content in files.items():
            path = self.tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git('init', '-q')
        self.git('add', 'src')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'fixture')
        self.commit = self.git('rev-parse', 'HEAD').stdout.strip()
        (self.base / 'settings.json').write_text(json.dumps({'menu': {'play_player': [
            0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0]}}))
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return True\n')
        self.plan = {
            'target': {'plugin': {'tree': str(self.tree), 'commit': self.commit, 'nro_sha256': 'a' * 64},
                       'bridge': {'tree': str(self.tree), 'commit': self.commit}, 'settings': 'settings.json'},
            'setup': [], 'segments': {}, 'gates': {},
            'rows': [{'id': 'r1', 'steps': [{'run': 'steps.py:ok'}]}],
        }

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.tree), *args], check=True, capture_output=True, text=True)

    def errors(self):
        return packet_gate.check(self.plan, self.base, self.plan)['errors']

    def target_run_layout(self):
        runs = Path(self.tmp.name) / 'runs'
        target = runs / 'run-01' / 'rows'
        target.parent.mkdir(parents=True)
        self.base.rename(target)
        self.base = target
        def cleanup():
            for directory, names, _ in os.walk(runs, topdown=True, followlinks=False):
                path = Path(directory)
                path.chmod(path.stat().st_mode | 0o700)
                names[:] = [name for name in names if not (path / name).is_symlink()]
        self.addCleanup(cleanup)
        return runs

    def test_staged_target_supplies_reads_and_preserves_packet_identity(self):
        runs = self.target_run_layout()
        self.plan['rows'][0]['reads'] = ['tr.menu:ack']
        before = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(before['errors'], [])
        tracked = self.tree / 'src/model/bridge_state.rs'
        tracked.write_text(tracked.read_text() + '\n// producer work in progress\n')
        dirty = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(dirty['errors'], [])
        packed = eden_test.pack_target(self.tree, self.commit, Path(self.tmp.name) / 'packs')
        receipt = eden_test.stage_target(self.commit, 'run-01', packed['pack_file'], runs=runs)
        staged = Path(receipt['staged'])
        (self.base.parent / 'config.json').write_text(json.dumps({'staged_targets': [self.commit]}))
        with mock.patch.object(packet_gate, '_git', wraps=packet_gate._git) as git:
            after = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(after['errors'], [])
        self.assertEqual((after['packet_sha256'], after['inputs_identity']),
                         (before['packet_sha256'], before['inputs_identity']))
        self.assertIn('ack', after['emitted']['tr.menu'])
        self.assertTrue(any(Path(call.args[0]) == staged and call.args[1] == 'show'
                            for call in git.call_args_list))
        missing = str(Path(self.tmp.name) / 'not-on-this-host')
        self.plan['target']['plugin']['tree'] = missing
        self.plan['target']['bridge']['tree'] = missing
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['target']['plugin']['tree'], missing)
        self.assertIn('ack', result['emitted']['tr.menu'])
        self.plan['rows'][0]['unrelated_path'] = missing
        self.assertIn(f'G4 rows.json: missing file {missing}', self.errors())

    def test_staged_head_mismatch_refuses_even_when_requested_object_exists(self):
        runs = self.target_run_layout()
        original = self.commit
        (self.tree / 'new.txt').write_text('new commit')
        self.git('add', 'new.txt')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'new')
        other = self.git('rev-parse', 'HEAD').stdout.strip()
        stage = runs / '_eden-test' / 'stage' / 'run-01' / 'targets' / original
        stage.parent.mkdir(parents=True)
        subprocess.run(['git', 'clone', '-q', '--no-hardlinks', str(self.tree), str(stage)],
                       check=True, capture_output=True)
        subprocess.run(['git', '-C', str(stage), 'cat-file', '-e', original],
                       check=True, capture_output=True)
        errors = self.errors()
        self.assertIn(f'G2 target: plugin staged tree {stage} is at {other}, not {original}', errors)

    def test_staged_tracked_untracked_and_ignored_changes_refused(self):
        runs = self.target_run_layout()
        (self.tree / '.gitignore').write_text('ignored.tmp\n')
        self.git('add', '.gitignore')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'ignore')
        commit = self.git('rev-parse', 'HEAD').stdout.strip()
        self.plan['target']['plugin']['commit'] = commit
        self.plan['target']['bridge']['commit'] = commit
        packed = eden_test.pack_target(self.tree, commit, Path(self.tmp.name) / 'packs')
        receipt = eden_test.stage_target(commit, 'run-01', packed['pack_file'], runs=runs)
        staged = Path(receipt['staged'])
        staged.chmod(staged.stat().st_mode | 0o700)
        for name in ('untracked.tmp', 'ignored.tmp'):
            path = staged / name
            path.write_text('new')
            self.assertTrue(any(error.startswith(f'G2 target: plugin staged tree {staged} changed after staging:')
                                for error in self.errors()), name)
            path.unlink()
        tracked = staged / 'src/model/bridge_state.rs'
        tracked.chmod(tracked.stat().st_mode | 0o200)
        tracked.write_text('edited\n')
        self.assertTrue(any(error.startswith(f'G2 target: plugin staged tree {staged} changed after staging:')
                            for error in self.errors()))

    def test_missing_producer_names_absent_staged_tree(self):
        self.target_run_layout()
        missing = str(Path(self.tmp.name) / 'missing')
        self.plan['target']['plugin']['tree'] = missing
        self.assertIn(f'G2 target: plugin.tree {missing} does not exist on this host and '
                      f'the run has no staged tree for {self.commit}', self.errors())

    def test_staged_tree_through_a_symlink_is_not_adopted(self):
        runs = self.target_run_layout()
        packed = eden_test.pack_target(self.tree, self.commit, Path(self.tmp.name) / 'packs')
        eden_test.stage_target(self.commit, 'run-02', packed['pack_file'], runs=runs)
        stage = runs / '_eden-test' / 'stage'
        (stage / 'run-01').symlink_to(stage / 'run-02', target_is_directory=True)
        staged = stage / 'run-01' / 'targets' / self.commit
        self.assertIn(f'G2 target: plugin staged tree {staged} cannot be read: symlink in staged path',
                      self.errors())

    def test_staged_target_supplies_format_keys_and_refusal_hints(self):
        runs = self.target_run_layout()
        packed = eden_test.pack_target(self.tree, self.commit, Path(self.tmp.name) / 'packs')
        eden_test.stage_target(self.commit, 'run-01', packed['pack_file'], runs=runs)
        missing = str(Path(self.tmp.name) / 'unavailable-producer')
        self.plan['target']['plugin']['tree'] = missing
        self.plan['target']['bridge']['tree'] = missing
        self.plan['rows'][0]['reads'] = ['tr.practice:cpu.4.ai_thinks']
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertIn('cpu.4.ai_thinks', result['emitted']['tr.practice'])
        self.plan['rows'][0]['reads'] = ['tr.practice:cpu.8.ai_thinks']
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('writes cpu.{entry}.ai_thinks with entry = 1 to 7', errors[0])

    def required_targets(self):
        runs = self.target_run_layout()
        first = self.commit
        (self.tree / 'second.txt').write_text('second')
        self.git('add', 'second.txt')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'second')
        second = self.git('rev-parse', 'HEAD').stdout.strip()
        self.plan['target']['bridge']['commit'] = second
        stage = runs / '_eden-test' / 'stage' / 'run-01' / 'targets'
        for commit in (first, second):
            pack = eden_test.pack_target(self.tree, commit, Path(self.tmp.name) / 'packs')
            eden_test.stage_target(commit, 'run-01', pack['pack_file'], runs=runs)
        (self.base.parent / 'config.json').write_text(json.dumps({'staged_targets': [first, second]}))
        return runs, stage, first, second

    def test_missing_one_required_target_never_falls_back_to_existing_producer(self):
        runs, stage, first, second = self.required_targets()
        self.plan['target']['providers'] = {'custom.bridge': {'repo': 'bridge', 'files': ['src/proto2.rs']}}
        self.plan['rows'][0]['reads'] = ['custom.bridge:screen']
        self.assertEqual(self.errors(), [])
        eden_test._remove_target_tree(stage / first)
        with mock.patch.object(packet_gate, '_git', wraps=packet_gate._git) as git:
            result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [
            f'G2 target: plugin staged tree {stage / first} is missing; this run gates staged targets only '
            f'(stage-target --commit {first} --label run-01)'])
        self.assertIn('screen', result['emitted']['custom.bridge'])
        self.assertTrue(any(Path(call.args[0]) == stage / second and call.args[1] == 'show'
                            for call in git.call_args_list))
        self.assertFalse(any(Path(call.args[0]) == self.tree for call in git.call_args_list))

    def test_unstage_all_required_targets_never_falls_back_to_existing_producer(self):
        runs, stage, first, second = self.required_targets()
        eden_test.unstage_targets('run-01', runs=runs)
        errors = self.errors()
        self.assertEqual(errors, [
            f'G2 target: plugin staged tree {stage / first} is missing; this run gates staged targets only '
            f'(stage-target --commit {first} --label run-01)',
            f'G2 target: bridge staged tree {stage / second} is missing; this run gates staged targets only '
            f'(stage-target --commit {second} --label run-01)'])

    def test_required_target_that_is_not_a_directory_is_missing(self):
        runs = self.target_run_layout()
        (self.base.parent / 'config.json').write_text(json.dumps({'staged_targets': [self.commit]}))
        staged = runs / '_eden-test' / 'stage' / 'run-01' / 'targets' / self.commit
        staged.parent.mkdir(parents=True)
        staged.write_text('not a checkout')
        self.assertIn(f'G2 target: plugin staged tree {staged} is missing; this run gates staged targets only '
                      f'(stage-target --commit {self.commit} --label run-01)', self.errors())

    def test_local_run_without_staged_targets_uses_the_producer(self):
        self.target_run_layout()
        self.plan['rows'][0]['reads'] = ['tr.menu:ack']
        for config in ({}, {'staged_targets': []}):
            with self.subTest(config=config):
                (self.base.parent / 'config.json').write_text(json.dumps(config))
                with mock.patch.object(packet_gate, '_git', wraps=packet_gate._git) as git:
                    self.assertEqual(self.errors(), [])
                self.assertTrue(any(Path(call.args[0]) == self.tree and call.args[1] == 'show'
                                    for call in git.call_args_list))

    def test_invalid_staged_targets_in_run_config_fail_closed(self):
        self.target_run_layout()
        for value in (None, {}, 'bad', [1], ['A' * 40], ['a' * 39], [self.commit, None]):
            with self.subTest(value=value):
                (self.base.parent / 'config.json').write_text(json.dumps({'staged_targets': value}))
                self.assertIn('G2 target: run config staged_targets is invalid', self.errors())

    def test_unreadable_or_malformed_run_config_fails_closed(self):
        self.target_run_layout()
        config = self.base.parent / 'config.json'
        for content in ('{', 'null', '[]'):
            with self.subTest(content=content):
                config.write_text(content)
                self.assertTrue(any(error.startswith(f'G2 target: run config {config} cannot be read: ')
                                    for error in self.errors()))
        config.unlink()
        config.mkdir()
        self.assertTrue(any(error.startswith(f'G2 target: run config {config} cannot be read: ')
                            for error in self.errors()))
        original_lstat = Path.lstat
        def denied(path, *args, **kwargs):
            if path == config:
                raise PermissionError('access denied')
            return original_lstat(path, *args, **kwargs)
        with mock.patch.object(Path, 'lstat', denied):
            self.assertIn(f'G2 target: run config {config} cannot be read: access denied', self.errors())

    def test_target_commit_not_listed_for_run_is_refused(self):
        self.target_run_layout()
        (self.base.parent / 'config.json').write_text(json.dumps({'staged_targets': ['f' * 40]}))
        errors = self.errors()
        self.assertIn(f'G2 target: plugin.commit {self.commit} was not staged for this run', errors)
        self.assertIn(f'G2 target: bridge.commit {self.commit} was not staged for this run', errors)

    def practice(self, old, new):
        self.assertIn(old, PRACTICE_STATE)
        (self.tree / 'src/model/practice_state.rs').write_text(PRACTICE_STATE.replace(old, new))
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'practice state')
        self.plan['target']['plugin']['commit'] = self.git('rev-parse', 'HEAD').stdout.strip()

    def refused(self, read):
        self.plan['rows'][0]['reads'] = [read]
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(f'{read} is not written by plugin', errors[0])
        return errors[0]

    def test_inputs_identity_covers_files_and_excludes_runner_writebacks(self):
        identity = getattr(packet_gate, 'inputs_identity', None)
        self.assertIsNotNone(identity)
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / 'rows.json').write_text('{}')
            (base / 'hook.py').write_text('return 1\n')
            (base / 'fixture.json').write_text('{"mode": 1}\n')
            (base / 'out').mkdir()
            (base / 'out' / 'receipt.json').write_text('output')
            (base / '__pycache__').mkdir()
            (base / '__pycache__' / 'x.pyc').write_bytes(b'cache')
            plan = {'segments': {'x': {'file': 'recorded.json'}},
                    'gates': {'x': {'reference': 'reference.png', 'acquire': True}}}
            before = identity(base, plan)
            files = [('fixture.json', base / 'fixture.json'), ('hook.py', base / 'hook.py')]
            runner_dir = Path(packet_gate.__file__).resolve().parent
            files += [(f'runner:{path.name}', path) for path in runner_dir.glob('*.py')
                      if path.is_file() and not path.is_symlink() and not path.name.startswith('test_')]
            wanted = hashlib.sha256()
            for name, path in sorted(files):
                wanted.update(f'{name}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n'.encode())
            self.assertEqual(before, wanted.hexdigest())
            (base / 'recorded.json').write_text('recorded')
            (base / 'reference.png').write_bytes(b'reference')
            plan['segments']['x']['recorded'] = 'now'
            plan['gates']['x']['acquired'] = 'now'
            self.assertEqual(identity(base, plan), before)
            (base / 'fixture.json').write_text('{"mode": 7}\n')
            self.assertNotEqual(identity(base, plan), before)

    def test_inputs_identity_excludes_nested_and_recovery_gif_outputs_at_the_runner_root(self):
        identity = getattr(packet_gate, 'inputs_identity', None)
        self.assertIsNotNone(identity)
        for explicit in (False, True):
            with self.subTest(explicit_root=explicit), tempfile.TemporaryDirectory() as temp:
                parent = Path(temp)
                checkout = parent / 'checkout'
                (checkout / '.git').mkdir(parents=True)
                base = checkout / 'packet' / 'rows'
                (base / 'clips').mkdir(parents=True)
                prefix = 'checkout/packet/rows/clips/' if explicit else 'packet/rows/clips/'

                def gif(name, steps=None):
                    return {'gif': {'name': name, 'out': prefix + name + '.gif', 'steps': steps or []}}

                plan = {'rows': [{'id': 'producer', 'steps': [gif('row', [gif('nested')])],
                                  'recover': [gif('row-recover', [gif('nested-recover')])]}],
                        'recover': [gif('plan-recover')]}
                if explicit:
                    plan['root'] = str(parent)
                neighbor = base / 'clips' / 'unnamed.dat'
                neighbor.write_text('before')
                before = identity(base, plan)
                for name in ('row', 'nested', 'row-recover', 'nested-recover', 'plan-recover'):
                    (base / 'clips' / (name + '.gif')).write_text('generated')
                self.assertEqual(identity(base, plan), before)
                (base / 'clips' / 'nested.gif').write_text('regenerated')
                self.assertEqual(identity(base, plan), before)
                neighbor.write_text('after')
                self.assertNotEqual(identity(base, plan), before)

    def test_inputs_identity_does_not_exclude_a_gif_out_rejected_by_inside_root(self):
        identity = getattr(packet_gate, 'inputs_identity', None)
        self.assertIsNotNone(identity)
        (self.base / 'clips').mkdir()
        neighbor = self.base / 'input.dat'
        neighbor.write_text('before')
        plan = {'rows': [{'steps': [{'gif': {'name': 'bad', 'out': 'clips/../input.dat'}}]}]}
        before = identity(self.base, plan)
        neighbor.write_text('after')
        self.assertNotEqual(identity(self.base, plan), before)

    def test_a_named_gif_output_cannot_mask_canonical_inputs(self):
        identity = getattr(packet_gate, 'inputs_identity', None)
        self.assertIsNotNone(identity)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / 'rows'
            base.mkdir()
            canonical = root / 'runner'
            canonical.mkdir()
            output = canonical / 'runner.py'
            output.write_text('before')
            plan = {'root': str(root), 'rows': [{'id': 'owner', 'steps': [
                {'gif': {'name': 'named', 'out': 'runner/runner.py'}}]}]}
            with mock.patch.object(packet_gate, '__file__', str(canonical / 'packet_gate.py')):
                before = identity(base, plan)
                output.write_text('after')
                self.assertNotEqual(identity(base, plan), before)
            self.assertIn('G4 row owner: gif out runner/runner.py names a packet input',
                          packet_gate.check(plan, base, plan)['errors'])

    def test_gif_output_cannot_mask_a_packet_hook_but_a_gif_is_excluded(self):
        identity = getattr(packet_gate, 'inputs_identity', None)
        self.assertIsNotNone(identity)
        gif = {'name': 'test', 'out': 'steps.py', 'steps': []}
        self.plan['rows'][0]['steps'].append({'gif': gif})
        before = identity(self.base, self.plan)
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return {"changed": 1}\n')
        self.assertNotEqual(identity(self.base, self.plan), before)
        self.assertIn('G4 row r1: gif out steps.py names a packet input', self.errors())

        gif['out'] = 'clips/a.gif'
        (self.base / 'clips').mkdir()
        before = identity(self.base, self.plan)
        self.assertFalse(any(error.startswith('G4 row r1: gif out') for error in self.errors()))
        (self.base / 'clips/a.gif').write_bytes(b'generated')
        self.assertEqual(identity(self.base, self.plan), before)

    def test_gif_collisions_cover_packet_inputs_and_keep_original_writeback_exclusions(self):
        identity = getattr(packet_gate, 'inputs_identity', None)
        self.assertIsNotNone(identity)
        cases = (
            ('rows.json', {}, False),
            ('segment.bin', {'segments': {'x': {'file': 'segment.bin'}}}, False),
            ('recorded.bin', {'segments': {'x': {'file': 'recorded.bin', 'recorded': 'now'}}}, True),
            ('reference.bin', {'gates': {'x': {'reference': 'reference.bin'}}}, False),
            ('acquired.bin', {'gates': {'x': {'reference': 'acquired.bin', 'acquire': True}}}, True),
            ('hook.step', {'rows': [{'id': 'r1', 'steps': [{'run': 'hook.step:ok'}]}]}, False),
            ('hook.gate', {'gates': {'x': {'kind': 'hook', 'module': 'hook.gate'}}}, False),
            ('hook.failure', {'on_failure_hook': 'hook.failure:ok'}, False),
            ('hook.rowfailure', {'rows': [{'id': 'r1', 'steps': [],
                                          'on_failure_hook': {'run': 'hook.rowfailure:ok'}}]}, False),
            ('hook.recover', {'recover': [{'run': 'hook.recover:ok'}]}, False),
            ('unreferenced.py', {}, False),
            ('unreferenced.json', {}, False),
            ('packet-gate.json', {}, False),
            ('out/output.json', {}, False),
            ('__pycache__/hook.pyc', {'recover': [{'run': '__pycache__/hook.pyc:ok'}]}, False),
        )
        for name, additions, unchanged in cases:
            with self.subTest(name=name):
                plan = {'rows': [{'id': 'r1', 'steps': []}], **additions}
                plan['rows'][0]['steps'].append({'gif': {'name': 'test', 'out': name}})
                path = self.base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('before')
                before = identity(self.base, plan)
                errors = packet_gate.check(plan, self.base, plan)['errors']
                self.assertIn(f'G4 row r1: gif out {name} names a packet input', errors)
                path.write_text('after')
                after = identity(self.base, plan)
                if unchanged:
                    self.assertEqual(before, after)
                else:
                    self.assertNotEqual(before, after)

    def test_gif_collisions_in_nested_and_recovery_steps_are_refused(self):
        plan = {
            'rows': [{'id': 'r1', 'steps': [{'gif': {'name': 'outer', 'out': 'ok.gif', 'steps': [
                {'gif': {'name': 'inner', 'out': 'inner.json'}}]}}],
                      'recover': [{'gif': {'name': 'row-recover', 'out': 'row.py'}}]}],
            'recover': [{'gif': {'name': 'plan-recover', 'out': 'plan.py'}}],
            'screen_classes': [{'class': 'menu', 'reach': [
                {'gif': {'name': 'reach', 'out': 'reach.json'}}]}],
        }
        errors = packet_gate.check(plan, self.base, plan)['errors']
        for name in ('inner.json', 'row.py', 'plan.py', 'reach.json'):
            self.assertIn(f'G4 row r1: gif out {name} names a packet input', errors)
        self.assertNotIn('G4 row r1: gif out ok.gif names a packet input', errors)

    def test_the_stale_capture_path_of_another_run_is_refused(self):
        path = '/home/intpa/eden-runs/rc15-fix-17060a5e-yoga-1/rows/cap-video.sh'
        (self.base / 'steps.py').write_text(f'def ok(ctx):\n    return {path!r}\n')
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(f'path into another run: {path}', errors[0])
        self.assertTrue(errors[0].startswith('G4 '), errors)
        self.plan['allow_paths'] = [{'path': path, 'reason': 'reviewed retained capture'}]
        self.assertEqual(self.errors(), [])
        (self.base / 'steps.py').write_text(
            'def ok(ctx):\n    return "/home/intpa/eden-runs/_eden-test/cap-video.sh"\n')
        self.plan.pop('allow_paths')
        self.assertEqual(self.errors(), [])

    def test_state_focus_paths_are_not_missing_files(self):
        (self.base / 'steps.py').write_text('def ok(ctx, focus):\n    return True\n')
        self.plan['gates']['focus'] = {'kind': 'state', 'provider': 'tr.menu', 'where': {'focus': '/-/0/row'}}
        self.plan['rows'][0]['steps'] = [
            {'gate': 'focus'}, {'run': 'steps.py:ok', 'args': {'focus': '/0/1/row'}}]
        self.assertEqual(self.errors(), [])
        path = '/home/intpa/does-not-exist/x.json'
        self.plan['rows'][0]['steps'][1]['args']['focus'] = path
        self.assertEqual(self.errors(), [f'G4 rows.json: missing file {path}'])

    def test_a_recipe_that_presses_the_cpu_chord_for_player_play_is_refused(self):
        seg = {'file': 'play.json', 'binding': 'play_player', 'recipe': [{'buttons': 'zr+up'}]}
        self.plan['segments']['play-p1'] = seg
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('recipe presses ZR+Up but play_player is bound to ZR+Left', errors[0])
        seg['recipe'][0]['buttons'] = 'zr+left'
        self.assertEqual(self.errors(), [])
        (self.base / 'play.json').write_text(json.dumps({'events': [{'buttons': 4608}, {'buttons': 0}]}))
        seg['recipe'][0]['buttons'] = 'zr+up'
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('recipe presses ZR+Up', errors[0])
        seg['recipe'][0]['buttons'] = 'zr+left'
        (self.base / 'play.json').write_text(json.dumps({'events': [{'buttons': 8704}, {'buttons': 0}]}))
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('compiled macro holds ZR+Up but play_player is bound to ZR+Left', errors[0])
        (self.base / 'settings.json').write_text('{"menu": {}}')
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('no persisted binding menu.play_player in settings.json', errors[0])

    def test_an_undeclared_flown_persisted_chord_is_refused(self):
        flags_up = [0] * 16
        flags_up[7] = flags_up[8] = 1
        flags_left = [0] * 16
        flags_left[7] = flags_left[10] = 1
        (self.base / 'settings.json').write_text(json.dumps({
            'menu': {'input_playback': flags_up, 'play_player': flags_left}}))
        sample = Path(__file__).parent / 'test-data' / 'i2-play-p1-chord.edenmacro.json'
        (self.base / 'play.json').write_bytes(sample.read_bytes())
        self.plan['segments']['play'] = {
            'file': 'play.json', 'recipe': [{'buttons': 'zr+up', 'frames': 3, 'gap_frames': 8}],
            'compiled': {'by': 'recipe_macro.py', 'fps': 60, 'min_frames': 2}}
        errors = self.errors()
        self.assertIn('G3 segment play: recipe presses ZR+Up but the compiled macro holds ZR+Left', errors)
        self.assertIn('G3 segment play: presses ZR+Up, the persisted chord of input_playback, without a "binding"',
                      errors)
        self.assertIn('G3 segment play: presses ZR+Left, the persisted chord of play_player, without a "binding"',
                      errors)
        settings = json.loads((self.base / 'settings.json').read_text())
        settings['menu']['play_also'] = flags_left
        (self.base / 'settings.json').write_text(json.dumps(settings))
        self.assertIn('G3 segment play: presses ZR+Left, the persisted chord of play_also, play_player, '
                      'without a "binding"', self.errors())
        segment = self.plan['segments']['play']
        segment['binding'] = 'play_player'
        segment['recipe'][0]['buttons'] = 'zr+left'
        self.assertEqual(self.errors(), [])
        segment.pop('binding')
        segment['recipe'][0]['buttons'] = 'a'
        (self.base / 'play.json').write_text(json.dumps({'events': [{'buttons': 1}, {'buttons': 0}]}))
        self.assertEqual(self.errors(), [])
        segment['recipe'][0]['buttons'] = 'b+up'
        (self.base / 'play.json').write_text(json.dumps({'events': [{'buttons': 8194}, {'buttons': 0}]}))
        self.assertEqual(self.errors(), [])
        self.plan['target'].pop('settings')
        self.assertEqual(self.errors(), [
            'G3 segment play: presses the chord B+Up but the target names no settings, so its binding cannot be checked'])
        segment['binding'] = 'play_player'
        self.assertIn('G3 segment play: presses the chord B+Up but the target names no settings, '
                      'so its binding cannot be checked', self.errors())

    def test_recorded_recipe_mismatch_warns_when_not_compiled(self):
        self.plan['segments']['play'] = {'file': 'play.json', 'recipe': [{'buttons': 'a'}]}
        (self.base / 'play.json').write_text(json.dumps({'events': [{'buttons': 2}, {'buttons': 0}]}))
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertIn('G3 segment play: recipe presses A but the compiled macro holds B', result['warnings'])

    def test_an_event_provider_extracts_each_named_event_key(self):
        self.plan['rows'][0]['reads'] = ['tr.event:fed run.p1', 'tr.event:fed hides',
                                         'tr.event:room.panel index', 'tr.event:room.panel partners']
        self.assertEqual(self.errors(), [])
        keys = packet_gate.emitted_keys(self.tree, self.commit, ['src/model/bridge_events.rs'])
        self.assertTrue({'fed run.p1', 'fed hides', 'room.panel index', 'room.panel partners',
                         'run.p1', 'hides', 'index', 'partners'} <= keys)
        self.plan['rows'][0]['reads'] = ['tr.event:fed p2']
        self.assertIn('tr.event:fed p2 is not written', self.errors()[0])
        self.plan['rows'][0]['reads'] = []
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return values.get("room.unknown")\n')
        self.assertEqual(self.errors(), [])

    def test_hooks_must_import_without_a_previous_hooks_path_change(self):
        old_path = sys.path[:]
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), old_path))
        (self.base / 'helper_for_d3_test.py').write_text('value = 1\n')
        (self.base / 'first.py').write_text(
            'import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n'
            'def ok(ctx): return True\n')
        (self.base / 'second.py').write_text('import helper_for_d3_test\ndef ok(ctx): return True\n')
        self.plan['rows'][0]['steps'] = [{'run': 'first.py:ok'}, {'run': 'second.py:ok'}]
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('G1 second.py: does not import on its own:', errors[0])
        self.assertIn('helper_for_d3_test', errors[0])
        import runner
        self.plan['start'] = 'main'
        self.plan['rows'][0].update(start='main', end='main', role='setup')
        manifest = self.base / 'rows.json'
        manifest.write_text(json.dumps(self.plan))
        report = runner.dry_run(manifest)
        self.assertEqual(report['result'], 'BROKEN')
        self.assertTrue(any(error.startswith('G1 second.py: does not import on its own:') for error in report['errors']))
        (self.base / 'second.py').write_text(
            'import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n'
            'import helper_for_d3_test\ndef ok(ctx): return True\n')
        self.assertEqual(self.errors(), [])

    def test_isolated_import_covers_gate_failure_and_recovery_hooks(self):
        for name in ('failure', 'recover', 'gate'):
            (self.base / f'{name}.py').write_text('import missing_d3_sibling\ndef check(*args, **kwargs): pass\n')
        self.plan['on_failure_hook'] = 'failure.py:check'
        self.plan['recover'] = [{'run': 'recover.py:check'}]
        self.plan['gates']['extra'] = {'kind': 'hook', 'module': 'gate.py', 'function': 'check'}
        self.plan['rows'][0]['steps'] = [{'gate': 'extra'}]
        errors = self.errors()
        for module in ('failure.py', 'recover.py', 'gate.py'):
            self.assertTrue(any(error.startswith(f'G1 {module}: does not import on its own:') for error in errors),
                            errors)
        self.assertEqual(len(errors), 3, errors)

    def test_timed_out_isolated_import_reports_its_last_stderr_line(self):
        error = subprocess.TimeoutExpired(['python'], 60, stderr=b'earlier\nlast useful line\n')
        errors = []
        with mock.patch.object(packet_gate.subprocess, 'run', side_effect=error) as run:
            packet_gate._hooks(self.plan, self.base, errors)
        self.assertEqual(errors, ['G1 steps.py: does not import on its own: last useful line'])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs['timeout'], 60)
        self.assertTrue(run.call_args.kwargs['capture_output'])

    def test_invalid_compiled_button_mask_is_reported_as_g3(self):
        self.plan['segments']['bad'] = {'file': 'bad.json', 'recipe': [{'buttons': 'a'}]}
        (self.base / 'bad.json').write_text(json.dumps({'events': [{'buttons': 'not a mask'}]}))
        errors = self.errors()
        self.assertTrue(any(error.startswith('G3 segment bad: compiled macro:') for error in errors), errors)

    def test_g5_requires_one_checked_outcome_for_test_rows(self):
        row = self.plan['rows'][0]
        row['role'] = 'test'
        error = ('G5 row r1: a test row with no effect gate, no effect hook, no judged "expected" '
                 'and no "outcome": "visual"; its PASS would only mean that no step raised')
        self.assertEqual(self.errors(), [error])
        row['expected'] = 'a result'
        self.assertEqual(self.errors(), [error + ' ("expected" is set, but the plan has no judge)'])
        self.plan['judge'] = {'cmd': 'judge.py'}
        self.assertEqual(self.errors(), [])
        row['judge'] = 'skip'
        self.assertEqual(self.errors(), [error])
        row.pop('judge')
        row.pop('expected')
        self.plan.pop('judge')
        row['steps'][0]['effect'] = True
        self.assertEqual(self.errors(), [])
        row['steps'] = [{'gate': 'fx'}]
        self.plan['gates']['fx'] = {'kind': 'status', 'effect': True}
        self.assertEqual(self.errors(), [])
        self.plan['gates']['fx'].pop('effect')
        row['steps'][0]['effect'] = True
        self.assertEqual(self.errors(), [])
        row['steps'] = [{'run': 'steps.py:ok'}]
        row['outcome'] = 'visual'
        self.assertEqual(self.errors(), [])
        row.pop('outcome')
        row['role'] = 'setup'
        self.assertEqual(self.errors(), [])

    def test_g5_blocks_the_real_dry_run(self):
        import runner
        self.plan['start'] = 'main'
        self.plan['rows'][0].update(start='main', end='main', role='test')
        manifest = self.base / 'rows.json'
        manifest.write_text(json.dumps(self.plan))
        report = runner.dry_run(manifest)
        self.assertEqual(report['result'], 'BROKEN')
        self.assertTrue(any(error.startswith('G5 row r1:') for error in report['errors']), report['errors'])

    def test_a_row_that_reads_a_line_the_build_does_not_write_is_refused(self):
        row = self.plan['rows'][0]
        row['reads'] = ['tr.menu:fed.p1']
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(f'tr.menu:fed.p1 is not written by plugin {self.commit[:8]}', errors[0])
        row['reads'] = ['tr.menu:strip.aims', 'tr.menu:page', 'tr.menu:zone.left', 'tr.menu:rows',
                        'tr.practice:wizard.step', 'tr.practice:wizard.y', 'tr.practice:phase']
        self.assertEqual(self.errors(), [])
        row['reads'] = ['user.ui:screen']
        self.assertIn('no extractor for provider user.ui (add target.providers)', self.errors()[0])
        self.plan['target']['providers'] = {'user.ui': {'repo': 'bridge', 'files': ['src/proto2.rs']}}
        self.assertEqual(self.errors(), [])
        row['reads'] = ['tr.menu:test.only']
        self.assertIn('tr.menu:test.only is not written', self.errors()[0])
        self.plan['target']['plugin']['commit'] = '0' * 40
        self.assertTrue(any('commit' in error for error in self.errors()), self.errors())

    def test_a_key_written_through_a_format_string_is_recognised(self):
        reads = self.plan['rows'][0]
        reads['reads'] = ['tr.practice:cpu.4.ai_thinks']
        self.assertEqual(self.errors(), [])
        reads['reads'] = ['tr.practice:cpu.1.ai_thinks', 'tr.practice:cpu.7.ai_thinks']
        self.assertEqual(self.errors(), [])
        keys = packet_gate.emitted_keys(self.tree, self.commit, ['src/model/practice_state.rs'])
        self.assertEqual({key for key in keys if key.startswith('cpu.')},
                         {f'cpu.{entry}.ai_thinks' for entry in range(1, 8)})
        reads['reads'] = ['tr.practice:cpu.4.ai_thinks', 'tr.practice:cpu.9.ai_thinks']
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('tr.practice:cpu.9.ai_thinks is not written by plugin', errors[0])

    def test_a_format_key_the_build_cannot_write_is_refused(self):
        for read in ('tr.practice:phaze', 'tr.practice:cpu.ai_thinks', 'tr.practice:cpu.4.ai_blinks',
                     'tr.practice:cpu.4.ai_thinks.more', 'tr.practice:cpu.x.ai_thinks',
                     'tr.practice:cpu.0.ai_thinks', 'tr.practice:cpu.8.ai_thinks', 'tr.practice:cpu.04.ai_thinks',
                     'tr.practice:cpu.-1.ai_thinks', 'tr.practice:cpu.{entry}.ai_thinks'):
            self.refused(read)

    def test_the_entry_range_follows_the_source(self):
        reads = self.plan['rows'][0]
        self.practice('CPU_ENTRIES: usize = 8', 'CPU_ENTRIES: usize = 5')
        reads['reads'] = ['tr.practice:cpu.1.ai_thinks', 'tr.practice:cpu.4.ai_thinks']
        self.assertEqual(self.errors(), [])
        self.assertIn('writes cpu.{entry}.ai_thinks with entry = 1 to 4', self.refused('tr.practice:cpu.5.ai_thinks'))
        self.practice('.skip(1)', '.skip(3)')
        reads['reads'] = ['tr.practice:cpu.3.ai_thinks', 'tr.practice:cpu.7.ai_thinks']
        self.assertEqual(self.errors(), [])
        self.assertIn('with entry = 3 to 7', self.refused('tr.practice:cpu.2.ai_thinks'))
        self.practice('.enumerate().skip(1)', '.enumerate()')
        reads['reads'] = ['tr.practice:cpu.0.ai_thinks', 'tr.practice:cpu.7.ai_thinks']
        self.assertEqual(self.errors(), [])
        self.refused('tr.practice:cpu.8.ai_thinks')
        self.practice('thinks: [u64; CPU_ENTRIES]', 'thinks: &[u64]')
        self.assertIn('writes cpu.{entry}.ai_thinks, but the gate cannot tell its values',
                      self.refused('tr.practice:cpu.4.ai_thinks'))
        self.practice('pub const CPU_ENTRIES: usize = 8;', 'pub const CPU_ENTRIES: usize = entries();')
        self.refused('tr.practice:cpu.4.ai_thinks')
        self.practice('for (entry, count) in thinks.into_iter()', 'for (entry, count) in thinks.into_iter().rev()')
        self.assertIn('.rev()', self.refused('tr.practice:cpu.4.ai_thinks'))

    def test_a_key_named_by_a_written_out_list_is_recognised(self):
        self.plan['rows'][0]['reads'] = ['tr.practice:step', 'tr.practice:score', 'tr.practice:wizard.selection',
                                         'tr.practice:attempt.frames']
        self.assertEqual(self.errors(), [])
        for read in ('tr.practice:hits', 'tr.practice:key', 'tr.practice:value'):
            self.refused(read)
        self.practice('("score", fields.score)', '(SCORE, fields.score)')
        self.refused('tr.practice:score')
        self.plan['rows'][0]['reads'] = ['tr.practice:step']
        self.assertEqual(self.errors(), [])

    def test_a_placeholder_the_gate_cannot_resolve_writes_no_key(self):
        keys = packet_gate.emitted_keys(self.tree, self.commit, ['src/model/practice_state.rs'])
        self.assertEqual({key for key in keys if key.startswith(('slot.', 'named.'))}, set())
        line = PRACTICE_STATE.split('slot.{slot}')[0].count('\n') + 1
        self.assertIn(f'; src/model/practice_state.rs:{line} writes slot.{{slot}}.value, '
                      'but the gate cannot tell its values', self.refused('tr.practice:slot.1.value'))
        self.assertIn('writes named.{name}.value, but the gate cannot tell its values: '
                      'no loop around the key binds name', self.refused('tr.practice:named.x.value'))
        self.plan['rows'][0]['reads'] = ['tr.practice:lane.2.frames', 'tr.practice:lane.3.frames']
        self.assertEqual(self.errors(), [])
        self.assertIn('writes lane.{lane}.frames with lane = 2 to 3', self.refused('tr.practice:lane.4.frames'))
        self.refused('tr.practice:lane.1.frames')

    def test_placeholders_of_one_loop_stay_together(self):
        keys = packet_gate.emitted_keys(self.tree, self.commit, ['src/model/practice_state.rs'])
        # One loop over pairs writes one key per pair, not every combination.
        self.assertEqual({key for key in keys if key.startswith(('player.', 'partner.'))},
                         {'player.left', 'partner.right'})
        self.plan['rows'][0]['reads'] = ['tr.practice:player.left', 'tr.practice:partner.right']
        self.assertEqual(self.errors(), [])
        for read in ('tr.practice:player.right', 'tr.practice:partner.left'):
            self.refused(read)
        # An index and its item come from the same pass of the loop.
        self.assertEqual({key for key in keys if key.startswith('medal.')}, {'medal.0.gold', 'medal.1.silver'})
        self.refused('tr.practice:medal.0.silver')
        # Two loops, one inside the other, do write every combination.
        self.assertEqual({key for key in keys if key.startswith(('top.', 'bottom.'))},
                         {'top.in', 'top.out', 'bottom.in', 'bottom.out'})

    def test_a_mutable_loop_binding_writes_no_key(self):
        self.practice('for (entry, count) in thinks.into_iter().enumerate().skip(1) {',
                      'for (mut entry, count) in thinks.into_iter().enumerate().skip(1) {\n        entry += 1;')
        for read in ('tr.practice:cpu.1.ai_thinks', 'tr.practice:cpu.4.ai_thinks', 'tr.practice:cpu.8.ai_thinks'):
            self.assertIn('writes cpu.{entry}.ai_thinks, but the gate cannot tell its values: '
                          'entry is a mutable binding', self.refused(read))
        self.practice('for lane in 2..=3 {', 'for mut lane in 2..=3 {\n        lane *= 2;')
        self.assertIn('lane is a mutable binding', self.refused('tr.practice:lane.2.frames'))
        self.practice('for (key, value) in [("score", fields.score)',
                      'for (mut key, value) in [("score", fields.score)')
        self.refused('tr.practice:score')
        self.plan['rows'][0]['reads'] = ['tr.practice:step', 'tr.practice:phase']
        self.assertEqual(self.errors(), [])

    def test_a_loop_that_can_stop_or_skip_writes_no_key(self):
        write = '        let _ = writeln!(out, "lane.{lane}.frames={}", 0);\n'
        # A pass the loop can skip: the gate cannot tell which lanes reach the write.
        self.practice('for lane in 2..=3 {', 'for lane in 2..=3 {\n        if lane == 2 { continue; }')
        for read in ('tr.practice:lane.2.frames', 'tr.practice:lane.3.frames'):
            self.assertIn('its loop can stop or skip a pass', self.refused(read))
        # A loop that can stop after a write.
        self.practice(write, write + '        if done { break; }\n')
        self.assertIn('its loop can stop or skip a pass', self.refused('tr.practice:lane.3.frames'))
        # A write under a condition on the placeholder itself.
        self.practice(write, '        if lane != 2 {\n    ' + write + '        }\n')
        for read in ('tr.practice:lane.2.frames', 'tr.practice:lane.3.frames'):
            self.assertIn('lane is used inside its loop before the key is written', self.refused(read))
        # A condition on something else leaves the keys as they are (the pairs of the practice state).
        self.plan['rows'][0]['reads'] = ['tr.practice:step', 'tr.practice:score', 'tr.practice:cpu.4.ai_thinks']
        self.assertEqual(self.errors(), [])

    def test_a_test_only_item_hides_nothing_that_follows_it(self):
        probe = '#[cfg(test)]\nfn probe(out: &mut String) {\n    let _ = writeln!(out, "probe.only={}", 1);\n}\n'
        self.practice('pub fn lanes(', probe + 'pub fn lanes(')
        self.plan['rows'][0]['reads'] = ['tr.practice:phase', 'tr.practice:cpu.4.ai_thinks',
                                         'tr.practice:lane.2.frames', 'tr.practice:player.left']
        self.assertEqual(self.errors(), [])
        self.refused('tr.practice:probe.only')
        # The same for a condition that can only hold in a test build; one that also holds on the console stays.
        host = ('#[cfg(all(test, not(target_os = "switch")))]\nfn host(out: &mut String) {\n'
                '    let _ = writeln!(out, "host.only={}", 1);\n}\n'
                '#[cfg(any(test, target_os = "switch"))]\nfn console(out: &mut String) {\n'
                '    let _ = writeln!(out, "console.key={}", 1);\n}\n')
        self.practice('pub fn lanes(', host + 'pub fn lanes(')
        self.plan['rows'][0]['reads'] = ['tr.practice:console.key', 'tr.practice:lane.2.frames']
        self.assertEqual(self.errors(), [])
        self.refused('tr.practice:host.only')
        # A test module written out at the end of the file still hides its own strings.
        end = '        let _ = writeln!(out, "medal.{rank}.{medal}=1");\n    }\n}\n'
        self.assertTrue(PRACTICE_STATE.endswith(end))
        self.practice(end, end + '#[cfg(test)]\nmod tests {\n    fn t() { let _ = format!("tests.only={}", 1); }\n}\n')
        self.refused('tr.practice:tests.only')
        self.plan['rows'][0]['reads'] = ['tr.practice:medal.0.gold']
        self.assertEqual(self.errors(), [])
        # A test-only item that never closes is refused, with the file named.
        self.practice('pub fn lanes(', '#[cfg(test)]\nfn probe(out: &mut String) {\npub fn lanes(')
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('extractor tr.practice: src/model/practice_state.rs', errors[0])
        self.assertIn('could not be scanned', errors[0])

    def test_an_event_line_with_a_format_key_names_its_event(self):
        self.plan['rows'][0]['reads'] = ['tr.event:seat owner.p1', 'tr.event:seat owner.cpu', 'tr.event:owner.cpu']
        self.assertEqual(self.errors(), [])
        keys = packet_gate.emitted_keys(self.tree, self.commit, ['src/model/bridge_events.rs'])
        self.assertEqual({key for key in keys if 'owner' in key},
                         {'owner.p1', 'owner.cpu', 'seat owner.p1', 'seat owner.cpu'})
        self.plan['rows'][0]['reads'] = ['tr.event:seat owner.p2']
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('tr.event:seat owner.p2 is not written by plugin', errors[0])

    def test_a_hook_read_of_a_format_key_is_checked_against_its_values(self):
        (self.base / 'steps.py').write_text(
            'def ok(ctx):\n    values = {}\n    return values["cpu.1.ai_thinks"], values.get("cpu.4.ai_thinks")\n')
        self.assertEqual(self.errors(), [])
        (self.base / 'steps.py').write_text('def ok(ctx):\n    values = {}\n    return values["cpu.9.ai_thinks"]\n')
        self.assertEqual(self.errors(), ['G2 steps.py: reads "cpu.9.ai_thinks", which the target does not write'])

    def test_a_row_in_the_wrong_recording_view_is_refused(self):
        self.plan['setup'] = [{'gives': ['recording.role=cpu']}]
        self.plan['rows'][0]['needs'] = ['recording.role=player']
        self.assertEqual(self.errors(), [
            'G6 row r1: needs "recording.role=player" but recording.role is "cpu" (set by setup)'])
        self.plan['rows'].insert(0, {'id': 'switch', 'gives': ['recording.role=player'],
                                     'steps': [{'run': 'steps.py:ok'}]})
        self.assertEqual(self.errors(), [])
        self.assertEqual(packet_gate.check(self.plan, self.base, self.plan)['rows']['r1']['provided_by'],
                         {'recording.role=player': 'switch'})

    def test_a_row_that_needs_a_take_nobody_made_is_refused(self):
        self.plan['rows'][0]['needs'] = ['take:p1-ground-jump']
        self.assertEqual(self.errors(), [
            'G6 row r1: needs "take:p1-ground-jump": no setup entry and no earlier row gives it'])

    def test_a_hook_called_with_arguments_it_does_not_take_is_refused(self):
        (self.base / 'steps.py').write_text('def step(ctx, scene):\n    return scene\n')
        self.plan['rows'][0]['steps'] = [{'run': 'steps.py:step', 'args': {'scenes': 'lobby'}}]
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('G1 row r1: steps.py:step:', errors[0])
        self.plan['rows'][0]['steps'][0]['args'] = {'scene': 'lobby'}
        (self.base / 'gate.py').write_text('def check(path, gate):\n    return True\n')
        self.plan['gates']['view'] = {'kind': 'hook', 'module': 'gate.py', 'function': 'check', 'cap': 1}
        self.plan['rows'][0]['steps'].append({'gate': 'view'})
        self.assertEqual(self.errors(), [])
        (self.base / 'gate.py').write_text('def check(path):\n    return True\n')
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('G1 row r1:', errors[0])
        self.assertIn('view', errors[0])

    def test_step_recovery_and_failure_hooks_bind_context_and_arguments(self):
        (self.base / 'steps.py').write_text(
            'def step(ctx, scene):\n    return scene\n'
            'def failed(ctx, scene):\n    return scene\n')
        row = self.plan['rows'][0]
        row['steps'] = [{'gif': {'steps': [{'run': 'steps.py:step', 'args': {'scene': 'lobby'}}]}}]
        row['recover'] = [{'run': 'steps.py:step', 'args': {'scene': 'lobby'}}]
        row['on_failure_hook'] = {'run': 'steps.py:failed', 'args': {'scene': 'lobby'}}
        self.assertEqual(self.errors(), [])
        row['recover'][0]['args'] = {'scenes': 'lobby'}
        row['on_failure_hook']['args'] = {'scenes': 'lobby'}
        errors = self.errors()
        self.assertEqual(len(errors), 2, errors)
        self.assertTrue(all(error.startswith('G1 row r1:') for error in errors), errors)
        self.assertTrue(any('steps.py:step' in error for error in errors), errors)
        self.assertTrue(any('steps.py:failed' in error for error in errors), errors)
        row['recover'][0]['args'] = {'scene': 'lobby'}
        row['on_failure_hook']['args'] = {'scene': 'lobby'}
        self.plan['recover'] = [{'run': 'steps.py:step', 'args': {'scenes': 'lobby'}}]
        self.plan['on_failure_hook'] = {'run': 'steps.py:failed', 'args': {'scenes': 'lobby'}}
        errors = self.errors()
        self.assertEqual(len(errors), 2, errors)
        self.assertTrue(any('steps.py:step' in error for error in errors), errors)
        self.assertTrue(any('steps.py:failed' in error for error in errors), errors)

    def test_a_bridge_provider_needs_a_valid_bridge_target(self):
        self.plan['target'].pop('bridge')
        self.plan['target']['providers'] = {'ssbu.ui': {'repo': 'bridge', 'files': ['src/proto2.rs']}}
        self.plan['rows'][0]['reads'] = ['ssbu.ui:screen']
        self.assertIn('G2 row r1: provider ssbu.ui needs target.bridge', self.errors())
        self.plan['target']['providers'] = []
        self.assertIn('G2 target: providers must be an object', self.errors())

    def bridge_fixture(self):
        tree = Path(self.tmp.name) / 'bridge-source'
        tree.mkdir()

        def write(name, content):
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        def git(*args):
            return subprocess.run(['git', '-C', str(tree), *args], check=True, capture_output=True, text=True)

        old_ui = r'fn encode() { write!(out, "schema=ssbu.ui/1\nobservation={}\nobservation.active={}\nscreen={screen}\n"); }' + '\n'
        write('src/native_ui/mod.rs', old_ui)
        write('src/proto2.rs', r'''
write!(out, "[Bridge] fighter entry={entry} kind={} x={} y={} status={} situation={} stock={} dead={} item={}");
write!(out, " eb={} ab={} cpu={} cbt={}");
write!(out, "[Bridge] input slot={slot} src={src} buttons={:#x} lx={} ly={} rx={} ry={}");
write!(out, "x={} y={} at={} tr={}");
write!(out, "[Bridge2] other hidden={}");
write!(out, "[Bridge] [Nested] other hidden={}");
''')
        write('src/hooks.rs', r'''
write!(out, "[Bridge] fighters_watch done entries={mask:#x}");
write!(out, "[Bridge] req fighters_watch result=ok entries={mask:#x} frames={frames}");
''')
        write('src/runtime.rs', r'''
let _ = write!(out, " ev={seq}");
write!(out, "[Bridge] state begin name={name} schema={schema} rev={rev} boot={boot} lines={lines} filtered={filtered}");
write!(out, "[Bridge] state end name={name} rev={rev} crc={crc}");
write!(out, "[Bridge] provider name={name} schema_id={schema_id} version={version} rev={rev} dropped={dropped} toggle_gen={toggle_gen}");
''')
        git('init', '-q')
        git('add', 'src')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'partial')
        partial = git('rev-parse', 'HEAD').stdout.strip()
        write('src/model.rs', 'write!(out, "[Bridge] stage id={stage}");\n')
        git('add', 'src/model.rs')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'old bridge')
        old = git('rev-parse', 'HEAD').stdout.strip()
        write('src/native_ui/mod.rs', old_ui.replace(
            '); }', r'); write!(out, "\ninput.ready={}\nscreen.gen={}\nscreen.gap={}"); }'))
        git('add', 'src/native_ui/mod.rs')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'new bridge')
        new = git('rev-parse', 'HEAD').stdout.strip()
        return tree, partial, old, new

    def bridge_ui_fixture(self):
        tree = Path(self.tmp.name) / 'ui-source'
        tree.mkdir()
        source = Path(__file__).parent / 'test-data' / 'bridge-ui'
        target = tree / 'src' / 'native_ui'
        target.mkdir(parents=True)

        def git(*args):
            return subprocess.run(['git', '-C', str(tree), *args], check=True, capture_output=True, text=True)

        shutil.copyfile(source / 'ed2655d4/src/native_ui/mod.rs', target / 'mod.rs')
        git('init', '-q')
        git('add', 'src')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'older excerpt')
        older = git('rev-parse', 'HEAD').stdout.strip()
        shutil.copyfile(source / 'c5efb86/src/native_ui/mod.rs', target / 'mod.rs')
        shutil.copyfile(source / 'c5efb86/src/native_ui/pause_focus.rs', target / 'pause_focus.rs')
        git('add', 'src')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'newer excerpts')
        newer = git('rev-parse', 'HEAD').stdout.strip()
        return tree, older, newer, git

    def test_legacy_builtin_ui_provider_uses_the_builtin_block(self):
        tree, _, newer, _ = self.bridge_ui_fixture()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': newer}
        self.plan['rows'][0]['reads'] = ['ssbu.ui:screen', 'ssbu.ui:pause.open']
        baseline = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(baseline['errors'], [])
        self.plan['target']['providers'] = {'ssbu.ui': {'repo': 'bridge', 'files': ['src/native_ui/mod.rs']}}
        report = packet_gate.check(self.plan, self.base, self.plan)
        warning = ('G2 target: providers.ssbu.ui repeats a built-in provider; '
                   'the built-in extraction is used (remove the entry)')
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['warnings'].count(warning), 1)
        self.assertEqual(report['emitted']['ssbu.ui'], baseline['emitted']['ssbu.ui'])
        self.plan['rows'][0]['reads'] = ['ssbu.ui:css.training']
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], [f'G2 row r1: ssbu.ui:css.training is not written by bridge '
                                            f'{newer[:8]} (src/native_ui/mod.rs)'])
        self.assertEqual(report['warnings'].count(warning), 1)
        self.assertEqual(report['emitted']['ssbu.ui'], baseline['emitted']['ssbu.ui'])
        self.plan['target']['providers']['ssbu.ui'].update(
            block='ssbu.ui', helpers=['src/native_ui/pause_focus.rs'])
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['warnings'].count(warning), 1)
        self.assertEqual(report['emitted']['ssbu.ui'], baseline['emitted']['ssbu.ui'])

    def test_legacy_builtin_ui_provider_is_also_used_for_hook_reads(self):
        tree, _, newer, _ = self.bridge_ui_fixture()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': newer}
        self.plan['target']['providers'] = {'ssbu.ui': {'repo': 'bridge', 'files': ['src/native_ui/mod.rs']}}
        warning = ('G2 target: providers.ssbu.ui repeats a built-in provider; '
                   'the built-in extraction is used (remove the entry)')
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return values.get("pause.open")\n')
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['warnings'].count(warning), 1)
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return values.get("pause.opne")\n')
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], ['G2 steps.py: reads "pause.opne", which the target does not write'])
        self.assertEqual(report['warnings'].count(warning), 1)

    def test_builtin_redefinitions_are_errors_and_do_not_replace_extraction(self):
        tree, _, newer, _ = self.bridge_ui_fixture()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': newer}
        self.plan['rows'][0]['reads'] = ['ssbu.ui:pause.open', 'ssbu.ui:css.training']
        baseline = packet_gate.check(self.plan, self.base, self.plan)
        expected_read = (f'G2 row r1: ssbu.ui:css.training is not written by bridge '
                         f'{newer[:8]} (src/native_ui/mod.rs)')
        self.assertEqual(baseline['errors'], [expected_read])
        for field, value in (('files', ['src/proto2.rs']), ('repo', 'plugin'), ('block', 'other'),
                             ('helpers', ['src/other.rs']), ('whole_literals', False)):
            with self.subTest(field=field):
                spec = {'repo': 'bridge', 'files': ['src/native_ui/mod.rs'], field: value}
                self.plan['target']['providers'] = {'ssbu.ui': spec}
                report = packet_gate.check(self.plan, self.base, self.plan)
                error = (f'G2 target: providers.ssbu.ui redefines a built-in provider ({field}); '
                         'remove the entry or give the provider another name')
                self.assertEqual(report['errors'], [error, expected_read])
                self.assertEqual(report['emitted']['ssbu.ui'], baseline['emitted']['ssbu.ui'])
                self.assertEqual(report['warnings'], [])
        self.plan['target']['providers'] = {
            'ssbu.ui': {'repo': 'plugin', 'files': ['src/other.rs'], 'block': 'other'}}
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], [
            'G2 target: providers.ssbu.ui redefines a built-in provider (repo, files, block); '
            'remove the entry or give the provider another name', expected_read])
        self.assertEqual(report['emitted']['ssbu.ui'], baseline['emitted']['ssbu.ui'])

    def test_legacy_tr_menu_file_order_and_custom_repository_coexist(self):
        tree = Path(self.tmp.name) / 'tile-provider'
        tree.mkdir()
        (tree / 'cursor.rs').write_text(r'write!(out, "p1.token={}");' + '\n')

        def git(*args):
            return subprocess.run(['git', '-C', str(tree), *args], check=True, capture_output=True, text=True)

        git('init', '-q')
        git('add', 'cursor.rs')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'tile')
        commit = git('rev-parse', 'HEAD').stdout.strip()
        self.plan['target']['repos'] = {'tile': {'tree': str(tree), 'commit': commit}}
        self.plan['target']['providers'] = {
            'tr.menu': {'repo': 'plugin', 'files': list(reversed(packet_gate.PROVIDERS['tr.menu']['files']))},
            'tile.cursor': {'repo': 'tile', 'files': ['cursor.rs']}}
        self.plan['rows'][0]['reads'] = ['tr.menu:zone.left', 'tile.cursor:p1.token']
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], [])
        self.assertIn('p1.token', report['emitted']['tile.cursor'])
        self.assertEqual(report['warnings'], [
            'G2 target: providers.tr.menu repeats a built-in provider; '
            'the built-in extraction is used (remove the entry)'])
        self.plan['target']['providers']['bridge'] = {
            'repo': 'bridge', 'files': list(reversed(packet_gate.PROVIDERS['bridge']['files'])),
            'whole_literals': False}
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['warnings'], [
            'G2 target: providers.tr.menu repeats a built-in provider; '
            'the built-in extraction is used (remove the entry)',
            'G2 target: providers.bridge repeats a built-in provider; '
            'the built-in extraction is used (remove the entry)'])

    def test_ui_block_uses_real_excerpts_and_only_declared_helper(self):
        tree, older, newer, git = self.bridge_ui_fixture()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': newer}
        positive = ['schema', 'screen', 'observation.active', 'input.ready', 'screen.gen', 'picker.phase',
                    'focus.index', 'focus.label', 'ldn.members', 'ldn.room.session',
                    'pause.open', 'pause.cpu_behavior', 'pause.panel']
        self.plan['rows'][0]['reads'] = [f'ssbu.ui:{key}' for key in positive]
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], [])
        print('D8 newer excerpt block keys:', len(report['emitted']['ssbu.ui']))
        for key in ('css.training', 'tile.cursor', 'ui ready', 'ready', 'menu.admission', 'ldn.create'):
            with self.subTest(key=key):
                self.plan['rows'][0]['reads'] = [f'ssbu.ui:{key}']
                self.assertEqual(self.errors(), [f'G2 row r1: ssbu.ui:{key} is not written by bridge '
                                                 f'{newer[:8]} (src/native_ui/mod.rs)'])
        self.plan['target']['bridge']['commit'] = older
        self.plan['rows'][0]['reads'] = ['ssbu.ui:screen']
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], [])
        print('D8 older excerpt block keys:', len(report['emitted']['ssbu.ui']))
        for key in ('input.ready', 'pause.open'):
            self.plan['rows'][0]['reads'] = [f'ssbu.ui:{key}']
            self.assertEqual(self.errors(), [f'G2 row r1: ssbu.ui:{key} is not written by bridge '
                                             f'{older[:8]} (src/native_ui/mod.rs)'])
        mod = tree / 'src/native_ui/mod.rs'
        mod.write_text(mod.read_text().replace('mod pause_focus;\n', ''))
        git('add', 'src/native_ui/mod.rs')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'undeclared helper')
        self.plan['target']['bridge']['commit'] = git('rev-parse', 'HEAD').stdout.strip()
        self.plan['rows'][0]['reads'] = ['ssbu.ui:pause.open']
        self.assertIn('ssbu.ui:pause.open is not written', self.errors()[0])

    def test_ui_block_requires_a_schema_writer_and_rejects_unbalanced_code(self):
        tree, _, _, git = self.bridge_ui_fixture()
        mod = tree / 'src/native_ui/mod.rs'
        mod.write_text('fn other() { write!(out, "screen={}"); }\n')
        git('add', 'src/native_ui/mod.rs')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'no block')
        commit = git('rev-parse', 'HEAD').stdout.strip()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': commit}
        self.plan['rows'][0]['reads'] = ['ssbu.ui:screen']
        self.assertEqual(self.errors(), [f'G2 row r1: extractor ssbu.ui: no function writes '
                                         f'schema=ssbu.ui/ in src/native_ui/mod.rs at {commit[:8]}'])
        mod.write_text('fn broken() { write!(out, "schema=ssbu.ui/1\\nscreen={}");\n')
        git('add', 'src/native_ui/mod.rs')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'unbalanced')
        self.plan['target']['bridge']['commit'] = git('rev-parse', 'HEAD').stdout.strip()
        self.assertTrue(any('extractor ssbu.ui: could not be scanned' in error for error in self.errors()))

    def ui_source(self, text, reads):
        """The gate's report for a bridge whose src/native_ui/mod.rs is `text`, with a row that reads the given
        ssbu.ui keys; and the commit's first eight characters."""
        tree = Path(tempfile.mkdtemp(dir=self.tmp.name))
        (tree / 'src/native_ui').mkdir(parents=True)
        (tree / 'src/native_ui/mod.rs').write_text(text)

        def git(*args):
            return subprocess.run(['git', '-C', str(tree), *args], check=True, capture_output=True, text=True)

        git('init', '-q')
        git('add', 'src')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'source')
        commit = git('rev-parse', 'HEAD').stdout.strip()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': commit}
        self.plan['rows'][0]['reads'] = [f'ssbu.ui:{key}' for key in reads]
        return packet_gate.check(self.plan, self.base, self.plan), commit[:8]

    def test_a_test_only_fn_inside_an_impl_is_skipped(self):
        self.assertIn('impl Sample {\n', UI_95ABB28)
        self.assertIn('    #[cfg(test)]\n    fn encode(self, rev: u64, boot: u64) -> Line<MAX_PAYLOAD> {\n', UI_95ABB28)
        self.assertLess(UI_95ABB28.index('#[cfg(test)]'), UI_95ABB28.index('schema=ssbu.ui/1'))
        # The file scans, and the real writer, which stands after the test-only fn, is read.
        written = ('schema', 'screen', 'observation.active', 'input.ready', 'screen.gen', 'picker.phase',
                   'focus.index', 'ldn.role', 'ldn.request.slot', 'ldn.prompt', 'ldn.room.key', 'ldn.net.key',
                   'ldn.net.nodes')
        report, _ = self.ui_source(UI_95ABB28, written)
        self.assertEqual(report['errors'], [])
        keys = report['emitted']['ssbu.ui']
        print('95abb28 excerpt block keys:', len(keys))
        for key in ('css.training', 'tile.cursor', 'ldn.create'):
            report, commit = self.ui_source(UI_95ABB28, [key])
            self.assertEqual(report['errors'], [f'G2 row r1: ssbu.ui:{key} is not written by bridge '
                                                f'{commit} (src/native_ui/mod.rs)'])
        # A key that only the test-only fn writes is not a written key, and the others stay as they were.
        body = '        self.encode_with_net(rev, boot, ldn_join::NetInfo::default())\n'
        self.assertEqual(UI_95ABB28.count(body), 1)
        marked = UI_95ABB28.replace(body, '        let _ = write!(out, "schema=ssbu.ui/1\\ntest.only={rev}\\n");\n' + body)
        report, _ = self.ui_source(marked, written)
        self.assertEqual((report['errors'], report['emitted'].get('ssbu.ui')), ([], keys))
        report, commit = self.ui_source(marked, ['test.only'])
        self.assertEqual(report['errors'], [f'G2 row r1: ssbu.ui:test.only is not written by bridge '
                                            f'{commit} (src/native_ui/mod.rs)'])

    def test_a_test_only_module_is_skipped_and_console_code_is_kept(self):
        module = ('#[cfg(test)]\n#[allow(dead_code)]\npub(crate) mod probe {\n    fn write(out: &mut Line<64>) {\n'
                  '        let _ = write!(out, "schema=ssbu.ui/1\\nmodule.only={}\\n", 1);\n    }\n}\n\n')
        # Not test-only: this item is built for the console too.
        console = ('#[cfg(any(test, target_os = "switch"))]\nfn console(out: &mut Line<64>) {\n'
                   '    let _ = write!(out, "schema=ssbu.ui/1\\nconsole.key={}\\n", 1);\n}\n\n')
        self.assertEqual(UI_95ABB28.count('impl Sample {\n'), 1)

        def item(cfg, key):
            return (f'#[cfg({cfg})]\nfn {key.replace(".", "_")}(out: &mut Line<64>) {{\n'
                    f'    let _ = write!(out, "schema=ssbu.ui/1\\n{key}={{}}\\n", 1);\n}}\n\n')

        # Other conditions that name `test`: read when the item can be built outside a test build, left out when
        # it cannot. A feature or a target is not followed.
        more = (item('not(test)', 'release.key') + item('any(feature = "diag", test)', 'diag.key')
                + item('not(any(test, feature = "diag"))', 'plain.key')
                + item('all(test, not(target_os = "switch"))', 'host.only')
                + item('all(any(test), feature = "diag")', 'nested.only'))
        source = UI_95ABB28.replace('impl Sample {\n', module + console + more + 'impl Sample {\n')
        report, _ = self.ui_source(source, ['screen', 'ldn.role', 'console.key', 'release.key', 'diag.key',
                                            'plain.key'])
        self.assertEqual(report['errors'], [])
        for key in ('module.only', 'host.only', 'nested.only'):
            report, commit = self.ui_source(source, [key])
            self.assertEqual(report['errors'], [f'G2 row r1: ssbu.ui:{key} is not written by bridge '
                                                f'{commit} (src/native_ui/mod.rs)'])
        # The declaration form the real file ends with, and a module written on the attribute's own line.
        self.assertTrue(UI_95ABB28.endswith('#[cfg(test)]\nmod tests;\n#[cfg(test)]\nmod gate_tests;\n'))
        one_line = '#[cfg(test)] mod more { fn w() { write!(out, "schema=ssbu.ui/1\\nmore.only={}"); } }\n'
        report, _ = self.ui_source(UI_95ABB28 + one_line, ['screen', 'ldn.role'])
        self.assertEqual(report['errors'], [])
        report, _ = self.ui_source(UI_95ABB28 + one_line, ['more.only'])
        self.assertEqual(len(report['errors']), 1, report['errors'])
        self.assertIn('ssbu.ui:more.only is not written by bridge', report['errors'][0])

    def test_a_source_that_does_not_balance_still_fails_loudly(self):
        def refused(text):
            report, _ = self.ui_source(text, ['screen'])
            return any('extractor ssbu.ui: could not be scanned' in error for error in report['errors'])

        report, _ = self.ui_source(UI_95ABB28, ['screen'])
        self.assertEqual(report['errors'], [])
        # A real item that never closes.
        tail = '        pause.write(&mut out);\n        out\n    }\n}\n'
        self.assertEqual(UI_95ABB28.count(tail), 1)
        self.assertTrue(refused(UI_95ABB28.replace(tail, tail[:-2])))
        # A test-only item that never closes.
        self.assertTrue(refused(UI_95ABB28 + '#[cfg(test)]\nfn open(out: &mut Line<8>) {\n    let _ = 1;\n'))
        # A test-only attribute on something that is not an item: the gate does not guess where it ends.
        field = '    observed: u64,\n'
        self.assertEqual(UI_95ABB28.count(field), 1)
        self.assertTrue(refused(UI_95ABB28.replace(field, '    #[cfg(test)]\n' + field)))
        # A file that is test-only as a whole.
        writer = 'fn w(out: &mut Line<8>) {\n    let _ = write!(out, "schema=ssbu.ui/1\\nscreen={}", 1);\n}\n'
        self.assertTrue(refused('#![cfg(test)]\n' + writer))
        self.assertFalse(refused('#![cfg(any(test, target_os = "switch"))]\n' + writer))
        # A condition the gate cannot read.
        self.assertTrue(refused('#[cfg(test all)]\n' + writer))
        self.assertTrue(refused('#[cfg(any(test)]\n' + writer))

    def test_rust_function_scanner_ignores_literals_comments_and_lifetimes(self):
        source = r'''
fn first<'a>(x: &'a str) {
    let a = '{'; let b = "}"; let c = b"\n}"; let d = r#"}"#;
    let e = '\''; let f = '\n'; // }
    /* } /* nested } */ } */
    write!(out, "schema=ssbu.ui/1\nscreen={}");
}
fn second() { write!(out, "other={}"); }
'''
        scanner = getattr(packet_gate, '_rust_function_bodies', None)
        self.assertIsNotNone(scanner)
        bodies = list(scanner(source))
        self.assertEqual(len(bodies), 2)
        self.assertIn('schema=ssbu.ui/', source[bodies[0][0]:bodies[0][1]])
        self.assertNotIn('other={}', source[bodies[0][0]:bodies[0][1]])
        self.assertIn('other={}', source[bodies[1][0]:bodies[1][1]])

    def test_ui_hook_read_net_uses_the_same_block_keys(self):
        tree, _, newer, _ = self.bridge_ui_fixture()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': newer}
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return values.get("pause.missing")\n')
        self.assertEqual(self.errors(), ['G2 steps.py: reads "pause.missing", which the target does not write'])
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return values.get("css.unrelated")\n')
        self.assertEqual(self.errors(), [])

    def test_bridge_does_not_take_whole_literal_values_as_keys(self):
        tree, _, _, commit = self.bridge_fixture()
        path = tree / 'src/proto2.rs'
        path.write_text(path.read_text() + 'fn names() { match x { _ => "ldn.create", } }\n')
        def git(*args):
            return subprocess.run(['git', '-C', str(tree), *args], check=True, capture_output=True, text=True)
        git('add', 'src/proto2.rs')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'value')
        commit = git('rev-parse', 'HEAD').stdout.strip()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': commit}
        self.plan['rows'][0]['reads'] = ['bridge:fighter status', 'bridge:ev', 'bridge:stage id']
        self.assertEqual(self.errors(), [])
        self.plan['rows'][0]['reads'] = ['bridge:ldn.create']
        errors = self.errors()
        self.assertTrue(any('bridge:ldn.create is not written' in error for error in errors), errors)

    def test_declared_block_provider_limits_literals_and_allows_optional_helpers(self):
        tree, _, _, _ = self.bridge_fixture()
        main = tree / 'src/custom.rs'
        main.write_text(r'''
mod helper;
fn block() {
    write!(out, "schema=mine/1\nfirst=1");
    write!(out, "start=1 after_space=2");
    write!(out, "[Bridge2] ignored=1\nignored.also=2");
    write!(out, r#"\nraw.false=1"#);
    write!(out, r#"raw.first=1"#);
    number(&mut out, "helper.key", 1);
    push_str(out, "page", 1);
    number(out, pair(1, 2), "later.key", "not.first");
    helpers::number::<u8>(out, "generic.key");
    number(out, "not a key", "not.second");
    number(other, "not.helper", 1);
    let value = "dotted.value";
    fn nested() { write!(out, "nested.key=1"); }
}
fn elsewhere() { write!(out, "outside=1"); }
''')
        helper = tree / 'src/helper.rs'
        helper.write_text(r'fn help(out: &mut Out) { write!(out, "\nhelp.key=1"); }' + '\n')

        def git(*args):
            return subprocess.run(['git', '-C', str(tree), *args], check=True, capture_output=True, text=True)

        git('add', 'src/custom.rs', 'src/helper.rs')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'custom block')
        commit = git('rev-parse', 'HEAD').stdout.strip()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': commit}
        self.plan['target']['providers'] = {
            'custom': {'repo': 'bridge', 'files': ['src/custom.rs'], 'block': 'mine', 'helpers': ['src/helper.rs']}}
        self.plan['rows'][0]['reads'] = ['custom:schema', 'custom:first', 'custom:start', 'custom:raw.first',
                                         'custom:helper.key', 'custom:page', 'custom:later.key',
                                         'custom:generic.key', 'custom:nested.key', 'custom:help.key']
        self.assertEqual(self.errors(), [])
        for key in ('after_space', 'ignored', 'ignored.also', 'raw.false', 'not.first', 'not.second',
                    'not.helper', 'dotted.value', 'outside'):
            with self.subTest(key=key):
                self.plan['rows'][0]['reads'] = [f'custom:{key}']
                self.assertTrue(any(f'custom:{key} is not written' in error for error in self.errors()))
        helper.write_text(r'fn help(out: &mut Out) { write!(out, "\nhelp.key=1");' + '\n')
        git('add', 'src/helper.rs')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'unbalanced helper')
        self.plan['target']['bridge']['commit'] = git('rev-parse', 'HEAD').stdout.strip()
        self.plan['rows'][0]['reads'] = ['custom:first']
        self.assertTrue(any('extractor custom: could not be scanned' in error for error in self.errors()))
        git('rm', '-q', 'src/helper.rs')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'missing optional helper')
        self.plan['target']['bridge']['commit'] = git('rev-parse', 'HEAD').stdout.strip()
        self.plan['rows'][0]['reads'] = ['custom:first']
        self.assertEqual(self.errors(), [])
        self.plan['rows'][0]['reads'] = ['custom:help.key']
        self.assertTrue(any('custom:help.key is not written' in error for error in self.errors()))

    def test_builtin_ssbu_ui_provider_checks_the_selected_bridge_commit(self):
        tree, _, old, new = self.bridge_fixture()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': new}
        self.plan['rows'][0]['reads'] = ['ssbu.ui:screen', 'ssbu.ui:observation.active', 'ssbu.ui:input.ready']
        self.assertEqual(self.errors(), [])
        self.plan['target']['bridge']['commit'] = old
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(f'ssbu.ui:input.ready is not written by bridge {old[:8]}', errors[0])

    def test_builtin_bridge_provider_extracts_tagged_events_and_bare_continuations(self):
        tree, _, _, commit = self.bridge_fixture()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': commit}
        self.plan['rows'][0]['reads'] = ['bridge:fighter status', 'bridge:fighter x', 'bridge:eb',
                                         'bridge:input lx', 'bridge:stage id', 'bridge:fighters_watch entries',
                                         'bridge:req result']
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], [])
        emitted = set(report['emitted']['bridge'])
        self.assertTrue({'fighter entry', 'fighter status', 'fighter x', 'eb', 'input lx',
                         'stage id', 'fighters_watch entries', 'req result'} <= emitted)
        self.assertNotIn('fighter eb', emitted)
        self.assertNotIn('other hidden', emitted)
        for key in ('fighter position', 'fighter eb'):
            with self.subTest(key=key):
                self.plan['rows'][0]['reads'] = [f'bridge:{key}']
                self.assertEqual(self.errors(), [f'G2 row r1: bridge:{key} is not written by bridge '
                                                 f'{commit[:8]} (src/proto2.rs, src/hooks.rs, src/model.rs, '
                                                 'src/runtime.rs)'])

    def test_builtin_bridge_provider_includes_runtime_stamps_and_events(self):
        tree, _, _, commit = self.bridge_fixture()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': commit}
        self.plan['rows'][0]['reads'] = ['bridge:ev', 'bridge:state name', 'bridge:state crc',
                                         'bridge:provider schema_id']
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], [])
        self.assertTrue({'ev', 'state name', 'state crc', 'provider schema_id'} <= set(report['emitted']['bridge']))
        self.plan['rows'][0]['reads'] = ['bridge:state colour']
        self.assertEqual(self.errors(), [f'G2 row r1: bridge:state colour is not written by bridge '
                                         f'{commit[:8]} (src/proto2.rs, src/hooks.rs, src/model.rs, src/runtime.rs)'])

    def test_builtins_require_the_bridge_target(self):
        self.plan['target'].pop('bridge')
        for provider, key in (('ssbu.ui', 'screen'), ('bridge', 'fighter status')):
            with self.subTest(provider=provider):
                self.plan['rows'][0]['reads'] = [f'{provider}:{key}']
                self.assertIn(f'G2 row r1: provider {provider} needs target.bridge', self.errors())

    def test_missing_bridge_provider_file_names_its_commit(self):
        tree, partial, _, _ = self.bridge_fixture()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': partial}
        self.plan['rows'][0]['reads'] = ['bridge:fighter status']
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith('G2 row r1: extractor bridge:'), errors)
        self.assertIn('src/model.rs', errors[0])
        self.assertIn(partial, errors[0])

    def test_declared_provider_can_use_a_third_repository(self):
        tree = Path(self.tmp.name) / 'tile-source'
        tree.mkdir()
        (tree / 'cursor.rs').write_text(r'write!(out, "screen={}\np1.token={}\nsettled={}");' + '\n')

        def git(*args):
            return subprocess.run(['git', '-C', str(tree), *args], check=True, capture_output=True, text=True)

        git('init', '-q')
        git('add', 'cursor.rs')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'tile')
        commit = git('rev-parse', 'HEAD').stdout.strip()
        self.plan['target']['repos'] = {'tile': {'tree': str(tree), 'commit': commit}}
        self.plan['target']['providers'] = {'tile.cursor': {'repo': 'tile', 'files': ['cursor.rs']}}
        self.plan['rows'][0]['reads'] = ['tile.cursor:p1.token']
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], [])
        self.assertIn('p1.token', report['emitted']['tile.cursor'])
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return values.get("p1.unknown")\n')
        self.assertEqual(self.errors(), ['G2 steps.py: reads "p1.unknown", which the target does not write'])
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return True\n')
        source = self.plan['target']['providers']['tile.cursor']
        source['repo'] = 'unknown'
        self.assertIn('G2 row r1: no extractor for provider tile.cursor (add target.providers)', self.errors())
        source['repo'] = 'tile'
        for name in ('plugin', 'bridge'):
            with self.subTest(reserved=name):
                self.plan['target']['repos'][name] = {'tree': str(tree), 'commit': commit}
                self.assertIn(f'G2 target: repos.{name} is reserved', self.errors())
                self.plan['target']['repos'].pop(name)
        self.plan['target']['repos']['tile']['commit'] = '0' * 40
        self.assertTrue(any('repos.tile.commit' in error and 'is not a commit' in error for error in self.errors()))
        self.plan['target']['repos']['tile']['commit'] = 'not-a-commit'
        self.assertIn('G2 target: repos.tile.commit must be 40 lower-case hex characters', self.errors())
        self.plan['target']['repos']['tile']['commit'] = commit
        self.plan['target']['repos']['tile']['tree'] = 'relative'
        self.assertIn('G2 target: repos.tile.tree must be an existing absolute directory', self.errors())
        self.plan['target']['repos']['tile']['tree'] = str(tree / 'missing')
        self.assertIn(f'G2 target: repos.tile.tree {tree / "missing"} does not exist on this host and '
                      f'the run has no staged tree for {commit}', self.errors())
        self.plan['target']['repos']['tile'] = 'bad'
        self.assertIn('G2 target: repos.tile must be an object', self.errors())
        self.plan['target']['repos'] = []
        self.assertIn('G2 target: repos must be an object', self.errors())

    def test_hook_read_prefixes_come_from_dotted_provider_keys_only(self):
        tree, _, _, commit = self.bridge_fixture()
        self.plan['target']['bridge'] = {'tree': str(tree), 'commit': commit}
        self.plan['target']['providers'] = {'probe': {'repo': 'bridge', 'files': ['src/proto2.rs']}}
        (self.base / 'steps.py').write_text(
            'def ok(ctx):\n    return [values.get("x.position"), values.get("y.velocity"), '
            'values.get("at.start"), values.get("tr.example")]\n')
        self.assertEqual(self.errors(), [])
        self.plan['target'].pop('providers')
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return values.get("screen.unknown")\n')
        self.assertEqual(self.errors(), ['G2 steps.py: reads "screen.unknown", which the target does not write'])

    def test_the_packet_digest_changes_with_any_file_and_ignores_out(self):
        initial = packet_gate.packet_digest(self.base)
        (self.base / 'out').mkdir()
        (self.base / 'out' / 'receipt.json').write_text('output')
        (self.base / 'packet-gate.json').write_text('receipt')
        (self.base / 'cache.pyc').write_text('cache')
        self.assertEqual(packet_gate.packet_digest(self.base), initial)
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return False\n')
        self.assertNotEqual(packet_gate.packet_digest(self.base), initial)

    def test_a_fixture_step_gives_what_a_later_row_needs(self):
        self.plan['rows'] = [
            {'id': 'switch', 'steps': [{'run': 'fixtures:recording_role', 'args': {'role': 'player'}}]},
            {'id': 'consumer', 'needs': ['recording.role=player'], 'steps': []},
        ]
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['rows']['consumer']['provided_by'], {'recording.role=player': 'switch'})
        self.plan['rows'][0]['steps'][0]['args']['role'] = 'cpu'
        self.assertEqual(self.errors(), [
            'G6 row consumer: needs "recording.role=player" but recording.role is "cpu" (set by switch)'])

    def test_a_seeded_room_step_gives_all_three_room_tokens(self):
        self.plan['rows'] = [
            {'id': 'room', 'steps': [{'run': 'fixtures:seeded_room',
                                      'args': {'behavior': 7, 'stage': 3}}]},
            {'id': 'consumer', 'needs': ['lobby.practice=on', 'partner.behavior=7', 'lobby.stage=3'], 'steps': []},
        ]
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['rows']['consumer']['provided_by'], {
            'lobby.practice=on': 'room', 'partner.behavior=7': 'room', 'lobby.stage=3': 'room'})
        self.plan['rows'][0]['steps'][0]['args']['behavior'] = 1
        self.assertEqual(self.errors(), [
            'G6 row consumer: needs "partner.behavior=7" but partner.behavior is "1" (set by room)'])

    def test_guard_first_use_gives_a_token_specific_to_its_game(self):
        self.plan['rows'] = [
            {'id': 'wizard', 'steps': [{'run': 'fixtures:guard_first_use',
                                        'args': {'scene': 'training', 'game': 'juggler'}}]},
            {'id': 'consumer', 'needs': ['guard.ready'], 'steps': []},
        ]
        self.assertEqual(self.errors(), [
            'G6 row consumer: needs "guard.ready": no setup entry and no earlier row gives it'])
        self.plan['rows'][1]['needs'] = ['practice.ready=juggler']
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['rows']['consumer']['provided_by'], {'practice.ready=juggler': 'wizard'})
        self.plan['rows'][0]['steps'][0]['args'].pop('game')
        self.plan['rows'][1]['needs'] = ['guard.ready']
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['rows']['consumer']['provided_by'], {'guard.ready': 'wizard'})

    def test_watch_fixtures_give_standing_and_specific_effective_behavior(self):
        self.plan['rows'] = [
            {'id': 'watch', 'steps': [{'run': 'fixtures:cpu_standing'},
                                      {'run': 'fixtures:partner_effective', 'args': {'eb': 1}}]},
            {'id': 'consumer', 'needs': ['cpu.standing', 'partner.effective=1'], 'steps': []},
        ]
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['rows']['consumer']['provided_by'], {
            'cpu.standing': 'watch', 'partner.effective=1': 'watch'})
        self.plan['rows'][1]['needs'] = ['partner.effective=34']
        self.assertEqual(self.errors(), [
            'G6 row consumer: needs "partner.effective=34" but partner.effective is "1" (set by watch)'])

    def shared_case_setup(self):
        shared = Path(self.tmp.name) / 'shared'
        shared.mkdir()
        (shared / 'sibling.py').write_text(
            'def evaluate(lines):\n'
            '    if "walking" in lines: raise RuntimeError("walking frame 2")\n'
            '    return {"frames": len(lines)}\n')
        (shared / 'sharedfx.py').write_text(
            'from sibling import evaluate\n'
            'EFFECT_JUDGES = {"hook": {"judge": "judge", "cases": "sample"}}\n'
            'def hook(ctx): return judge(lines=["standing"])\n'
            'def judge(lines): return evaluate(lines)\n')
        cases = shared / 'library' / 'cases' / 'sample'
        cases.mkdir(parents=True)
        (cases / 'standing.log').write_text('standing\n')
        (cases / 'walking.log').write_text('walking\n')
        (cases / 'a.json').write_text(json.dumps(
            {'files': {'lines': 'standing.log'}, 'expect': 'accept'}))
        (cases / 'b.json').write_text(json.dumps(
            {'files': {'lines': 'walking.log'}, 'expect': 'reject', 'contains': 'walking'}))
        plan = {'rows': [{'id': 'trial', 'role': 'test',
                          'steps': [{'run': 'sharedfx:hook', 'effect': True}]}]}
        return shared, cases, plan

    def shared_case_check(self, shared, plan):
        with mock.patch.object(packet_gate, 'SHARED_DIR', shared, create=True), \
             mock.patch.object(packet_gate, 'shared_hook_names', return_value=('sharedfx', 'fixtures'), create=True):
            return packet_gate.check(plan, self.base, plan)

    def test_shared_hook_without_entry_and_fixture_cases_belong_to_runner(self):
        shared, _, plan = self.shared_case_setup()
        (shared / 'sharedfx.py').write_text('raise RuntimeError("must not import")\ndef hook(ctx): return 1\n')
        message = 'G9 row trial: effect hook sharedfx:hook has no judge cases; its evidence is trusted'
        report = self.shared_case_check(shared, plan)
        self.assertIn(message, report['warnings'])
        plan['require_cases'] = True
        self.assertIn(message + ' (require_cases)', self.shared_case_check(shared, plan)['errors'])
        (shared / 'sharedfx.py').write_text('EFFECT_JUDGES = {"other": {"judge": "j", "cases": "sample"}}\n')
        self.assertIn(message + ' (require_cases)', self.shared_case_check(shared, plan)['errors'])
        plan['rows'][0]['steps'] = [{'run': 'fixtures:cpu_standing', 'effect': True,
                                     'cases': {'judge': 'unused.py:judge', 'dir': 'unused'}}]
        report = self.shared_case_check(shared, plan)
        self.assertIn('G9 row trial: fixtures:cpu_standing is a shared hook; its judge cases belong to '
                      'the runner (remove "cases")', report['errors'])

    def test_fixture_effect_judge_replays_cases_and_enforces_binding(self):
        shared, cases, plan = self.shared_case_setup()
        fixture = shared / 'fixtures.py'
        source = (shared / 'sharedfx.py').read_text()
        fixture.write_text(source)
        plan['rows'][0]['steps'] = [{'run': 'fixtures:hook', 'effect': True}]
        report = self.shared_case_check(shared, plan)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['warnings'], [])
        self.assertEqual(report['cases'].get('trial', {}).get('fixtures:judge'), {'accept': 1, 'reject': 1})
        (cases / 'standing.log').write_text('walking\n')
        self.assertIn('G9 row trial: case a.json: expected accept, the judge refused: walking frame 2',
                      self.shared_case_check(shared, plan)['errors'])
        (cases / 'standing.log').write_text('standing\n')
        fixture.write_text(source.replace('return judge(lines=["standing"])', 'return 1'))
        self.assertIn('G9 row trial: fixtures:hook does not call its judge judge',
                      self.shared_case_check(shared, plan)['errors'])

    def test_non_effect_and_setup_fixture_hooks_do_not_need_cases(self):
        shared, _, plan = self.shared_case_setup()
        plan['require_cases'] = True
        plan['rows'] = [
            {'id': 'trial', 'role': 'test', 'steps': [
                {'run': 'fixtures:seeded_room'}, {'run': 'fixtures:guard_first_use'}]},
            {'id': 'setup', 'role': 'setup', 'steps': [
                {'run': 'fixtures:cpu_standing', 'effect': True},
                {'run': 'fixtures:seeded_room', 'effect': True,
                 'cases': {'judge': 'unused.py:judge', 'dir': 'unused'}}]},
        ]
        report = self.shared_case_check(shared, plan)
        self.assertFalse(any(item.startswith('G9 ') for item in report['errors'] + report['warnings']))

    def test_shared_judge_cases_use_siblings_and_run_once_for_multiple_rows(self):
        shared, _, plan = self.shared_case_setup()
        plan['rows'].append({'id': 'again', 'role': 'test', 'steps': plan['rows'][0]['steps']})
        original = subprocess.run
        with mock.patch.object(packet_gate.subprocess, 'run', wraps=original) as calls:
            report = self.shared_case_check(shared, plan)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['warnings'], [])
        loaders = [call for call in calls.call_args_list if packet_gate.CASE_LOADER in call.args[0]]
        self.assertEqual(len(loaders), 1)
        for row in ('trial', 'again'):
            self.assertEqual(report['cases'][row]['sharedfx:judge'], {'accept': 1, 'reject': 1})
        module = shared / 'sharedfx.py'
        module.write_text(module.read_text().replace(
            'def hook(ctx): return judge(lines=["standing"])',
            'def hook(ctx):\n    def nested(): return judge(lines=["standing"])\n    return nested()'))
        self.assertEqual(self.shared_case_check(shared, plan)['errors'], [])

    def test_shared_judge_rejects_mismatched_outcomes_and_missing_polarity(self):
        shared, cases, plan = self.shared_case_setup()
        (cases / 'standing.log').write_text('walking\n')
        report = self.shared_case_check(shared, plan)
        self.assertIn('G9 row trial: case a.json: expected accept, the judge refused: walking frame 2',
                      report['errors'])
        (cases / 'standing.log').write_text('standing\n')
        (cases / 'walking.log').write_text('standing\n')
        self.assertIn('G9 row trial: case b.json: expected reject, the judge accepted',
                      self.shared_case_check(shared, plan)['errors'])
        (cases / 'walking.log').write_text('walking\n')
        (cases / 'b.json').write_text(json.dumps(
            {'files': {'lines': 'walking.log'}, 'expect': 'reject', 'contains': 'not present'}))
        self.assertIn('G9 row trial: case b.json: refused, but not with "not present": walking frame 2',
                      self.shared_case_check(shared, plan)['errors'])
        (cases / 'b.json').unlink()
        self.assertIn('G9 row trial: cases need at least one accept and one reject (found 1 accept, 0 reject)',
                      self.shared_case_check(shared, plan)['errors'])

    def test_shared_judge_validates_binding_folder_and_table(self):
        shared, _, plan = self.shared_case_setup()
        module = shared / 'sharedfx.py'
        source = module.read_text()
        module.write_text(source.replace('return judge(lines=["standing"])', 'return 1'))
        self.assertIn('G9 row trial: sharedfx:hook does not call its judge judge',
                      self.shared_case_check(shared, plan)['errors'])
        module.write_text(source.replace('def judge(lines): return evaluate(lines)', 'def other(lines): return 1'))
        self.assertIn('G9 row trial: sharedfx:judge is not a function of sharedfx',
                      self.shared_case_check(shared, plan)['errors'])
        for folder in ('missing', '../sample', '.', '/tmp', 'one\\two', 'bad\x00name'):
            with self.subTest(folder=folder):
                module.write_text(source.replace('"cases": "sample"', f'"cases": {json.dumps(folder)}'))
                self.assertIn(f'G9 row trial: cases folder library/cases/{folder} is missing',
                              self.shared_case_check(shared, plan)['errors'])
        module.write_text(source)
        plan['rows'][0]['steps'][0]['cases'] = {'judge': 'fake.py:judge', 'dir': 'fake'}
        self.assertIn('G9 row trial: sharedfx:hook is a shared hook; its judge cases belong to '
                      'the runner (remove "cases")', self.shared_case_check(shared, plan)['errors'])
        del plan['rows'][0]['steps'][0]['cases']
        for table in ('EFFECT_JUDGES = dict()', 'EFFECT_JUDGES = "judge"',
                      'EFFECT_JUDGES = {"hook": "judge"}'):
            with self.subTest(table=table):
                module.write_text(table + '\ndef hook(ctx): return 1\n')
                self.assertTrue(any(item.startswith('G9 row trial: sharedfx.EFFECT_JUDGES is malformed:')
                                    for item in self.shared_case_check(shared, plan)['errors']))

    def test_shared_judge_cases_cannot_read_outside_library_cases(self):
        shared, cases, plan = self.shared_case_setup()
        (shared / 'outside.log').write_text('standing\n')
        (cases / 'a.json').write_text(json.dumps(
            {'files': {'lines': '../../../outside.log'}, 'expect': 'accept'}))
        report = self.shared_case_check(shared, plan)
        self.assertTrue(any(item.startswith('G9 row trial: cases:') and 'leaves the rows folder' in item
                            for item in report['errors']))

    def case_setup(self):
        (self.base / 'judge.py').write_text(
            'def judge(lines):\n'
            '    if any("walking" in line for line in lines): raise RuntimeError("walking frame 2")\n'
            '    return {"frames": len(lines)}\n')
        (self.base / 'steps.py').write_text(
            'import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent))\n'
            'import judge\ndef ok(ctx): return {"frames": 3}\n')
        directory = self.base / 'cases'
        directory.mkdir()
        (directory / 'standing.log').write_text('still\nstill\nstill\n')
        (directory / 'walking.log').write_text('still\nwalking\nstill\n')
        accept = {'args': {}, 'files': {'lines': 'standing.log'}, 'expect': 'accept'}
        reject = {'args': {}, 'files': {'lines': 'walking.log'}, 'expect': 'reject', 'contains': 'walking'}
        (directory / 'a.json').write_text(json.dumps(accept))
        (directory / 'b.json').write_text(json.dumps(reject))
        self.plan['rows'] = [{'id': 'trial', 'role': 'test',
                              'steps': [{'run': 'steps.py:ok', 'effect': True,
                                         'cases': {'judge': 'judge.py:judge', 'dir': 'cases'}}]}]
        return directory, accept, reject

    def test_judge_cases_accept_standing_and_reject_walking(self):
        self.case_setup()
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result.get('cases', {}).get('trial', {}).get('judge.py:judge'),
                         {'accept': 1, 'reject': 1})

    def nested_verdict_cases(self):
        directory, _, _ = self.case_setup()
        (directory / 'a.json').unlink()
        (directory / 'b.json').unlink()
        (self.base / 'judge.py').write_text(
            'def judge(lines): return {"verdict": "PASS" if lines[0] == "standing" else "FAIL"}\n')
        for folder, content, expect in (('good', 'standing', 'accept'), ('bad', 'walking', 'reject')):
            case_dir = directory / folder
            case_dir.mkdir()
            (case_dir / 'bridge.log').write_text(content + '\n')
            (case_dir / 'case.json').write_text(json.dumps({
                'args': {}, 'files': {'lines': 'bridge.log'}, 'expect': expect}))
        return directory

    def test_nested_cases_use_relative_files_and_verdict_evidence(self):
        self.nested_verdict_cases()
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result.get('cases', {}).get('trial', {}).get('judge.py:judge'),
                         {'accept': 1, 'reject': 1})

    def test_nested_cases_with_the_same_basename_are_reported_separately(self):
        directory = self.nested_verdict_cases()
        (directory / 'good' / 'bridge.log').write_text('walking\n')
        (directory / 'bad' / 'bridge.log').write_text('standing\n')
        errors = self.errors()
        self.assertIn('G9 row trial: case bad/case.json: expected reject, the judge accepted', errors)
        self.assertTrue(any(error.startswith('G9 row trial: case good/case.json: expected accept, '
                                             'the judge refused:') for error in errors))

    def test_non_case_json_in_a_nested_case_directory_is_malformed(self):
        directory = self.nested_verdict_cases()
        (directory / 'good' / 'data.json').write_text('{}')
        self.assertTrue(any('good/data.json: expect must be accept or reject' in error for error in self.errors()))

    def test_a_reject_case_fed_standing_lines_is_refused(self):
        directory, _, reject = self.case_setup()
        reject['files']['lines'] = 'standing.log'
        (directory / 'b.json').write_text(json.dumps(reject))
        self.assertIn('G9 row trial: case b.json: expected reject, the judge accepted', self.errors())

    def test_cases_need_both_accept_and_reject(self):
        directory, _, _ = self.case_setup()
        (directory / 'b.json').unlink()
        self.assertIn('G9 row trial: cases need at least one accept and one reject (found 1 accept, 0 reject)',
                      self.errors())

    def test_a_judge_with_a_missing_import_cannot_run_cases(self):
        self.case_setup()
        (self.base / 'judge.py').write_text('import missing_sibling\ndef judge(lines): return {"frames": 1}\n')
        self.assertTrue(any('G9 row trial: judge.py:judge could not run its cases:' in error
                            and 'missing_sibling' in error for error in self.errors()))

    def test_non_json_judge_output_is_reported_as_a_case_failure(self):
        self.case_setup()
        (self.base / 'judge.py').write_text(
            'import os\ndef judge(lines):\n    os.write(1, b"\\xff")\n    return {"frames": len(lines)}\n')
        self.assertTrue(any(error.startswith('G9 row trial: judge.py:judge could not run its cases:')
                            and 'output' in error for error in self.errors()))

    def test_case_file_inputs_cannot_leave_the_rows_folder(self):
        directory, _, reject = self.case_setup()
        reject['files']['lines'] = '../../outside.log'
        (directory / 'b.json').write_text(json.dumps(reject))
        self.assertTrue(any(error.startswith('G9 row trial: cases:') and 'leaves the rows folder' in error
                            for error in self.errors()))

    def test_invalid_json_missing_expect_and_missing_file_are_malformed(self):
        directory, _, reject = self.case_setup()
        for text in ('not json', json.dumps({'args': {}}),
                     json.dumps({**reject, 'files': {'lines': 'missing.log'}})):
            with self.subTest(text=text):
                (directory / 'b.json').write_text(text)
                self.assertTrue(any(error.startswith('G9 row trial: cases:') for error in self.errors()))

    def test_judge_refusing_an_accept_case_is_an_error(self):
        directory, accept, _ = self.case_setup()
        accept['files']['lines'] = 'walking.log'
        (directory / 'a.json').write_text(json.dumps(accept))
        self.assertIn('G9 row trial: case a.json: expected accept, the judge refused: walking frame 2',
                      self.errors())

    def test_a_boolean_return_is_not_evidence(self):
        directory, _, reject = self.case_setup()
        reject['contains'] = 'True'
        (directory / 'b.json').write_text(json.dumps(reject))
        (self.base / 'judge.py').write_text('def judge(lines): return True\n')
        self.assertIn('G9 row trial: case a.json: expected accept, the judge refused: True', self.errors())

    def test_a_judge_directory_pair_uses_one_subprocess_for_multiple_rows(self):
        self.case_setup()
        self.plan['rows'].append({'id': 'again', 'role': 'test', 'steps': self.plan['rows'][0]['steps']})
        original = subprocess.run
        with mock.patch.object(packet_gate.subprocess, 'run', wraps=original) as calls:
            result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        loaders = [call for call in calls.call_args_list if packet_gate.CASE_LOADER in call.args[0]]
        self.assertEqual(len(loaders), 1)
        self.assertEqual(result.get('cases', {}).get('again', {}).get('judge.py:judge'),
                         {'accept': 1, 'reject': 1})

    def test_reject_contains_must_match_the_reason(self):
        directory, _, reject = self.case_setup()
        reject['contains'] = 'different reason'
        (directory / 'b.json').write_text(json.dumps(reject))
        self.assertIn('G9 row trial: case b.json: refused, but not with "different reason": walking frame 2',
                      self.errors())

    def test_case_judge_must_be_imported_by_the_hook(self):
        self.case_setup()
        (self.base / 'steps.py').write_text('def ok(ctx): return {"frames": 3}\n')
        self.assertIn('G9 row trial: steps.py does not import judge.py', self.errors())
        (self.base / 'steps.py').write_text(
            'import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent))\n'
            'from judge import judge\ndef ok(ctx): return {"frames": 3}\n')
        self.assertEqual(self.errors(), [])

    def test_missing_hook_cases_warn_or_refuse_including_shared_fixtures(self):
        self.plan['rows'] = [{'id': 'trial', 'role': 'test', 'steps': [{'run': 'steps.py:ok', 'effect': True}]}]
        message = 'G9 row trial: effect hook steps.py:ok has no judge cases; its evidence is trusted'
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertIn(message, result['warnings'])
        self.plan['require_cases'] = True
        self.assertIn(message + ' (require_cases)', self.errors())
        self.plan['rows'][0]['steps'] = [{'run': 'fixtures:cpu_standing', 'effect': True}]
        result = packet_gate.check(self.plan, self.base, self.plan)
        fixture = 'G9 row trial: effect hook fixtures:cpu_standing has no judge cases; its evidence is trusted'
        self.assertIn(fixture + ' (require_cases)', result['errors'])
        self.plan.pop('require_cases')
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertIn(fixture, result['warnings'])
        self.assertFalse(any(message.startswith('G9 ') for message in result['errors']))

    def test_require_cases_is_checked_without_a_target_too(self):
        self.plan.pop('target')
        self.plan['rows'] = [{'id': 'trial', 'role': 'test', 'steps': [{'run': 'steps.py:ok', 'effect': True}]}]
        message = 'G9 row trial: effect hook steps.py:ok has no judge cases; its evidence is trusted'
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['result'], 'NO TARGET')
        self.assertIn(message, result['warnings'])
        self.plan['require_cases'] = True
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['result'], 'BROKEN')
        self.assertIn(message + ' (require_cases)', result['errors'])

    def test_case_rejects_empty_or_unverified_evidence(self):
        directory, _, reject = self.case_setup()
        reject['contains'] = 'unverified'
        (directory / 'b.json').write_text(json.dumps(reject))
        (self.base / 'judge.py').write_text(
            'def judge(lines):\n'
            '    return {"unverified": "walking"} if "walking" in lines else {"frames": len(lines)}\n')
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result.get('cases', {}).get('trial', {}).get('judge.py:judge'),
                         {'accept': 1, 'reject': 1})

    def test_a_hook_that_plays_a_macro_nobody_defines_is_refused(self):
        (self.base / 'steps.py').write_text('def ok(ctx):\n    ctx.play("missing")\n')
        self.assertEqual(self.errors(),
                         ['G8 steps.py: ctx.play("missing") names no segment of this plan'])
        self.plan['segments']['missing'] = {'file': 'x.json'}
        self.assertEqual(self.errors(), [])
        (self.base / 'steps.py').write_text('def ok(ctx):\n    routes.play(ctx, "tap-a1")\n')
        self.assertEqual(self.errors(), [])
        (self.base / 'steps.py').write_text('def ok(ctx):\n    other.play(ctx, "missing-too")\n')
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['warnings'],
                         ['G8 steps.py: 1 play call(s) with a computed name or unknown callee are not checked'])
        (self.base / 'steps.py').write_text('def ok(ctx):\n    ctx.play(pick())\n    ctx.play(pick())\n')
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['warnings'],
                         ['G8 steps.py: 2 play call(s) with a computed name or unknown callee are not checked'])

    HOOK_HEAD = 'import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent))\n'

    def play_gate(self, files, rows=None):
        """The gate's errors, its warnings and the play names it checked, for hook files written beside rows.json."""
        for name, source in files.items():
            (self.base / name).write_text(source)
        if rows is not None:
            self.plan['rows'] = rows
        result = packet_gate.check(self.plan, self.base, self.plan)
        return result['errors'], result['warnings'], result.get('plays')

    def test_a_play_inside_an_imported_helper_is_checked(self):
        helper = ('def press(ctx):\n    ctx.play("tap-a")\n'
                  'def unused(ctx):\n    ctx.play("never-recorded")\n    ctx.play(pick())\n')
        steps = self.HOOK_HEAD + 'import helper\ndef ok(ctx):\n    helper.press(ctx)\n'
        # The helper plays a segment the plan does not have: refused at gate time, with the helper named.
        self.assertEqual(self.play_gate({'steps.py': steps, 'helper.py': helper}),
                         (['G8 helper.py: ctx.play("tap-a") names no segment of this plan'], [], []))
        # With the segment it is checked. `unused` is not reached by this plan, so its plays are not read.
        self.plan['segments']['tap-a'] = {'file': 'tap-a.json'}
        self.assertEqual(self.play_gate({}), ([], [], ['tap-a']))
        # A name imported from the helper, and a helper two modules away, which does reach `unused`.
        steps = self.HOOK_HEAD + 'from helper import press as tap\nimport deeper as d\ndef ok(ctx):\n    tap(ctx)\n    d.relay(ctx)\n'
        deeper = self.HOOK_HEAD + 'import helper\ndef relay(ctx):\n    helper.unused(ctx)\n'
        self.assertEqual(self.play_gate({'steps.py': steps, 'deeper.py': deeper}), (
            ['G8 helper.py: ctx.play("never-recorded") names no segment of this plan'],
            ['G8 helper.py: 1 play call(s) with a computed name or unknown callee are not checked'], ['tap-a']))
        # A step module is still read as a whole, reached or not, as before.
        steps = self.HOOK_HEAD + 'import helper\ndef ok(ctx):\n    helper.press(ctx)\ndef spare(ctx):\n    ctx.play("spare")\n'
        self.assertEqual(self.play_gate({'steps.py': steps}),
                         (['G8 steps.py: ctx.play("spare") names no segment of this plan'], [], ['tap-a']))

    def test_a_helper_play_is_read_however_it_is_imported_and_whatever_the_context_is_called(self):
        head = self.HOOK_HEAD
        helper = 'def press(ctx):\n    ctx.play("tap-a")\n'
        renamed = 'def press(context):\n    context.play("tap-a")\n'
        kept = 'def press(ctx):\n    session = ctx\n    session.play("tap-a")\n'
        shapes = {
            'an import inside the hook': (head + 'def ok(ctx):\n    from helper import press\n    press(ctx)\n', helper),
            'the module imported inside the hook': (head + 'def ok(ctx):\n    import helper as h\n    h.press(ctx)\n', helper),
            'a helper that names its context differently': (head + 'import helper\ndef ok(ctx):\n    helper.press(ctx)\n', renamed),
            'the context passed by name': (head + 'import helper\ndef ok(ctx):\n    helper.press(context=ctx)\n', renamed),
            'a hook that names its own context differently': (head + 'import helper\ndef ok(run):\n    helper.press(run)\n', renamed),
            'a context kept in a local': (head + 'import helper\ndef ok(ctx):\n    helper.press(ctx)\n', kept),
            'a star import': (head + 'from helper import *\ndef ok(ctx):\n    press(ctx)\n', helper),
            'a module-level name for the helper': (head + 'import helper\nok = helper.press\n', renamed),
        }
        for label, (steps, source) in shapes.items():
            self.plan['segments'].pop('tap-a', None)
            self.assertEqual(self.play_gate({'steps.py': steps, 'helper.py': source}),
                             (['G8 helper.py: ctx.play("tap-a") names no segment of this plan'], [], []), label)
            self.plan['segments']['tap-a'] = {'file': 'tap-a.json'}
            self.assertEqual(self.play_gate({}), ([], [], ['tap-a']), label)
        # Where the gate cannot tell that a call is a play of the context, it says so; it does not pass over it.
        warning = ['G8 helper.py: 1 play call(s) with a computed name or unknown callee are not checked']
        session = ('class Session:\n    def __init__(self, context):\n        self.run = context\n'
                   '    def press(self):\n        self.run.play("tap-a")\n')
        for label, steps, source in (
                ('a context passed with *', head + 'import helper\ndef ok(ctx):\n    helper.press(*[ctx])\n', renamed),
                ('a context kept under another attribute',
                 head + 'import helper\ndef ok(ctx):\n    helper.Session(ctx).press()\n', session),
                ('a context handed on through a callback',
                 head + 'import helper\ndef ok(ctx):\n    helper.each(helper.press, ctx)\n',
                 renamed + 'def each(fn, context):\n    fn(context)\n')):
            self.assertEqual(self.play_gate({'steps.py': steps, 'helper.py': source}), ([], warning, []), label)

    def test_play_names_the_gate_can_resolve(self):
        self.plan['segments'].update({name: {'file': f'{name}.json'} for name in ('tap-a', 'tap-b', 'tap-x', 'hold')})
        tables = ('TAP = {"A": "tap-a", "B": "tap-b", "X": "tap-x"}\nHOLD = "hold"\n'
                  'FIRST, SECOND = TAP["A"], TAP["B"]\n')

        def gate(body, extra=''):
            steps = (self.HOOK_HEAD + 'import tables\nfrom tables import TAP, HOLD\n' + extra
                     + 'def ok(ctx, fast=False):\n' + body)
            return self.play_gate({'tables.py': tables, 'steps.py': steps})

        # An entry of a table written out in a module, also through the module's name.
        self.assertEqual(gate('    ctx.play(TAP["A"])\n'), ([], [], ['tap-a']))
        self.assertEqual(gate('    ctx.play(tables.TAP["B"])\n'), ([], [], ['tap-b']))
        # A key the gate cannot tell: every entry of the table.
        self.assertEqual(gate('    for button in pick():\n        ctx.play(TAP[button])\n'),
                         ([], [], ['tap-a', 'tap-b', 'tap-x']))
        # A choice between names, a module constant, a local set once.
        self.assertEqual(gate('    ctx.play(TAP["X"] if fast else HOLD)\n'), ([], [], ['hold', 'tap-x']))
        self.assertEqual(gate('    name = tables.HOLD\n    ctx.play(name)\n'), ([], [], ['hold']))
        # Two module constants set in one statement.
        self.assertEqual(gate('    ctx.play(tables.FIRST if fast else tables.SECOND)\n'), ([], [], ['tap-a', 'tap-b']))
        # A play on the context an object keeps. It was neither checked nor warned about.
        session = ('class Session:\n    def __init__(self, ctx):\n        self.ctx = ctx\n'
                   '    def press(self):\n        self.ctx.play(TAP["A"])\n')
        self.assertEqual(gate('    Session(ctx).press()\n', session), ([], [], ['tap-a']))
        # `from routes import play` is the library call.
        self.assertEqual(gate('    play(ctx, "tap-a1")\n    play(ctx, "no-such-macro")\n', 'from routes import play\n'),
                         (['G8 steps.py: routes.play("no-such-macro") names no routes library macro'], [], []))
        # A table entry that is no segment of the plan.
        del self.plan['segments']['tap-x']
        self.assertEqual(gate('    ctx.play(TAP["X"])\n'),
                         (['G8 steps.py: ctx.play("tap-x") names no segment of this plan'], [], []))
        # What stays unchecked: a key the table does not have, a local set twice, a name built at run time, a
        # table the gate cannot read, a table name that is a local here, a play that goes around the context.
        warning = ['G8 steps.py: 1 play call(s) with a computed name or unknown callee are not checked']
        for body in ('    ctx.play(TAP["Y"])\n', '    name = HOLD\n    name = pick()\n    ctx.play(name)\n',
                     '    ctx.play(f"tap-{pick()}")\n', '    ctx.play(dict(TAP)["A"])\n',
                     '    TAP = pick()\n    ctx.play(TAP["A"])\n', '    ctx.runner.play(ctx.on, "hold")\n'):
            self.assertEqual(gate(body), ([], warning, []), body)

    def test_a_play_name_passed_to_a_helper_is_followed(self):
        self.plan['segments'].update({name: {'file': f'{name}.json'}
                                      for name in ('tap-a', 'tap-b', 'take-p1', 'take-cpu')})
        helper = ('def press(ctx, name, times=1):\n    for _ in range(times):\n        ctx.play(name)\n'
                  'def relay(ctx, which):\n    press(ctx, which)\n'
                  'def again(ctx, name, left=2):\n    ctx.play(name)\n    if left:\n        again(ctx, name, left - 1)\n')

        def gate(body, rows=None):
            steps = self.HOOK_HEAD + 'import helper\ndef ok(ctx, take="take-p1"):\n' + body
            return self.play_gate({'helper.py': helper, 'steps.py': steps}, rows)

        # The argument at the call, by place and by name; a helper that calls itself.
        self.assertEqual(gate('    helper.press(ctx, "tap-a")\n    helper.press(ctx, name="tap-b", times=2)\n'),
                         ([], [], ['tap-a', 'tap-b']))
        self.assertEqual(gate('    helper.again(ctx, "tap-a")\n'), ([], [], ['tap-a']))
        # Through a second helper back to the hook's own parameter: its default, then what the manifest passes.
        self.assertEqual(gate('    helper.relay(ctx, take)\n'), ([], [], ['take-p1']))
        step = {'run': 'steps.py:ok', 'args': {'take': 'take-cpu'}}
        self.assertEqual(gate('    helper.relay(ctx, take)\n', [{'id': 'r1', 'steps': [step]}]), ([], [], ['take-cpu']))
        self.assertEqual(gate('    helper.relay(ctx, take)\n', [{'id': 'r1', 'steps': [step, {'run': 'steps.py:ok'}]}]),
                         ([], [], ['take-cpu', 'take-p1']))
        # A name that is no segment, passed down to a play: the gate cannot tell whether it reaches the play, so
        # it does not refuse it; the play is not checked, and the gate says so.
        warning = ['G8 helper.py: 1 play call(s) with a computed name or unknown callee are not checked']
        step['args']['take'] = 'take-p2'
        self.assertEqual(gate('    helper.relay(ctx, take)\n', [{'id': 'r1', 'steps': [step]}]), ([], warning, []))
        # Not followed, so the play warns: an argument built at run time, a helper handed on as a value, arguments
        # passed with *, a parameter the helper changes, a manifest argument that is not a name.
        rows = [{'id': 'r1', 'steps': [{'run': 'steps.py:ok'}]}]
        for body in ('    helper.press(ctx, pick())\n', '    helper.press(ctx, "tap-a")\n    queue(helper.press)\n',
                     '    helper.press(*pick())\n'):
            self.assertEqual(gate(body, rows), ([], warning, []), body)
        changed = helper.replace('def relay(ctx, which):\n', 'def relay(ctx, which):\n    which = which or pick()\n')
        self.assertNotEqual(changed, helper)
        (self.base / 'helper.py').write_text(changed)
        self.assertEqual(self.play_gate({'steps.py': self.HOOK_HEAD + 'import helper\ndef ok(ctx):\n'
                                         '    helper.relay(ctx, "tap-a")\n'}), ([], warning, []))
        step['args']['take'] = 3
        self.assertEqual(gate('    helper.relay(ctx, take)\n', [{'id': 'r1', 'steps': [step]}]), ([], warning, []))

    def test_a_helper_that_plays_from_two_namespaces_is_not_refused(self):
        # One helper, two ways to play: a segment of the plan through ctx.play, anything else through the routes
        # library. `tap-a` is a segment only, `tap-a1` a library macro only; both calls are valid.
        self.plan['segments']['tap-a'] = {'file': 'tap-a.json'}
        steps = self.HOOK_HEAD + 'import helper\ndef ok(ctx):\n    helper.press(ctx, "tap-a")\n    helper.press(ctx, "tap-a1")\n'

        def gate(segment_play, library_play, extra=''):
            helper = ('import routes\nTAP = {"A": "tap-a", "Y": "tap-y"}\n'
                      'def press(ctx, name):\n    if name in ctx.runner.plan.get("segments", {}):\n'
                      f'        {segment_play}\n    else:\n        {library_play}\n' + extra)
            return self.play_gate({'steps.py': steps, 'helper.py': helper})

        # The names a parameter can hold are names a play may take, not names it must take. Without a declaration
        # the gate cannot tell which reach which play: it refuses nothing, checks nothing, and says so.
        warning = 'G8 helper.py: {} play call(s) with a computed name or unknown callee are not checked'
        self.assertEqual(gate('ctx.play(name)', 'routes.play(ctx, name)'), ([], [warning.format(2)], []))
        # Each play declares its own closed set: checked, with no complaint about the other branch's name.
        self.assertEqual(gate('ctx.play(name, among=("tap-a",))', 'routes.play(ctx, name, among=("tap-a1",))'),
                         ([], [], ['tap-a']))
        # Still refused: a declared name of the wrong kind, a written name that can never be played, and a written
        # name outside its own set.
        self.assertEqual(gate('ctx.play(name, among=("tap-a1",))', 'routes.play(ctx, name, among=("tap-a",))'), ([
            'G8 helper.py: ctx.play among "tap-a1" names no segment of this plan',
            'G8 helper.py: routes.play among "tap-a" names no routes library macro'], [], []))
        self.assertEqual(gate('ctx.play("tap-a1")', 'routes.play(ctx, "tap-a")'), ([
            'G8 helper.py: ctx.play("tap-a1") names no segment of this plan',
            'G8 helper.py: routes.play("tap-a") names no routes library macro'], [], []))
        self.assertEqual(gate('ctx.play("tap-a", among=("tap-b",))', 'routes.play(ctx, name, among=("tap-a1",))'), ([
            'G8 helper.py: ctx.play among "tap-b" names no segment of this plan',
            'G8 helper.py: ctx.play("tap-a") is not among the names it declares'], [], []))
        # A table read with a key the gate cannot tell is the same kind of set: an entry that is no segment makes
        # the play unchecked, not refused. With a written key it is refused.
        self.assertEqual(gate('ctx.play(TAP[name])', 'routes.play(ctx, name, among=("tap-a1",))'),
                         ([], [warning.format(1)], []))
        self.assertEqual(gate('ctx.play(TAP["Y"])', 'routes.play(ctx, name, among=("tap-a1",))'),
                         (['G8 helper.py: ctx.play("tap-y") names no segment of this plan'], [], []))

    def test_every_place_a_manifest_names_a_hook_is_read(self):
        self.plan['segments'].update({f'seg-{name}': {'file': f'{name}.json'} for name in 'abcde'})
        helper = ''.join(f'def play_{name}(ctx):\n    ctx.play("seg-{name}")\n' for name in 'abcde')
        steps = (self.HOOK_HEAD + 'import helper\n'
                 'def step(ctx):\n    helper.play_a(ctx)\n'
                 'def recover(ctx):\n    helper.play_b(ctx)\n'
                 'def failed(ctx):\n    helper.play_c(ctx)\n'
                 'def plan_failed(ctx):\n    helper.play_d(ctx)\n'
                 'def ready(ctx, gate):\n    helper.play_e(ctx)\n    return True\n')
        self.plan['on_failure_hook'] = {'run': 'steps.py:plan_failed'}
        self.plan['gates'] = {'ready': {'kind': 'hook', 'module': 'steps.py', 'function': 'ready'}}
        rows = [{'id': 'r1', 'steps': [{'run': 'steps.py:step'}], 'recover': [{'run': 'steps.py:recover'}],
                 'on_failure_hook': 'steps.py:failed'}]
        self.assertEqual(self.play_gate({'helper.py': helper, 'steps.py': steps}, rows),
                         ([], [], ['seg-a', 'seg-b', 'seg-c', 'seg-d', 'seg-e']))

    def test_play_names_use_the_actual_callees_namespace(self):
        self.plan['segments']['plan-only'] = {'file': 'plan-only.json'}
        (self.base / 'steps.py').write_text(
            'import routes as r\ndef ok(ctx):\n    ctx.play("tap-zr1")\n    r.play(ctx, "plan-only")\n')
        self.assertEqual(self.errors(), [
            'G8 steps.py: ctx.play("tap-zr1") names no segment of this plan',
            'G8 steps.py: routes.play("plan-only") names no routes library macro'])
        self.plan['segments']['tap-zr1'] = {'file': 'tap-zr1.json'}
        (self.base / 'steps.py').write_text(
            'import routes as r\ndef ok(ctx):\n    ctx.play("tap-zr1")\n'
            '    r.play(ctx, "tap-a1")\n    r.play(ctx, "smash-main")\n')
        self.assertEqual(self.errors(), [])

    def test_a_declared_set_of_play_names_is_checked(self):
        self.plan['segments'].update({'take-p1': {'file': 'p1.json'}, 'take-cpu': {'file': 'cpu.json'}})

        def gate(source):
            (self.base / 'steps.py').write_text(source)
            result = packet_gate.check(self.plan, self.base, self.plan)
            return result['errors'], result['warnings']

        warning = ['G8 steps.py: 1 play call(s) with a computed name or unknown callee are not checked']
        head = 'def ok(ctx):\n    name = pick()\n'
        # No declaration: the computed name is not checked, and the gate says so.
        self.assertEqual(gate(head + '    ctx.play(name)\n'), ([], warning))
        # A declared closed set whose names all resolve is checked: no warning.
        self.assertEqual(gate(head + '    ctx.play(name, among=("take-p1", "take-cpu"))\n'), ([], []))
        self.assertEqual(gate(head + '    ctx.play(name, among=["take-p1"])\n'), ([], []))
        # A set written once at module level.
        self.assertEqual(gate('TAKES = ("take-p1", "take-cpu")\n' + head + '    ctx.play(name, among=TAKES)\n'), ([], []))
        # A declared name that does not resolve is refused.
        self.assertEqual(gate(head + '    ctx.play(name, among=("take-p1", "take-p2"))\n'),
                         (['G8 steps.py: ctx.play among "take-p2" names no segment of this plan'], []))
        # routes.play is checked against the routes library, not the plan.
        route = 'import routes\ndef ok(ctx):\n    name = pick()\n'
        self.assertEqual(gate(route + '    routes.play(ctx, name, among=("tap-a1", "smash-main"))\n'), ([], []))
        self.assertEqual(gate(route + '    routes.play(ctx, name, among=("tap-a1", "take-p1"))\n'),
                         (['G8 steps.py: routes.play among "take-p1" names no routes library macro'], []))
        # A set the gate cannot read is no declaration: built at run time, a parameter, a name bound twice, empty.
        for among in ('names()', 'takes', '()', 'TWICE', '("take-p1", 3)'):
            source = ('TWICE = ("take-p1",)\nTWICE = ("take-cpu",)\n'
                      'def ok(ctx, takes=("take-p1",)):\n    name = pick()\n' f'    ctx.play(name, among={among})\n')
            self.assertEqual(gate(source), ([], warning), among)
        # A written name outside its own set is refused.
        self.assertEqual(gate('def ok(ctx):\n    ctx.play("take-cpu", among=("take-p1",))\n'),
                         (['G8 steps.py: ctx.play("take-cpu") is not among the names it declares'], []))
        # Also when another call of the same hook plays that name.
        self.assertEqual(gate('def ok(ctx):\n    ctx.play("take-cpu")\n    ctx.play("take-cpu", among=("take-p1",))\n'),
                         (['G8 steps.py: ctx.play("take-cpu") is not among the names it declares'], []))

    def test_a_hook_that_reads_a_key_the_build_does_not_write_is_refused(self):
        (self.base / 'steps.py').write_text(
            'def ok(ctx):\n    values = {}\n    return values.get("recording.page.rolle"), values["evidence.path"]\n')
        error = 'G2 steps.py: reads "recording.page.rolle", which the target does not write'
        self.assertEqual(self.errors(), [error])
        self.plan['reads_ignore'] = [{'key': 'recording.page.rolle', 'reason': ''}]
        self.assertEqual(self.errors(), [error])
        self.plan['reads_ignore'] = [{'key': 'recording.page.rolle', 'reason': 'planned'}]
        self.assertEqual(self.errors(), [])

    def test_an_available_provider_file_still_contributes_when_a_sibling_is_missing(self):
        self.git('rm', '-q', 'src/model/record_place.rs')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'partial provider')
        self.plan['target']['plugin']['commit'] = self.git('rev-parse', 'HEAD').stdout.strip()
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return values.get("zone.lefft")\n')
        self.assertEqual(self.errors(), ['G2 steps.py: reads "zone.lefft", which the target does not write'])

    def test_the_read_net_includes_declared_bridge_providers(self):
        self.plan['target']['providers'] = {'native.ui': {'repo': 'bridge', 'files': ['src/proto2.rs']}}
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return values["ui.focuz"]\n')
        self.assertEqual(self.errors(), ['G2 steps.py: reads "ui.focuz", which the target does not write'])

    @unittest.skipUnless(os.environ.get('PACKET_GATE_TR_TREE') and os.environ.get('PACKET_GATE_WITNESS'),
                         'recorded TrainingRooms witness not supplied')
    def test_recorded_tr_menu_witness(self):
        files = ['src/model/bridge_state.rs', 'src/model/record_place.rs']
        keys = packet_gate.emitted_keys(Path(os.environ['PACKET_GATE_TR_TREE']),
                                        os.environ.get('PACKET_GATE_TR_COMMIT', 'HEAD'), files)
        witness = json.loads(Path(os.environ['PACKET_GATE_WITNESS']).read_text())['baseline']['values']
        expected = set(witness) - {'schema'}
        print('extractor keys not in witness:', sorted(keys - expected))
        self.assertEqual(expected - keys, set())

    @unittest.skipUnless(os.environ.get('PACKET_GATE_TR_TREE')
                         and os.environ.get('PACKET_GATE_TR_COMMIT', '').startswith('e7667f26'),
                         'TrainingRooms tree at e7667f26 not supplied')
    def test_practice_keys_at_install_3(self):
        keys = packet_gate.emitted_keys(Path(os.environ['PACKET_GATE_TR_TREE']), os.environ['PACKET_GATE_TR_COMMIT'],
                                        ['src/model/practice_state.rs'])
        self.assertEqual({key for key in keys if key.startswith('cpu.')},
                         {f'cpu.{entry}.ai_thinks' for entry in range(1, 8)})
        self.assertEqual({'step', 'score', 'streak', 'best', 'attempts', 'hits', 'wizard.step', 'last.outcome',
                          'attempt.frames', 'target.count', 'wizard.value'} - keys, set())

    @unittest.skipUnless(os.environ.get('PACKET_GATE_TR_TREE') and os.environ.get('PACKET_GATE_FED_LOG'),
                         'recorded fed witness not supplied')
    def test_recorded_fed_witness(self):
        keys = packet_gate.emitted_keys(Path(os.environ['PACKET_GATE_TR_TREE']),
                                        os.environ.get('PACKET_GATE_TR_COMMIT', 'HEAD'),
                                        ['src/model/bridge_events.rs'])
        lines = [line for line in Path(os.environ['PACKET_GATE_FED_LOG']).read_text().splitlines() if ' fed p1=' in line]
        self.assertTrue(lines, 'fed witness has no fed p1 lines')
        for line in lines:
            payload = line.split(' fed ', 1)[1]
            payload = re.sub(r'(?: ev=\d+)? frame=\d+ wall=\d+$', '', payload)
            expected = {'fed ' + key for key in re.findall(r'(?:^| )([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)=', payload)}
            self.assertEqual(expected - keys, set())

    @unittest.skipUnless(os.environ.get('PACKET_GATE_BRIDGE_TREE') and os.environ.get('PACKET_GATE_BRIDGE_COMMIT')
                         and os.environ.get('PACKET_GATE_BRIDGE_LOG'), 'recorded bridge witness not supplied')
    def test_recorded_bridge_fighter_witness(self):
        keys = packet_gate._provider_keys(Path(os.environ['PACKET_GATE_BRIDGE_TREE']),
                                          os.environ['PACKET_GATE_BRIDGE_COMMIT'],
                                          packet_gate.PROVIDERS['bridge'])
        lines = [line for line in Path(os.environ['PACKET_GATE_BRIDGE_LOG']).read_text().splitlines()
                 if '[Bridge] fighter entry=' in line]
        self.assertTrue(lines, 'bridge witness has no fighter entry lines')
        words = {key for line in lines
                 for key in re.findall(r'(?:^| )([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)=', line)}
        missing = {key for key in words if key not in keys and f'fighter {key}' not in keys}
        print('bridge witness keys the extractor lacks:', sorted(missing))
        self.assertEqual(missing, set())

    @unittest.skipUnless(os.environ.get('PACKET_GATE_BRIDGE_TREE'), 'bridge source not supplied')
    def test_real_bridge_95abb28_is_scanned(self):
        tree = os.environ['PACKET_GATE_BRIDGE_TREE']
        self.plan['target']['bridge'] = {'tree': tree, 'commit': '95abb2829574c49f28789cd2a9c3df63a1181d0b'}
        positive = ('schema', 'screen', 'observation.active', 'input.ready', 'screen.gen', 'picker.phase',
                    'focus.index', 'focus.label', 'ldn.members', 'ldn.room.key', 'ldn.net.key', 'ldn.net.session',
                    'pause.open', 'pause.cpu_behavior', 'pause.panel')
        self.plan['rows'][0]['reads'] = [f'ssbu.ui:{key}' for key in positive]
        report = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(report['errors'], [])
        print('95abb28 real block keys:', len(report['emitted']['ssbu.ui']))
        # This bridge no longer writes ldn.room.session (it writes ldn.net.session); the gate follows the source.
        for key in ('css.training', 'tile.cursor', 'ui ready', 'ready', 'menu.admission', 'ldn.create',
                    'ldn.room.session'):
            self.plan['rows'][0]['reads'] = [f'ssbu.ui:{key}']
            self.assertEqual(self.errors(), ['G2 row r1: ssbu.ui:' + key + ' is not written by bridge '
                                             '95abb282 (src/native_ui/mod.rs)'])

    @unittest.skipUnless(os.environ.get('PACKET_GATE_BRIDGE_TREE'), 'bridge source not supplied')
    def test_real_bridge_ui_block_provider(self):
        tree = os.environ['PACKET_GATE_BRIDGE_TREE']
        positive = ('schema', 'screen', 'observation.active', 'input.ready', 'screen.gen', 'picker.phase',
                    'focus.index', 'focus.label', 'ldn.members', 'ldn.room.session',
                    'pause.open', 'pause.cpu_behavior', 'pause.panel')
        negative = ('css.training', 'tile.cursor', 'ui ready', 'ready', 'menu.admission', 'ldn.create')
        for revision, accepted, refused, legacy in (
            ('c5efb86', positive, negative, False),
            ('ed2655d4', ('screen',), ('input.ready', 'pause.open'), False),
            ('c5efb86', positive, negative, True),
            ('2bf3d1c8ce790000be25b2348294b4a4fd2d6a03', positive, negative, True),
        ):
            commit = subprocess.run(['git', '-C', tree, 'rev-parse', f'{revision}^{{commit}}'],
                                    check=True, capture_output=True, text=True).stdout.strip()
            self.plan['target']['bridge'] = {'tree': tree, 'commit': commit}
            if legacy:
                self.plan['target']['providers'] = {'ssbu.ui': {'repo': 'bridge', 'files': ['src/native_ui/mod.rs']}}
            else:
                self.plan['target'].pop('providers', None)
            self.plan['rows'][0]['reads'] = [f'ssbu.ui:{key}' for key in accepted]
            report = packet_gate.check(self.plan, self.base, self.plan)
            self.assertEqual(report['errors'], [])
            warning = ('G2 target: providers.ssbu.ui repeats a built-in provider; '
                       'the built-in extraction is used (remove the entry)')
            self.assertEqual(report['warnings'].count(warning), int(legacy))
            for key in refused:
                self.plan['rows'][0]['reads'] = [f'ssbu.ui:{key}']
                report = packet_gate.check(self.plan, self.base, self.plan)
                self.assertEqual(report['errors'], [f'G2 row r1: ssbu.ui:{key} is not written by bridge '
                                                    f'{commit[:8]} (src/native_ui/mod.rs)'])
                self.assertEqual(report['warnings'].count(warning), int(legacy))


if __name__ == '__main__':
    unittest.main(verbosity=2)
