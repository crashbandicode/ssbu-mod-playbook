#!/usr/bin/env python3
"""Offline packet gate tests with a temporary committed source tree."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
            'src/proto2.rs': 'writeln!(out, "screen={}", screen);\n',
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

    def test_a_row_that_reads_a_line_the_build_does_not_write_is_refused(self):
        row = self.plan['rows'][0]
        row['reads'] = ['tr.menu:fed.p1']
        errors = self.errors()
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(f'tr.menu:fed.p1 is not written by plugin {self.commit[:8]}', errors[0])
        row['reads'] = ['tr.menu:strip.aims', 'tr.menu:page', 'tr.menu:zone.left', 'tr.menu:rows',
                        'tr.practice:wizard.step', 'tr.practice:wizard.y', 'tr.practice:phase']
        self.assertEqual(self.errors(), [])
        row['reads'] = ['ssbu.ui:screen']
        self.assertIn('no extractor for provider ssbu.ui (add target.providers)', self.errors()[0])
        self.plan['target']['providers'] = {'ssbu.ui': {'repo': 'bridge', 'files': ['src/proto2.rs']}}
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

    def test_the_packet_digest_changes_with_any_file_and_ignores_out(self):
        initial = packet_gate.packet_digest(self.base)
        (self.base / 'out').mkdir()
        (self.base / 'out' / 'receipt.json').write_text('output')
        (self.base / 'packet-gate.json').write_text('receipt')
        (self.base / 'cache.pyc').write_text('cache')
        self.assertEqual(packet_gate.packet_digest(self.base), initial)
        (self.base / 'steps.py').write_text('def ok(ctx):\n    return False\n')
        self.assertNotEqual(packet_gate.packet_digest(self.base), initial)

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
