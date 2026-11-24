import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import judge_queue
import native_picker
import routes
import test_native_picker as recorded
from test_setup_calls import MenuReplay


CASES = Path(__file__).parent / 'library' / 'cases'
BOOT = '0x3dec61593ef80877'


def load_case(path):
    case = json.loads(path.read_text())
    args = dict(case.get('args', {}))
    for key, name in case.get('files', {}).items():
        args[key] = (path.parent / name).read_text().splitlines()
    return case, args


class EffectJudgeTests(unittest.TestCase):
    def check_cases(self, hook):
        folder = CASES / hook
        paths = sorted(folder.rglob('*.json'))
        self.assertTrue(paths)
        judge = getattr(routes, routes.EFFECT_JUDGES[hook]['judge'])
        for path in paths:
            with self.subTest(case=path.name):
                case, args = load_case(path)
                try:
                    value = judge(**args)
                    outcome = 'accept' if judge_queue.effect_evidence(value) else 'reject'
                    text = repr(value)
                except Exception as exc:
                    outcome, text = 'reject', str(exc)
                self.assertEqual(outcome, case['expect'], text)
                if 'contains' in case:
                    self.assertIn(case['contains'], text)

    def test_expect_fighters_recorded_cases(self):
        self.check_cases('expect_fighters')

    def test_reach_training_recorded_cases(self):
        self.check_cases('reach_training')

    def test_each_hook_has_an_accept_and_required_rejects(self):
        for hook in ('expect_fighters', 'reach_training'):
            with self.subTest(hook=hook):
                folder = CASES / hook
                paths = {path.name: json.loads(path.read_text()) for path in folder.glob('*.json')}
                self.assertTrue(any(case['expect'] == 'accept' for case in paths.values()))
                for name in ('reject-other-fighter.json', 'reject-missing-roster.json',
                             'reject-other-boot.json', 'reject-other-run.json'):
                    self.assertEqual(paths[name]['expect'], 'reject')
                if hook == 'expect_fighters':
                    self.assertEqual(paths['reject-missing-body.json']['expect'], 'reject')
                else:
                    self.assertEqual(paths['reject-other-stage.json']['expect'], 'reject')
                    self.assertEqual(paths['reject-no-stage.json']['expect'], 'reject')
                    self.assertEqual(paths['reject-superseded-stage.json']['expect'], 'reject')

    def test_effect_table_names_existing_judges_and_hooks_call_them_directly(self):
        self.assertEqual(routes.EFFECT_JUDGES, {
            'reach_training': {'judge': 'judge_reach_training', 'cases': 'reach_training'},
            'expect_fighters': {'judge': 'judge_expect_fighters', 'cases': 'expect_fighters'},
        })
        for hook, definition in routes.EFFECT_JUDGES.items():
            with self.subTest(hook=hook):
                judge = definition['judge']
                self.assertTrue(callable(getattr(routes, judge, None)))
                tree = ast.parse(inspect.getsource(getattr(routes, hook)))
                self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                                    and node.func.id == judge for node in ast.walk(tree)))

    def test_judges_do_not_reference_runtime_io_or_clock(self):
        forbidden = {'ctx', 'time', 'open', 'Path', 'subprocess', 'socket', 'bridge_v2', 'instance_path'}
        for name in ('judge_reach_training', 'judge_expect_fighters'):
            with self.subTest(judge=name):
                tree = ast.parse(inspect.getsource(getattr(routes, name)))
                names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                self.assertFalse(names & forbidden)

    def test_judges_reject_a_boundary_after_the_greeting(self):
        for hook in ('expect_fighters', 'reach_training'):
            path = next((CASES / hook).glob('accept-*.json'))
            case, args = load_case(path)
            args['lines'].append('[Adapter] reconnect same_boot=1 boot=0x1111111111111111 connects=2 cursor=0')
            with self.subTest(hook=hook), self.assertRaisesRegex(RuntimeError, 'bridge connection changed during the read'):
                getattr(routes, routes.EFFECT_JUDGES[hook]['judge'])(**args)

    def test_judge_rejects_an_other_boot_state_block(self):
        _, args = load_case(CASES / 'expect_fighters' / 'accept-tk06.json')
        args['lines'].append('[Bridge] state begin name=ssbu.ui schema=ssbu.ui/1 rev=1 '
                             'boot=0xa1e80deccd2a9a15 lines=1 filtered=0 frame=1 wall=1')
        with self.assertRaisesRegex(RuntimeError, 'is from boot 0xa1e80deccd2a9a15, not this boot 0x3dec61593ef80877'):
            routes.judge_expect_fighters(**args)

    def test_roster_reply_rejects_records_older_than_its_fence(self):
        _, args = load_case(CASES / 'expect_fighters' / 'accept-tk06.json')
        args['lines'] = [line.replace('frame=5775 ', 'frame=5773 ')
                         if '[Bridge] roster entry=' in line else line for line in args['lines']]
        with self.assertRaisesRegex(RuntimeError, 'no roster entry'):
            routes.judge_expect_fighters(**args)

    def test_fighter_reply_rejects_records_older_than_its_fence(self):
        _, args = load_case(CASES / 'expect_fighters' / 'accept-tk06.json')
        args['lines'] = [line.replace('frame=5789 ', 'frame=5787 ')
                         if '[Bridge] fighter entry=' in line else line for line in args['lines']]
        with self.assertRaisesRegex(RuntimeError, 'no live body'):
            routes.judge_expect_fighters(**args)

    def test_reach_stage_requires_a_recorded_observation(self):
        _, missing = load_case(CASES / 'reach_training' / 'reject-no-stage.json')
        for stage in ('Final Destination', 'Battlefield'):
            with self.subTest(stage=stage), self.assertRaisesRegex(RuntimeError, 'no stage observation'):
                routes.judge_reach_training(**{**missing, 'stage': stage})
        _, witnessed = load_case(CASES / 'reach_training' / 'accept-docs-enter-training.json')
        try:
            result = routes.judge_reach_training(**witnessed)
        except RuntimeError as error:
            self.fail(f'recorded stage must pass: {error}')
        self.assertEqual(result['stage_id'], 3)
        with self.assertRaisesRegex(RuntimeError, 'live Training state does not match the requested setup'):
            routes.judge_reach_training(**{**witnessed, 'stage': 'Battlefield'})

    def test_reach_signature_and_hook_do_not_supply_stage_id(self):
        self.assertNotIn('stage_id', inspect.signature(routes.judge_reach_training).parameters)
        tree = ast.parse(inspect.getsource(routes.reach_training))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'judge_reach_training']
        self.assertTrue(calls)
        self.assertTrue(all(keyword.arg != 'stage_id' for call in calls for keyword in call.keywords))

    def test_live_comparison_does_not_copy_the_live_stage(self):
        tree = ast.parse(inspect.getsource(routes._effect_compare_live))
        self.assertFalse(any(isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                             and node.value.id == 'live' and isinstance(node.slice, ast.Constant)
                             and node.slice.value == 'stage_id' for node in ast.walk(tree)))


class EffectHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log = self.root / 'bridge.log'
        self.recorded = (CASES / 'expect_fighters' / 'accept-tk06.lines').read_text().splitlines()
        self.log.write_text(self.recorded[0] + '\nplugin=ssbu-hw-bridge/0.1.0 result=installed\n')
        self.ctx = SimpleNamespace(
            runner=SimpleNamespace(routes_expect={'p1_kind': 0, 'cpu_kind': 7}), on='s',
            instance={'bridge_log': str(self.log)}, row_dir=str(self.root), check_stop=lambda: None,
        )
        self.io = recorded.Replay(native_blocks=[recorded.l8w_native(1)])
        self.ui = recorded.native(1, screen='training', **{'screen.context': 'training'})

    def invoke(self, cpu='Fox', live_body=7, recorded_body=7, extra=''):
        state = {'roster': {0: {'kind': 0, 'nana': False}, 1: {'kind': 7, 'nana': False}}}
        bodies = {0: {'kind': 0}, 1: {'kind': live_body}}

        def append(lines):
            with self.log.open('a') as stream:
                stream.write('\n'.join(lines) + '\n')

        def roster(*args, **kwargs):
            append(self.recorded[1:6])
            return state

        def fighters(*args, **kwargs):
            lines = [line.replace('fighter entry=1 kind=7 ', f'fighter entry=1 kind={recorded_body} ')
                     for line in self.recorded[6:]]
            append(lines + ([extra] if extra else []))
            return bodies

        with patch.object(routes, 'BridgePickerIO', return_value=self.io), \
             patch.object(native_picker, 'wait_sample', return_value=self.ui), \
             patch.object(routes, 'native_state', side_effect=roster), \
             patch.object(routes, '_switch_hw', return_value=SimpleNamespace(fighters_now=fighters)), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            return routes.expect_fighters(self.ctx, p1='Mario', cpu=cpu)

    def test_hook_and_judge_agree_on_recorded_lines(self):
        result = self.invoke()
        try:
            judged = routes.judge_expect_fighters(self.recorded, p1='Mario', cpu='Fox', boot=BOOT)
        except NotImplementedError:
            self.fail('judge is not implemented')
        self.assertEqual(result['verified'], judged)
        self.assertEqual(self.io.actions, [])

    def test_hook_preserves_the_existing_fighter_mismatch_message(self):
        with self.assertRaisesRegex(RuntimeError, 'entry 1 wanted Greninja kind 53: roster kind 7, body kind 7'):
            self.invoke(cpu='Greninja')

    def test_hook_refuses_disagreement_between_live_read_and_judged_lines(self):
        with self.assertRaisesRegex(RuntimeError, 'expect_fighters: the judged lines and the live read disagree'):
            self.invoke(live_body=3)

    def test_hook_refuses_disagreement_when_the_lines_have_the_wrong_fighter(self):
        with self.assertRaisesRegex(RuntimeError, 'expect_fighters: the judged lines and the live read disagree'):
            self.invoke(recorded_body=3)

    def test_hook_refuses_a_connection_change_during_the_reads(self):
        boundary = f'[Adapter] reconnect same_boot=1 boot={BOOT} connects=2 cursor=0'
        with self.assertRaisesRegex(RuntimeError, 'bridge connection changed during the read'):
            self.invoke(extra=boundary)

    def test_reach_hook_and_judge_agree_on_recorded_roster_and_live_stage(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        state = {'stage': 3, 'roster': {0: {'kind': 0, 'nana': False}, 1: {'kind': 7, 'nana': False}}}
        stage_line = (CASES / 'reach_training' / 'other-run-tk06.lines').read_text().splitlines()[1]
        with self.log.open('a') as stream:
            stream.write(stage_line + '\n')

        def roster(*args, **kwargs):
            with self.log.open('a') as stream:
                stream.write('\n'.join(self.recorded[1:6]) + '\n')
            return state

        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', side_effect=roster), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            result = routes.reach_training(self.ctx, 'Mario', cpu='Fox', stage='Final Destination')
        try:
            judged = routes.judge_reach_training([self.recorded[0], stage_line, *self.recorded[1:]],
                                                p1='Mario', cpu='Fox', stage='Final Destination', boot=BOOT)
        except RuntimeError as error:
            self.fail(f'recorded stage must pass: {error}')
        self.assertEqual(result['verified'], judged)
        self.assertEqual(result['verified']['stage_id'], 3)
        self.assertEqual(replay.actions, [])

    def test_reach_hook_refuses_disagreement_with_the_recorded_roster(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        state = {'stage': 3, 'roster': {0: {'kind': 0, 'nana': True}, 1: {'kind': 7, 'nana': False}}}
        stage_line = (CASES / 'reach_training' / 'other-run-tk06.lines').read_text().splitlines()[1]
        with self.log.open('a') as stream:
            stream.write(stage_line + '\n')

        def roster(*args, **kwargs):
            with self.log.open('a') as stream:
                stream.write('\n'.join(self.recorded[1:6]) + '\n')
            return state

        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', side_effect=roster), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            with self.assertRaisesRegex(RuntimeError, 'reach_training: the judged lines and the live read disagree'):
                routes.reach_training(self.ctx, 'Mario', cpu='Fox', stage='Final Destination')

    def test_reach_hook_refuses_stage_only_before_latest_greeting(self):
        replay = MenuReplay()
        replay.menu['open'] = '0'
        state = {'stage': 3, 'roster': {0: {'kind': 0, 'nana': False}, 1: {'kind': 7, 'nana': False}}}
        stage_line = (CASES / 'reach_training' / 'other-run-tk06.lines').read_text().splitlines()[1]
        self.log.write_text(stage_line + '\n' + self.recorded[0] +
                            '\nplugin=ssbu-hw-bridge/0.1.0 result=installed\n')

        def roster(*args, **kwargs):
            with self.log.open('a') as stream:
                stream.write('\n'.join(self.recorded[1:6]) + '\n')
            return state

        with patch.object(routes, 'BridgePickerIO', return_value=replay), \
             patch.object(routes, 'native_state', side_effect=roster), \
             patch.object(routes, '_save', side_effect=lambda ctx, name, value: value):
            with self.assertRaisesRegex(RuntimeError, 'no stage observation in this bridge connection'):
                routes.reach_training(self.ctx, 'Mario', cpu='Fox', stage='Final Destination')


if __name__ == '__main__':
    unittest.main()
