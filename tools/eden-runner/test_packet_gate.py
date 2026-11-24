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
import packet_gate  # noqa: E402


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
writeln!(out, "test.only={}", n);
''',
            'src/model/record_place.rs': 'writeln!(out, "recording.page.role={}", role);\n',
            'src/model/practice_state.rs': r'''
writeln!(out, "phase={phase}");
("wizard.step", fields.wizard_step);
writeln!(out, "wizard.x={x}\nwizard.y={y}");
''',
            'src/model/bridge_events.rs': r'''
write!(text, "fed p1={} cpu={} run.p1={} run.cpu={} preview={} hides={}", a, b, c, d, e, f);
write!(text, "hold dir={dir} polls={polls} fires={fires} jump={}");
write!(text, "room.panel row={row} index={index}");
write!(text, "room.panel applied partners={partners} behavior={behavior}");
write!(text, "clip ctx={} total={} in={} out={}");
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
        self.assertIn('G2 target: repos.tile.tree must be an existing absolute directory', self.errors())
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

    def test_shared_hook_without_entry_warns_or_refuses_and_fixtures_remain_exempt(self):
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

    def test_missing_hook_cases_warn_or_refuse_but_shared_fixtures_are_exempt(self):
        self.plan['rows'] = [{'id': 'trial', 'role': 'test', 'steps': [{'run': 'steps.py:ok', 'effect': True}]}]
        message = 'G9 row trial: effect hook steps.py:ok has no judge cases; its evidence is trusted'
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertIn(message, result['warnings'])
        self.plan['require_cases'] = True
        self.assertIn(message + ' (require_cases)', self.errors())
        self.plan['rows'][0]['steps'] = [{'run': 'fixtures:cpu_standing', 'effect': True}]
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertFalse(any(message.startswith('G9 ') for message in result['errors'] + result['warnings']))

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
        (self.base / 'steps.py').write_text('def ok(ctx, name="x"):\n    ctx.play(name)\n    ctx.play(name)\n')
        result = packet_gate.check(self.plan, self.base, self.plan)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['warnings'],
                         ['G8 steps.py: 2 play call(s) with a computed name or unknown callee are not checked'])

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
