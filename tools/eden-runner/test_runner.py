#!/usr/bin/env python3
"""Offline tests for runner.py with a fake Eden: no emulator, no input. Run: python3 -I test_runner.py"""
import copy
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner  # noqa: E402
import routes  # noqa: E402

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


class FreshEden(FakeEden):
    def __init__(self, bridge_log, frames, colours=None):
        super().__init__({})
        self.bridge_log = bridge_log
        self.frames = list(frames)
        self.colours = list(colours or [])
        self.screenshot_reply = {'backend': 'window', 'uniform': False, 'warning': 'test warning'}

    def request(self, method, params):
        if method == 'ui.screenshot':
            if self.colours:
                self.colour = self.colours.pop(0)
            response = super().request(method, params)
            frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
            if self.bridge_log is not None:
                with self.bridge_log.open('a') as stream:
                    stream.write(f'[Bridge] boot=0xabc frame={frame} wall={frame * 17}\n')
            response['result'] = dict(self.screenshot_reply)
            return response
        return super().request(method, params)


class ReviewClassTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            'segments': {'shared': {'library': 'smash-main'}},
            'gates': {'shared-gate': {'library': 'main'}},
            'setup': [{'state': 'training', 'via': 'fixture', 'ref': 'settings',
                       'reviewed': {'path': 'reviewed.json', 'sha256': 'a' * 64}}],
            'recover': [{'run': 'routes:recover'}],
            'rows': [{'id': 'r1', 'start': 'menu', 'end': 'menu', 'start_gate': 'shared-gate',
                      'recover': [{'run': 'routes_online:recover'}],
                      'steps': [{'run': 'routes:to_main'}, {'play': 'shared'}, {'gate': 'shared-gate'},
                                {'shot': 'end'}]}],
        }

    def test_plan_identity_is_canonical_and_does_not_mutate_the_expanded_plan(self):
        plan = runner.expand_library(copy.deepcopy(self.plan))
        plan['segments']['shared'].update(sha256='recorded', recorded='when')
        plan['gates']['shared-gate'].update(sha256='acquired', acquired='when')
        before = copy.deepcopy(plan)
        expected = copy.deepcopy(plan)
        for entry in expected['segments'].values():
            entry.pop('sha256', None)
            entry.pop('recorded', None)
        for entry in expected['gates'].values():
            entry.pop('sha256', None)
            entry.pop('acquired', None)
        expected = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':'),
                                             default=str).encode()).hexdigest()
        self.assertEqual(runner.plan_identity(plan), expected)
        self.assertEqual(plan, before)

    def test_plan_identity_changes_for_meaningful_plan_edits(self):
        plan = runner.expand_library(copy.deepcopy(self.plan))
        plan['rows'][0]['steps'][0]['args'] = {'direction': 'up'}
        plan['rows'][0]['needs'] = ['x']
        plan['rows'][0]['gives'] = ['y']
        plan['target'] = {'plugin': {'commit': 'a'}}
        plan['segments']['shared']['recipe'] = [{'buttons': 'a'}]
        initial = runner.plan_identity(plan)
        changes = (
            ('row', lambda p: p['rows'][0].update(id='another')),
            ('step', lambda p: p['rows'][0]['steps'].append({'shot': 'extra'})),
            ('argument', lambda p: p['rows'][0]['steps'][0]['args'].update(direction='down')),
            ('needs', lambda p: p['rows'][0]['needs'].append('z')),
            ('gives', lambda p: p['rows'][0]['gives'].append('z')),
            ('target', lambda p: p['target']['plugin'].update(commit='b')),
            ('recipe', lambda p: p['segments']['shared']['recipe'][0].update(buttons='b')),
            ('file', lambda p: p['segments']['shared'].update(file='another.edenmacro.json')),
            ('start_gate', lambda p: p['segments']['shared'].update(start_gate='another')),
        )
        for name, change in changes:
            with self.subTest(name=name):
                changed = copy.deepcopy(plan)
                change(changed)
                self.assertNotEqual(runner.plan_identity(changed), initial)

    def test_shared_hooks_segments_gates_and_reviewed_setup_are_library_only(self):
        review = runner.review_class(self.plan, Path('.'))
        self.assertEqual((review['class'], review['private']), ('library-only', []))

    def test_private_step_segment_gate_and_setup_are_listed(self):
        variants = [
            ('row r1 step 0', lambda p: p['rows'][0]['steps'][0].update(run='project.py:open')),
            ('segment shared', lambda p: p['segments']['shared'].pop('library')),
            ('gate shared-gate', lambda p: p['gates']['shared-gate'].pop('library')),
            ('setup 0', lambda p: p.update(setup=[{'via': 'fixture'}])),
            ('setup 0', lambda p: p.update(setup=[{'via': 'record'}])),
        ]
        for where, change in variants:
            with self.subTest(where=where):
                plan = copy.deepcopy(self.plan)
                change(plan)
                report = runner.review_class(plan, Path('.'))
                self.assertEqual(report['class'], 'full')
                self.assertIn(where, [item['where'] for item in report['private']])
                self.assertTrue(all(item['what'] for item in report['private']))

    def test_nested_steps_recovery_and_failure_hook_are_listed(self):
        self.plan['rows'][0]['steps'][3] = {'gif': {'steps': [{'run': 'project.py:nested'}]}}
        self.plan['rows'][0]['recover'].append({'run': 'project.py:recover'})
        self.plan['rows'][0]['on_failure_hook'] = {'run': 'project.py:failure'}
        self.plan['recover'].append({'run': 'project.py:recover'})
        where = [item['where'] for item in runner.review_class(self.plan, Path('.'))['private']]
        self.assertIn('row r1 step 3', where)
        self.assertIn('row r1 recover 1', where)
        self.assertIn('row r1', where)
        self.assertIn('plan recover 1', where)

    def test_segment_and_row_start_gates_are_reviewed(self):
        self.plan['gates']['private'] = {'kind': 'status'}
        self.plan['segments']['shared']['start_gate'] = 'private'
        self.plan['rows'][0]['start_gate'] = 'private'
        where = [item['where'] for item in runner.review_class(self.plan, Path('.'))['private']]
        self.assertIn('segment shared', where)
        self.assertIn('gate private', where)

    def test_dry_run_includes_review_without_changing_result(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rows.json'
            path.write_text(json.dumps({'start': 'menu', 'rows': [
                {'id': 'r1', 'start': 'menu', 'end': 'menu', 'steps': [{'shot': 'x'}]}]}))
            report = runner.dry_run(path)
            self.assertEqual(report['result'], 'READY')
            self.assertEqual(report['review']['class'], 'full')
            self.assertEqual(report['review']['private'][0]['what'], 'no setup plan')
            plan = json.loads(path.read_text())
            plan['setup'] = [{'state': 'training', 'via': 'fixture', 'ref': 'settings',
                              'reviewed': {'path': 'reviewed.json', 'sha256': 'a' * 64}}]
            path.write_text(json.dumps(plan))
            self.assertEqual(runner.dry_run(path)['review'],
                             {'class': 'library-only', 'private': [], 'missing_reasons': [],
                              'composition': [
                                  {'where': 'plan', 'item': {'start': 'menu', '_keys': ['rows', 'setup', 'start']}},
                                  {'where': 'imports', 'item': {'segments': {}, 'gates': {}}},
                                  {'where': 'setup 0', 'item': plan['setup'][0]},
                                  {'where': 'row r1', 'item': {'id': 'r1', 'start': 'menu', 'end': 'menu',
                                                              '_keys': ['end', 'id', 'start', 'steps']}},
                                  {'where': 'row r1 step 0', 'item': {'shot': 'x'}}]})
            plan['setup'] = [{'via': 'fixture'}]
            path.write_text(json.dumps(plan))
            report = runner.dry_run(path)
            self.assertEqual((report['result'], report['errors']), ('READY', []))
            self.assertEqual(report['review']['class'], 'full')
            self.assertEqual(report['review']['private'][0]['where'], 'setup 0')
            path.write_text(json.dumps({'segments': {'missing': {'library': 'not-an-entry'}}}))
            report = runner.dry_run(path)
            self.assertEqual(report['result'], 'BROKEN')
            self.assertEqual(report['review'], {'class': 'full', 'private': [], 'missing_reasons': [],
                                                'composition': []})


class ReviewAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.manifest = self.dir / 'rows.json'
        (self.dir / 'custom.py').write_text('def boot(ctx):\n    return True\n')
        self.macro = self.dir / 'private.json'
        self.macro.write_text(json.dumps({'events': [{'frame': 1, 'buttons': 1}]}))
        self.plan = {
            'start': 'menu',
            'segments': {'route': {'library': 'smash-main'}},
            'gates': {'main': {'library': 'main'}, 'installed': {'library': 'installed'},
                      'smash-submenu': {'library': 'smash-submenu'}},
            'setup': [{'state': 'training', 'via': 'fixture', 'ref': 'settings',
                       'reviewed': {'path': 'reviewed.json', 'sha256': 'a' * 64}}],
            'rows': [{'id': 'r1', 'start': 'menu', 'end': 'menu',
                      'steps': [{'gate': 'main'}, {'play': 'route'}, {'gate': 'installed'},
                                {'run': 'routes:to_main'}, {'call': 'status'}]}],
        }

    def test_up_check_binds_the_target_to_plugin_pins_and_embedded_commit(self):
        plugin = self.dir / 'plugin.nro'
        plugin.write_bytes(b'plugin abcdef12 embedded')
        digest = sha(plugin)
        self.plan['target'] = {'plugin': {'commit': 'abcdef12' + '0' * 32, 'nro_sha256': digest}}
        self.manifest.write_text(json.dumps(self.plan))
        config = {'instances': [{'plugins': [{'path': str(plugin), 'sha256': digest}]}]}
        def check(config):
            with mock.patch.object(runner, 'dry_run', return_value={'errors': [], 'result': 'READY'}):
                return runner.up_check(self.manifest, config)
        self.assertEqual(check(config)['errors'], [])
        two = {'instances': [{'plugins': [{'path': str(plugin), 'sha256': '0' * 64}]},
                             {'plugins': [{'path': str(plugin), 'sha256': digest}]}]}
        self.assertEqual(check(two)['errors'], [])
        self.plan['target']['plugin']['nro_sha256'] = digest.upper()
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(check(config)['errors'], [])
        self.plan['target']['plugin']['nro_sha256'] = digest
        self.manifest.write_text(json.dumps(self.plan))
        mismatch = {'instances': [{'plugins': [{'path': str(plugin), 'sha256': '0' * 64}]}]}
        report = check(mismatch)
        self.assertEqual(report['result'], 'BROKEN')
        self.assertIn(f'packet gate: target NRO {digest[:12]}… is not a plugin pin of this run', report['errors'])
        plugin.write_bytes(b'other build')
        report = check(config)
        self.assertIn('packet gate: target NRO plugin.nro does not carry commit abcdef12', report['errors'])
        plugin.unlink()
        report = check(config)
        self.assertTrue(any('packet gate: target NRO plugin.nro cannot be read:' in e for e in report['errors']))
        self.plan['target']['plugin']['nro_sha256'] = 123
        self.manifest.write_text(json.dumps(self.plan))
        with mock.patch.object(runner, 'dry_run', return_value={'errors': ['invalid NRO hash'], 'result': 'BROKEN'}):
            self.assertEqual(runner.up_check(self.manifest, config)['result'], 'BROKEN')

    def test_up_check_mode_refuses_a_missing_target_without_instances(self):
        self.manifest.write_text(json.dumps(self.plan))
        result = subprocess.run([sys.executable, '-I', '-B', str(Path(runner.__file__)), 'up-check',
                                 str(self.manifest), '--out', str(self.dir / 'out')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue((self.dir / 'out/dry-run.json').is_file())
        self.assertTrue((self.dir / 'out/packet-gate.json').is_file())

    def report(self, plan):
        self.manifest.write_text(json.dumps(plan))
        report = runner.dry_run(self.manifest)
        self.assertEqual((report['result'], report['errors']), ('READY', []))
        return report

    def private_segment(self, plan):
        plan['segments']['mine'] = {'file': str(self.macro), 'sha256': sha(self.macro)}

    def test_clean_packet_is_library_only_and_reports_defaulted_roles(self):
        report = self.report(self.plan)
        self.assertEqual((report['review']['class'], report['review']['private']), ('library-only', []))
        self.assertEqual(report['roles_defaulted'], ['r1'])

    def test_reason_on_library_steps_and_setup_does_not_make_them_private(self):
        plan = copy.deepcopy(self.plan)
        plan['setup'][0]['reason'] = 'Reviewed fixture for this stage'
        plan['rows'][0]['steps'][3]['reason'] = 'Navigate to main'
        report = self.report(plan)
        self.assertEqual(report['review']['class'], 'library-only')
        self.assertEqual(report['review']['private'], [])
        self.assertEqual(report['review']['missing_reasons'], [])

    def test_private_reasons_and_missing_reasons_are_reported_without_blocking(self):
        plan = copy.deepcopy(self.plan)
        plan['setup'] = [{'via': 'hook', 'ref': 'custom.py:boot', 'reason': 'Custom staging'},
                         {'via': 'hook', 'ref': 'custom.py:other'}]
        plan['rows'][0]['steps'] = [{'run': 'custom.py:boot', 'reason': 'One-off operation'},
                                    {'call': 'settings.set', 'params': {}}]
        report = self.report(plan)
        self.assertEqual(report['review']['class'], 'full')
        entries = {item['where']: item for item in report['review']['private']}
        self.assertEqual(entries['setup 0']['reason'], 'Custom staging')
        self.assertEqual(entries['row r1 step 0']['reason'], 'One-off operation')
        self.assertEqual(report['review']['missing_reasons'], ['row r1 step 1', 'setup 1'])

    def test_setup_blocks_and_role_are_library_only(self):
        plan = copy.deepcopy(self.plan)
        plan['rows'][0].update(role='setup', steps=[
            {'run': 'routes:tr_open', 'args': {'page': 'cpu'}},
            {'run': 'routes:tr_close'},
            {'run': 'routes:set_cpu', 'args': {'fighter': 'Mario'}},
            {'run': 'routes:drill_observe', 'args': {'attempts': 1, 'cap': 10}},
            {'gate': 'main'},
        ])
        report = self.report(plan)
        self.assertEqual((report['review']['class'], report['review']['private']), ('library-only', []))
        row = next(item['item'] for item in report['review']['composition'] if item['where'] == 'row r1')
        self.assertEqual(row['role'], 'setup')

    def test_available_bridge_blocks_compose_as_library_only(self):
        plan = copy.deepcopy(self.plan)
        plan['setup'] = [{'state': 'training', 'via': 'hook', 'ref': 'routes:reach_training'}]
        names = ('reach_training', 'set_cpu', 'set_player', 'tr_settings', 'setup_state', 'tr_open',
                 'tr_goto', 'tr_set', 'tr_close', 'wizard_place', 'drill_start', 'drill_observe', 'drill_stop')
        for name in names:
            self.assertTrue(callable(getattr(routes, name, None)), name)
        plan['rows'][0].update(role='setup', steps=[{'run': f'routes:{name}'} for name in names]
                               + [{'gate': 'main'}])
        report = self.report(plan)
        self.assertEqual((report['review']['class'], report['review']['private']), ('library-only', []))
        row = next(item['item'] for item in report['review']['composition'] if item['where'] == 'row r1')
        self.assertEqual(row['role'], 'setup')

    def test_composition_distinguishes_reviewer_packets(self):
        first = copy.deepcopy(self.plan)
        first['gates']['confirm'] = {'library': 'main'}
        first['rows'][0].update(start_gate='confirm', steps=[
            {'gate': 'confirm', 'on': 'guest'},
            {'copy': 'settings', 'as': 'before', 'on': 'guest'},
            {'json_diff': {'source': 'settings', 'before': 'before', 'must_change': ['p1']}, 'on': 'guest'}])
        second = copy.deepcopy(first)
        second['gates']['confirm'] = {'library': 'installed'}
        second['rows'][0]['steps'][0]['on'] = 'host'
        second['rows'][0]['steps'][1]['on'] = 'host'
        second['rows'][0]['steps'][2]['on'] = 'host'
        second['rows'][0]['steps'][2]['json_diff'].pop('must_change')
        reviews = [self.report(plan)['review'] for plan in (first, second)]
        self.assertEqual([review['class'] for review in reviews], ['library-only', 'library-only'])
        self.assertNotEqual(reviews[0].get('composition', reviews[0].get('arguments')),
                            reviews[1].get('composition', reviews[1].get('arguments')))
        for plan, review in zip((first, second), reviews):
            entries = {entry['where']: entry['item'] for entry in review['composition']}
            self.assertEqual(entries['imports']['gates']['confirm'], plan['gates']['confirm']['library'])
            self.assertEqual(entries['row r1']['start_gate'], 'confirm')
            self.assertEqual(entries['row r1 step 0'], plan['rows'][0]['steps'][0])
            self.assertEqual(entries['row r1 step 1'], plan['rows'][0]['steps'][1])
            self.assertEqual(entries['row r1 step 2'], plan['rows'][0]['steps'][2])

    def test_composition_distinguishes_empty_row_recovery_override(self):
        first = copy.deepcopy(self.plan)
        first['recover'] = [{'play': 'route'}]
        first['rows'] = [
            {'id': 'r1', 'start': 'menu', 'end': 'menu', 'on_failure': 'continue', 'steps': [{'gate': 'main'}]},
            {'id': 'r2', 'start': 'menu', 'end': 'menu', 'start_gate': 'main', 'steps': [{'gate': 'main'}]}]
        second = copy.deepcopy(first)
        second['rows'][1]['recover'] = []
        self.assertEqual(runner.recover_steps_of(first, first['rows'][1]), [{'play': 'route'}])
        self.assertEqual(runner.recover_steps_of(second, second['rows'][1]), [])
        reviews = [self.report(plan)['review'] for plan in (first, second)]
        self.assertEqual([review['class'] for review in reviews], ['library-only', 'library-only'])
        self.assertNotEqual(reviews[0]['composition'], reviews[1]['composition'])
        first_row = next(e['item'] for e in reviews[0]['composition'] if e['where'] == 'row r2')
        second_row = next(e['item'] for e in reviews[1]['composition'] if e['where'] == 'row r2')
        self.assertNotIn('recover', first_row)
        self.assertEqual(second_row['recover'], [])
        self.assertNotIn('recover', first_row['_keys'])
        self.assertIn('recover', second_row['_keys'])

    def test_composition_reconstructs_clean_packet(self):
        plan = copy.deepcopy(self.plan)
        plan['setup'][0]['differs_only'] = ['menu.cpu_percent']
        plan['rows'][0]['recover'] = [{'call': 'status', 'params': {'side': 'guest'}}]
        plan['rows'].append({'id': 'r2', 'start': 'menu', 'end': 'menu',
                             'steps': [{'gate': 'main'}], 'recover': []})
        plan['recover'] = [{'gate': 'main'}, {'library': 'smash-main'}]
        variants = [plan, copy.deepcopy(plan), copy.deepcopy(plan), copy.deepcopy(plan)]
        variants[1]['recover'] = []
        variants[1]['setup'] = []
        del variants[2]['rows'][1]['recover']
        del variants[2]['recover']
        variants[3]['rows'][1]['steps'] = []
        for variant in variants:
            with self.subTest(keys=sorted(variant), second_row=variant['rows'][1]):
                self.manifest.write_text(json.dumps(variant))
                report = runner.dry_run(self.manifest)
                if variant['rows'][1]['steps']:
                    self.assertEqual((report['result'], report['errors']), ('READY', []))
                else:
                    self.assertEqual(report['result'], 'BROKEN')
                    self.assertIn('row r2: no steps', report['errors'])
                composition = report['review']['composition']
                header = dict(composition[0]['item'])
                plan_keys = header.pop('_keys')
                self.assertEqual(plan_keys, sorted(variant))
                imports = composition[1]['item']
                rebuilt = header
                for key in ('segments', 'gates'):
                    if key in plan_keys and key not in rebuilt:
                        rebuilt[key] = {alias: {'library': value} if isinstance(value, str) else value
                                        for alias, value in imports[key].items()}
                for key in ('setup', 'rows', 'recover'):
                    if key in plan_keys and key not in rebuilt:
                        rebuilt[key] = []
                row = None
                for entry in composition[2:]:
                    where, item = entry['where'], entry['item']
                    if where.startswith('setup '):
                        rebuilt['setup'].append(item)
                    elif where.startswith('row ') and ' step ' not in where and ' recover ' not in where:
                        row = dict(item)
                        row_keys = row.pop('_keys')
                        for key in ('steps', 'recover'):
                            if key in row_keys and key not in row:
                                row[key] = []
                        rebuilt['rows'].append(row)
                    elif ' step ' in where:
                        row['steps'].append(item)
                    elif where.startswith('row ') and ' recover ' in where:
                        row['recover'].append(item)
                    elif where.startswith('plan recover '):
                        rebuilt['recover'].append(item)
                self.assertEqual(rebuilt, variant)

    def test_composition_keeps_wrong_type_sections_and_empty_imports(self):
        for section in ('rows', 'segments', 'gates', 'setup', 'recover'):
            with self.subTest(section=section):
                raw = copy.deepcopy(self.plan)
                raw[section] = None
                item = runner.review_class(self.plan, self.dir, raw)['composition'][0]['item']
                self.assertIn(section, item['_keys'])
                self.assertIsNone(item[section])
        for section in ('steps', 'recover'):
            with self.subTest(section=section):
                raw = copy.deepcopy(self.plan)
                raw['rows'][0][section] = None
                composition = runner.review_class(self.plan, self.dir, raw)['composition']
                item = next(entry['item'] for entry in composition if entry['where'] == 'row r1')
                self.assertIn(section, item['_keys'])
                self.assertIsNone(item[section])
        raw = copy.deepcopy(self.plan)
        raw['segments'], raw['gates'], raw['recover'] = {}, {}, []
        raw['rows'][0]['steps'] = []
        self.manifest.write_text(json.dumps(raw))
        report = runner.dry_run(self.manifest)
        self.assertEqual(report['result'], 'BROKEN')
        self.assertIn('row r1: no steps', report['errors'])
        review = report['review']
        self.assertEqual(review['composition'][0]['item']['recover'], [])
        self.assertNotIn('segments', review['composition'][0]['item'])
        self.assertNotIn('gates', review['composition'][0]['item'])
        self.assertEqual(review['composition'][1]['item'], {'segments': {}, 'gates': {}})
        self.assertEqual(next(e['item'] for e in review['composition'] if e['where'] == 'row r1')['steps'], [])

    def test_full_composition_includes_raw_private_step_and_import_override(self):
        plan = copy.deepcopy(self.plan)
        plan['gates']['main']['tolerance'] = 255
        plan['rows'][0]['steps'].append({'ui': 'trigger', 'target': 'widget', 'on': 'guest'})
        review = self.report(plan)['review']
        self.assertEqual(review['class'], 'full')
        entries = {entry['where']: entry['item'] for entry in review['composition']}
        self.assertEqual(entries['imports']['gates']['main'], plan['gates']['main'])
        self.assertEqual(entries['row r1 step 5'], plan['rows'][0]['steps'][5])

    def test_composition_lists_raw_items_in_order(self):
        plan = copy.deepcopy(self.plan)
        plan['setup'][0].update(to='settings.json', instance='a', differs_only=['menu.cpu_percent'], ignored=True)
        plan['rows'][0]['steps'] = [
            {'gate': 'main'}, {'play': 'route'}, {'library': 'smash-main'},
            {'run': 'routes:wait_lines', 'args': {'lines': [['console', '.*']]}},
            {'call': 'status', 'params': {'selector': 'a'}},
            {'run': 'routes:to_main'}, {'call': 'status'}]
        plan['rows'][0]['recover'] = [{'call': 'status', 'params': {'selector': 'b'}},
                                     {'run': 'routes:wait_lines', 'args': {'lines': [['trace', 'ok']]}}]
        plan['recover'] = [{'gate': 'main'}, {'play': 'route'}, {'library': 'smash-main'}]
        report = self.report(plan)['review']
        self.assertEqual(report['class'], 'library-only')
        self.assertEqual(report['composition'], [
            {'where': 'plan', 'item': {'start': 'menu', '_keys': sorted(plan)}},
            {'where': 'imports', 'item': {'segments': {'route': 'smash-main'},
                                           'gates': {'main': 'main', 'installed': 'installed',
                                                     'smash-submenu': 'smash-submenu'}}},
            {'where': 'setup 0', 'item': plan['setup'][0]},
            {'where': 'row r1', 'item': {'id': 'r1', 'start': 'menu', 'end': 'menu',
                                        '_keys': sorted(plan['rows'][0])}},
            *[{'where': f'row r1 step {i}', 'item': step} for i, step in enumerate(plan['rows'][0]['steps'])],
            *[{'where': f'row r1 recover {i}', 'item': step} for i, step in enumerate(plan['rows'][0]['recover'])],
            *[{'where': f'plan recover {i}', 'item': step} for i, step in enumerate(plan['recover'])]])

        reordered = {key: plan[key] for key in ('start', 'rows', 'recover', 'setup', 'segments', 'gates')}
        reordered_report = self.report(reordered)['review']
        self.assertEqual(reordered_report['class'], 'library-only')
        self.assertEqual(reordered_report['composition'], report['composition'])

    def test_full_review_still_lists_composition(self):
        plan = copy.deepcopy(self.plan)
        plan['rows'][0]['steps'].append({'run': 'custom.py:boot', 'args': {'value': 7}})
        plan['rows'][0]['recover'] = [{'call': 'settings.set', 'params': {'key': 'value'}}]
        report = self.report(plan)['review']
        self.assertEqual(report['class'], 'full')
        self.assertIn({'where': 'row r1 step 5', 'item': plan['rows'][0]['steps'][5]}, report['composition'])
        self.assertIn({'where': 'row r1 recover 0', 'item': plan['rows'][0]['recover'][0]}, report['composition'])

    def test_broken_library_reference_has_empty_composition(self):
        self.manifest.write_text(json.dumps({'segments': {'bad': {'library': 'not-an-entry'}}}))
        report = runner.dry_run(self.manifest)
        self.assertEqual(report['result'], 'BROKEN')
        self.assertEqual(report['review']['composition'], [])

    def test_allowlist_rejects_reviewer_cases_and_unused_entries(self):
        ref = self.dir / 'private-gate.png'
        Image.new('RGB', (64, 36), RED).save(ref)

        def private_gate(plan, name):
            plan['gates'][name] = {'kind': 'image', 'reference': str(ref), 'sha256': sha(ref),
                                   'roi': [0, 0, 64, 36], 'cap': 1}

        def hook_gate(plan):
            private_gate(plan, 'routes.main')
            plan['rows'][0]['steps'].append({'run': 'routes:gate', 'args': {'name': 'main'}})

        def retry_gate(plan):
            private_gate(plan, 'menu')
            plan['recover'] = [{'call': 'status'}]

        def private_judge(plan):
            plan['judge'] = {'cmd': 'custom.py'}
            plan['rows'][0]['expected'] = 'a result'

        def unused_segment(plan):
            self.private_segment(plan)

        def scaled(plan):
            plan['segments'] = {}
            plan['gates'] = {'main': {'library': 'main'}}
            plan['rows'][0]['steps'] = [{'gate': 'main'}]
            plan['scale'] = 'fit'

        variants = [
            ('gate routes.main', hook_gate),
            ('gate menu', retry_gate),
            ('plan key judge', private_judge),
            ('plan key scale', scaled),
            ('gate unused', lambda p: private_gate(p, 'unused')),
            ('segment mine', unused_segment),
            ('plan key surprise', lambda p: p.update(surprise=True)),
            ('row r1 key surprise', lambda p: p['rows'][0].update(surprise=True)),
            ('step 1 key unchecked', lambda p: p['rows'][0]['steps'][1].update(unchecked=True)),
            ('step 5', lambda p: p['rows'][0]['steps'].append({'future_verb': 'x'})),
        ]
        for expected, change in variants:
            with self.subTest(expected=expected):
                plan = copy.deepcopy(self.plan)
                change(plan)
                review = self.report(plan)['review']
                self.assertEqual(review['class'], 'full')
                self.assertIn(expected, ' '.join(item['what'] for item in review['private']))

    def test_reviewer_cases_are_private(self):
        def raw_library_step(plan):
            self.private_segment(plan)
            plan['rows'][0]['steps'].append({'library': 'mine'})

        variants = [
            ('tolerance', lambda p: p['gates']['main'].update(tolerance=255)),
            ('pattern', lambda p: p['gates']['installed'].update(pattern='.*')),
            ('custom.py:boot', lambda p: p['setup'][0].update(via='hook', ref='custom.py:boot')),
            ('fixture', lambda p: p['setup'][0].update(reviewed=None)),
            ('plan key on_failure_hook', lambda p: p.update(on_failure_hook='custom.py:boot')),
            ('settings.set', lambda p: p['rows'][0]['steps'].append({'call': 'settings.set'})),
            ('segment mine', raw_library_step),
        ]
        for expected, change in variants:
            with self.subTest(expected=expected):
                plan = copy.deepcopy(self.plan)
                change(plan)
                report = self.report(plan)['review']
                self.assertEqual(report['class'], 'full')
                self.assertIn(expected, ' '.join(item['what'] for item in report['private']))

    def test_nested_gate_recovery_start_gate_and_setup_policy(self):
        plan = copy.deepcopy(self.plan)
        self.private_segment(plan)
        plan['gates']['main']['recover'] = 'mine'
        plan['gates']['smash-submenu']['tolerance'] = 255
        report = self.report(plan)['review']
        messages = ' '.join(item['what'] for item in report['private'])
        self.assertIn('segment mine', messages)
        self.assertIn('tolerance', messages)
        for setup in ([], [{'state': 'training', 'via': 'segment', 'ref': 'library:not-real'}],
                      [{'state': 'training', 'via': 'record', 'ref': 'mine'}]):
            with self.subTest(setup=setup):
                plan = copy.deepcopy(self.plan)
                plan['setup'] = setup
                self.assertEqual(self.report(plan)['review']['class'], 'full')

    def test_ui_is_private_and_shared_segment_setup_is_clean(self):
        plan = copy.deepcopy(self.plan)
        plan['setup'] = [{'state': 'training', 'via': 'segment', 'ref': 'library:smash-main'}]
        self.assertEqual(self.report(plan)['review']['class'], 'library-only')
        plan['rows'][0]['steps'].append({'ui': 'trigger'})
        review = self.report(plan)['review']
        self.assertEqual(review['class'], 'full')
        self.assertIn('ui', ' '.join(item['what'] for item in review['private']))


class RowRoleTests(unittest.TestCase):
    def test_default_and_explicit_roles(self):
        self.assertEqual(runner.row_role({'start': 'menu', 'end': 'training'}), 'setup')
        self.assertEqual(runner.row_role({'start': 'training', 'end': 'training'}), 'test')
        self.assertEqual(runner.row_role({'start': 'menu', 'end': 'training', 'role': 'test'}), 'test')
        self.assertEqual(runner.row_role({'start': 'training', 'end': 'training', 'role': 'setup'}), 'setup')

    def test_dry_run_refuses_other_role(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rows.json'
            path.write_text(json.dumps({'start': 'menu', 'rows': [
                {'id': 'r1', 'start': 'menu', 'end': 'menu', 'role': 'other', 'steps': [{'shot': 'x'}]}]}))
            report = runner.dry_run(path)
            self.assertEqual(report['result'], 'BROKEN')
            self.assertIn('role', ' '.join(report['errors']))
            self.assertIn('other', ' '.join(report['errors']))


class TextGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        fixture = Path(__file__).resolve().parent / 'fixtures/trace-text/set-text-excerpt.jsonl'
        self.trace = self.dir / 'trace.jsonl'
        shutil.copyfile(fixture, self.trace)
        self.plan = {'start': 'training', 'gates': {'pane': {
            'kind': 'text', 'pane': 'tr_crumb', 'pattern': 'CPU Character', 'cap': 1, 'whole_file': True}},
            'rows': [{'id': 'r1', 'start': 'training', 'end': 'training', 'steps': [{'gate': 'pane'}]}]}
        self.manifest = self.dir / 'rows.json'
        self.instances = {'a': {'socket': 'fake', 'trace_jsonl': str(self.trace)}}
        self.out = self.dir / 'out'

    def make(self):
        self.manifest.write_text(json.dumps(self.plan))
        return runner.Runner(self.manifest, self.instances, self.out, 15, client_factory=lambda socket: FakeEden({}))

    def fixture_record(self):
        return json.loads(self.trace.read_bytes().splitlines()[9])

    def write_records(self, *records):
        self.trace.write_text(''.join(json.dumps(record) + '\n' for record in records))

    def run_row_records(self, *records):
        r = self.make()

        def append():
            with self.trace.open('a') as f:
                f.write(''.join(json.dumps(record) + '\n' for record in records))

        with mock.patch.object(r, 'session_start', side_effect=append):
            return r.run()

    def test_row_unknown_length_invalidates_and_later_valid_recovers(self):
        valid = self.fixture_record()
        damaged = copy.deepcopy(valid)
        damaged.update(sequence='1732', frame='5524')
        damaged['registers'].pop('x2')
        damaged['captures'][1] = {'error': 'memory_unavailable'}
        recovered = copy.deepcopy(valid)
        recovered.update(sequence='1733', frame='5525')
        self.trace.write_bytes(b'')
        self.plan['gates']['pane'].pop('whole_file')
        self.plan['gates']['pane'].update(point_id=1, cap=.03)
        with self.assertRaises(RuntimeError) as caught:
            self.run_row_records(valid, damaged)
        self.assertIn('unreadable', str(caught.exception))
        self.assertIn('1732', str(caught.exception))
        self.assertIn('no length register', str(caught.exception))
        (self.out / 'STOP-INPUT').unlink()
        self.trace.write_bytes(b'')
        self.run_row_records(valid, damaged, recovered)
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['sequence'], evidence['scope']), (1733, 'row'))

    def test_row_missing_sequence_invalidates_earlier_match(self):
        valid = self.fixture_record()
        damaged = copy.deepcopy(valid)
        damaged.pop('sequence')
        self.trace.write_bytes(b'')
        self.plan['gates']['pane'].pop('whole_file')
        self.plan['gates']['pane'].update(point_id=1, cap=.03)
        with self.assertRaises(RuntimeError) as caught:
            self.run_row_records(valid, damaged)
        self.assertIn('unreadable', str(caught.exception))
        self.assertIn('no sequence or frame', str(caught.exception))

    def test_row_missing_address_or_point_invalidates_earlier_match(self):
        valid = self.fixture_record()
        self.plan['gates']['pane'].pop('whole_file')
        self.plan['gates']['pane'].update(point_id=1, cap=.03)
        for missing, why in (('address', 'no pane address'), ('point', 'no point id')):
            with self.subTest(missing=missing):
                damaged = copy.deepcopy(valid)
                damaged.update(sequence='1732', frame='5524')
                if missing == 'address':
                    damaged['registers'].pop('x0')
                else:
                    damaged.pop('point_id')
                self.trace.write_bytes(b'')
                with self.assertRaises(RuntimeError) as caught:
                    self.run_row_records(valid, damaged)
                self.assertIn('1732', str(caught.exception))
                self.assertIn(why, str(caught.exception))
                (self.out / 'STOP-INPUT').unlink()

    def test_row_loss_marker_invalidates_and_later_valid_recovers(self):
        valid = self.fixture_record()
        recovered = copy.deepcopy(valid)
        recovered.update(sequence='1733', frame='5525')
        loss = {'schema': 1, 'event': 'loss', 'records': '7'}
        self.trace.write_bytes(b'')
        self.plan['gates']['pane'].pop('whole_file')
        self.plan['gates']['pane'].update(point_id=1, cap=.03)
        with self.assertRaises(RuntimeError) as caught:
            self.run_row_records(valid, loss)
        self.assertIn('trace loss (7 records)', str(caught.exception))
        (self.out / 'STOP-INPUT').unlink()
        self.trace.write_bytes(b'')
        self.run_row_records(valid, loss, recovered)
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['sequence'], evidence['scope']), (1733, 'row'))

    def test_row_other_pane_name_does_not_invalidate_even_at_same_address(self):
        lines = self.trace.read_bytes().splitlines()
        valid, other = json.loads(lines[9]), json.loads(lines[5])
        other.update(sequence='1732', frame='5524', point_id=valid['point_id'])
        for same_address in (False, True):
            with self.subTest(same_address=same_address):
                other['registers']['x0'] = valid['registers']['x0'] if same_address else '0xdeadbeef'
                self.trace.write_bytes(b'')
                self.plan['gates']['pane'].pop('whole_file', None)
                self.plan['gates']['pane'].update(point_id=1, cap=.03)
                (self.out / 'r1.json').unlink(missing_ok=True)
                self.run_row_records(valid, other)
                evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
                self.assertEqual((evidence['sequence'], evidence['scope']), (1730, 'row'))

    def test_row_transition_uses_only_row_and_reports_prior_scope(self):
        lines = self.trace.read_bytes().splitlines()
        empty, valid = json.loads(lines[1]), json.loads(lines[9])
        repeat = copy.deepcopy(valid)
        repeat.update(sequence='1732', frame='5524')
        self.write_records(empty, valid)
        self.plan['gates']['pane'].pop('whole_file')
        self.plan['gates']['pane'].update(point_id=1, cap=.03)
        self.run_row_records(repeat)
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['scope'], evidence['transition'], evidence['previous']), ('row', 'sampled', None))
        self.assertEqual(evidence['before_scope'],
                         {'text': 'Fighters & Stages  >  CPU Character   page 1 of 1',
                          'sequence': 1730, 'frame': 5523, 'pane': valid['registers']['x0']})

        (self.out / 'r1.json').unlink()
        self.write_records(empty, valid)
        changed = copy.deepcopy(empty)
        changed.update(sequence='1732', frame='5524')
        repeat.update(sequence='1733', frame='5525')
        self.run_row_records(changed, repeat)
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['transition'], evidence['previous']['sequence']), ('measured', 1732))
        self.assertEqual(evidence['before_scope']['sequence'], 1730)

    def test_text_unknown_capture_fails_and_newer_good_record_recovers(self):
        valid = self.fixture_record()
        self.assertGreater(int(valid['registers']['x2'], 0), 0)
        damaged = copy.deepcopy(valid)
        damaged['sequence'], damaged['frame'] = '1732', '5524'
        damaged['captures'][1] = {'error': 'memory_unavailable'}
        self.write_records(valid, damaged)
        self.plan['gates']['pane'].update(whole_file=True, point_id=1, cap=.03)
        with self.assertRaisesRegex(RuntimeError, 'unreadable.*1732'):
            self.make().run()
        (self.out / 'STOP-INPUT').unlink()
        recovered = copy.deepcopy(valid)
        recovered['sequence'], recovered['frame'] = '1733', '5525'
        with self.trace.open('a') as f:
            f.write(json.dumps(recovered) + '\n')
        self.make().run()
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['sequence'], evidence['frame'], evidence['scope']), (1733, 5525, 'whole_file'))

    def test_pane_capture_loss_for_known_key_fails(self):
        valid = self.fixture_record()
        damaged = copy.deepcopy(valid)
        damaged['sequence'], damaged['frame'] = '1732', '5524'
        damaged['captures'][0] = {'error': 'memory_unavailable'}
        self.write_records(valid, damaged)
        self.plan['gates']['pane'].update(whole_file=True, point_id=1, cap=.03)
        with self.assertRaisesRegex(RuntimeError, 'unreadable.*1732'):
            self.make().run()

    def test_text_capture_error_without_length_is_unknown(self):
        valid = self.fixture_record()
        damaged = copy.deepcopy(valid)
        damaged['sequence'], damaged['frame'] = '1732', '5524'
        damaged['registers'].pop('x2')
        damaged['captures'][1] = {'error': 'memory_unavailable'}
        self.assertEqual(runner.trace_text.parse(json.dumps(damaged))['why'], 'no length register')
        self.write_records(damaged)
        self.plan['gates']['pane'].update(whole_file=True, point_id=1, cap=.03)
        with self.assertRaisesRegex(RuntimeError, 'unreadable.*1732.*no length register'):
            self.make().run()

    def test_unrelated_pane_capture_loss_does_not_invalidate(self):
        valid = self.fixture_record()
        damaged = copy.deepcopy(valid)
        damaged['sequence'], damaged['frame'] = '1732', '5524'
        damaged['registers']['x0'] = '0xdeadbeef'
        damaged['captures'][0] = {'error': 'memory_unavailable'}
        self.write_records(valid, damaged)
        self.plan['gates']['pane'].update(whole_file=True, point_id=1, cap=.03)
        self.make().run()
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['sequence'], evidence['scope']), (1730, 'whole_file'))

    def test_pane_capture_loss_from_another_point_does_not_invalidate(self):
        valid = self.fixture_record()
        damaged = copy.deepcopy(valid)
        damaged['sequence'], damaged['frame'], damaged['point_id'] = '1732', '5524', '2'
        damaged['captures'][0] = {'error': 'memory_unavailable'}
        self.write_records(valid, damaged)
        self.plan['gates']['pane'].update(whole_file=True, point_id=1, cap=.03)
        self.make().run()
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['sequence'], str(evidence['point_id'])), (1730, '1'))

    def test_text_match_without_sequence_and_frame_is_not_accepted(self):
        valid = self.fixture_record()
        del valid['sequence']
        del valid['frame']
        self.write_records(valid)
        self.plan['gates']['pane'].update(whole_file=True, cap=.03)
        with self.assertRaisesRegex(RuntimeError, 'unreadable.*no sequence or frame'):
            self.make().run()

    def test_text_gate_defaults_to_row_scope_and_whole_file_is_explicit(self):
        valid = self.fixture_record()
        self.write_records(valid)
        self.plan['gates']['pane']['cap'] = .03
        del self.plan['gates']['pane']['whole_file']
        with self.assertRaisesRegex(RuntimeError, 'did not pass within'):
            self.make().run()
        (self.out / 'STOP-INPUT').unlink()
        self.plan['gates']['pane']['whole_file'] = True
        self.make().run()
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['scope'], evidence['sequence']), ('whole_file', 1730))

    def test_text_gate_accepts_a_record_written_after_row_start(self):
        valid = self.fixture_record()
        self.trace.write_bytes(b'')
        del self.plan['gates']['pane']['whole_file']
        r = self.make()
        with mock.patch.object(r, 'session_start', side_effect=lambda: self.write_records(valid)):
            r.run()
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['scope'], evidence['sequence']), ('row', 1730))

    def test_empty_text_capture_error_written_during_row_passes(self):
        empty = self.trace.read_bytes().splitlines(keepends=True)[0]
        self.trace.write_bytes(b'')
        self.plan['gates']['pane'].update(pane='tr_tab_00', pattern='^$', cap=.05)
        del self.plan['gates']['pane']['whole_file']
        r = self.make()
        with mock.patch.object(r, 'session_start', side_effect=lambda: self.trace.write_bytes(empty)):
            r.run()
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['scope'], evidence['sequence'], evidence['text']), ('row', 75, ''))

    def test_row_scope_uses_pre_row_key_for_unreadable_sample(self):
        valid = self.fixture_record()
        damaged = copy.deepcopy(valid)
        damaged['sequence'], damaged['frame'] = '1732', '5524'
        damaged['captures'][0] = {'error': 'memory_unavailable'}
        self.write_records(valid)
        del self.plan['gates']['pane']['whole_file']
        self.plan['gates']['pane'].update(point_id=1, cap=.03)
        r = self.make()

        def append():
            with self.trace.open('a') as f:
                f.write(json.dumps(damaged) + '\n')

        with mock.patch.object(r, 'session_start', side_effect=append):
            with self.assertRaisesRegex(RuntimeError, 'unreadable.*1732'):
                r.run()

    def test_row_scope_keeps_pre_row_observation_separate_from_transition(self):
        lines = self.trace.read_bytes().splitlines()
        empty, valid = json.loads(lines[1]), json.loads(lines[9])
        self.write_records(empty)
        del self.plan['gates']['pane']['whole_file']
        r = self.make()

        def append():
            with self.trace.open('a') as f:
                f.write(json.dumps(valid) + '\n')

        with mock.patch.object(r, 'session_start', side_effect=append):
            r.run()
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['scope'], evidence['transition'], evidence['previous']), ('row', 'sampled', None))
        self.assertEqual(evidence['before_scope']['sequence'], 82)

    def test_row_scope_captures_path_object_trace_offset(self):
        self.write_records(self.fixture_record())
        self.instances['a']['trace_jsonl'] = self.trace
        del self.plan['gates']['pane']['whole_file']
        self.plan['gates']['pane']['cap'] = .03
        with self.assertRaisesRegex(RuntimeError, 'did not pass within'):
            self.make().run()

    def test_incremental_poll_retains_pane_key_for_unreadable_sample(self):
        lines = self.trace.read_bytes().splitlines()
        empty = json.loads(lines[1])
        damaged = copy.deepcopy(empty)
        damaged['sequence'], damaged['frame'] = '1732', '5524'
        damaged['captures'][0] = {'error': 'memory_unavailable'}
        self.write_records(empty)
        self.plan['gates']['pane'].update(whole_file=True, point_id=1, cap=.03)
        appended = []

        def sleep(_delay):
            if not appended:
                with self.trace.open('a') as f:
                    f.write(json.dumps(damaged) + '\n')
                appended.append(True)

        with mock.patch.object(runner.time, 'sleep', side_effect=sleep):
            with self.assertRaisesRegex(RuntimeError, 'unreadable.*1732'):
                self.make().run()
        self.assertTrue(appended)

    def test_previous_text_from_another_native_pane_is_not_a_transition(self):
        lines = self.trace.read_bytes().splitlines()
        old, valid = json.loads(lines[1]), json.loads(lines[9])
        old['registers']['x0'] = '0xdeadbeef'
        self.write_records(old, valid)
        self.plan['gates']['pane']['whole_file'] = True
        self.make().run()
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['transition'], evidence['previous']), ('sampled', None))

    def test_text_whole_file_requires_a_boolean(self):
        self.plan['gates']['pane']['whole_file'] = 'yes'
        self.manifest.write_text(json.dumps(self.plan))
        report = runner.dry_run(self.manifest)
        self.assertEqual(report['result'], 'BROKEN')
        self.assertIn('whole_file', ' '.join(report['errors']))

    def test_dry_run_accepts_text_gate_and_rejects_bad_inputs(self):
        self.manifest.write_text(json.dumps(self.plan))
        report = runner.dry_run(self.manifest)
        self.assertEqual((report['result'], report['errors'], report['review']['class']), ('READY', [], 'full'))
        for change, expected in (({'pane': ''}, 'pane'), ({'pane': None}, 'pane'), ({'pattern': '['}, 'unterminated')):
            with self.subTest(change=change):
                plan = copy.deepcopy(self.plan)
                plan['gates']['pane'].update(change)
                self.manifest.write_text(json.dumps(plan))
                report = runner.dry_run(self.manifest)
                self.assertEqual(report['result'], 'BROKEN')
                self.assertIn(expected, ' '.join(report['errors']))

    def test_pass_records_latest_pane_text_sequence_and_frame(self):
        r = self.make()
        r.run()
        receipt = json.loads((self.out / 'r1.json').read_text())
        self.assertEqual(receipt['status'], 'PASS')
        pane_key = json.loads(self.trace.read_bytes().splitlines()[10])['registers']['x0']
        self.assertEqual(receipt['results'][0]['evidence'],
                         {'pane': 'tr_crumb', 'text': 'Fighters & Stages  >  CPU Character   page 1 of 1',
                          'sequence': 1731, 'frame': 5523, 'pane_key': pane_key, 'scope': 'whole_file',
                          'transition': 'measured',
                          'previous': {'text': '', 'sequence': 311, 'frame': 5285, 'pane': pane_key},
                          'before_scope': None})
        self.assertEqual(r.last_gate_outcome['role'], 'trace')
        self.assertEqual(runner.harness.assertion_role({'kind': 'text', 'asserts': 'effect'}), 'effect')

    def test_text_evidence_identifies_pane_and_previous_different_text(self):
        latest = json.loads(self.trace.read_bytes().splitlines()[10])
        self.make().run()
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual(evidence['pane_key'], latest['registers']['x0'])
        self.assertEqual(evidence['previous'], {'text': '', 'sequence': 311, 'frame': 5285,
                                                 'pane': latest['registers']['x0']})

    def test_text_evidence_without_earlier_change_has_no_previous_text(self):
        lines = self.trace.read_bytes().splitlines(keepends=True)
        self.trace.write_bytes(lines[9] + lines[10])
        self.make().run()
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['transition'], evidence['previous']), ('unmeasured', None))

    def test_nonmatching_pattern_times_out(self):
        self.plan['gates']['pane']['pattern'] = 'never appears'
        self.plan['gates']['pane']['cap'] = .08
        with self.assertRaisesRegex(RuntimeError, 'did not pass within'):
            self.make().run()
        self.assertEqual(json.loads((self.out / 'r1.json').read_text())['status'], 'FAILED')

    def test_older_matching_text_cannot_override_newest_text(self):
        self.plan['gates']['pane']['cap'] = .08
        lines = self.trace.read_bytes().splitlines(keepends=True)
        with self.trace.open('ab') as f:
            f.write(lines[1])
        with self.assertRaisesRegex(RuntimeError, 'did not pass within'):
            self.make().run()

    def test_new_record_is_seen_while_polling(self):
        lines = self.trace.read_bytes().splitlines(keepends=True)
        self.trace.write_bytes(lines[1])
        self.plan['gates']['pane']['cap'] = .5
        read = runner.trace_text.scan
        appended = []

        def scan(*args, **kwargs):
            result = read(*args, **kwargs)
            if not appended:
                with self.trace.open('ab') as f:
                    f.write(lines[9])
                appended.append(True)
            return result

        with mock.patch.object(runner.trace_text, 'scan', side_effect=scan):
            self.make().run()
        result = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual(result['sequence'], 1730)
        self.assertEqual(result['previous']['text'], '')

    def test_declared_but_missing_trace_file_fails_clearly(self):
        self.trace.unlink()
        self.plan['gates']['pane']['cap'] = .08
        r = self.make()
        with self.assertRaisesRegex(RuntimeError, 'trace file not found'):
            r.run()
        self.assertTrue(r.last_gate_outcome['missing_evidence'])

    def test_missing_trace_declaration_fails_clearly(self):
        self.instances['a'].pop('trace_jsonl')
        r = self.make()
        with self.assertRaisesRegex(RuntimeError, 'not tracing'):
            r.run()
        self.assertEqual(json.loads((self.out / 'r1.json').read_text())['status'], 'FAILED')
        self.assertTrue(r.last_gate_outcome['missing_evidence'])

    def test_legacy_trace_file_source(self):
        self.instances['a']['trace'] = self.instances['a'].pop('trace_jsonl')
        self.make().run()
        self.assertEqual(json.loads((self.out / 'r1.json').read_text())['status'], 'PASS')

    def test_trace_marker_without_file_path_fails_clearly(self):
        self.instances['a'].pop('trace_jsonl')
        self.instances['a']['trace'] = {'points': ['armed']}
        with self.assertRaisesRegex(RuntimeError, 'trace_jsonl'):
            self.make().run()

    def test_custom_name_offset(self):
        original = json.loads(self.trace.read_bytes().splitlines()[9])
        pane = bytearray.fromhex(original['captures'][0]['hex'])
        pane[0x90:0x90 + 25] = pane[0xb0:0xb0 + 25]
        pane[0xb0:0xb0 + 25] = bytes(25)
        original['captures'][0]['hex'] = pane.hex()
        self.trace.write_text(json.dumps(original) + '\n')
        self.plan['gates']['pane']['name_at'] = 0x90
        self.make().run()
        result = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((result['pane'], result['sequence']), ('tr_crumb', 1730))

    def test_point_id_dry_run_validation(self):
        for point_id, valid in (('1', True), (1, True), (None, False), (True, False), (1.5, False), ([], False)):
            with self.subTest(point_id=point_id):
                self.plan['gates']['pane']['point_id'] = point_id
                self.manifest.write_text(json.dumps(self.plan))
                report = runner.dry_run(self.manifest)
                self.assertEqual(report['result'] == 'READY', valid)
                if not valid:
                    self.assertIn('point_id', ' '.join(report['errors']))

    def test_point_bound_gate_ignores_a_newer_other_point(self):
        lines = self.trace.read_bytes().splitlines(keepends=True)
        other = json.loads(lines[10])
        text = 'other point'
        other['registers']['x2'] = hex(len(text))
        other['captures'][1]['hex'] = text.encode('utf-16-le').hex()
        self.trace.write_bytes(lines[9] + (json.dumps(other) + '\n').encode())
        self.plan['gates']['pane']['point_id'] = 1
        self.make().run()
        evidence = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual((evidence['sequence'], str(evidence['point_id'])), (1730, '1'))

    def test_nonexistent_point_fails_at_cap(self):
        self.plan['gates']['pane'].update(point_id='not-a-point', cap=.08)
        with self.assertRaisesRegex(RuntimeError, 'did not pass within'):
            self.make().run()

    def test_gate_does_not_reparse_old_lines_on_the_next_poll(self):
        lines = self.trace.read_bytes().splitlines(keepends=True)
        self.trace.write_bytes(lines[1] + lines[3])
        self.plan['gates']['pane']['cap'] = 1
        appended = []
        with mock.patch.object(runner.trace_text, 'parse', wraps=runner.trace_text.parse) as parse:
            def sleep(_delay):
                if not appended:
                    self.assertEqual(parse.call_count, 1)
                    with self.trace.open('ab') as f:
                        f.write(lines[9])
                    appended.append(True)

            with mock.patch.object(runner.time, 'sleep', side_effect=sleep):
                self.make().run()
        self.assertEqual([json.loads(call.args[0])['sequence'] for call in parse.call_args_list], ['82', '1730'])
        self.assertEqual(json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']['sequence'], 1730)

    def test_gate_rescans_a_replaced_trace(self):
        lines = self.trace.read_bytes().splitlines(keepends=True)
        self.trace.write_bytes(lines[1])
        replaced = []

        def sleep(_delay):
            if not replaced:
                target = self.dir / 'new-trace.jsonl'
                target.write_bytes(lines[9])
                target.replace(self.trace)
                replaced.append(True)

        with mock.patch.object(runner.time, 'sleep', side_effect=sleep):
            self.make().run()
        result = json.loads((self.out / 'r1.json').read_text())['results'][0]['evidence']
        self.assertEqual(result['sequence'], 1730)
        self.assertIsNone(result['previous'])


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

    def waiting_run(self):
        eden = FakeEden({})
        run = self.make(eden)
        now = [0.0]
        eden.played_paths, eden.played_times = [], []
        original = eden.request

        def request(method, params):
            if method == 'macro.play':
                eden.played_paths.append(params['path'])
                eden.played_times.append(now[0])
            if method == 'macro.status' and now[0] < 1.2:
                eden.calls.append(method)
                return {'ok': True, 'result': {'state': 'playing', 'error': ''}}
            return original(method, params)

        eden.request = request
        clock = mock.patch.object(runner.time, 'monotonic', side_effect=lambda: now[0])
        sleep = mock.patch.object(runner.time, 'sleep', side_effect=lambda delay: now.__setitem__(0, now[0] + delay))
        clock.start()
        sleep.start()
        self.addCleanup(clock.stop)
        self.addCleanup(sleep.stop)
        return run, eden, now

    def test_an_abandoned_recheck_cannot_disarm_a_successor_image_gate(self):
        for overlap in (False, True):
            with self.subTest(overlap=overlap):
                eden = FakeEden({})
                eden.colour = RED
                run = self.make(eden)
                run.plan['gates']['status'] = {'kind': 'status', 'cap': 2, 'expect': {'running': True}}
                run.plan['segments']['to-red']['start_gate'] = 'status'
                entered, release = threading.Event(), threading.Event()
                workers, armed, status_count, poll_count, screenshots = [], [], [], [], []
                original = eden.request

                def request(method, params):
                    if method == 'status':
                        status_count.append(1)
                        if len(status_count) == 2:
                            workers.append(threading.current_thread())
                            entered.set()
                            release.wait(2)
                        return {'ok': True, 'result': {'running': True}}
                    if method == 'macro.status':
                        poll_count.append(1)
                        return {'ok': True, 'result': {'state': 'playing' if len(poll_count) == 1 else 'idle'}}
                    if method == 'ui.screenshot':
                        screenshots.append(params['path'])
                        eden.colour = BLUE if len(screenshots) == 1 else RED
                        armed.append(run._arm_frames)
                        if overlap and workers and not release.is_set():
                            prior = (copy.deepcopy(run.last_gate_outcome), run.screen_reached,
                                     list(run.gate_saw_evidence))
                            release.set()
                            workers[0].join(1)
                            self.assertFalse(workers[0].is_alive())
                            armed.append(run._arm_frames)
                            self.assertEqual((run.last_gate_outcome, run.screen_reached, run.gate_saw_evidence), prior)
                    return original(method, params)

                eden.request = request
                try:
                    if overlap:
                        with mock.patch.object(runner.macro_gate, 'IDLE_CAP', .4):
                            with self.assertRaises(routes.closed_loop.DispatchRejected):
                                run.play('a', 'to-red')
                        self.assertTrue(entered.is_set())
                        self.assertNotIn('macro.play', eden.calls)
                    with mock.patch.object(runner.still_fresh, 'usable', return_value=True):
                        run.check_gate('a', 'red')
                    self.assertEqual(armed, [True, True, True] if overlap else [True, True])
                    self.assertEqual(len(screenshots), 2)
                    self.assertEqual(Image.open(io.BytesIO(run._gate_baseline)).getpixel((0, 0)), BLUE)
                    self.assertEqual(Image.open(io.BytesIO(run._gate_latest)).getpixel((0, 0)), RED)
                    self.assertEqual(run.last_gate_outcome['gate'], 'red')
                    self.assertTrue(run.last_gate_outcome['passed'])
                finally:
                    release.set()
                    for worker in workers:
                        worker.join(1)
                        self.assertFalse(worker.is_alive())

    def test_a_superseded_screenshot_cannot_publish_into_its_successor_gate(self):
        eden = FakeEden({})
        eden.colour = RED
        run = self.make(eden)
        entered, release = threading.Event(), threading.Event()
        original = eden.request
        errors, armed, screenshots = [], [], []

        def request(method, params):
            if method == 'ui.screenshot':
                screenshots.append(params['path'])
                if threading.current_thread() is worker:
                    entered.set()
                    release.wait(2)
                else:
                    self.assertTrue(run._arm_frames)
                    release.set()
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                    armed.append(run._arm_frames)
            return original(method, params)

        def old_gate():
            try:
                run.check_gate('a', 'red')
            except Exception as error:
                errors.append(error)

        eden.request = request
        worker = threading.Thread(target=old_gate)
        try:
            with mock.patch.object(runner.still_fresh, 'usable', return_value=True):
                worker.start()
                self.assertTrue(entered.wait(1))
                run.check_gate('a', 'red')
            self.assertEqual(armed, [True])
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], routes.closed_loop.DispatchRejected)
            self.assertNotIn(screenshots[0], run.still_records)
            self.assertTrue(run._gate_baseline)
            self.assertTrue(run._gate_latest)
            self.assertEqual(run.last_gate_outcome['gate'], 'red')
            self.assertEqual(len(run.gate_saw_evidence), 1)
        finally:
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_a_superseded_state_gate_cannot_consume_the_new_gate_state(self):
        eden = FakeEden({})
        run = self.make(eden)
        run.plan['gates']['native'] = {'kind': 'state', 'provider': 'ssbu.ui', 'cap': 2}
        run.plan['gates']['status'] = {'kind': 'status', 'cap': 2, 'expect': {'running': True}}
        run.current_row = {'id': 'r1'}
        key = ('r1', 'a', 'ssbu.ui')
        run.state_marks = {key: {'boot': 'boot', 'rev': 1}}
        run.state_pending = {('r1', 'a'): 'press'}
        entered, release = threading.Event(), threading.Event()
        errors = []

        def state_block(*args, **kwargs):
            entered.set()
            release.wait(2)
            return {'boot': 'boot', 'rev': 2, 'values': {'focus': 'row'}}, 'confirmed'

        def old_gate():
            try:
                run.check_gate('a', 'native')
            except Exception as error:
                errors.append(error)

        original = eden.request

        def request(method, params):
            if method == 'status':
                return {'ok': True, 'result': {'running': True}}
            return original(method, params)

        eden.request = request
        worker = threading.Thread(target=old_gate)
        try:
            with mock.patch.object(run, '_state_block', side_effect=state_block):
                worker.start()
                self.assertTrue(entered.wait(1))
                run.check_gate('a', 'status')
                release.set()
                worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], routes.closed_loop.DispatchRejected)
            self.assertIn(key, run.state_marks)
            self.assertEqual(run.state_pending[('r1', 'a')], 'press')
            self.assertEqual(run.last_gate_outcome['gate'], 'status')
            self.assertEqual(len(run.gate_saw_evidence), 1)
        finally:
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_a_superseded_frame_read_cannot_change_the_successor_frame_source(self):
        eden = FakeEden({})
        eden.colour = RED
        run = self.make(eden)
        source = runner.still_fresh.FrameSource(None)
        source.frame = 5
        run._frame_sources['a'] = source
        entered, release = threading.Event(), threading.Event()
        errors = []

        def read(frame_source):
            if threading.current_thread() is worker:
                frame_source.frame = 999
                entered.set()
                release.wait(2)
            else:
                self.assertEqual(source.frame, 5)
                frame_source.frame = 100
                release.set()
                worker.join(1)
                self.assertFalse(worker.is_alive())
            return None

        def old_gate():
            try:
                run.check_gate('a', 'red')
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=old_gate)
        try:
            with mock.patch.object(runner.still_fresh.FrameSource, 'read', autospec=True, side_effect=read), \
                    mock.patch.object(runner.still_fresh, 'usable', return_value=True):
                worker.start()
                self.assertTrue(entered.wait(1))
                run.check_gate('a', 'red')
            self.assertEqual(run._frame_sources['a'].frame, 100)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], routes.closed_loop.DispatchRejected)
            self.assertEqual(len(run.still_records), 1)
            self.assertEqual(len((run.out / 'stills.jsonl').read_text().splitlines()), 1)
        finally:
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_a_superseded_freshness_record_cannot_update_the_ledger(self):
        eden = FakeEden({})
        eden.colour = RED
        run = self.make(eden)
        entered, release = threading.Event(), threading.Event()
        errors = []
        sha256 = runner.still_fresh.hashlib.sha256

        def digest(data=b''):
            if threading.current_thread() is worker:
                entered.set()
                release.wait(2)
            elif entered.is_set() and not release.is_set():
                release.set()
                worker.join(1)
                self.assertFalse(worker.is_alive())
            return sha256(data)

        def old_gate():
            try:
                run.check_gate('a', 'red')
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=old_gate)
        try:
            with mock.patch.object(runner.still_fresh.hashlib, 'sha256', side_effect=digest), \
                    mock.patch.object(runner.still_fresh, 'usable', return_value=True):
                worker.start()
                self.assertTrue(entered.wait(1))
                run.check_gate('a', 'red')
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], routes.closed_loop.DispatchRejected)
            self.assertEqual(len(run.still_records), 1)
            self.assertEqual(len((run.out / 'stills.jsonl').read_text().splitlines()), 1)
            latest = next(iter(run.still_records.values()))
            self.assertEqual(run.stills.runs['a']['sha256'], latest['sha256'])
        finally:
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_a_gate_superseded_before_publication_writes_no_freshness_line(self):
        eden = FakeEden({})
        eden.colour = RED
        run = self.make(eden)
        entered, release, errors = threading.Event(), threading.Event(), []
        sha256 = runner.still_fresh.hashlib.sha256

        def digest(data=b''):
            if threading.current_thread() is old:
                entered.set()
                release.wait(2)
            return sha256(data)

        def old_gate():
            try:
                run.check_gate('a', 'red')
            except Exception as error:
                errors.append(error)

        old = threading.Thread(target=old_gate)
        try:
            with mock.patch.object(runner.still_fresh.hashlib, 'sha256', side_effect=digest), \
                    mock.patch.object(runner.still_fresh, 'usable', return_value=True):
                old.start()
                self.assertTrue(entered.wait(1))
                run.check_gate('a', 'red')
                release.set()
                old.join(1)
            self.assertFalse(old.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], routes.closed_loop.DispatchRejected)
            rows = runner.still_fresh.load(run.out / 'stills.jsonl')
            self.assertEqual(len(rows), 1)
            self.assertEqual(len((run.out / 'stills.jsonl').read_text().splitlines()), 1)
            self.assertEqual(set(run.still_records), {rows[0]['path']})
        finally:
            release.set()
            old.join(1)
            self.assertFalse(old.is_alive())

    def test_gate_publication_and_supersession_are_serialized(self):
        eden = FakeEden({})
        eden.colour = RED
        run = self.make(eden)
        entered, release, successor_started = threading.Event(), threading.Event(), threading.Event()
        errors, paths = [], {}
        append = run.stills.append

        def delayed_append(record):
            paths[threading.current_thread().name] = record['path']
            if threading.current_thread() is old:
                entered.set()
                release.wait(2)
            return append(record)

        def gate():
            try:
                if threading.current_thread() is successor:
                    successor_started.set()
                run.check_gate('a', 'red')
            except Exception as error:
                errors.append(error)

        old = threading.Thread(target=gate, name='old')
        successor = threading.Thread(target=gate, name='successor')
        try:
            with mock.patch.object(run.stills, 'append', side_effect=delayed_append), \
                    mock.patch.object(runner.still_fresh, 'usable', return_value=True):
                old.start()
                self.assertTrue(entered.wait(1))
                successor.start()
                self.assertTrue(successor_started.wait(1))
                successor.join(.1)
                waited_for_append = successor.is_alive()
                release.set()
                old.join(1)
                successor.join(1)
            self.assertFalse(old.is_alive())
            self.assertFalse(successor.is_alive())
            self.assertTrue(waited_for_append)
            rows = runner.still_fresh.load(run.out / 'stills.jsonl')
            self.assertEqual([row['path'] for row in rows], [paths['old'], paths['successor']])
            self.assertEqual(len((run.out / 'stills.jsonl').read_text().splitlines()), 2)
            self.assertEqual(set(run.still_records), set(paths.values()))
            self.assertTrue(all(isinstance(error, routes.closed_loop.DispatchRejected) for error in errors))
        finally:
            release.set()
            old.join(1)
            successor.join(1)
            self.assertFalse(old.is_alive())
            self.assertFalse(successor.is_alive())

    def test_abandoned_append_cannot_extend_a_successor_gate_or_qualify_its_still(self):
        eden = FakeEden({})
        eden.colour = RED
        original_request = eden.request

        def request(method, params):
            if method == 'status':
                return {'ok': True, 'result': {'running': True}}
            return original_request(method, params)

        eden.request = request
        run = self.make(eden)
        run.plan['gates']['status'] = {'kind': 'status', 'cap': .1, 'expect': {'running': True}}
        run.captures = []
        previous = {'sha256': 'prior', 'boot': None, 'frame': None, 'first_file': 'prior', 'first_frame': None}
        run.stills.runs['a'] = previous.copy()
        entered, release = threading.Event(), threading.Event()
        append, workers, old_result = run.stills.append, [], []

        def paused_append(record):
            if 'path' in record and not entered.is_set():
                workers.append(threading.current_thread())
                entered.set()
                release.wait(1.5)
            return append(record)

        def old_gate():
            old_result.append(runner.macro_gate.bounded_call(lambda _: run.check_gate('a', 'red'), 'gate', .2))

        old = threading.Thread(target=old_gate)
        timer = threading.Timer(.55, release.set)
        try:
            with mock.patch.object(run.stills, 'append', side_effect=paused_append), \
                    mock.patch.object(runner.still_fresh, 'usable', return_value=True):
                old.start()
                self.assertTrue(entered.wait(1))
                old.join(.4)
                self.assertFalse(old.is_alive())
                self.assertEqual(old_result, [(False, None)])
                timer.start()
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError,
                                            'gate status on a did not pass within 0.1 s: '
                                            'gate evidence was still being written by an earlier check'):
                    run.check_gate('a', 'status', cap=.1)
                self.assertLessEqual(time.monotonic() - started, .15)
                self.assertTrue(run.last_gate_outcome['timed_out'])
                self.assertIn('gate evidence was still being written', run.last_gate_outcome['reason'])
                timer.join(1)
                workers[0].join(1)
            self.assertFalse(workers[0].is_alive())
            rows = [json.loads(line) for line in (run.out / 'stills.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]['retract'], rows[0]['file'])
            self.assertEqual(rows[1]['sha256'], rows[0]['sha256'])
            self.assertEqual(rows[1]['reason'], 'written by a superseded gate check')
            self.assertTrue(rows[1]['utc'])
            self.assertEqual(runner.still_fresh.load(run.out / 'stills.jsonl'), [])
            self.assertEqual(run.still_records, {})
            self.assertEqual(run.stills.runs['a'], previous)
            self.assertEqual(run.captures, [])
            self.assertEqual(run.last_gate_outcome['gate'], 'status')
            self.assertTrue(run.last_gate_outcome['timed_out'])
            actions = [json.loads(line) for line in (run.out / 'actions.jsonl').read_text().splitlines()]
            self.assertFalse(any(action.get('gate') == 'status' and action.get('pass') is True for action in actions))
        finally:
            release.set()
            if timer.is_alive():
                timer.join(1)
            old.join(1)
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())
            self.assertFalse(old.is_alive())

    def test_successor_status_gate_passes_when_evidence_lock_is_free(self):
        eden = FakeEden({})
        eden.request = lambda method, params: {'ok': True, 'result': {'running': True}}
        run = self.make(eden)
        run.plan['gates']['status'] = {'kind': 'status', 'cap': .1, 'expect': {'running': True}}
        started = time.monotonic()
        run.check_gate('a', 'status')
        self.assertLess(time.monotonic() - started, .1)
        self.assertTrue(run.last_gate_outcome['passed'])

    def test_abandoned_append_is_retracted_without_advancing_identical_run(self):
        eden = FakeEden({})
        eden.colour = RED
        run = self.make(eden)
        previous = {'sha256': 'prior', 'boot': None, 'frame': None, 'first_file': 'prior', 'first_frame': None}
        run.stills.runs['a'] = previous.copy()
        entered, release = threading.Event(), threading.Event()
        append, workers, result = run.stills.append, [], []

        def paused_append(record):
            if 'path' in record and not entered.is_set():
                workers.append(threading.current_thread())
                entered.set()
                release.wait(1)
            return append(record)

        def gate():
            result.append(runner.macro_gate.bounded_call(lambda _: run.check_gate('a', 'red'), 'gate', .1))

        old = threading.Thread(target=gate)
        try:
            with mock.patch.object(run.stills, 'append', side_effect=paused_append), \
                    mock.patch.object(runner.still_fresh, 'usable', return_value=True):
                old.start()
                self.assertTrue(entered.wait(1))
                old.join(.3)
                self.assertEqual(result, [(False, None)])
                release.set()
                workers[0].join(1)
            self.assertFalse(workers[0].is_alive())
            rows = [json.loads(line) for line in (run.out / 'stills.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]['retract'], rows[0]['file'])
            self.assertEqual(rows[1]['sha256'], rows[0]['sha256'])
            self.assertEqual(run.still_records, {})
            self.assertEqual(run.stills.runs['a'], previous)
        finally:
            release.set()
            old.join(1)
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())
            self.assertFalse(old.is_alive())

    def test_gate_passes_only_its_remaining_cap_after_waiting_for_publication(self):
        eden = FakeEden({})
        eden.request = lambda method, params: {'ok': True, 'result': {'running': True}}
        run = self.make(eden)
        run.plan['gates']['status'] = {'kind': 'status', 'cap': .4, 'expect': {'running': True}}
        entered, release = threading.Event(), threading.Event()
        remaining = []
        check = run.check_gate_spec

        def holder():
            with run._gate_lock:
                entered.set()
                release.wait(1)

        def checked(on, gate, name):
            remaining.append(gate['cap'])
            return check(on, gate, name)

        worker = threading.Thread(target=holder)
        timer = threading.Timer(.12, release.set)
        try:
            with mock.patch.object(run, 'check_gate_spec', side_effect=checked):
                worker.start()
                self.assertTrue(entered.wait(1))
                timer.start()
                run.check_gate('a', 'status')
            self.assertEqual(len(remaining), 1)
            self.assertGreater(remaining[0], .15)
            self.assertLess(remaining[0], .34)
            self.assertTrue(run.last_gate_outcome['passed'])
        finally:
            release.set()
            if timer.is_alive():
                timer.join(1)
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_gate_screenshot_publication_lock_obeys_the_original_deadline(self):
        eden = FakeEden({})
        eden.colour = RED
        run = self.make(eden)
        entered, release = threading.Event(), threading.Event()
        request = eden.request

        def holder():
            with run._gate_lock:
                entered.set()
                release.wait(1)

        worker = threading.Thread(target=holder)
        timer = threading.Timer(.3, release.set)

        def screenshot(method, params):
            result = request(method, params)
            if method == 'ui.screenshot':
                worker.start()
                self.assertTrue(entered.wait(1))
                timer.start()
            return result

        eden.request = screenshot
        try:
            started = time.monotonic()
            with mock.patch.object(runner.still_fresh, 'usable', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'gate red on a did not pass within 0.15 s: '
                                            'gate evidence was still being written'):
                    run.check_gate('a', 'red', cap=.15)
            self.assertLessEqual(time.monotonic() - started, .2)
            self.assertEqual(run.still_records, {})
            self.assertTrue(run.last_gate_outcome['timed_out'])
        finally:
            release.set()
            if timer.is_alive():
                timer.join(1)
            if worker.ident is not None:
                worker.join(1)
                self.assertFalse(worker.is_alive())

    def test_plain_screenshot_survives_a_publication_lock_timeout(self):
        eden = FakeEden({})
        run = self.make(eden)
        entered, release = threading.Event(), threading.Event()

        def holder():
            with run._gate_lock:
                entered.set()
                release.wait(1)

        worker = threading.Thread(target=holder)
        timer = threading.Timer(.2, release.set)
        try:
            with mock.patch.object(runner, 'STILL_PUBLICATION_CAP', .05, create=True):
                worker.start()
                self.assertTrue(entered.wait(1))
                timer.start()
                with self.assertRaisesRegex(RuntimeError, 'screenshot.*evidence.*being written'):
                    run.shot('a', 'plain')
            images = list(run.row_dir.glob('*-plain.png'))
            self.assertEqual(len(images), 1)
            self.assertTrue(images[0].is_file())
        finally:
            release.set()
            if timer.is_alive():
                timer.join(1)
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_a_capped_gate_acquisition_persists_its_reference_metadata(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1001])
        eden.colour = RED
        run = self.make(eden)
        run.acquired = []
        gate = {'kind': 'image', 'reference': 'refs/capped.png', 'acquire': True,
                'roi': [0, 0, 64, 36], 'cap': 1, 'interval': .03}
        run.plan['gates']['capped'] = gate
        result = run.check_gate('a', 'capped', cap=.9)
        recorded = json.loads(self.manifest.read_text())['gates']['capped']
        self.assertEqual(recorded['sha256'], sha(result))
        self.assertTrue(recorded['acquired'])
        self.assertEqual(run.plan['gates']['capped']['sha256'], recorded['sha256'])

    def test_timed_out_acquisition_rolls_back_at_each_publication_stage(self):
        for phase in ('reference', 'manifest', 'receipt'):
            with self.subTest(phase=phase):
                bridge = self.dir / f'{phase}-bridge.log'
                self.instances['a']['bridge_log'] = str(bridge)
                eden = FreshEden(bridge, [1000, 1001, 1002, 1003])
                eden.colour = RED
                name = 'acquire-' + phase
                gate = {'kind': 'image', 'reference': f'refs/{phase}.png', 'acquire': True,
                        'roi': [0, 0, 64, 36], 'cap': 2, 'interval': .01}
                self.plan['gates'][name] = gate
                self.manifest.write_text(json.dumps(self.plan))
                before = self.manifest.read_bytes()
                run = self.make(eden)
                entered, release = threading.Event(), threading.Event()
                workers = []
                reference = self.dir / gate['reference']
                replace = Path.replace

                def block():
                    workers.append(threading.current_thread())
                    entered.set()
                    release.wait(1)

                def replacement(path, target):
                    if ((phase == 'reference' and Path(target) == reference)
                            or (phase == 'manifest' and Path(target) == self.manifest)):
                        block()
                    return replace(path, target)

                class Receipts(list):
                    def append(self, value):
                        if phase == 'receipt' and not entered.is_set():
                            block()
                        super().append(value)

                run.acquired = Receipts()
                try:
                    with mock.patch.object(Path, 'replace', new=replacement):
                        answered = runner.macro_gate.bounded_call(
                            lambda _: run.check_gate('a', name), 'gate', .22)
                        self.assertEqual(answered, (False, None))
                        self.assertTrue(entered.is_set())
                        report = runner.dry_run(self.manifest)
                        self.assertIn(name, report['references_to_acquire'])
                        probe = subprocess.run(
                            [sys.executable, '-B', '-c',
                             'import json,sys; import runner; '
                             'print(json.dumps(runner.dry_run(__import__("pathlib").Path(sys.argv[1]))'
                             '["references_to_acquire"]))', str(self.manifest)],
                            cwd=Path(runner.__file__).parent, text=True, capture_output=True, timeout=2, check=True)
                        self.assertIn(name, json.loads(probe.stdout))
                        release.set()
                        workers[0].join(1)
                        self.assertFalse(workers[0].is_alive())
                    self.assertEqual(self.manifest.read_bytes(), before)
                    self.assertFalse(reference.exists())
                    self.assertEqual(run.acquired, [])
                    self.assertNotIn('sha256', run.plan['gates'][name])
                    self.assertNotIn('acquired', run.plan['gates'][name])
                    with mock.patch.object(run, 'compare_region',
                                           side_effect=AssertionError('unapproved reference was used')):
                        result = run.check_gate('a', name)
                    entry = json.loads(self.manifest.read_text())['gates'][name]
                    self.assertEqual(entry['sha256'], sha(result))
                    self.assertTrue(entry['acquired'])
                    self.assertEqual(len(run.acquired), 1)
                finally:
                    release.set()
                    for worker in workers:
                        worker.join(1)
                        self.assertFalse(worker.is_alive())

    def test_abandoned_replacement_restores_an_older_reference(self):
        bridge = self.dir / 'old-bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1001])
        eden.colour = RED
        reference = self.dir / 'refs' / 'old.png'
        Image.new('RGB', (64, 36), BLUE).save(reference)
        old_bytes = reference.read_bytes()
        gate = {'kind': 'image', 'reference': 'refs/old.png', 'acquire': True, 'sha256': sha(reference),
                'roi': [0, 0, 64, 36], 'cap': 2, 'interval': .01}
        self.plan['gates']['old'] = gate
        self.manifest.write_text(json.dumps(self.plan))
        before = self.manifest.read_bytes()
        run = self.make(eden)
        run.acquired = []
        entered, release = threading.Event(), threading.Event()
        workers = []
        replace = Path.replace

        def replacement(path, target):
            if Path(target) == reference:
                workers.append(threading.current_thread())
                entered.set()
                release.wait(1)
            return replace(path, target)

        try:
            with mock.patch.object(Path, 'replace', new=replacement):
                answered = runner.macro_gate.bounded_call(
                    lambda _: run.acquire('a', 'old', run.plan['gates']['old'], time.monotonic() + 2),
                    'gate', .22)
                self.assertEqual(answered, (False, None))
                self.assertTrue(entered.is_set())
                release.set()
                workers[0].join(1)
                self.assertFalse(workers[0].is_alive())
            self.assertEqual(reference.read_bytes(), old_bytes)
            self.assertEqual(self.manifest.read_bytes(), before)
            self.assertEqual(run.acquired, [])
            self.assertEqual(run.plan['gates']['old'], gate)
        finally:
            release.set()
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())

    def test_an_exception_during_acquisition_restores_reference_and_manifest(self):
        bridge = self.dir / 'error-bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1001])
        eden.colour = RED
        gate = {'kind': 'image', 'reference': 'refs/error.png', 'acquire': True,
                'roi': [0, 0, 64, 36], 'cap': 2, 'interval': .01}
        self.plan['gates']['error'] = gate
        self.manifest.write_text(json.dumps(self.plan))
        before = self.manifest.read_bytes()
        run = self.make(eden)
        run.acquired = []
        replace = Path.replace

        def replacement(path, target):
            if Path(target) == self.manifest:
                raise OSError('manifest replacement failed')
            return replace(path, target)

        with mock.patch.object(Path, 'replace', new=replacement):
            with self.assertRaisesRegex(OSError, 'manifest replacement failed'):
                run.check_gate('a', 'error')
        self.assertFalse((self.dir / gate['reference']).exists())
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(run.acquired, [])
        self.assertEqual(run.plan['gates']['error'], gate)

    def test_failed_acquisition_rollback_keeps_intent_and_backups(self):
        bridge = self.dir / 'rollback-error-bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1001])
        eden.colour = RED
        reference = self.dir / 'refs' / 'rollback-error.png'
        Image.new('RGB', (64, 36), BLUE).save(reference)
        old = reference.read_bytes()
        gate = {'kind': 'image', 'reference': 'refs/rollback-error.png', 'acquire': True,
                'sha256': sha(reference), 'roi': [0, 0, 64, 36], 'cap': 2, 'interval': .01}
        self.plan['gates']['rollback-error'] = gate
        self.manifest.write_text(json.dumps(self.plan))
        run = self.make(eden)
        run.acquired = []
        replace = Path.replace
        replaced = []

        def replacement(path, target):
            if Path(target) == reference:
                if replaced:
                    raise OSError('reference restore failed')
                replaced.append(True)
            if Path(target) == self.manifest:
                raise OSError('manifest replacement failed')
            return replace(path, target)

        with mock.patch.object(Path, 'replace', new=replacement):
            with self.assertRaisesRegex(RuntimeError, 'acquisition rollback failed; the reference remains unapproved'):
                run.acquire('a', 'rollback-error', run.plan['gates']['rollback-error'], time.monotonic() + 2)
        self.assertTrue(runner.acquisition_intent(reference).exists())
        self.assertFalse(runner.reference_approved(self.dir, gate))
        self.assertEqual(run.acquired, [])
        backups = [path for path in reference.parent.iterdir() if path.is_file() and path.read_bytes() == old]
        self.assertEqual(len(backups), 1)

    def test_legacy_hashed_acquisition_reference_remains_usable(self):
        eden = FakeEden({})
        eden.colour = RED
        gate = dict(self.plan['gates']['red'], acquire=True)
        self.plan['gates']['legacy'] = gate
        self.manifest.write_text(json.dumps(self.plan))
        run = self.make(eden)
        run.acquired = []
        run.check_gate('a', 'legacy')
        self.assertEqual(run.acquired, [])
        self.assertTrue(run.last_gate_outcome['passed'])

    def test_pending_intent_rejects_even_a_matching_legacy_hash_and_can_be_reacquired(self):
        bridge = self.dir / 'pending-bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1001])
        eden.colour = RED
        gate = dict(self.plan['gates']['red'], acquire=True, interval=.01)
        self.plan['gates']['legacy'] = gate
        self.manifest.write_text(json.dumps(self.plan))
        reference = self.dir / gate['reference']
        intent = runner.acquisition_intent(reference)
        intent.touch()
        run = self.make(eden)
        run.acquired = []
        self.assertIn('legacy', runner.dry_run(self.manifest)['references_to_acquire'])
        self.assertTrue(runner.harness.gate_inputs_missing(run, 'a', {**gate, 'acquire': False}))
        with mock.patch.object(run, 'compare_region', side_effect=AssertionError('unapproved reference was used')):
            run.check_gate('a', 'legacy')
        self.assertFalse(intent.exists())
        self.assertTrue(json.loads(self.manifest.read_text())['gates']['legacy']['acquired'])
        self.assertEqual(len(run.acquired), 1)

    def test_abandoned_caller_wins_before_acquisition_claim(self):
        bridge = self.dir / 'abandoned-claim-bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1001])
        eden.colour = RED
        name = 'abandoned-claim'
        gate = {'kind': 'image', 'reference': 'refs/abandoned-claim.png', 'acquire': True,
                'roi': [0, 0, 64, 36], 'cap': 2, 'interval': .01}
        self.plan['gates'][name] = gate
        self.manifest.write_text(json.dumps(self.plan))
        before = self.manifest.read_bytes()
        run = self.make(eden)
        run.acquired = []
        entered, release = threading.Event(), threading.Event()
        workers, claims = [], []
        claim = runner.macro_gate.claim

        def delayed_claim():
            workers.append(threading.current_thread())
            entered.set()
            release.wait(1)
            claims.append(claim())
            return claims[-1]

        try:
            with mock.patch.object(runner.macro_gate, 'claim', side_effect=delayed_claim):
                answered = runner.macro_gate.bounded_call(lambda _: run.check_gate('a', name), 'gate', .22)
                self.assertEqual(answered, (False, None))
                self.assertTrue(entered.is_set())
                report = runner.dry_run(self.manifest)
                self.assertIn(name, report['references_to_acquire'])
                release.set()
                workers[0].join(1)
                self.assertFalse(workers[0].is_alive())
            self.assertEqual(claims, [False])
            self.assertEqual(self.manifest.read_bytes(), before)
            self.assertFalse((self.dir / gate['reference']).exists())
            self.assertEqual(run.acquired, [])
            actions = [json.loads(line) for line in (run.out / 'actions.jsonl').read_text().splitlines()]
            self.assertFalse(any(action.get('acquired') for action in actions))
        finally:
            release.set()
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())

    def test_claimed_acquisition_rolls_back_if_its_approving_write_fails(self):
        bridge = self.dir / 'claimed-error-bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1001])
        eden.colour = RED
        name = 'claimed-error'
        gate = {'kind': 'image', 'reference': 'refs/claimed-error.png', 'acquire': True,
                'roi': [0, 0, 64, 36], 'cap': 2, 'interval': .01}
        self.plan['gates'][name] = gate
        self.manifest.write_text(json.dumps(self.plan))
        before = self.manifest.read_bytes()
        run = self.make(eden)
        run.acquired = []
        intent = runner.acquisition_intent(self.dir / gate['reference'])
        unlink = Path.unlink
        failed = []

        def fail_once(path, *args, **kwargs):
            if path == intent and not failed:
                failed.append(True)
                raise OSError('approval write failed')
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, 'unlink', new=fail_once):
            answered, error = runner.macro_gate.bounded_call(lambda _: run.check_gate('a', name), 'gate', .5)
        self.assertFalse(answered)
        self.assertIsInstance(error, OSError)
        self.assertEqual(str(error), 'approval write failed')
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertFalse((self.dir / gate['reference']).exists())
        self.assertEqual(run.acquired, [])

    def test_claimed_acquisition_completes_even_if_the_caller_deadline_passes_at_unlock(self):
        bridge = self.dir / 'unlock-bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1001])
        eden.colour = RED
        name = 'unlock'
        gate = {'kind': 'image', 'reference': 'refs/unlock.png', 'acquire': True,
                'roi': [0, 0, 64, 36], 'cap': 2, 'interval': .01}
        self.plan['gates'][name] = gate
        self.manifest.write_text(json.dumps(self.plan))
        run = self.make(eden)
        run.acquired = []
        entered, release, deadline = threading.Event(), threading.Event(), threading.Event()
        workers, answers, errors = [], [], []
        abandon = runner.macro_gate._Outcome.abandon

        class PausedLock:
            def __init__(self):
                self.lock = threading.RLock()

            def acquire(self, *args, **kwargs):
                return self.lock.acquire(*args, **kwargs)

            def release(self):
                self.lock.release()
                context = getattr(run._gate_context, 'gate', None)
                if context is not None and context.get('acquisition_completed') and not entered.is_set():
                    workers.append(threading.current_thread())
                    entered.set()
                    release.wait(1)

        def abandon_at_deadline(outcome):
            abandoned = abandon(outcome)
            if not abandoned:
                deadline.set()
            return abandoned

        def call():
            try:
                answers.append(runner.macro_gate.bounded_call(lambda _: run.check_gate('a', name), 'gate', .22))
            except BaseException as error:
                errors.append(error)

        run._gate_lock = PausedLock()
        caller = threading.Thread(target=call)
        try:
            with mock.patch.object(runner.macro_gate._Outcome, 'abandon', new=abandon_at_deadline):
                caller.start()
                self.assertTrue(entered.wait(1))
                self.assertTrue(deadline.wait(1))
                self.assertTrue(caller.is_alive())
                self.assertNotIn(name, runner.dry_run(self.manifest)['references_to_acquire'])
                release.set()
                caller.join(1)
                self.assertFalse(caller.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(answers, [(True, str(self.dir / gate['reference']))])
            self.assertTrue(run.last_gate_outcome['passed'])
            self.assertEqual(len(run.acquired), 1)
        finally:
            release.set()
            caller.join(1)
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())

    def test_acquisition_receipt_publication_cannot_outlive_the_gate_cap(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1001])
        eden.colour = RED
        run = self.make(eden)
        run.acquired = []
        run.plan['gates']['capped'] = {'kind': 'image', 'reference': 'refs/blocked.png', 'acquire': True,
                                       'roi': [0, 0, 64, 36], 'cap': .25, 'interval': .03}
        original_manifest = self.manifest.read_bytes()
        entered, release = threading.Event(), threading.Event()
        chmod = runner.os.chmod

        def holder():
            with run._gate_lock:
                entered.set()
                release.wait(1)

        worker = threading.Thread(target=holder)
        timer = threading.Timer(.4, release.set)

        def stage(path, mode):
            chmod(path, mode)
            worker.start()
            self.assertTrue(entered.wait(1))
            timer.start()

        try:
            started = time.monotonic()
            with mock.patch.object(runner.os, 'chmod', side_effect=stage):
                with self.assertRaisesRegex(RuntimeError, 'gate capped on a did not pass within 0.25 s: '
                                            'gate evidence was still being written'):
                    run.check_gate('a', 'capped')
            self.assertLessEqual(time.monotonic() - started, .3)
            self.assertTrue(run.last_gate_outcome['timed_out'])
            self.assertEqual(run.acquired, [])
            self.assertFalse((self.dir / 'refs' / 'blocked.png').exists())
            self.assertEqual(self.manifest.read_bytes(), original_manifest)
        finally:
            release.set()
            if timer.is_alive():
                timer.join(1)
            if worker.ident is not None:
                worker.join(1)
                self.assertFalse(worker.is_alive())

    def test_action_log_publication_and_gate_supersession_are_serialized(self):
        eden = FakeEden({})
        run = self.make(eden)
        for name in ('old', 'new'):
            run.plan['gates'][name] = {'kind': 'status', 'cap': 2, 'expect': {'running': True}}
        entered, release, started = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original_open, original_log = Path.open, run.log

        def open_file(path, *args, **kwargs):
            if path == run.out / 'actions.jsonl' and threading.current_thread() is old:
                entered.set()
                release.wait(2)
            return original_open(path, *args, **kwargs)

        def tagged(record):
            record['owner'] = threading.current_thread().name
            return original_log(record)

        def gate(name):
            try:
                if name == 'new':
                    started.set()
                run.check_gate('a', name)
            except Exception as error:
                errors.append(error)

        eden.request = lambda method, params: {'ok': True, 'result': {'running': True}}
        run.log = tagged
        old = threading.Thread(target=gate, args=('old',), name='old')
        successor = threading.Thread(target=gate, args=('new',), name='successor')
        try:
            with mock.patch.object(Path, 'open', new=open_file):
                old.start()
                self.assertTrue(entered.wait(1))
                successor.start()
                self.assertTrue(started.wait(1))
                successor.join(.1)
                waited = successor.is_alive()
                release.set()
                old.join(1)
                successor.join(1)
            self.assertFalse(old.is_alive())
            self.assertFalse(successor.is_alive())
            self.assertTrue(waited)
            rows = [json.loads(line) for line in (run.out / 'actions.jsonl').read_text().splitlines()]
            owners = [row['owner'] for row in rows]
            successor_at = owners.index('successor')
            self.assertTrue(successor_at > 0)
            self.assertTrue(all(owner == 'old' for owner in owners[:successor_at]))
            self.assertTrue(all(owner == 'successor' for owner in owners[successor_at:]))
            self.assertTrue(all(isinstance(error, routes.closed_loop.DispatchRejected) for error in errors))
        finally:
            release.set()
            old.join(1)
            successor.join(1)
            self.assertFalse(old.is_alive())
            self.assertFalse(successor.is_alive())

    def test_a_superseded_hook_cannot_change_gate_evidence(self):
        eden = FakeEden({})
        run = self.make(eden)
        run.plan['gates']['hook'] = {'kind': 'hook', 'cap': 2}
        run.plan['gates']['status'] = {'kind': 'status', 'cap': 2, 'expect': {'running': True}}
        entered, release = threading.Event(), threading.Event()
        errors = []
        original = eden.request

        def request(method, params):
            if method == 'status':
                return {'ok': True, 'result': {'running': True}}
            return original(method, params)

        def hook(path, gate):
            entered.set()
            release.wait(2)
            return True

        def old_gate():
            try:
                run.check_gate('a', 'hook')
            except Exception as error:
                errors.append(error)

        eden.request = request
        worker = threading.Thread(target=old_gate)
        try:
            with mock.patch.object(runner, 'load_hook', return_value=hook), \
                    mock.patch.object(runner.still_fresh, 'usable', return_value=True):
                worker.start()
                self.assertTrue(entered.wait(1))
                run.check_gate('a', 'status')
                release.set()
                worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], routes.closed_loop.DispatchRejected)
            self.assertTrue(run._gate_evidence)
            self.assertEqual(run.gate_saw_evidence, [True])
            self.assertEqual(run.last_gate_outcome['gate'], 'status')
        finally:
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_a_superseded_acquisition_cannot_publish_gate_or_receipt_state(self):
        eden = FakeEden({})
        run = self.make(eden)
        run.plan['gates']['acquire'] = {'kind': 'image', 'reference': 'refs/acquired.png',
                                       'roi': [0, 0, 64, 36], 'cap': 2, 'interval': .01, 'acquire': True}
        run.plan['gates']['status'] = {'kind': 'status', 'cap': 2, 'expect': {'running': True}}
        run.acquired = []
        manifest_before = self.manifest.read_bytes()
        entered, release = threading.Event(), threading.Event()
        errors = []
        original = eden.request
        digest = runner.digest

        def request(method, params):
            if method == 'status':
                return {'ok': True, 'result': {'running': True}}
            return original(method, params)

        def delayed_digest(path):
            if threading.current_thread() is worker:
                entered.set()
                release.wait(2)
            return digest(path)

        def old_gate():
            try:
                run.check_gate('a', 'acquire')
            except Exception as error:
                errors.append(error)

        eden.request = request
        worker = threading.Thread(target=old_gate)
        try:
            with mock.patch.object(runner, 'digest', side_effect=delayed_digest), \
                    mock.patch.object(runner.still_fresh, 'usable', return_value=True):
                worker.start()
                self.assertTrue(entered.wait(1))
                run.check_gate('a', 'status')
                release.set()
                worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], routes.closed_loop.DispatchRejected)
            self.assertNotIn('sha256', run.plan['gates']['acquire'])
            self.assertNotIn('acquired', run.plan['gates']['acquire'])
            self.assertEqual(run.acquired, [])
            self.assertFalse((self.dir / 'refs' / 'acquired.png').exists())
            self.assertEqual(self.manifest.read_bytes(), manifest_before)
            self.assertEqual(run.last_gate_outcome['gate'], 'status')
        finally:
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_a_superseded_gate_does_not_start_its_recovery(self):
        eden = FakeEden({})
        run = self.make(eden)
        run.plan['gates']['old'] = {'kind': 'status', 'expect': {'running': False},
                                    'cap': 2, 'recover': 'to-red'}
        run.plan['gates']['new'] = {'kind': 'status', 'expect': {'running': True}, 'cap': 2}
        entered, release = threading.Event(), threading.Event()
        errors = []
        original = eden.request

        def request(method, params):
            if method == 'status':
                if threading.current_thread() is worker:
                    entered.set()
                    release.wait(2)
                return {'ok': True, 'result': {'running': True}}
            return original(method, params)

        def old_gate():
            try:
                run.gate('a', 'old')
            except Exception as error:
                errors.append(error)

        eden.request = request
        worker = threading.Thread(target=old_gate)
        try:
            worker.start()
            self.assertTrue(entered.wait(1))
            run.gate('a', 'new')
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], routes.closed_loop.DispatchRejected)
            self.assertNotIn('macro.play', eden.calls)
            self.assertEqual(run.last_gate_outcome['gate'], 'new')
        finally:
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_a_superseded_recovery_recheck_cannot_requalify_or_play(self):
        eden = FakeEden({})
        run = self.make(eden)
        run.plan['gates']['old'] = {'kind': 'status', 'expect': {'running': False},
                                    'cap': 2, 'recover': 'to-red'}
        run.plan['gates']['start'] = {'kind': 'status', 'expect': {'running': True}, 'cap': 2}
        run.plan['gates']['new'] = {'kind': 'status', 'expect': {'running': True}, 'cap': 2}
        run.plan['segments']['to-red']['start_gate'] = 'start'
        entered, release = threading.Event(), threading.Event()
        errors, reads, polls = [], [], []
        original_request, original_check = eden.request, run.check_gate

        def request(method, params):
            if method == 'macro.status':
                polls.append(1)
                return {'ok': True, 'result': {'state': 'playing' if len(polls) == 1 else 'idle'}}
            if method == 'status':
                if threading.current_thread() is not threading.main_thread():
                    entered.set()
                    release.wait(2)
                return {'ok': True, 'result': {'running': True}}
            return original_request(method, params)

        def check(on, name, *, cap=None):
            reads.append((name, cap))
            if name == 'old' and cap is None:
                raise RuntimeError('initial destination failure')
            if name == 'start' and cap is None:
                return True
            return original_check(on, name, cap=cap)

        def old_gate():
            try:
                run.gate('a', 'old')
            except Exception as error:
                errors.append(error)

        eden.request = request
        run.check_gate = check
        worker = threading.Thread(target=old_gate)
        try:
            with mock.patch.object(runner.macro_gate, 'IDLE_CAP', 2):
                worker.start()
                self.assertTrue(entered.wait(1))
                run.gate('a', 'new')
                release.set()
                worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], routes.closed_loop.DispatchRejected)
            self.assertEqual([name for name, cap in reads if name == 'start' and cap is not None], [])
            self.assertNotIn('macro.play', eden.calls)
            self.assertEqual(run.last_gate_outcome['gate'], 'new')
        finally:
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_a_latched_dashboard_macro_releases_before_waiting(self):
        path, _, _ = routes.macro_file('lobby-a')
        release = json.loads(path.read_text())['events'][-1]
        self.assertEqual(release['buttons'], 0)
        self.assertEqual(release['sticks'], [0, 0, 0, 0])
        self.assertEqual(release['triggers'], [0, 0])
        bridge = self.dir / 'bridge.log'
        bridge.write_text('')
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FakeEden({})
        run = self.make(eden)
        run.closed_loop_owns_idle = True
        ctx = runner.StepContext(run, 'a')
        picker = routes.BridgePickerIO(ctx)

        picker.send('lobby-a')
        self.assertEqual(eden.calls, ['macro.play', 'macro.status', 'input.reset'])
        self.assertTrue(picker.in_flight)
        self.assertIn('a', picker.pending)

        picker.finish()
        picker.send('main-menu-down')
        self.assertEqual(eden.calls[3:], ['macro.play', 'macro.status', 'input.reset'])

    def test_runner_play_waits_for_idle_and_rechecks_the_start_gate(self):
        run, eden, now = self.waiting_run()
        run.plan['segments']['to-red']['start_gate'] = 'red'
        with mock.patch.object(run, 'check_gate', return_value=True) as check:
            run.play('a', 'to-red')
        self.assertEqual(check.call_count, 2)
        self.assertEqual(eden.calls.count('macro.play'), 1)
        self.assertGreaterEqual(eden.played_times[0], 1.2)

    def test_runner_play_does_not_play_when_the_start_screen_left_during_the_wait(self):
        run, eden, _ = self.waiting_run()
        run.plan['segments']['to-red']['start_gate'] = 'red'
        with mock.patch.object(run, 'check_gate', side_effect=[True, RuntimeError('start screen left')]):
            with self.assertRaisesRegex(RuntimeError, 'start screen left'):
                run.play('a', 'to-red')
        self.assertNotIn('macro.play', eden.calls)

    def test_a_segment_without_a_start_gate_is_played_after_the_wait(self):
        run, eden, now = self.waiting_run()
        run.play('a', 'to-red')
        self.assertEqual(eden.calls.count('macro.play'), 1)
        self.assertEqual(eden.played_paths, [str(self.dir / 'segments' / 'to-red.edenmacro.json')])
        self.assertGreaterEqual(eden.played_times[0], 1.2)

    def test_gate_recovery_is_skipped_when_the_gate_passes_after_the_wait(self):
        run, eden, _ = self.waiting_run()
        run.plan['gates']['red']['recover'] = 'to-red'
        with mock.patch.object(run, 'check_gate', side_effect=[RuntimeError('not red'), True, True]) as check:
            run.gate('a', 'red')
        self.assertEqual(check.call_count, 3)
        self.assertNotIn('macro.play', eden.calls)

    def test_gate_recovery_refuses_a_segment_changed_after_the_dry_run(self):
        run, eden, _ = self.waiting_run()
        run.plan['gates']['red']['recover'] = 'to-red'
        run.plan['segments']['to-red']['sha256'] = '0' * 64
        with mock.patch.object(run, 'check_gate', side_effect=RuntimeError('not red')):
            with self.assertRaisesRegex(RuntimeError, 'changed since the dry run'):
                run.gate('a', 'red')
        self.assertNotIn('macro.play', eden.calls)

    def test_gate_recovery_refuses_a_segment_without_a_hash(self):
        run, eden, _ = self.waiting_run()
        run.plan['gates']['red']['recover'] = 'to-red'
        run.plan['segments']['to-red'].pop('sha256', None)
        with mock.patch.object(run, 'check_gate', side_effect=RuntimeError('not red')):
            with self.assertRaisesRegex(RuntimeError, 'to-red has no sha256; refusing to play an unhashed'):
                run.gate('a', 'red')
        self.assertNotIn('macro.play', eden.calls)
        self.assertNotIn('macro.status', eden.calls)

    def test_a_stop_from_the_recovery_gate_recheck_is_not_mistaken_for_a_failed_gate(self):
        class Cancelled(RuntimeError):
            pass

        run, eden, _ = self.waiting_run()
        run.plan['gates']['red']['recover'] = 'to-red'
        calls, armed = [], [False]

        def stop():
            if armed[0]:
                armed[0] = False
                raise Cancelled('one-shot STOP')

        def gate(on, name, **options):
            calls.append(name)
            if len(calls) == 1:
                raise RuntimeError('not red')
            armed[0] = True
            stop()

        with mock.patch.object(run, 'check_stop', stop), \
                mock.patch.object(run, 'check_gate', side_effect=gate):
            with self.assertRaisesRegex(Cancelled, 'one-shot STOP'):
                run.gate('a', 'red')
        self.assertNotIn('macro.play', eden.calls)
        self.assertEqual(getattr(run, 'row_macro_waits', []), [])

    def test_play_library_uses_the_same_gate(self):
        run, eden, _ = self.waiting_run()
        entry = {'file': 'segments/to-red.edenmacro.json', 'sha256': sha(self.dir / 'segments/to-red.edenmacro.json'),
                 'start_gate': 'red'}
        with mock.patch.object(runner, 'LIBRARY', self.dir), \
                mock.patch.object(runner, 'library_index', return_value={'segments': {'shared': entry}}), \
                mock.patch.object(run, 'check_gate', return_value=True) as check:
            run.play_library('a', 'shared')
        self.assertEqual(check.call_count, 2)
        self.assertEqual(eden.calls.count('macro.play'), 1)
        self.assertEqual(eden.played_paths, [str(self.dir / 'segments' / 'to-red.edenmacro.json')])

    def test_a_runner_macro_wait_is_listed_in_the_row_receipt(self):
        run, eden, _ = self.waiting_run()
        run.plan['rows'] = [{'id': 'r1', 'start': 'blue', 'end': 'red',
                             'steps': [{'play': 'to-red', 'unchecked': True}]}]
        run.run()
        receipt = json.loads((run.out / 'r1.json').read_text())
        self.assertEqual(len(receipt['macro_waits']), 1)
        self.assertEqual(receipt['macro_waits'][0]['outcome'], 'the caller declared no witness, accepted')
        rows = (run.out / 'r1' / 'stall-events.jsonl').read_text().splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])['block'], 'runner')
        self.assertEqual(eden.calls.count('macro.play'), 1)

    def test_runner_call_itself_never_waits(self):
        run, eden, now = self.waiting_run()
        run.call('a', 'macro.play', {'path': str(self.dir / 'segments' / 'to-red.edenmacro.json')})
        self.assertEqual(eden.calls.count('macro.play'), 1)
        self.assertNotIn('macro.status', eden.calls)
        self.assertEqual(now[0], 0)

    def test_a_start_gate_recheck_that_outlasts_the_budget_sends_nothing(self):
        run, eden, now = self.waiting_run()
        run.plan['segments']['to-red']['start_gate'] = 'red'
        run.plan['gates']['red']['cap'] = 40
        caps = []
        entered, release = threading.Event(), threading.Event()
        workers = []
        bounded_call = runner.macro_gate.bounded_call

        def check(on, gate, name):
            caps.append(gate['cap'])
            if len(caps) == 2:
                workers.append(threading.current_thread())
                entered.set()
                release.wait(2)
            return True

        def bounded(call, method, cap, **kwargs):
            if kwargs.get('with_completion'):
                join = threading.Thread.join

                def ordered_join(worker, timeout=None):
                    self.assertTrue(entered.wait(1))
                    self.assertIs(worker, workers[0])
                    now[0] += method + .1
                    release.set()
                    join(worker, 1)
                    self.assertFalse(worker.is_alive())

                with mock.patch.object(threading.Thread, 'join', new=ordered_join):
                    return bounded_call(call, method, cap, **kwargs)
            return bounded_call(call, method, cap, **kwargs)

        try:
            with mock.patch.object(run, 'check_gate_spec', side_effect=check), \
                    mock.patch.object(runner.macro_gate, 'bounded_call', side_effect=bounded):
                with self.assertRaises(routes.closed_loop.DispatchRejected):
                    run.play('a', 'to-red')
        finally:
            release.set()
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())
        self.assertEqual(len(caps), 2)
        self.assertLess(caps[1], 15)
        self.assertEqual(run.plan['gates']['red']['cap'], 40)
        self.assertNotIn('macro.play', eden.calls)
        self.assertEqual(len(run.row_macro_waits), 1)
        self.assertEqual(run.row_macro_waits[0]['outcome'], 'not sent: the bound passed during requalification')

    def test_a_blocked_status_start_gate_recheck_cannot_outlive_the_idle_budget(self):
        run, eden, now = self.waiting_run()
        run.plan['segments']['to-red']['start_gate'] = 'red'
        run.plan['gates']['red'] = {'kind': 'status', 'cap': 40, 'expect': {'running': True}}
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        gate_done, logs = threading.Event(), []
        original = eden.request
        check_spec = run.check_gate_spec
        status_calls, workers = [], []

        def checked(on, gate, name):
            try:
                return check_spec(on, gate, name)
            finally:
                if entered.is_set():
                    gate_done.set()

        def request(method, params):
            if method == 'macro.status':
                eden.calls.append(method)
                return {'ok': True, 'result': {'state': 'playing' if now[0] < 14.4 else 'idle'}}
            if method == 'status':
                status_calls.append(method)
                if len(status_calls) == 2:
                    workers.append(threading.current_thread())
                    entered.set()
                    try:
                        release.wait(.8)
                        now[0] += 4
                    finally:
                        exited.set()
                    return {'ok': True, 'result': {'accepted': True, 'job': 'late'}}
                return {'ok': True, 'result': {'running': True}}
            return original(method, params)

        def expire():
            if entered.wait(1):
                now[0] = 15

        eden.request = request
        run.check_gate_spec = checked
        run.log = logs.append
        timer = threading.Thread(target=expire)
        timer.start()
        try:
            with self.assertRaises(routes.closed_loop.DispatchRejected):
                run.play('a', 'to-red')
            rejected_at = now[0]
            self.assertTrue(entered.is_set())
            self.assertLessEqual(rejected_at, 15.25)
            self.assertNotIn('macro.play', eden.calls)
            self.assertEqual([event['outcome'] for event in run.row_macro_waits],
                             ['not sent: requalification did not answer within the bound'])
            last_gate_outcome = copy.deepcopy(run.last_gate_outcome)
        finally:
            release.set()
            timer.join(1)
            self.assertTrue(exited.wait(1))
            self.assertTrue(gate_done.wait(1))
            workers[0].join(1)
            self.assertFalse(workers[0].is_alive())
        self.assertFalse(timer.is_alive())
        self.assertNotIn('macro.play', eden.calls)
        self.assertNotIn('jobs.get', eden.calls)
        self.assertEqual(len([entry for entry in logs if entry.get('gate') == 'red']), 1)
        self.assertEqual(run.last_gate_outcome, last_gate_outcome)

    def test_gate_recovery_after_a_wait_needs_the_segments_own_start_gate(self):
        run, eden, _ = self.waiting_run()
        run.plan['gates']['red']['recover'] = 'to-red'
        run.plan['gates']['blue'] = {**run.plan['gates']['red'], 'recover': None}
        run.plan['segments']['to-red']['start_gate'] = 'blue'
        checks = []

        def check(on, name, **options):
            checks.append(name)
            if name == 'red':
                raise RuntimeError('not red')
            if checks.count('blue') > 1:
                raise RuntimeError('not blue')
            return True

        with mock.patch.object(run, 'check_gate', side_effect=check):
            with self.assertRaisesRegex(RuntimeError, r'to-red.*blue'):
                run.gate('a', 'red')
        self.assertEqual(checks, ['red', 'blue', 'red', 'blue'])
        self.assertNotIn('macro.play', eden.calls)
        self.assertEqual(len(run.row_macro_waits), 1)
        self.assertIn('not sent:', run.row_macro_waits[0]['outcome'])

    def test_gate_recovery_after_a_wait_sends_when_both_gates_qualify(self):
        run, eden, now = self.waiting_run()
        run.plan['gates']['red']['recover'] = 'to-red'
        run.plan['gates']['red']['cap'] = 40
        run.plan['gates']['blue'] = {**run.plan['gates']['red'], 'recover': None}
        run.plan['segments']['to-red']['start_gate'] = 'blue'
        checks = []

        def check(on, name, **options):
            checks.append((name, options))
            if name == 'red' and sum(checked == 'red' for checked, _ in checks) < 3:
                if sum(checked == 'red' for checked, _ in checks) == 2:
                    now[0] += 2
                raise RuntimeError('not red')
            return True

        with mock.patch.object(run, 'check_gate', side_effect=check):
            run.gate('a', 'red')
        self.assertEqual([name for name, _ in checks], ['red', 'blue', 'red', 'blue', 'red'])
        self.assertAlmostEqual(checks[2][1]['cap'] - checks[3][1]['cap'], 2)
        self.assertEqual(eden.calls.count('macro.play'), 1)
        self.assertEqual(run.row_macro_waits[0]['outcome'], 'requalified, accepted')

    def test_play_library_caps_its_start_gate_recheck_to_the_remaining_budget(self):
        run, eden, now = self.waiting_run()
        run.plan['gates']['red']['cap'] = 40
        entry = {'file': 'segments/to-red.edenmacro.json',
                 'sha256': sha(self.dir / 'segments/to-red.edenmacro.json'), 'start_gate': 'red'}
        original = eden.request

        def request(method, params):
            if method == 'macro.status' and now[0] < 14.2:
                eden.calls.append(method)
                return {'ok': True, 'result': {'state': 'playing', 'error': ''}}
            return original(method, params)

        eden.request = request
        with mock.patch.object(runner, 'LIBRARY', self.dir), \
                mock.patch.object(runner, 'library_index', return_value={'segments': {'shared': entry}}), \
                mock.patch.object(run, 'check_gate_spec', return_value=True) as check:
            run.play_library('a', 'shared')
        self.assertEqual(check.call_count, 2)
        self.assertAlmostEqual(check.call_args_list[1].args[1]['cap'], 15 - now[0])
        self.assertEqual(run.plan['gates']['red']['cap'], 40)
        self.assertEqual(eden.calls.count('macro.play'), 1)

    def test_gate_recovery_on_an_idle_channel_keeps_the_existing_fast_path(self):
        eden = FakeEden({})
        run = self.make(eden)
        run.plan['gates']['red']['recover'] = 'to-red'
        run.plan['segments']['to-red']['start_gate'] = 'paused'
        events = []
        request = eden.request

        def check(on, name, **options):
            events.append(name)
            if events == ['red']:
                raise RuntimeError('not red')
            return True

        def record_request(method, params):
            if method == 'macro.play':
                events.append('macro.play')
            return request(method, params)

        eden.request = record_request
        with mock.patch.object(run, 'check_gate', side_effect=check):
            run.gate('a', 'red')
        self.assertEqual(events, ['red', 'paused', 'macro.play', 'red'])
        self.assertEqual(eden.calls.count('macro.play'), 1)
        self.assertEqual(getattr(run, 'row_macro_waits', []), [])

    def test_idle_recovery_is_refused_when_the_segments_start_gate_fails(self):
        eden = FakeEden({})
        run = self.make(eden)
        run.plan['gates']['red']['recover'] = 'to-red'
        run.plan['segments']['to-red']['start_gate'] = 'paused'
        names = []

        def check(on, name, **options):
            names.append(name)
            raise RuntimeError('not ' + name)

        with mock.patch.object(run, 'check_gate', side_effect=check):
            with self.assertRaisesRegex(RuntimeError, r'to-red.*paused'):
                run.gate('a', 'red')
        self.assertEqual(names, ['red', 'paused'])
        self.assertNotIn('macro.play', eden.calls)

    def test_idle_recovery_checks_the_start_gate_before_its_one_press(self):
        eden = FakeEden({})
        run = self.make(eden)
        run.plan['gates']['red']['recover'] = 'to-red'
        run.plan['segments']['to-red']['start_gate'] = 'paused'
        names = []

        def check(on, name, **options):
            names.append(name)
            if names == ['red']:
                raise RuntimeError('not red')
            return True

        with mock.patch.object(run, 'check_gate', side_effect=check):
            run.gate('a', 'red')
        self.assertEqual(names, ['red', 'paused', 'red'])
        self.assertEqual(eden.calls.count('macro.play'), 1)

    def test_stop_during_the_recovery_start_gate_propagates_without_input(self):
        eden = FakeEden({})
        run = self.make(eden)
        run.plan['gates']['red']['recover'] = 'to-red'
        run.plan['segments']['to-red']['start_gate'] = 'paused'

        def check(on, name, **options):
            if name == 'red':
                raise RuntimeError('not red')
            raise runner.StopRun('STOP-INPUT')

        with mock.patch.object(run, 'check_gate', side_effect=check):
            with self.assertRaises(runner.StopRun):
                run.gate('a', 'red')
        self.assertNotIn('macro.play', eden.calls)
        self.assertEqual(getattr(run, 'row_macro_waits', []), [])

    def test_a_rejected_dispatch_after_requalification_is_recorded_as_rejected(self):
        run, eden, _ = self.waiting_run()
        run.plan['segments']['to-red']['start_gate'] = 'red'
        original = eden.request

        def request(method, params):
            if method == 'macro.play':
                eden.calls.append(method)
                return {'ok': False, 'error': 'input_busy'}
            return original(method, params)

        eden.request = request
        with mock.patch.object(run, 'check_gate', return_value=True):
            with self.assertRaises(routes.closed_loop.DispatchRejected):
                run.play('a', 'to-red')
        self.assertEqual(eden.calls.count('macro.play'), 1)
        self.assertIsNone(eden.playing)
        self.assertEqual(len(run.row_macro_waits), 1)
        self.assertIn('requalified, rejected:', run.row_macro_waits[0]['outcome'])
        self.assertIn('input_busy', run.row_macro_waits[0]['outcome'])

    def test_shot_records_the_game_frame_and_edens_reply(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        run = self.make(FreshEden(bridge, [1000]))
        path = run.shot('a', 'capture')
        record = run.still_records[str(path)]
        self.assertEqual((record['boot'], record['frame'], record['wall']), ('0xabc', 1000, 17000))
        self.assertEqual((record['backend'], record['uniform'], record['warning']),
                         ('window', False, 'test warning'))
        self.assertEqual(record['sha256'], sha(path))
        self.assertEqual(record['status'], 'fresh')
        self.assertEqual(json.loads((run.out / 'stills.jsonl').read_text().splitlines()[0]), record)

    def test_a_row_capture_and_its_freshness_record_name_the_same_bytes(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        self.plan['rows'] = [{'id': 'r1', 'start': 'blue', 'end': 'blue',
                              'steps': [{'shot': 'first'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        run = self.make(FreshEden(bridge, [1000], [BLUE]))
        with mock.patch.object(runner.Runner, 'bridge_at_shot', return_value={}):
            run.run()
        receipt = json.loads((run.out / 'r1.json').read_text())
        self.assertEqual(len(receipt['captures']), 1)
        capture = receipt['captures'][0]
        path = run.out / 'r1' / capture['file']
        record = run.still_records[str(path)]
        journal = json.loads((run.out / 'stills.jsonl').read_text())
        self.assertEqual(set(capture), {'name', 'file', 'sha256', 'utc', 'status'})
        self.assertEqual(capture['file'], record['file'])
        self.assertEqual(capture['file'], journal['file'])
        self.assertEqual(capture['sha256'], record['sha256'])
        self.assertEqual(capture['sha256'], journal['sha256'])
        self.assertEqual(capture['sha256'], sha(path))
        self.assertEqual(record['status'], 'fresh')

    def test_shot_keeps_the_boot_across_a_long_log(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        bridge.write_bytes(b'[Bridge] boot=0xabc frame=1000 wall=17000\n')
        run = self.make(FakeEden({}))
        first = run.still_records[str(run.shot('a', 'first'))]
        with bridge.open('ab') as stream:
            stream.write(b'[Bridge] state long-padding=xxxxxxxxxxxxxxxxxxxxxxxx frame=1200 wall=20400\n' * 2000)
        self.assertGreater(bridge.stat().st_size, 65536)
        second = run.still_records[str(run.shot('a', 'second'))]
        self.assertEqual((first['status'], second['status']), ('fresh', 'refused'))
        self.assertEqual((second['boot'], second['frame'], second['unchanged_frames']), ('0xabc', 1200, 200))

    def test_shot_does_not_inherit_boot_from_a_replaced_log(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        bridge.write_bytes(b'[Bridge] boot=0xabc frame=1000 wall=17000\n')
        run = self.make(FakeEden({}))
        run.shot('a', 'first')
        replacement = self.dir / 'replacement.log'
        replacement.write_bytes(b'[Bridge] padding=' + b'x' * 200 + b' frame=1200 wall=20400\n')
        replacement.replace(bridge)
        second = run.still_records[str(run.shot('a', 'second'))]
        self.assertEqual((second['boot'], second['frame'], second['status']), (None, 1200, 'unverified'))

    def test_a_frozen_still_is_listed_in_the_row_receipt(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        self.plan['rows'] = [
            {'id': 'r1', 'start': 'blue', 'end': 'blue', 'steps': [{'shot': 'first'}, {'shot': 'frozen'}]},
            {'id': 'r2', 'start': 'blue', 'end': 'blue', 'steps': [{'shot': 'changed'}]},
        ]
        self.manifest.write_text(json.dumps(self.plan))
        run = self.make(FreshEden(bridge, [1000, 1200, 1300], [BLUE, BLUE, RED]))
        with mock.patch.object(runner.Runner, 'bridge_at_shot', return_value={}):
            run.run()
        first = json.loads((run.out / 'r1.json').read_text())
        second = json.loads((run.out / 'r2.json').read_text())
        self.assertEqual(first['status'], 'PASS')
        self.assertEqual(len(first['stills_refused']), 1)
        self.assertIn('unchanged across 200 game frames', first['stills_refused'][0]['reason'])
        self.assertIn('frozen.png', first['stills_refused'][0]['file'])
        self.assertNotIn('stills_refused', second)

    def test_an_image_gate_does_not_pass_on_a_refused_still(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1200])
        eden.colour = RED
        run = self.make(eden)
        run.shot('a', 'anchor')
        gate = {**self.plan['gates']['red'], 'cap': .15, 'interval': .03}
        with self.assertRaisesRegex(RuntimeError, 'still refused as evidence: unchanged across 200 game frames'):
            run.check_gate_spec('a', gate, 'red')

    def test_an_image_gate_passes_on_a_fresh_matching_still(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000])
        eden.colour = RED
        run = self.make(eden)
        path = run.check_gate_spec('a', self.plan['gates']['red'], 'red')
        self.assertEqual(run.still_records[path]['status'], 'fresh')

    def test_a_change_gate_does_not_pass_on_a_refused_still(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [900, 1000, 1200], [BLUE, RED, RED])
        run = self.make(eden)
        run.marks = {}
        run._step_mark('a', {'mark': 'before'})
        run.shot('a', 'anchor')
        gate = {'kind': 'change', 'mark': 'before', 'roi': [0, 0, 64, 36], 'stable': False,
                'cap': .15, 'interval': .03}
        with self.assertRaisesRegex(RuntimeError, 'still refused as evidence'):
            run.check_gate_spec('a', gate, 'change')

    def test_a_refused_mark_is_not_established(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        run = self.make(FreshEden(bridge, [1000, 1200]))
        run.marks = {}
        run.shot('a', 'anchor')
        with self.assertRaisesRegex(RuntimeError, 'mark before on a: still refused as evidence: unchanged across 200'):
            run._step_mark('a', {'mark': 'before'})
        self.assertNotIn(('a', 'before'), run.marks)

    def test_a_failed_remark_does_not_leave_the_previous_mark(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        run = self.make(FreshEden(bridge, [1000, 1200]))
        run.marks = {}
        run._step_mark('a', {'mark': 'before'})
        with self.assertRaisesRegex(RuntimeError, 'still refused as evidence'):
            run._step_mark('a', {'mark': 'before'})
        self.assertNotIn(('a', 'before'), run.marks)

    def test_a_change_gate_refuses_a_refused_baseline(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        run = self.make(FreshEden(bridge, [1000, 1200, 1300], [BLUE, BLUE, RED]))
        run.shot('a', 'anchor')
        before = run.shot('a', 'refused-mark')
        fresh = run.shot('a', 'fresh-after')
        self.assertEqual((run.still_records[str(before)]['status'], run.still_records[str(fresh)]['status']),
                         ('refused', 'fresh'))
        run.marks = {('a', 'before'): before}
        gate = {'kind': 'change', 'mark': 'before', 'roi': [0, 0, 64, 36], 'stable': False,
                'cap': .15, 'interval': .03}
        with mock.patch.object(run, 'shot', return_value=fresh) as shot:
            with self.assertRaisesRegex(RuntimeError, 'gate change: baseline mark before is not valid evidence: '
                                                      'unchanged across 200'):
                run.check_gate_spec('a', gate, 'change')
            shot.assert_not_called()

    def test_a_change_gate_passes_against_a_fresh_baseline(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        run = self.make(FreshEden(bridge, [1000, 1010], [BLUE, RED]))
        run.marks = {}
        run._step_mark('a', {'mark': 'before'})
        gate = {'kind': 'change', 'mark': 'before', 'roi': [0, 0, 64, 36], 'stable': False,
                'cap': .15, 'interval': .03}
        path = run.check_gate_spec('a', gate, 'change')
        self.assertEqual(run.still_records[path]['status'], 'fresh')

    def test_a_change_gate_refuses_an_error_baseline(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        run = self.make(FreshEden(bridge, [1000, 1010], [BLUE, RED]))
        before = run.shot('a', 'before')
        fresh = run.shot('a', 'after')
        run.still_records[str(before)] = {'status': 'error', 'reason': 'freshness check failed: disk'}
        run.marks = {('a', 'before'): before}
        gate = {'kind': 'change', 'mark': 'before', 'roi': [0, 0, 64, 36], 'stable': False,
                'cap': .15, 'interval': .03}
        with mock.patch.object(run, 'shot', return_value=fresh) as shot:
            with self.assertRaisesRegex(RuntimeError, 'baseline mark before is not valid evidence: '
                                                      'freshness check failed: disk'):
                run.check_gate_spec('a', gate, 'change')
            shot.assert_not_called()

    def test_a_change_gate_refuses_a_missing_baseline_record(self):
        run = self.make(FakeEden({}))
        run.marks = {('a', 'before'): self.dir / 'missing-record.png'}
        gate = {'kind': 'change', 'mark': 'before', 'roi': [0, 0, 64, 36], 'stable': False,
                'cap': .15, 'interval': .03}
        with mock.patch.object(run, 'shot') as shot:
            with self.assertRaisesRegex(RuntimeError, 'baseline mark before is not valid evidence: '
                                                      'no freshness record'):
                run.check_gate_spec('a', gate, 'change')
            shot.assert_not_called()

    def test_a_mark_accepts_same_and_unverified_stills(self):
        run = self.make(FakeEden({}))
        run.marks = {}
        run._step_mark('a', {'mark': 'unverified'})
        self.assertEqual(run.still_records[str(run.marks[('a', 'unverified')])]['status'], 'unverified')

        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        run = self.make(FreshEden(bridge, [1000, 1060]))
        run.marks = {}
        run.shot('a', 'anchor')
        run._step_mark('a', {'mark': 'same'})
        self.assertEqual(run.still_records[str(run.marks[('a', 'same')])]['status'], 'same')

    def test_a_ledger_write_failure_never_authorises_an_image_gate(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        self.plan['gates']['red'].update(cap=.15, interval=.03)
        self.plan['rows'] = [{'id': 'r1', 'start': 'blue', 'end': 'blue',
                              'steps': [{'shot': 'anchor'}, {'gate': 'red'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        journal = self.dir / 'out' / 'stills.jsonl'

        class BrokenLedgerEden(FreshEden):
            def __init__(self):
                super().__init__(bridge, [1000, 1200], [RED, RED])
                self.screenshot_count = 0

            def request(self, method, params):
                if method == 'ui.screenshot':
                    self.screenshot_count += 1
                    if self.screenshot_count == 2:
                        journal.unlink()
                        journal.mkdir()
                return super().request(method, params)

        run = self.make(BrokenLedgerEden())
        with mock.patch.object(runner.Runner, 'bridge_at_shot', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'still not usable as evidence: freshness check failed:'):
                run.run()
        receipt = json.loads((run.out / 'r1.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertTrue(receipt['still_errors'])
        self.assertTrue(all((run.out / 'r1' / entry['file']).is_file() for entry in receipt['still_errors']))
        self.assertTrue(all(entry['reason'].startswith('freshness check failed:') for entry in receipt['still_errors']))

    def test_a_ledger_write_failure_never_authorises_acquire(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1200])
        eden.colour = RED
        run = self.make(eden)
        run.acquired = []
        run.shot('a', 'anchor')
        journal = run.out / 'stills.jsonl'
        journal.unlink()
        journal.mkdir()
        gate = {'kind': 'image', 'reference': 'refs/acquired-error.png', 'acquire': True,
                'roi': [0, 0, 64, 36], 'cap': .15, 'interval': .03}
        with self.assertRaisesRegex(RuntimeError, 'still not usable as evidence: freshness check failed:'):
            run.check_gate_spec('a', gate, 'acquired')
        self.assertFalse((self.dir / gate['reference']).exists())
        self.assertEqual(run.acquired, [])
        self.assertTrue(run.row_still_errors)
        self.assertTrue(all((run.row_dir / entry['file']).is_file() for entry in run.row_still_errors))

    def test_a_ledger_write_failure_blocks_a_mark(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        run = self.make(FreshEden(bridge, [1000, 1200]))
        run.marks = {}
        run.shot('a', 'anchor')
        journal = run.out / 'stills.jsonl'
        journal.unlink()
        journal.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'mark before on a: still not usable as evidence: '
                                                  'freshness check failed:'):
            run._step_mark('a', {'mark': 'before'})
        self.assertNotIn(('a', 'before'), run.marks)
        self.assertTrue((run.row_dir / run.row_still_errors[-1]['file']).is_file())

    def test_the_picture_is_kept_when_recording_fails(self):
        run = self.make(FakeEden({}))
        (run.out / 'stills.jsonl').mkdir()
        path = run.shot('a', 'diagnostic')
        self.assertTrue(path.is_file())
        record = run.still_records[str(path)]
        self.assertEqual((record['file'], record['path'], record['on'], record['status']),
                         (path.name, str(path), 'a', 'error'))
        self.assertTrue(record['reason'].startswith('freshness check failed:'))
        self.assertEqual(run.row_still_errors, [{'file': path.name, 'reason': record['reason']}])

    def test_a_frame_source_exception_preserves_the_picture_and_records_an_error(self):
        run = self.make(FakeEden({}))
        with mock.patch.object(runner.still_fresh.FrameSource, 'read', side_effect=RuntimeError('frame failure')):
            path = run.shot('a', 'diagnostic')
        self.assertTrue(path.is_file())
        self.assertEqual(run.still_records[str(path)]['status'], 'error')
        self.assertIn('frame failure', run.row_still_errors[0]['reason'])
        self.assertIn('still_error', (run.out / 'actions.jsonl').read_text())

    def test_still_errors_are_reported_and_reset_between_rows(self):
        self.plan['rows'] = [
            {'id': 'r1', 'start': 'blue', 'end': 'blue', 'steps': [{'shot': 'broken'}]},
            {'id': 'r2', 'start': 'blue', 'end': 'blue', 'steps': [{'shot': 'healthy'}]},
        ]
        self.manifest.write_text(json.dumps(self.plan))
        run = self.make(FakeEden({}))
        original = run.stills.record
        attempts = []

        def record(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError('temporary ledger outage')
            return original(*args, **kwargs)

        output = io.StringIO()
        with mock.patch.object(run.stills, 'record', side_effect=record), mock.patch('sys.stdout', output):
            run.run()
        first = json.loads((run.out / 'r1.json').read_text())
        second = json.loads((run.out / 'r2.json').read_text())
        self.assertEqual((first['status'], second['status']), ('PASS', 'PASS'))
        self.assertEqual(len(first['still_errors']), 1)
        self.assertNotIn('still_errors', second)
        self.assertIn('"still_errors": 1', output.getvalue())

    def test_a_missing_freshness_record_cannot_authorise_an_image_gate(self):
        run = self.make(FakeEden({}))
        path = self.dir / 'matching.png'
        Image.new('RGB', (64, 36), RED).save(path)
        gate = {**self.plan['gates']['red'], 'cap': .15, 'interval': .03}
        with mock.patch.object(run, 'shot', return_value=path):
            with self.assertRaisesRegex(RuntimeError, 'still not usable as evidence: no freshness record'):
                run.check_gate_spec('a', gate, 'red')
        self.assertTrue(path.is_file())

    def test_a_missing_freshness_record_cannot_authorise_a_change_gate(self):
        run = self.make(FakeEden({}))
        run.marks = {}
        run._step_mark('a', {'mark': 'before'})
        path = self.dir / 'different.png'
        Image.new('RGB', (64, 36), RED).save(path)
        gate = {'kind': 'change', 'mark': 'before', 'roi': [0, 0, 64, 36], 'stable': False,
                'cap': .15, 'interval': .03}
        with mock.patch.object(run, 'shot', return_value=path):
            with self.assertRaisesRegex(RuntimeError, 'still not usable as evidence: no freshness record'):
                run.check_gate_spec('a', gate, 'change')
        self.assertTrue(path.is_file())

    def test_a_missing_freshness_record_cannot_authorise_acquire(self):
        run = self.make(FakeEden({}))
        run.acquired = []
        path = self.dir / 'unrecorded.png'
        Image.new('RGB', (64, 36), RED).save(path)
        gate = {'kind': 'image', 'reference': 'refs/unrecorded.png', 'acquire': True,
                'roi': [0, 0, 64, 36], 'cap': .15, 'interval': .03}
        with mock.patch.object(run, 'shot', return_value=path):
            with self.assertRaisesRegex(RuntimeError, 'still not usable as evidence: no freshness record'):
                run.check_gate_spec('a', gate, 'acquired')
        self.assertTrue(path.is_file())
        self.assertFalse((self.dir / gate['reference']).exists())
        self.assertEqual(run.acquired, [])

    def test_acquire_never_writes_a_reference_from_a_refused_still(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)
        eden = FreshEden(bridge, [1000, 1200])
        eden.colour = RED
        run = self.make(eden)
        run.acquired = []
        run.shot('a', 'anchor')
        gate = {'kind': 'image', 'reference': 'refs/acquired.png', 'acquire': True,
                'roi': [0, 0, 64, 36], 'cap': .15, 'interval': .03}
        with self.assertRaisesRegex(RuntimeError, 'still refused as evidence'):
            run.check_gate_spec('a', gate, 'acquired')
        self.assertFalse((self.dir / gate['reference']).exists())
        self.assertEqual(run.acquired, [])

    def test_acquire_writes_a_reference_from_a_fresh_still(self):
        bridge = self.dir / 'bridge.log'
        self.instances['a']['bridge_log'] = str(bridge)

        class NewBootEden(FreshEden):
            def __init__(self):
                super().__init__(bridge, [1000, 1200])
                self.colour = RED
                self.shots = 0

            def request(self, method, params):
                result = super().request(method, params)
                if method == 'ui.screenshot':
                    self.shots += 1
                    if self.shots >= 2:
                        with bridge.open('a') as stream:
                            stream.write('[Bridge] boot=0xdef frame=1200 wall=20400\n')
                return result

        run = self.make(NewBootEden())
        run.acquired = []
        manifest_mode = self.manifest.stat().st_mode & 0o777
        gate = {'kind': 'image', 'reference': 'refs/acquired.png', 'acquire': True,
                'roi': [0, 0, 64, 36], 'cap': 1, 'interval': .03}
        result = run.check_gate_spec('a', gate, 'acquired')
        self.assertEqual(result, str(self.dir / gate['reference']))
        self.assertEqual(run.still_records[run.acquired[0]['shot']]['status'], 'fresh')
        self.assertTrue(Path(result).is_file())
        self.assertEqual(self.manifest.stat().st_mode & 0o777, manifest_mode)

    def test_shot_without_a_bridge_log_behaves_as_before(self):
        eden = FreshEden(None, [1000])
        eden.colour = RED
        run = self.make(eden)
        path = run.shot('a', 'without-bridge')
        self.assertEqual(run.still_records[str(path)]['status'], 'unverified')
        passed = run.check_gate_spec('a', self.plan['gates']['red'], 'red')
        self.assertEqual(run.still_records[passed]['status'], 'unverified')

    def test_fresh_runner_restores_captured_bridge_log_without_overwriting(self):
        run = self.dir / 'out' / 'bridge-run'
        run.mkdir(parents=True)
        (run / 'capture.json').write_text(json.dumps({
            'pid': 123, 'port': 6979, 'instance': 'a', 'bridge_log': str(run / 'bridge.log'), 'started': 1,
        }))
        self.make(FakeEden({}))
        self.assertEqual(self.instances['a']['bridge_log'], str(run / 'bridge.log'))
        self.instances['a']['bridge_log'] = 'existing.log'
        self.make(FakeEden({}))
        self.assertEqual(self.instances['a']['bridge_log'], 'existing.log')

    def test_dry_run_accepts_disabled_early_stop(self):
        self.plan['early_stop'] = False
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(runner.dry_run(self.manifest)['errors'], [])

    def test_gates_comes_from_instances_not_a_hardcoded_filename(self):
        self.instances['a']['diag_log'] = str(self.dir / 'practice-diagnostics.log')
        self.assertEqual(self.make(FakeEden({})).source('a', 'gates'), Path('gates'))
        path = self.dir / 'practice-gates.log'
        self.instances['a']['gates'] = str(path)
        self.assertEqual(self.make(FakeEden({})).source('a', 'gates'), path)

    def test_dry_run_ready_to_record(self):
        report = runner.dry_run(self.manifest)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['missing_segments'], ['to-blue'])
        self.assertEqual(report['result'], 'READY_TO_RECORD')

    def test_run_refuses_a_plan_without_a_target_and_dry_run_reports_it(self):
        check = getattr(runner, 'up_check', None)
        self.assertTrue(callable(check))
        report = check(self.manifest)
        self.assertIn('packet gate: rows.json has no "target" (the build the rows were checked against)',
                      report['errors'])
        self.assertEqual(report['result'], 'BROKEN')
        report = runner.dry_run(self.manifest)
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['packet_gate']['result'], 'NO TARGET')

    def test_dry_run_writes_the_packet_receipt_and_run_requires_a_target(self):
        out = self.dir / 'out'
        self.assertEqual(runner.main(['dry-run', str(self.manifest), '--out', str(out)]), 0)
        receipt = json.loads((out / 'packet-gate.json').read_text())
        self.assertEqual(receipt['result'], 'NO TARGET')
        self.assertEqual(receipt['packet_sha256'], runner.packet_gate.packet_digest(self.dir))
        self.assertEqual(receipt['manifest_sha256'], sha(self.manifest))
        self.assertEqual(receipt['runner_sha256'], sha(Path(runner.__file__)))
        self.assertEqual(runner.main(['run', str(self.manifest), '--out', str(out)]), 1)
        self.assertEqual(json.loads((out / 'dry-run.json').read_text())['result'], 'BROKEN')

    def test_state_gate_poll_s_validation(self):
        state = {'kind': 'state', 'provider': 'tr.menu', 'where': {'phase': 'flight'}, 'cap': 1}
        self.plan['gates']['timed'] = state
        for value in (.05, 2, None, '0.2', True):
            with self.subTest(invalid=value):
                state['poll_s'] = value
                self.manifest.write_text(json.dumps(self.plan))
                errors = runner.dry_run(self.manifest)['errors']
                self.assertIn('gate timed: poll_s must be between 0.1 and 1.0', errors)
        for value in (.1, .2, 1, 1.0):
            with self.subTest(valid=value):
                state['poll_s'] = value
                self.manifest.write_text(json.dumps(self.plan))
                errors = runner.dry_run(self.manifest)['errors']
                self.assertNotIn('gate timed: poll_s must be between 0.1 and 1.0', errors)

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
        self.plan['rows'][1]['role'] = 'test'
        self.manifest.write_text(json.dumps(self.plan))
        eden = FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)
        self.make(eden).run()
        for row, role in (('r1', 'setup'), ('r2', 'test'), ('r3', 'setup')):
            receipt = json.loads((self.dir / 'out' / f'{row}.json').read_text())
            self.assertEqual(receipt['status'], 'PASS')
            self.assertEqual(receipt['role'], role)
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

    def test_non_json_step_value_writes_receipt_and_runs_next_row(self):
        (self.dir / 'steps.py').write_text(
            'def return_set(ctx):\n'
            '    return {"stage_ids": {3}}\n')
        self.plan['rows'] = [
            {'id': 'r1', 'start': 'blue', 'end': 'blue',
             'steps': [{'run': 'steps.py:return_set'}]},
            {'id': 'r2', 'start': 'blue', 'end': 'blue',
             'steps': [{'shot': 'next'}]},
        ]
        self.manifest.write_text(json.dumps(self.plan))
        self.make(FakeEden({})).run()
        first = json.loads((self.dir / 'out/r1.json').read_text())
        second = json.loads((self.dir / 'out/r2.json').read_text())
        attempts = [json.loads(line) for line in (self.dir / 'out/attempts.jsonl').read_text().splitlines()]
        self.assertEqual(first['status'], 'PASS')
        self.assertEqual(first['results'][0]['value']['stage_ids'], '{3}')
        self.assertEqual(second['status'], 'PASS')
        self.assertEqual([(row['id'], row['status']) for row in attempts], [('r1', 'PASS'), ('r2', 'PASS')])

    def test_log_source_names_with_or_without_log_suffix(self):
        self.instances = {'a': {'socket': 'fake', 'console': str(self.log)}}  # gate says console_log
        eden = FakeEden({'to-red': RED, 'to-blue': BLUE}, log_path=self.log)
        self.make(eden).run()
        self.assertEqual(json.loads((self.dir / 'out' / 'r2.json').read_text())['status'], 'PASS')

    def test_a_hook_play_the_gate_did_not_check_stops_before_anything_is_sent(self):
        def run(source, args=None):
            (self.dir / 'steps.py').write_text(source)
            step = {'run': 'steps.py:press', **({'args': args} if args else {})}
            self.plan['rows'] = [{'id': 'r1', 'start': 'blue', 'end': 'red', 'steps': [step]}]
            self.manifest.write_text(json.dumps(self.plan))
            shutil.rmtree(self.dir / 'out', ignore_errors=True)
            eden = FakeEden({'to-red': RED})
            try:
                self.make(eden).run()
            except RuntimeError:
                pass
            receipt = json.loads((self.dir / 'out' / 'r1.json').read_text())
            return receipt['status'], eden.calls.count('macro.play'), receipt.get('error', '')

        # A name the gate checked for this plan is played: written at the call, or passed by the manifest.
        self.assertEqual(run('def press(ctx):\n    ctx.play("to-red")\n')[:2], ('PASS', 1))
        self.assertEqual(run('def press(ctx, name):\n    ctx.play(name)\n', {'name': 'to-red'})[:2], ('PASS', 1))
        # A name built at run time is one the gate could not check: the play stops and nothing is sent, even
        # though the name is a segment of the plan.
        status, sent, error = run('def press(ctx):\n    ctx.play("to-" + "red")\n')
        self.assertEqual((status, sent), ('FAILED', 0))
        self.assertIn('G8: ctx.play("to-red") is a name the packet gate did not check for this plan', error)
        self.assertIn('nothing was sent', error)
        # The same call with its closed set declared is checked, and plays.
        self.assertEqual(run('def press(ctx):\n    ctx.play("to-" + "red", among=("to-red", "pause"))\n')[:2],
                         ('PASS', 1))
        # A name outside the declared set stops too.
        status, sent, error = run('def press(ctx):\n    ctx.play("to-" + "red", among=("pause",))\n')
        self.assertEqual((status, sent), ('FAILED', 0))
        self.assertIn('to-red is not among the names this step declares', error)
        # A value outside a call's own set stops even though another call of this plan plays that value.
        status, sent, error = run('def press(ctx):\n    ctx.play("pause")\n'
                                  '    ctx.play("pa" + "use", among=("to-red",))\n')
        self.assertEqual((status, sent), ('FAILED', 1))
        self.assertIn('pause is not among the names this step declares', error)

    def test_an_unchecked_manifest_segment_has_no_side_effect_when_a_hook_plays_it(self):
        # Both segments are in the manifest. `to-red` has a valid hash and a start gate; `to-blue` has a recipe and
        # no file, so playing it would record. The gate checked neither for this plan: its only hook plays `pause`.
        self.plan['segments']['to-red']['start_gate'] = 'red'
        (self.dir / 'steps.py').write_text('def press(ctx):\n    ctx.play("pause")\n')
        self.plan['rows'] = [{'id': 'r1', 'start': 'blue', 'end': 'blue', 'steps': [{'run': 'steps.py:press'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        eden = FakeEden({'to-red': RED})
        run = self.make(eden)
        self.assertEqual(run.play_names(), {'pause'})
        ctx = runner.StepContext(run, 'a')
        before = list(eden.calls)
        with mock.patch.object(run, 'gate') as gate, mock.patch.object(run, 'record') as record:
            for name in ('to-red', 'to-blue'):
                with self.assertRaisesRegex(RuntimeError, f'G8: ctx.play\\("{name}"\\) is a name the packet gate '
                                                          'did not check for this plan; nothing was sent'):
                    ctx.play(name)
            gate.assert_not_called()
            record.assert_not_called()
        # No request reached Eden: no start-gate capture, no recording, no macro.
        self.assertEqual(eden.calls, before)
        self.assertFalse((self.dir / 'segments' / 'to-blue.edenmacro.json').exists())
        # The checked name does go through, start to finish.
        ctx.play('pause')
        self.assertEqual(eden.calls[len(before):].count('macro.play'), 1)

    def _check_unusual_step_value(self, expression, expected):
        (self.dir / 'steps.py').write_text('def value(ctx):\n    return ' + expression + '\n')
        self.plan['rows'] = [
            {'id': 'r1', 'start': 'blue', 'end': 'blue', 'steps': [{'run': 'steps.py:value'}]},
            {'id': 'r2', 'start': 'blue', 'end': 'blue', 'steps': [{'shot': 'next'}]},
        ]
        self.manifest.write_text(json.dumps(self.plan))
        try:
            self.make(FakeEden({})).run()
        except (TypeError, ValueError) as exc:
            self.fail(str(exc))
        first = json.loads((self.dir / 'out/r1.json').read_text())
        second = json.loads((self.dir / 'out/r2.json').read_text())
        self.assertEqual(first['status'], 'PASS')
        self.assertEqual(first['results'][0]['value'], expected)
        self.assertEqual(second['status'], 'PASS')

    def test_non_json_step_value_writes_receipt_and_runs_next_row(self):
        self._check_unusual_step_value('{"stage_ids": {3}}', {'stage_ids': '{3}'})

    def test_an_unwritable_step_value_is_recorded_as_text(self):
        self._check_unusual_step_value('{("a", 1): 2}', "{('a', 1): 2}")

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

    def test_attempt_journal_preserves_failure_retry_and_skips_passed_rows(self):
        self.plan['rows'] = [self.plan['rows'][0]]
        self.plan['rows'][0]['role'] = 'test'
        self.plan['gates']['red']['cap'] = .1
        self.manifest.write_text(json.dumps(self.plan))
        with self.assertRaises(RuntimeError):
            self.make(FakeEden({'to-red': BLUE})).run()
        (self.dir / 'out' / 'STOP-INPUT').unlink()
        self.make(FakeEden({'to-red': RED})).run()
        journal = self.dir / 'out' / 'attempts.jsonl'
        before = journal.read_bytes()
        self.make(FakeEden({'to-red': RED})).run()
        self.assertEqual(journal.read_bytes(), before)
        attempts = [json.loads(line) for line in before.splitlines()]
        self.assertEqual([(a['id'], a['role'], a['status']) for a in attempts],
                         [('r1', 'test', 'FAILED'), ('r1', 'test', 'PASS')])
        self.assertTrue(all(set(a) == {'id', 'role', 'role_source', 'manifest_sha256', 'start', 'end',
                                       'started', 'ended', 'status'} for a in attempts))
        self.assertTrue(all(a['role_source'] == 'explicit' for a in attempts))
        self.assertLessEqual(attempts[0]['started'], attempts[0]['ended'])
        self.assertLess(attempts[0]['started'], attempts[1]['started'])
        self.assertLessEqual(attempts[1]['started'], attempts[1]['ended'])
        receipt = json.loads((self.dir / 'out' / 'r1.json').read_text())
        self.assertEqual((receipt['status'], receipt['started'], receipt['ended']),
                         ('PASS', attempts[1]['started'], attempts[1]['ended']))

    def test_receipts_and_journal_record_explicit_and_default_role_sources(self):
        self.plan['rows'] = [
            {'id': 'explicit', 'start': 'blue', 'end': 'blue', 'role': 'setup', 'steps': [{'shot': 'one'}]},
            {'id': 'default', 'start': 'blue', 'end': 'blue', 'steps': [{'shot': 'two'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.make(FakeEden({})).run()
        attempts = [json.loads(line) for line in (self.dir / 'out' / 'attempts.jsonl').read_text().splitlines()]
        self.assertEqual([(a['id'], a['role'], a['role_source']) for a in attempts],
                         [('explicit', 'setup', 'explicit'), ('default', 'test', 'default')])
        for attempt in attempts:
            receipt = json.loads((self.dir / 'out' / (attempt['id'] + '.json')).read_text())
            self.assertEqual((receipt['role'], receipt['role_source']), (attempt['role'], attempt['role_source']))

    def test_attempt_journal_carries_receipt_manifest_and_screens(self):
        self.plan['rows'] = [
            {'id': 'r1', 'start': 'blue', 'end': 'blue', 'steps': [{'shot': 'one'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.make(FakeEden({})).run()
        receipt = json.loads((self.dir / 'out/r1.json').read_text())
        attempt = json.loads((self.dir / 'out/attempts.jsonl').read_text())
        self.assertEqual({key: attempt[key] for key in ('manifest_sha256', 'start', 'end')},
                         {key: receipt[key] for key in ('manifest_sha256', 'start', 'end')})
        self.assertEqual(attempt['manifest_sha256'], sha(self.manifest))

    def test_attempt_journal_includes_refused_unexecuted_row(self):
        r = self.make(FakeEden({}))
        with mock.patch.object(r, 'session_start', side_effect=runner.StopRun('recording unavailable')):
            with self.assertRaises(runner.StopRun):
                r.run()
        receipt = json.loads((self.dir / 'out' / 'r1.json').read_text())
        attempts = [json.loads(line) for line in (self.dir / 'out' / 'attempts.jsonl').read_text().splitlines()]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0], {key: receipt[key] for key in
                                      ('id', 'role', 'role_source', 'manifest_sha256', 'start', 'end',
                                       'started', 'ended', 'status', 'not_executed')})
        self.assertEqual(attempts[0]['role_source'], 'default')

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
        self.assertEqual(index['gates']['red']['frame'], [64, 36])
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

    def test_a_dependent_row_is_not_played_after_its_source_failed(self):
        self._continue_plan()
        self._third_row_recovers_to_red()
        self.plan['rows'][0]['gives'] = ['drill:a']
        self.plan['rows'][1]['needs'] = ['drill:a']
        self.plan['rows'][1]['steps'] = [{'call': 'settings.get'}]
        self.manifest.write_text(json.dumps(self.plan))
        out = self.dir / 'out'
        out.mkdir()
        (out / 'packet-gate.json').write_text(json.dumps(
            {'rows': {'r2': {'needs': ['drill:a'], 'provided_by': {'drill:a': 'r1'}}}}))
        eden = FakeEden({'to-red': RED, 'to-fix': RED}, log_path=self.log)
        self.assertEqual(self.make(eden).run(), 'FAILED')
        receipt = json.loads((out / 'r2.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertEqual(receipt['error'], 'NOT RUN (setup): needs "drill:a" from row r1, which did not pass')
        self.assertTrue(receipt['not_executed'])
        self.assertEqual(receipt['failure_kind'], 'harness-suspect')
        self.assertNotIn('settings.get', eden.calls)
        self.assertEqual(json.loads((out / 'r3.json').read_text())['status'], 'PASS')
        shutil.rmtree(out)
        self.plan['rows'][0]['steps'] = [{'call': 'status'}]
        self.manifest.write_text(json.dumps(self.plan))
        out.mkdir()
        (out / 'packet-gate.json').write_text(json.dumps(
            {'rows': {'r2': {'needs': ['drill:a'], 'provided_by': {'drill:a': 'r1'}}}}))
        eden = FakeEden({'to-red': RED}, log_path=self.log)
        self.assertEqual(self.make(eden).run(), 'DONE')
        self.assertEqual(json.loads((out / 'r2.json').read_text())['status'], 'PASS')
        self.assertIn('settings.get', eden.calls)
        for gates_status, should_run in (('PASS', True), (None, False), ('FAIL', False)):
            with self.subTest(gates_status=gates_status):
                shutil.rmtree(out)
                out.mkdir()
                (out / 'packet-gate.json').write_text(json.dumps(
                    {'rows': {'r2': {'needs': ['drill:a'], 'provided_by': {'drill:a': 'r1'}}}}))
                source = {'status': 'RAN-UNVERIFIED'}
                source['plan_identity'] = runner.plan_identity(runner.expand_library(copy.deepcopy(self.plan)))
                source['instances_sha256'] = None
                source['inputs_identity'] = getattr(runner.packet_gate, 'inputs_identity', lambda *_: None)(
                    self.dir, runner.expand_library(copy.deepcopy(self.plan)))
                if gates_status is not None:
                    source['gates_status'] = gates_status
                (out / 'r1.json').write_text(json.dumps(source))
                eden = FakeEden({'to-fix': RED}, log_path=self.log)
                self.assertEqual(self.make(eden).run(only={'r2'}), 'DONE' if should_run else 'FAILED')
                receipt = json.loads((out / 'r2.json').read_text())
                self.assertEqual(receipt['status'], 'PASS' if should_run else 'FAILED')
                self.assertEqual('settings.get' in eden.calls, should_run)
                if not should_run:
                    self.assertTrue(receipt['not_executed'])
                    self.assertEqual(receipt['error'],
                                     'NOT RUN (setup): needs "drill:a" from row r1, which did not pass')

    def _identity_dependency_plan(self, recording=False):
        self.plan['early_stop'] = False
        self.plan['rows'] = [
            {'id': 'producer', 'start': 'blue', 'end': 'blue', 'role': 'setup', 'gives': ['seed'],
             'steps': [{'play': 'to-blue', 'unchecked': True}] if recording else [{'call': 'status'}]},
            {'id': 'dependent', 'start': 'blue', 'end': 'blue', 'role': 'setup', 'needs': ['seed'],
             'steps': [{'call': 'settings.get'}]},
        ]
        self.manifest.write_text(json.dumps(self.plan))
        out = self.dir / 'out'
        out.mkdir(exist_ok=True)
        dependencies = {'dependent': {'needs': ['seed'], 'provided_by': {'seed': 'producer'}}}
        (out / 'packet-gate.json').write_text(json.dumps({'rows': dependencies}))
        return out, dependencies

    def test_dependency_refuses_a_different_packet_or_instances_file(self):
        out, dependencies = self._identity_dependency_plan()
        first = self.dir / 'instances-one.json'
        second = self.dir / 'instances-two.json'
        first.write_text(json.dumps(self.instances))
        second.write_text(json.dumps(self.instances) + '\n')
        self.assertNotEqual(sha(first), sha(second))
        for reason in ('packet', 'instances', 'missing'):
            with self.subTest(reason=reason):
                if (out / 'dependent.json').exists():
                    (out / 'dependent.json').unlink()
                eden = FakeEden({})
                engine = runner.Runner(self.manifest, copy.deepcopy(self.instances), out, 15,
                                       client_factory=lambda _: eden, instances_sha256=sha(second))
                source = {'status': 'PASS',
                          'plan_identity': 'another-packet' if reason == 'packet' else runner.plan_identity(engine.plan),
                          'instances_sha256': sha(second) if reason == 'packet' else sha(first),
                          'inputs_identity': getattr(runner.packet_gate, 'inputs_identity', lambda *_: None)(
                              self.dir, engine.plan)}
                if reason == 'missing':
                    source.pop('plan_identity')
                (out / 'producer.json').write_text(json.dumps(source))
                self.assertEqual(engine.run(only={'dependent'}), 'FAILED')
                receipt = json.loads((out / 'dependent.json').read_text())
                self.assertTrue(receipt['not_executed'])
                self.assertEqual(receipt['failure_kind'], 'harness-suspect')
                self.assertEqual(receipt['error'], 'NOT RUN (setup): needs "seed" from row producer, '
                                                   'whose receipt is from another packet or another Up')
                self.assertEqual(receipt.get('instances_sha256'), sha(second))
                self.assertNotIn('settings.get', eden.calls)

    def test_dependency_without_instances_file_rejects_a_different_hash(self):
        out, _ = self._identity_dependency_plan()
        eden = FakeEden({})
        engine = self.make(eden)
        (out / 'producer.json').write_text(json.dumps({
            'status': 'PASS', 'plan_identity': runner.plan_identity(engine.plan), 'instances_sha256': 'old-up',
            'inputs_identity': getattr(runner.packet_gate, 'inputs_identity', lambda *_: None)(self.dir, engine.plan)}))
        self.assertEqual(engine.run(only={'dependent'}), 'FAILED')
        receipt = json.loads((out / 'dependent.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertIsNone(receipt.get('instances_sha256'))
        self.assertNotIn('settings.get', eden.calls)

    def test_named_pass_row_reruns_and_unnamed_pass_row_is_skipped(self):
        self.plan['rows'] = [
            {'id': 'again', 'start': 'blue', 'end': 'blue', 'steps': [{'call': 'settings.get'}]},
            {'id': 'keep', 'start': 'blue', 'end': 'blue', 'steps': [{'call': 'status'}]},
        ]
        self.manifest.write_text(json.dumps(self.plan))
        out = self.dir / 'out'
        out.mkdir()
        for row in ('again', 'keep'):
            (out / f'{row}.json').write_text(json.dumps({'status': 'PASS', 'old': row}))
        original = (out / 'keep.json').read_bytes()
        eden = FakeEden({})
        engine = self.make(eden)
        self.assertEqual(engine.run(only={'again'}), 'DONE')
        self.assertEqual(engine.executed_rows, ['again'])
        self.assertIn('settings.get', eden.calls)
        self.assertNotIn('status', eden.calls)
        self.assertEqual((out / 'keep.json').read_bytes(), original)
        self.assertNotIn('old', json.loads((out / 'again.json').read_text()))

    def test_selected_producer_is_rerun_before_selected_dependent(self):
        out, dependencies = self._identity_dependency_plan()
        for row in ('producer', 'dependent'):
            (out / f'{row}.json').write_text(json.dumps({'status': 'PASS', 'plan_identity': 'old-packet'}))
        instances = self.dir / 'instances.json'
        instances.write_text(json.dumps(self.instances))
        eden = FakeEden({})
        report = {'errors': [], 'result': 'READY', 'packet_gate': {'rows': dependencies}}
        with mock.patch.object(runner, 'up_check', return_value=report), \
             mock.patch.object(runner, 'default_client_factory', return_value=lambda _: eden):
            result = runner.main(['run', str(self.manifest), '--instances', str(instances), '--out', str(out),
                                  '--rows', 'producer,dependent'])
        self.assertEqual(result, 0)
        self.assertEqual(eden.calls.count('settings.get'), 1)
        producer = json.loads((out / 'producer.json').read_text())
        dependent = json.loads((out / 'dependent.json').read_text())
        self.assertEqual((producer['status'], dependent['status']), ('PASS', 'PASS'))
        self.assertIsInstance(producer.get('inputs_identity'), str)
        self.assertEqual(producer.get('inputs_identity'), dependent.get('inputs_identity'))
        self.assertIsInstance(producer.get('plan_identity'), str)
        self.assertEqual(producer.get('plan_identity'), dependent.get('plan_identity'))
        self.assertEqual(producer.get('instances_sha256'), sha(instances))
        self.assertEqual(dependent.get('instances_sha256'), sha(instances))
        self.assertLess(producer['started'], dependent['started'])

    def test_recorded_segment_does_not_block_the_dependent(self):
        out, _ = self._identity_dependency_plan(recording=True)
        instances = self.dir / 'instances.json'
        instances.write_text(json.dumps(self.instances))
        eden = FakeEden({'to-blue': BLUE})
        engine = runner.Runner(self.manifest, copy.deepcopy(self.instances), out, 15,
                               client_factory=lambda _: eden, instances_sha256=sha(instances))
        self.assertEqual(engine.run(), 'DONE')
        producer = json.loads((out / 'producer.json').read_text())
        dependent = json.loads((out / 'dependent.json').read_text())
        self.assertEqual((producer['status'], dependent['status']), ('PASS', 'PASS'))
        self.assertIsInstance(producer.get('inputs_identity'), str)
        self.assertEqual(producer.get('inputs_identity'), dependent.get('inputs_identity'))
        self.assertIsInstance(producer.get('plan_identity'), str)
        self.assertEqual(producer.get('plan_identity'), dependent.get('plan_identity'))
        self.assertNotEqual(producer['manifest_sha256'], dependent['manifest_sha256'])
        self.assertIn('recorded', json.loads(self.manifest.read_text())['segments']['to-blue'])
        self.assertIn('macro.record', eden.calls)
        self.assertIn('settings.get', eden.calls)

    def test_recording_identity_is_stable_after_fresh_process_reexpands_library(self):
        self.plan['segments']['shared'] = {'library': 'smash-main'}
        self.plan['gates']['shared'] = {'library': 'main'}
        self.manifest.write_text(json.dumps(self.plan))
        eden = FakeEden({'to-blue': BLUE})
        engine = self.make(eden)
        before = runner.plan_identity(engine.plan)
        self.assertIsInstance(before, str)
        engine.record('a', 'to-blue', engine.plan['segments']['to-blue'])
        after = runner.plan_identity(engine.plan)
        self.assertEqual(before, after)
        saved = json.loads(self.manifest.read_text())
        self.assertIn('recorded', saved['segments']['to-blue'])
        self.assertEqual(runner.expand_library(copy.deepcopy(saved)), saved)
        code = ('import json, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); '
                'import runner; p = Path(sys.argv[2]); '
                'r = runner.Runner(p, {"a": {"socket": "fake"}}, Path(sys.argv[3]), 15, client_factory=lambda _: None); '
                'print(runner.plan_identity(r.plan))')
        proc = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(Path(runner.__file__).parent),
                               str(self.manifest), str(self.dir / 'new-process-out')],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), before)

    def test_dry_run_packet_receipt_records_plan_identity(self):
        out = self.dir / 'out'
        result = runner.main(['dry-run', str(self.manifest), '--out', str(out)])
        self.assertEqual(result, 0)
        receipt = json.loads((out / 'packet-gate.json').read_text())
        self.assertIsInstance(receipt.get('plan_identity'), str)
        self.assertEqual(receipt.get('plan_identity'), runner.plan_identity(runner.expand_library(copy.deepcopy(self.plan))))

    def test_changed_hook_blocks_dependent_then_selected_producer_refreshes_it(self):
        out, _ = self._identity_dependency_plan()
        self.plan['rows'][0]['steps'] = [{'run': 'producer.py:run'}]
        self.manifest.write_text(json.dumps(self.plan))
        hook = self.dir / 'producer.py'
        hook.write_text('def run(ctx): return {"version": 1}\n')
        self.assertEqual(self.make(FakeEden({})).run(only={'producer'}), 'DONE')
        old = json.loads((out / 'producer.json').read_text())
        self.assertIsInstance(old.get('inputs_identity'), str)
        hook.write_text('def run(ctx): return {"version": 2}\n')
        eden = FakeEden({})
        self.assertEqual(self.make(eden).run(only={'dependent'}), 'FAILED')
        refused = json.loads((out / 'dependent.json').read_text())
        self.assertEqual(refused.get('error'), 'NOT RUN (setup): needs "seed" from row producer, '
                                             'whose receipt is from another packet or another Up')
        self.assertTrue(refused.get('not_executed'))
        self.assertNotIn('settings.get', eden.calls)
        eden = FakeEden({})
        self.assertEqual(self.make(eden).run(only={'producer', 'dependent'}), 'DONE')
        self.assertEqual(eden.calls.count('settings.get'), 1)
        new = json.loads((out / 'producer.json').read_text())
        dependent = json.loads((out / 'dependent.json').read_text())
        self.assertNotEqual(old['inputs_identity'], new['inputs_identity'])
        self.assertEqual(new['inputs_identity'], dependent['inputs_identity'])
        self.assertLess(new['started'], dependent['started'])

    def test_changed_fixture_data_blocks_the_dependent_before_its_call(self):
        out, _ = self._identity_dependency_plan()
        fixture = self.dir / 'fixture.json'
        fixture.write_text('{"behavior": 1}\n')
        self.assertEqual(self.make(FakeEden({})).run(only={'producer'}), 'DONE')
        source = json.loads((out / 'producer.json').read_text())
        self.assertIsInstance(source.get('inputs_identity'), str)
        fixture.write_text('{"behavior": 7}\n')
        eden = FakeEden({})
        self.assertEqual(self.make(eden).run(only={'dependent'}), 'FAILED')
        refused = json.loads((out / 'dependent.json').read_text())
        self.assertTrue(refused.get('not_executed'))
        self.assertIn('whose receipt is from another packet or another Up', refused.get('error', ''))
        self.assertNotIn('settings.get', eden.calls)

    def _gif_dependency_fixture(self):
        class GifEden(FakeEden):
            def request(self, method, params):
                if method == 'recording.start':
                    self.calls.append(method)
                    self.gif = dict(params)
                    return {'ok': True, 'result': {'state': 'recording'}}
                if method == 'recording.stop':
                    self.calls.append(method)
                    width = self.gif['width']
                    images = [Image.new('RGB', (width, max(1, width * 9 // 16)), color)
                              for color in (RED, BLUE)]
                    images[0].save(self.gif['path'], save_all=True, append_images=images[1:],
                                   duration=80, loop=0)
                    return {'ok': True, 'result': {'state': 'finished'}}
                if method == 'recording.status':
                    self.calls.append(method)
                    return {'ok': True, 'result': {'state': 'finished', 'frames': 2, 'dropped_frames': 0}}
                return super().request(method, params)

        out, _ = self._identity_dependency_plan()
        self.plan['root'] = str(self.dir)
        self.plan['rows'][0]['steps'] = [
            {'gif': {'name': 'moment', 'preset': 'evidence', 'fps': 12, 'out': 'clips/moment.gif', 'steps': []}}]
        self.manifest.write_text(json.dumps(self.plan))
        (self.dir / 'clips').mkdir()
        neighbor = self.dir / 'clips' / 'fixture.dat'
        neighbor.write_text('before')
        return out, neighbor, GifEden

    def test_a_producers_named_gif_does_not_block_its_dependent(self):
        out, _, gif_client = self._gif_dependency_fixture()
        eden = gif_client({})
        self.assertEqual(self.make(eden).run(), 'DONE')
        producer = json.loads((out / 'producer.json').read_text())
        dependent = json.loads((out / 'dependent.json').read_text())
        self.assertEqual((producer['status'], dependent['status']), ('PASS', 'PASS'))
        self.assertTrue((self.dir / 'clips/moment.gif').is_file())
        self.assertIsInstance(producer.get('inputs_identity'), str)
        self.assertEqual(producer.get('inputs_identity'), dependent.get('inputs_identity'))
        self.assertIn('recording.start', eden.calls)
        self.assertIn('recording.stop', eden.calls)
        self.assertIn('settings.get', eden.calls)

    def test_an_unnamed_file_beside_a_gif_blocks_its_dependent_when_changed(self):
        out, neighbor, gif_client = self._gif_dependency_fixture()
        self.assertEqual(self.make(gif_client({})).run(only={'producer'}), 'DONE')
        self.assertTrue((self.dir / 'clips/moment.gif').is_file())
        neighbor.write_text('after')
        eden = gif_client({})
        self.assertEqual(self.make(eden).run(only={'dependent'}), 'FAILED')
        dependent = json.loads((out / 'dependent.json').read_text())
        self.assertTrue(dependent.get('not_executed'))
        self.assertIn('whose receipt is from another packet or another Up', dependent.get('error', ''))
        self.assertNotIn('settings.get', eden.calls)

    def test_an_acquired_reference_does_not_block_the_dependent(self):
        out, _ = self._identity_dependency_plan()
        self.plan['gates']['new'] = {'kind': 'image', 'reference': 'refs/new.png', 'roi': [0, 0, 64, 36],
                                     'acquire': True, 'cap': 3, 'interval': .05}
        self.plan['rows'][0]['steps'] = [{'gate': 'new'}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(self.make(FakeEden({})).run(only={'producer'}), 'REVIEW')
        producer = json.loads((out / 'producer.json').read_text())
        self.assertIsInstance(producer.get('inputs_identity'), str)
        self.assertTrue((self.dir / 'refs/new.png').is_file())
        eden = FakeEden({})
        self.assertEqual(self.make(eden).run(only={'dependent'}), 'DONE')
        dependent = json.loads((out / 'dependent.json').read_text())
        self.assertEqual(producer['inputs_identity'], dependent['inputs_identity'])
        self.assertIn('settings.get', eden.calls)

    def test_fresh_processes_compute_the_same_inputs_identity(self):
        self.manifest.write_text(json.dumps(self.plan))
        code = ('import json, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); '
                'import runner, packet_gate; p = Path(sys.argv[2]); '
                'print(packet_gate.inputs_identity(p.parent, runner.expand_library(json.loads(p.read_text()))))')
        command = [sys.executable, '-I', '-B', '-c', code, str(Path(runner.__file__).parent), str(self.manifest)]
        first = subprocess.run(command, capture_output=True, text=True)
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertRegex(first.stdout.strip(), r'^[a-f0-9]{64}$')
        self.assertEqual(first.stdout, second.stdout)

    def test_packet_gate_receipt_carries_inputs_identity(self):
        result = runner.main(['dry-run', str(self.manifest), '--out', str(self.dir / 'out')])
        self.assertEqual(result, 0)
        gate = json.loads((self.dir / 'out/packet-gate.json').read_text())
        self.assertIsInstance(gate.get('inputs_identity'), str)

    def _third_row_recovers_to_red(self):
        original = self.dir / 'segments/to-red.edenmacro.json'
        target = self.dir / 'segments/to-fix.edenmacro.json'
        shutil.copy2(original, target)
        self.plan['segments']['to-fix'] = {'file': 'segments/to-fix.edenmacro.json', 'sha256': sha(target)}
        self.plan['rows'][2].update(start_gate='red', recover=[{'play': 'to-fix', 'unchecked': True}])
        self.plan['early_stop'] = False

    def test_failed_recovery_marks_successor_unexecuted_then_recovers_next_row(self):
        self._continue_plan()
        self._third_row_recovers_to_red()
        self.plan['rows'][1]['steps'] = [{'shot': 'ok'}]
        self.manifest.write_text(json.dumps(self.plan))
        eden = FakeEden({'to-red': BLUE, 'to-fix': RED}, log_path=self.log)
        engine = self.make(eden)
        self.assertEqual(engine.run(), 'FAILED')
        self.assertFalse((self.dir / 'out' / 'STOP-INPUT').exists())
        blocked = json.loads((self.dir / 'out' / 'r2.json').read_text())
        self.assertEqual(blocked['status'], 'FAILED')
        self.assertTrue(blocked['not_executed'])
        self.assertTrue(blocked['error'].startswith('not run: recovery to its start failed: '))
        self.assertFalse(blocked['recovery']['ok'])
        self.assertEqual(blocked['next_recovery'], {'to': 'r3', 'ok': True})
        self.assertEqual(blocked['results'], [])
        self.assertFalse(list((self.dir / 'out/r2').glob('*ok*')))
        self.assertEqual(json.loads((self.dir / 'out' / 'r3.json').read_text())['status'], 'PASS')
        receipt = json.loads((self.dir / 'out' / 'r1.json').read_text())
        self.assertFalse(receipt['recovery']['ok'])
        self.assertIn('recovery to r2 failed', receipt['recovery']['error'])
        attempts = [json.loads(line) for line in (self.dir / 'out/attempts.jsonl').read_text().splitlines()]
        self.assertTrue(next(a for a in attempts if a['id'] == 'r2')['not_executed'])
        summary = json.loads((self.dir / 'out' / 'summary.json').read_text())
        self.assertEqual([row['status'] for row in summary['rows']], ['fail', 'fail', 'pass'])

    def test_recovery_hook_raises_and_writes_only_in_recovery_subfolder(self):
        self._continue_plan()
        self._third_row_recovers_to_red()
        with (self.dir / 'steps.py').open('a') as f:
            f.write('def broken(ctx):\n    (ctx.row_dir / "recover.json").write_text("{}")\n'
                    '    raise RuntimeError("recover broke")\n')
        self.plan['rows'][0]['on_failure'] = 'continue'
        self.plan['rows'][1]['recover'] = [{'run': 'steps.py:broken'}]
        self.plan['rows'][1]['steps'] = [{'shot': 'ok'}]
        self.manifest.write_text(json.dumps(self.plan))
        engine = self.make(FakeEden({'to-fix': RED}, log_path=self.log))
        self.assertEqual(engine.run(), 'FAILED')
        receipt = json.loads((self.dir / 'out/r1.json').read_text())
        self.assertIn('recover broke', receipt['recovery']['error'])
        self.assertEqual(receipt['recovery']['to'], 'r2')
        self.assertFalse(receipt['recovery']['ok'])
        self.assertTrue((self.dir / 'out/r2/recovery/recover.json').is_file())
        self.assertFalse((self.dir / 'out/r2/recover.json').exists())
        summary = json.loads((self.dir / 'out/summary.json').read_text())
        self.assertEqual([row['status'] for row in summary['rows']], ['fail', 'fail', 'pass'])
        self.assertFalse((self.dir / 'out/STOP-INPUT').exists())

    def test_failed_rerun_recovery_marks_row_unexecuted(self):
        self._continue_plan()
        with (self.dir / 'steps.py').open('a') as f:
            f.write('def broken(ctx):\n    raise RuntimeError("rerun recover broke")\n')
        self.plan['rows'][1]['recover'] = [{'run': 'steps.py:broken'}]
        self.plan['rows'][1]['steps'] = [{'shot': 'ok'}]
        self.manifest.write_text(json.dumps(self.plan))
        out = self.dir / 'out'
        out.mkdir()
        (out / 'r2.json').write_text(json.dumps({'status': 'FAILED'}))
        engine = self.make(FakeEden({}, log_path=self.log))
        self.assertEqual(engine.run(only={'r2'}), 'FAILED')
        receipt = json.loads((out / 'r2.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertTrue(receipt['not_executed'])
        self.assertEqual(receipt['results'], [])
        self.assertTrue(receipt['error'].startswith('not run: recovery to its start failed: '))
        self.assertFalse(receipt['recovery']['ok'])
        self.assertIn('rerun recover broke', receipt['recovery']['error'])
        self.assertFalse((out / 'STOP-INPUT').exists())
        self.assertIsNone(engine.recovered_to)

    def test_game_stopping_during_recovery_still_stops_run(self):
        self._continue_plan()
        engine = self.make(FakeEden({}, log_path=self.log))
        with mock.patch.object(engine, 'game_running', side_effect=[True, False]), \
             mock.patch.object(engine, 'recover_to', side_effect=runner.RecoveryFailed('game quit')):
            with self.assertRaises(runner.RecoveryFailed):
                engine.run()
        self.assertTrue((self.dir / 'out/STOP-INPUT').exists())
        self.assertFalse((self.dir / 'out/r2.json').exists())

    def test_failed_rerun_recovery_still_stops_if_game_is_not_running(self):
        self._continue_plan()
        out = self.dir / 'out'
        out.mkdir()
        (out / 'r2.json').write_text(json.dumps({'status': 'FAILED'}))
        engine = self.make(FakeEden({}, log_path=self.log))
        with mock.patch.object(engine, 'game_running', return_value=False), \
             mock.patch.object(engine, 'recover_to', side_effect=runner.RecoveryFailed('game quit')):
            with self.assertRaises(runner.RecoveryFailed):
                engine.run(only={'r2'})
        self.assertTrue((out / 'STOP-INPUT').exists())
        receipt = json.loads((out / 'r2.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertFalse(receipt['recovery']['ok'])

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
        tree = Path(tempfile.mkdtemp(prefix='eden-target-'))
        self.addCleanup(shutil.rmtree, tree)
        subprocess.run(['git', '-C', str(tree), 'init', '-q'], check=True)
        (tree / 'source').write_text('target')
        subprocess.run(['git', '-C', str(tree), 'add', 'source'], check=True)
        subprocess.run(['git', '-C', str(tree), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-qm', 'fixture'], check=True)
        commit = subprocess.run(['git', '-C', str(tree), 'rev-parse', 'HEAD'], check=True,
                                capture_output=True, text=True).stdout.strip()
        self.plan['target'] = {'plugin': {'tree': str(tree), 'commit': commit, 'nro_sha256': 'a' * 64}}
        self.manifest.write_text(json.dumps(self.plan))
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
            def __init__(self, screens, frames=4, frame_ms=80, source=None):
                super().__init__(screens)
                self.frames, self.frame_ms, self.gif, self.params = frames, frame_ms, None, []
                self.source = source

            def request(self, method, params):
                self.params.append((method, dict(params or {})))
                if method == 'recording.start':
                    self.gif = dict(params)
                    return {'ok': True, 'result': {'state': 'recording'}}
                if method == 'recording.stop':
                    spec = self.gif or {}
                    width = spec.get('width', 640)
                    height = max(1, round(width * 9 / 16))
                    colors = (RED, BLUE, (100, 100, 100))
                    images = [Image.new('RGB', (width, height), colors[i % len(colors)]) for i in range(self.frames)]
                    images[0].save(spec['path'], save_all=True, append_images=images[1:],
                                   duration=self.frame_ms, loop=0)
                    self.gif = None
                    return {'ok': True, 'result': {'state': 'finished'}}
                if method == 'status' and self.source is not None:
                    return {'ok': True, 'result': self.source}
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
        self.assertEqual(started['fps'], 12)
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

        long = GifEden({}, frames=130)
        gif = {'name': 'long', 'preset': 'docs', 'width': 160, 'reason': 'duration fixture',
               'steps': [{'shot': 'mid'}]}
        self.plan['rows'][0]['steps'] = [{'gif': gif}]
        self.manifest.write_text(json.dumps(self.plan))
        (self.dir / 'out' / 'g1.json').unlink()
        (self.dir / 'out' / 'STOP-INPUT').unlink()
        self.make(long).run()
        info = json.loads((self.dir / 'out' / 'g1.json').read_text())['results'][0]['gif']
        self.assertEqual(info['duration'], 10.0)

        source = {'game_fps': 5, 'stats_age_ms': 50, 'running': True, 'loading_complete': True}
        (self.dir / 'out' / 'g1.json').unlink()
        self.assertEqual(self.make(GifEden({}, frame_ms=200, source=source)).run(), 'DONE')
        info = json.loads((self.dir / 'out' / 'g1.json').read_text())['results'][0]['gif']
        self.assertEqual((info['sampled_fps'], info['source_fps']), (5, 5))
        (self.dir / 'out' / 'g1.json').unlink()
        with self.assertRaisesRegex(RuntimeError, r'at least 12 fps'):
            self.make(GifEden({}, frame_ms=200, source=source | {'stats_age_ms': 5000})).run()

    def test_legacy_and_custom_publication_cannot_request_keyframe_only(self):
        published = {"name": "published", "preset": "docs", "fps": 1}
        presets = {"gif_presets": {"custom": {"width": 1280, "fps": 12, "max_duration_s": 10}}}
        self.plan['rows'] = [{'id': 'g1', 'start': 'blue', 'end': 'blue', 'steps': [{'gif': published}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertIn('at least 12 fps', ' | '.join(runner.dry_run(self.manifest)['errors']))
        for gif in (published, published | {"preset": "custom", "reason": "smaller file"},
                    published | {"preset": "evidence", "out": "docs/media/published.gif"}):
            with self.subTest(gif=gif):
                spec = runner.resolve_gif_spec(presets, {}, gif)
                self.assertRegex(runner.gif_policy_error(spec) or "", r"at least 12 fps")
        menu = published | {"scene": "menu", "fps": 10}
        self.assertIsNone(runner.gif_policy_error(runner.resolve_gif_spec(presets, {}, menu)))
        spec = runner.resolve_gif_spec(presets, {}, menu | {"fps": 9})
        self.assertRegex(runner.gif_policy_error(spec) or "", r"at least 10 fps")

    def test_dry_run_checks_preview_frame_rate_even_with_a_reason(self):
        gif = {'name': 'hover', 'preset': 'preview', 'steps': []}
        self.plan['rows'] = [{'id': 'g1', 'start': 'blue', 'end': 'blue', 'steps': [{'gif': gif}]}]
        for preset, minimum in (('preview', 12), ('preview-menu', 10)):
            with self.subTest(preset=preset):
                gif.update(preset=preset, fps=minimum, reason='smaller file')
                self.manifest.write_text(json.dumps(self.plan))
                self.assertEqual(runner.dry_run(self.manifest)['errors'], [])
                gif['fps'] -= 1
                self.manifest.write_text(json.dumps(self.plan))
                self.assertIn(f'at least {minimum} fps', ' | '.join(runner.dry_run(self.manifest)['errors']))

    def test_preview_verifies_motion_in_written_gif(self):
        raw = self.dir / 'raw.gif'
        out = self.dir / 'preview.gif'
        frames = [Image.new('RGB', (160, 90), color) for color in (RED, BLUE) * 6]
        frames[0].save(raw, save_all=True, append_images=frames[1:], duration=[80, 90, 80] * 4, loop=0)
        spec = runner.resolve_gif_spec({}, {}, {'name': 'demo', 'preset': 'preview', 'width': 160})
        receipt = runner.publish_gif(raw, out, spec)
        self.assertEqual(receipt['frames'], 12)
        self.assertEqual(receipt['duration'], 1.0)

        frames[0].save(raw, duration=1000)
        with self.assertRaisesRegex(runner.GifRefused, r'at least 12 fps'):
            runner.publish_gif(raw, out, spec)
        self.assertFalse(out.exists())

    def test_preview_accepts_a_sampled_hold_that_pillow_merges(self):
        from PIL import GifImagePlugin
        raw = self.dir / 'raw.gif'
        out = self.dir / 'preview.gif'
        frames = [Image.new('P', (160, 90), color) for color in (0, 1)]
        for frame in frames:
            frame.putpalette([*RED, *BLUE])
        # Eden emits an image descriptor per successful sample, including identical frames.
        with raw.open('wb') as capture:
            capture.writelines(GifImagePlugin.getheader(frames[0])[0])
            for frame, delay in zip([frames[0]] * 6 + [frames[1]] * 6, [80, 90, 80] * 4):
                capture.writelines(GifImagePlugin.getdata(frame, duration=delay))
            capture.write(b';')
        with Image.open(raw) as capture:
            self.assertEqual(capture.n_frames, 12)

        spec = runner.resolve_gif_spec({}, {}, {'name': 'demo', 'preset': 'preview', 'width': 160})
        receipt = runner.publish_gif(raw, out, spec)
        self.assertEqual((receipt['frames'], receipt['frames_sampled'], receipt['duration']), (2, 12, 1.0))

        frames[0].save(raw, save_all=True, append_images=frames[1:], duration=500, loop=0)
        with self.assertRaisesRegex(runner.GifRefused, r'at least 12 fps.*sampled'):
            runner.publish_gif(raw, out, spec)

    def test_native_gif_size_fitting_keeps_all_frames(self):
        from unittest.mock import patch
        raw = self.dir / 'raw.gif'
        out = self.dir / 'preview.gif'
        frames = [Image.new('RGB', (160, 90), color) for color in (RED, BLUE) * 6]
        frames[0].save(raw, save_all=True, append_images=frames[1:], duration=80, loop=0)
        spec = runner.resolve_gif_spec({}, {}, {'name': 'demo', 'preset': 'docs', 'width': 160, 'max_bytes': 100})
        real_save = Image.Image.save
        attempts = []

        def simulated_size(image, target, **kwargs):
            attempts.append(len(kwargs['append_images']) + 1)
            return real_save(image, target, **kwargs)

        with patch.object(Image.Image, 'save', simulated_size):
            with self.assertRaisesRegex(runner.GifRefused, r'trim or crop'):
                runner.publish_gif(raw, out, spec)
        self.assertEqual(attempts, [12] * 4)
        self.assertFalse(out.exists())

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
        self.plan['gif_presets'] = {'lobby': {'width': 800, 'fps': 12, 'max_duration_s': 4, 'max_bytes': 2_000_000}}
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
                    size = (self.gif['width'], 32)
                    blue, red = (Image.new('RGB', size, color) for color in (BLUE, RED))
                    blue.save(self.gif['path'], save_all=True, append_images=[red], duration=[80, 80])
                    return {'ok': True, 'result': {}}
                if method == 'recording.status':
                    return {'ok': True, 'result': {'state': 'finished', 'frames': 1, 'error': None}}
                return super().request(method, params)

        # The custom preset's width and reason are preserved.
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

    def _save_shots(self, shots):
        paths = {}
        for name, image in shots.items():
            path = self.dir / f'{name}.png'
            image.save(path)
            paths[name] = path
        return paths

    def test_roi_scale_fit_matches_main_at_full_half_and_half_rendered(self):
        # The library main gate is the ROI only. Its coordinates are the 1280x720 frame they were captured on.
        library = json.loads((Path(runner.__file__).resolve().parent / 'library' / 'index.json').read_text())
        self.assertEqual(library['gates']['main']['frame'], [1280, 720])
        self.assertTrue(library['gates']['main']['reference_is_roi'])
        roi = library['gates']['main']['roi']
        frame = [1280, 720]
        with Image.open(Path(runner.__file__).resolve().parent / 'library' / 'refs' / 'main.png') as ref_im:
            reference = ref_im.convert('RGB')
        self.assertEqual(reference.size, (roi[2] - roi[0], roi[3] - roi[1]))
        full = Image.new('RGB', tuple(frame), (12, 24, 48))
        full.paste(reference, (roi[0], roi[1]))
        half = full.resize((640, 360), Image.Resampling.BOX)
        rendered = half.resize(tuple(frame), Image.Resampling.BILINEAR)
        title = Image.new('RGB', tuple(frame), (180, 40, 20))
        with Image.open(Path(runner.__file__).resolve().parent / 'library' / 'refs' / 'css.png') as css:
            title.paste(css.convert('RGB').resize(reference.size, Image.Resampling.BOX), (roi[0], roi[1]))
        paths = self._save_shots({'full': full, 'half': half, 'rendered': rendered, 'title': title})
        ref = self.dir / 'refs' / 'main.png'
        reference.save(ref)

        diff, note = runner.Runner.compare_region(paths['full'], ref, roi, True, 'fit', frame, 8)
        self.assertEqual(diff, 0)
        self.assertIsNone(note)
        diff, note = runner.Runner.compare_region(paths['half'], ref, roi, True, 'fit', frame, 8)
        self.assertLessEqual(diff, 8)
        self.assertEqual(note, {'scale': 'fit', 'frame': [640, 360], 'reference': frame, 'compared': [150, 35]})
        diff, note = runner.Runner.compare_region(paths['rendered'], ref, roi, True, 'fit', frame, 8)
        self.assertLessEqual(diff, 8)
        self.assertEqual(note['scale'], 'fit')
        self.assertEqual(note['frame'], frame)
        self.assertEqual(note['reference'], frame)
        self.assertEqual(note['compared'], [150, 35])
        diff, note = runner.Runner.compare_region(paths['title'], ref, roi, True, 'fit', frame, 8)
        self.assertGreater(diff, 8)
        self.assertNotIn('compared', note or {})
        # Without the captured frame size, fit still ignores an ROI reference.
        bare, bare_note = runner.Runner.compare_region(paths['half'], ref, roi, True, 'fit')
        self.assertGreater(bare, 8)
        self.assertIsNone(bare_note)

        self.plan['gates']['main'] = {
            'kind': 'image', 'reference': 'refs/main.png', 'sha256': sha(ref), 'roi': roi, 'frame': frame,
            'reference_is_roi': True, 'scale': 'fit', 'tolerance': 8, 'cap': 0.4, 'interval': 0.01}
        self.plan['rows'] = [{'id': 'menu', 'start': 'blue', 'end': 'main', 'steps': [{'gate': 'main'}]}]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertEqual(runner.dry_run(self.manifest)['errors'], [])

        def paint(path):
            eden = FakeEden({})
            original = eden.request

            def request(method, params):
                reply = original(method, params)
                if method == 'ui.screenshot':
                    shutil.copy(path, params['path'])
                return reply

            eden.request = request
            return eden

        self.assertEqual(self.make(paint(paths['half'])).run(), 'DONE')
        receipt = json.loads((self.dir / 'out' / 'menu.json').read_text())
        self.assertEqual(receipt['status'], 'PASS')
        self.assertEqual(receipt['scale'][0],
                         {'gate': 'main', 'scale': 'fit', 'frame': [640, 360], 'reference': frame,
                          'compared': [150, 35]})
        (self.dir / 'out' / 'menu.json').unlink()
        with self.assertRaises(RuntimeError):
            self.make(paint(paths['title'])).run()
        failed = json.loads((self.dir / 'out' / 'menu.json').read_text())
        self.assertEqual(failed['status'], 'FAILED')
        self.assertNotIn('scale', failed)

        self.plan['gates']['main'].pop('frame')
        self.manifest.write_text(json.dumps(self.plan))
        self.assertIn('needs "frame"', ' | '.join(runner.dry_run(self.manifest)['errors']))
        self.plan['gates']['main']['frame'] = [0, 720]
        self.manifest.write_text(json.dumps(self.plan))
        self.assertIn('frame must be [width, height]', ' | '.join(runner.dry_run(self.manifest)['errors']))

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


class BridgeAtShotTests(unittest.TestCase):
    """judge evidence: numeric bridge state recorded with every still on a bridge instance (2026-11-18)."""

    def test_bridge_state_is_recorded_with_the_still_and_never_fails_it(self):
        import routes
        calls = []
        tr = {'pos': {'0': {'dmg': 34.8, 'x': -30.0, 'y': 0.01, 'lr': 1.0, 'status': 73, 'situation': 2,
                            'frame': 9}}, 'frame': 9, 'status_ids': {'FIGHTER_STATUS_KIND_DAMAGE_FLY': 73},
              'stage': 308, 'bodies_from': 'pos'}
        # A slice 3 bridge: its own fighters in any battle scene (a local VS here, no TR status names).
        v2 = {'pos': {4: {'dmg': 12.0, 'x': 40.0, 'y': 0.01, 'lr': -1.0, 'status': 10, 'situation': 0, 'stock': 1,
                          'dead': 0, 'item': -1, 'frame': 77}}, 'frame': 77, 'status_ids': {}, 'bodies_from': 'fighters'}
        answers = [tr, v2]

        class Hw:
            @staticmethod
            def bridge_bodies(run, cap):
                calls.append((run, cap))
                return answers.pop(0)
        fake = type('R', (), {'instances': {'sw': {'bridge_log': '/tmp/run-x/bridge.log'}, 'eden': {}},
                              'BRIDGE_SHOT_CAP_S': runner.Runner.BRIDGE_SHOT_CAP_S})()
        saved = routes._switch_hw
        routes._switch_hw = lambda: Hw
        try:
            got = runner.Runner.bridge_at_shot(fake, 'sw')
            self.assertEqual(got, {'bridge': {'pos': tr['pos'], 'frame': 9, 'status_ids': tr['status_ids'],
                                              'bodies_from': 'pos'}})
            got = runner.Runner.bridge_at_shot(fake, 'sw')
            self.assertEqual(got, {'bridge': {'pos': v2['pos'], 'frame': 77, 'status_ids': {}, 'bodies_from': 'fighters'}})
            self.assertEqual(calls, [(Path('/tmp/run-x'), 1.5)] * 2)
            self.assertEqual(runner.Runner.bridge_at_shot(fake, 'eden'), {})  # no bridge_log: nothing asked
            self.assertEqual(len(calls), 2)

            def timeout(run, cap):
                raise RuntimeError("bridge state: no answer for ['pos']")
            Hw.bridge_bodies = staticmethod(timeout)
            self.assertIn('no answer', runner.Runner.bridge_at_shot(fake, 'sw')['bridge_error'])
        finally:
            routes._switch_hw = saved


class DeclaredPlayTests(unittest.TestCase):
    """A play that declares the names it may take (`among=`) stops before any input on another name."""

    def test_a_segment_play_outside_its_declared_set_stops(self):
        fake = mock.Mock()
        fake.instances = {'a': {}}
        fake.play_names.return_value = {'take-p1', 'take-cpu', 'written'}
        ctx = runner.StepContext(fake, 'a')
        ctx.play('take-p1', among=('take-p1', 'take-cpu'))
        ctx.play('written')
        self.assertEqual(fake.play.call_args_list, [mock.call('a', 'take-p1'), mock.call('a', 'written')])
        with self.assertRaisesRegex(RuntimeError, 'take-p2 is not among the names this step declares'):
            ctx.play('take-p2', among=('take-p1', 'take-cpu'))
        # A name the gate did not check for this plan stops, declared at the call or not.
        for among in (None, ('unchecked',)):
            with self.assertRaisesRegex(RuntimeError, 'G8: ctx.play\\("unchecked"\\) is a name the packet gate '
                                                      'did not check for this plan; nothing was sent'):
                ctx.play('unchecked', among=among)
        self.assertEqual(fake.play.call_count, 2)

    def test_a_route_play_outside_its_declared_set_stops(self):
        ctx = mock.Mock()
        ctx.on = 'a'
        routes.play(ctx, 'tap-a1', among=('tap-a1',))
        self.assertEqual([call.args[0] for call in ctx.call.call_args_list], ['macro.play'])
        ctx.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'smash-main is not among the names this step declares'):
            routes.play(ctx, 'smash-main', among=('tap-a1',))
        ctx.call.assert_not_called()
        routes.play(ctx, 'smash-main')
        self.assertEqual(ctx.call.call_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
