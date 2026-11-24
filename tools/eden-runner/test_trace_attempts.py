"""Host probes of release-marker decoding and bounded trace observation."""

import importlib
import json
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import routes
import test_setup_blocks as fake


# Source: /home/intpa/smash-training-lobby/artifacts/phase23/edge-juggle/evidence/cp1b-pyra38-1/trace/edge-juggle-p0-83eecc32.trace.jsonl
# SHA-256 bb83adcca818425248239fbe6fe575d0c382cb50fcb9be9b57cb5ff1e573a9df
# Source: /home/intpa/smash-training-lobby/artifacts/phase23/edge-juggle/evidence/cp1b-hero32-3/trace/edge-juggle-p0-83eecc32.trace.jsonl
# SHA-256 a1966f89197ed537e24864e9502f2b9399b4ba186a15b4118efc24f24a6a3103
# Level 9 release 312411 through terminal 312685 has no point-5 planner entry in that source.
DATA = Path(__file__).parent / 'test-data'


def excerpt(name):
    return [json.loads(line) for line in (DATA / name).read_text().splitlines()]


def contiguous(rows):
    ordered = {int(row['sequence']): row for row in rows}
    return ''.join(json.dumps(ordered.get(seq, {'sequence': str(seq), 'point_id': 'other', 'event': 'entry'}))
                   + '\n' for seq in range(min(ordered), max(ordered) + 1))


class AttemptDecodeTests(unittest.TestCase):
    def decoder(self):
        return importlib.import_module('trace_attempts')

    def test_pyra_reviewed_release_plan_and_terminal(self):
        release, plan, terminal = excerpt('cp1b-pyra-attempt.jsonl')
        decode = self.decoder()
        first, end = decode.attempt(release), decode.attempt(terminal)
        self.assertEqual((first['sequence'], first['cpu_kind'], first['stage_form']), (44362, 0x5a, 0))
        self.assertEqual([round(n, 2) for n in first['cpu']], [-183.99, -50.0])
        self.assertEqual([round(n, 2) for n in first['p1']], [-81.70, -10.70])
        self.assertEqual(first['cpu_words'], ['0xc337fc50', '0xc247ffbc'])
        self.assertEqual(first['damage'], 100.0)
        self.assertEqual((decode.plan(plan)['route'], decode.plan(plan)['row']), (0, 38))
        self.assertEqual((end['sequence'], end['terminal']), (44870, 'ledge'))
        self.assertEqual(end['cpu_kind'], 0x5b)

    def test_hero_reviewed_first_release(self):
        release, terminal = excerpt('cp1b-hero-attempt.jsonl')
        decode = self.decoder()
        first, end = decode.attempt(release), decode.attempt(terminal)
        self.assertEqual((first['sequence'], first['cpu_kind'], first['stage_form']), (57904, 0x53, 0))
        self.assertEqual([round(n, 2) for n in first['cpu']], [-119.99, 20.0])
        self.assertEqual((end['sequence'], end['terminal']), (58100, 'ledge'))

    def test_bad_marker_fails_closed(self):
        record = excerpt('cp1b-pyra-attempt.jsonl')[0]
        record['registers'].pop('x2')
        with self.assertRaisesRegex(RuntimeError, 'marker'):
            self.decoder().attempt(record)

    def test_point_binding_uses_configured_symbol(self):
        decode = self.decoder()
        points = {'points': [{'id': '19', 'state': 'armed', 'mode': 'records',
                              'target': 'libtraining_rooms.nro!edge_juggle_attempt'}]}
        self.assertEqual(decode.point_id(points, 'edge_juggle_attempt'), '19')
        points['points'][0]['target'] = 'nro:build+0x1234'
        with self.assertRaisesRegex(RuntimeError, 'point'):
            decode.point_id(points, 'edge_juggle_attempt')

    def test_partial_line_appended_between_stat_and_sample_is_skipped(self):
        decode = self.decoder()
        first, second = excerpt('cp1b-hero-attempt.jsonl')
        first_line = (json.dumps(first) + '\n').encode()
        second_line = (json.dumps(second) + '\n').encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.jsonl'
            path.write_bytes(first_line[:130])
            open_file = Path.open
            appended = False

            def open_after_append(target, *args, **kwargs):
                nonlocal appended
                if target == path and not appended:
                    appended = True
                    with open_file(target, 'ab') as output:
                        output.write(first_line[130:] + second_line)
                return open_file(target, *args, **kwargs)

            with patch.object(Path, 'open', new=open_after_append):
                stream = decode.Stream(path, '8')
            marks = stream.read()
            self.assertEqual([mark['sequence'] for mark in marks], [int(second['sequence'])])


class ObserveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'trace.jsonl'
        self.path.write_text('')
        self.ctx = SimpleNamespace(runner=SimpleNamespace(), row_dir=directory.name, on='s',
                                   instance={'trace_jsonl': str(self.path)})
        self.pending = []

        def counters(point):
            rows = []
            for line in self.path.read_text().splitlines():
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and str(row.get('sequence', '')).isdigit():
                        rows.append(row)
                except ValueError:
                    pass
            recorded = sum(str(row.get('point_id')) == point for row in rows)
            return {'hits': str(recorded), 'matched': str(recorded), 'recorded': str(recorded), 'dropped': '0'}

        self.counters = counters

        def call(method, params):
            rows = []
            for line in self.path.read_text().splitlines():
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and str(row.get('sequence', '')).isdigit():
                        rows.append(row)
                except ValueError:
                    pass
            maximum = max((int(row['sequence']) for row in rows), default=0)
            own = int(counters('8')['recorded'])
            other = max(0, maximum - own)
            return {
                'points': [{'id': '8', 'state': 'armed', 'mode': 'records',
                            'target': 'libtraining_rooms.nro!edge_juggle_attempt',
                            'process_id': '81', 'address': '0x1234', **counters('8')},
                           {'id': '99', 'state': 'armed', 'mode': 'records',
                            'target': 'other!point', 'hits': str(other), 'matched': str(other),
                            'recorded': str(other), 'dropped': '0'}],
                'output': str(self.path), 'output_error': '', 'boot_error': '', 'file_lost': '0'}

        self.ctx.call = call

        def check_stop():
            if self.pending:
                with self.path.open('a') as stream:
                    stream.write(self.pending.pop(0))

        self.ctx.check_stop = check_stop
        self.io = fake.MenuIO(self.ctx)
        self.io.menu.update({'game': 'Guard the Edge', 'game.running': '1'})

    def invoke(self, **kwargs):
        with patch.object(routes, 'BridgePickerIO', return_value=self.io):
            result = routes.drill_observe(self.ctx, **kwargs)
        json.dumps(result)
        return result

    def test_counts_new_terminal_and_saves_receipt(self):
        release, terminal = excerpt('cp1b-hero-attempt.jsonl')
        rows = [release] + [{'event': 'entry', 'point_id': 'other', 'sequence': str(seq)}
                            for seq in range(57905, 58100)] + [terminal]
        self.pending = [''.join(json.dumps(row) + '\n' for row in rows)]
        result = self.invoke(attempts=1, cap=.5)
        self.assertEqual(result['verified']['releases'], 1)
        self.assertEqual(result['verified']['terminals'], {'ledge': 1})
        self.assertEqual(result['verified']['first_sequence'], 57904)
        self.assertEqual(result['verified']['last_sequence'], 58100)
        self.assertEqual(result['inputs'], 0)
        self.assertTrue((Path(self.ctx.row_dir) / 'routes-drill-observe.json').is_file())

    def test_delayed_pre_observation_terminal_is_not_counted(self):
        terminal = dict(excerpt('cp1b-hero-attempt.jsonl')[1], sequence='1')
        self.pending = [json.dumps(terminal) + '\n']
        original = self.ctx.call

        def recorded_before_file(method, params):
            result = original(method, params)
            for key in ('hits', 'matched', 'recorded'):
                result['points'][0][key] = '1'
            return result

        self.ctx.call = recorded_before_file
        with self.assertRaisesRegex(RuntimeError, r'0 terminals observed.*wanted 1'):
            self.invoke(attempts=1, cap=.05)
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-drill-observe.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)
        self.assertEqual(receipt['observed']['markers'], [])
        self.assertEqual(receipt['observed']['markers_before_observation'][0]['sequence'], 1)

    def test_observe_excludes_stale_wrong_arm_then_counts_new_marker(self):
        stale = dict(excerpt('cp1b-hero-attempt.jsonl')[1], sequence='1')
        stale['registers'] = dict(stale['registers'])
        stale['registers']['x0'] = hex(int(stale['registers']['x0'], 16) | (1 << 24))
        current = dict(excerpt('cp1b-hero-attempt.jsonl')[1], sequence='2')
        self.pending = [contiguous([stale, current])]
        original = self.ctx.call

        def recorded_before_file(method, params):
            result = original(method, params)
            if not self.path.stat().st_size:
                for key in ('hits', 'matched', 'recorded'):
                    result['points'][0][key] = '1'
            return result

        self.ctx.call = recorded_before_file
        result = self.invoke(attempts=1, cap=.5)
        self.assertEqual(result['verified']['fence'],
                         {'sequence': 1, 'taken': 'at the start of the observation'})
        self.assertEqual(result['verified']['markers_before_observation'][0]['sequence'], 1)
        self.assertEqual(result['verified']['first_sequence'], 2)

    def test_observe_counter_sum_must_cover_file_sequence(self):
        terminal = dict(excerpt('cp1b-hero-attempt.jsonl')[1], sequence='1')
        self.pending = [json.dumps(terminal) + '\n']
        original = self.ctx.call

        def missing_counter(method, params):
            result = original(method, params)
            if self.path.stat().st_size:
                for key in ('hits', 'matched', 'recorded'):
                    result['points'][0][key] = '0'
            return result

        self.ctx.call = missing_counter
        with self.assertRaisesRegex(RuntimeError, 'trace counters do not account for file sequences'):
            self.invoke(attempts=1, cap=.5)

    def test_expiry_reports_count(self):
        with self.assertRaisesRegex(RuntimeError, r'0.*terminal'):
            self.invoke(attempts=1, cap=.02)

    def test_loss_and_unreadable_marker_fail(self):
        for line in ('{"event":"loss","records":"1"}\n', '{"event":"entry","point_id":"8"}\n',
                     '{"event":"unknown","point_id":"8","sequence":"2"}\n'):
            with self.subTest(line=line):
                self.pending = [line]
                with self.assertRaisesRegex(RuntimeError, 'loss|marker'):
                    self.invoke(attempts=1, cap=.2)

    def test_stopped_game_fails_unless_allowed(self):
        terminal = excerpt('cp1b-hero-attempt.jsonl')[1]
        self.io.menu['game.running'] = '0'
        self.pending = [json.dumps(terminal) + '\n']
        with self.assertRaisesRegex(RuntimeError, 'stopped'):
            self.invoke(attempts=1, cap=.5)
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-drill-observe.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)
        self.assertEqual(receipt['observed']['running'], '0')
        terminal = dict(terminal, sequence='58101')
        self.pending = [json.dumps(terminal) + '\n']
        result = self.invoke(attempts=1, cap=.5, allow_stopped=True)
        self.assertEqual(result['verified']['running'], '0')

    def test_changed_game_and_juggler_terminal_are_not_guard_observation(self):
        terminal = excerpt('cp1b-hero-attempt.jsonl')[1]
        terminal['registers']['x0'] = hex(int(terminal['registers']['x0'], 16) | (1 << 24))
        self.pending = [json.dumps(terminal) + '\n']
        read = self.io.read
        count = 0

        def changed(provider, cap):
            nonlocal count
            count += 1
            if count > 1:
                self.io.menu['game'] = 'Juggler'
            return read(provider, cap)

        self.io.read = changed
        with self.assertRaisesRegex(RuntimeError, r'expected.*Guard|sequence.*expected'):
            self.invoke(attempts=1, cap=.5)
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-drill-observe.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)

    def test_filtered_attempt_hit_is_not_complete_observation(self):
        self.pending = [json.dumps(excerpt('cp1b-hero-attempt.jsonl')[1]) + '\n']
        original = self.ctx.call

        def call(method, params):
            result = original(method, params)
            if self.path.stat().st_size:
                result['points'][0]['hits'] = str(int(result['points'][0]['hits']) + 1)
            return result

        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, 'expected every attempt hit recorded'):
            self.invoke(attempts=1, cap=.5)

    def test_changed_perfect_bit_names_the_marker(self):
        release, terminal = excerpt('cp1b-hero-attempt.jsonl')
        terminal['registers']['x0'] = hex(int(terminal['registers']['x0'], 16) ^ (1 << 25))
        self.pending = [contiguous([release, terminal])]
        with self.assertRaisesRegex(RuntimeError, r'marker sequence 58100: expected perfect=.*saw'):
            self.invoke(attempts=1, cap=.5)

    def test_cpu_kinds_can_change_within_one_observation(self):
        release, _, terminal = excerpt('cp1b-pyra-attempt.jsonl')
        self.pending = [contiguous([release, terminal])]
        result = self.invoke(attempts=1, cap=.5, perfect=True)
        self.assertEqual(result['verified']['cpu_kinds'], [0x5a, 0x5b])

    def test_epoch_change_fails(self):
        terminal = excerpt('cp1b-hero-attempt.jsonl')[1]
        self.pending = [json.dumps(terminal) + '\n']
        original = self.io.read
        count = 0

        def read(provider, cap):
            nonlocal count
            count += 1
            if count > 1:
                self.io.actions.append('external')
                self.io.fault = 'epoch'
            return original(provider, cap)

        self.io.read = read
        with self.assertRaisesRegex(RuntimeError, 'connection'):
            self.invoke(attempts=1, cap=.5)

    def test_missing_sequence_and_dropped_records_fail(self):
        first, last = excerpt('cp1b-hero-attempt.jsonl')
        self.pending = [json.dumps(first) + '\n' + json.dumps(last) + '\n']
        with self.assertRaisesRegex(RuntimeError, 'sequence'):
            self.invoke(attempts=1, cap=.5)

    def test_no_trace_fails(self):
        del self.ctx.instance['trace_jsonl']
        with self.assertRaisesRegex(RuntimeError, 'trace'):
            self.invoke(attempts=1, cap=.1)

    def test_trace_drop_after_baseline_fails(self):
        terminal = excerpt('cp1b-hero-attempt.jsonl')[1]
        self.pending = [json.dumps(terminal) + '\n']
        original = self.ctx.call
        calls = 0

        def call(method, params):
            nonlocal calls
            calls += 1
            result = original(method, params)
            if calls > 1:
                result['points'][0]['dropped'] = '1'
            return result

        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, 'loss'):
            self.invoke(attempts=1, cap=.5)

    def test_trace_replacement_fails_closed(self):
        def replace():
            replacement = self.path.with_name('replacement.jsonl')
            replacement.write_text('')
            replacement.replace(self.path)

        self.ctx.check_stop = replace
        with self.assertRaisesRegex(RuntimeError, 'continuity'):
            self.invoke(attempts=1, cap=.2)

    def test_point_is_not_a_counter_or_duplicate(self):
        decode = importlib.import_module('trace_attempts')
        point = {'id': '19', 'state': 'armed', 'mode': 'counters',
                 'target': 'libtraining_rooms.nro!edge_juggle_attempt'}
        with self.assertRaisesRegex(RuntimeError, 'point'):
            decode.point_id({'points': [point]}, 'edge_juggle_attempt')
        point['mode'] = 'records'
        with self.assertRaisesRegex(RuntimeError, 'point'):
            decode.point_id({'points': [point, dict(point, id='20')]}, 'edge_juggle_attempt')

    def test_counter_fields_are_mandatory(self):
        original = self.ctx.call

        def call(method, params):
            result = original(method, params)
            del result['points'][0]['dropped']
            return result

        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, 'counter'):
            self.invoke(attempts=1, cap=.2)

    def test_slow_final_trace_check_cannot_pass_after_cap(self):
        terminal = excerpt('cp1b-hero-attempt.jsonl')[1]
        self.pending = [json.dumps(terminal) + '\n']
        original = self.ctx.call
        calls = 0

        def call(method, params):
            nonlocal calls
            calls += 1
            if calls == 3:
                time.sleep(.03)
            return original(method, params)

        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            self.invoke(attempts=1, cap=.02)

    def test_terminal_recorded_during_first_snapshot_is_before_observation(self):
        terminal = excerpt('cp1b-hero-attempt.jsonl')[1]
        original = self.ctx.call
        calls = 0

        def call(method, params):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.path.write_text(json.dumps(terminal) + '\n')
            return original(method, params)

        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, r'0 terminals observed.*wanted 1'):
            self.invoke(attempts=1, cap=.05)

    def test_nonzero_initial_counters_are_recorded_and_allowed(self):
        release = excerpt('cp1b-hero-attempt.jsonl')[0]
        self.pending = [json.dumps(release) + '\n']
        original = self.ctx.call

        def call(method, params):
            result = original(method, params)
            result['points'][0]['dropped'] = '3'
            result['file_lost'] = '5'
            return result

        self.ctx.call = call
        result = self.invoke(attempts=1, cap=.5, count='releases')
        self.assertEqual(result['verified']['releases'], 1)
        self.assertEqual(result['verified']['terminals'], {})
        self.assertEqual(result['verified']['trace_loss_start'], {'file_lost': 5, 'point_dropped': 3})
        self.assertEqual(result['verified']['trace_loss_end'], {'file_lost': 5, 'point_dropped': 3})

    def test_nonzero_baseline_with_increased_file_loss_fails(self):
        release = excerpt('cp1b-hero-attempt.jsonl')[0]
        self.pending = [json.dumps(release) + '\n']
        original = self.ctx.call
        calls = 0

        def call(method, params):
            nonlocal calls
            calls += 1
            result = original(method, params)
            result['file_lost'] = '9' if calls >= 3 else '8'
            return result

        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, 'loss'):
            self.invoke(attempts=1, cap=.5, count='releases')

    def test_counter_reset_is_not_a_valid_delta_fence(self):
        release = excerpt('cp1b-hero-attempt.jsonl')[0]
        self.pending = [json.dumps(release) + '\n']
        original = self.ctx.call
        calls = 0

        def call(method, params):
            nonlocal calls
            calls += 1
            result = original(method, params)
            result['points'][0]['dropped'] = '0' if calls >= 3 else '2'
            return result

        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, 'loss'):
            self.invoke(attempts=1, cap=.5, count='releases')

    def test_releases_expiry_and_unknown_count_fail(self):
        with self.assertRaisesRegex(RuntimeError, '0.*release'):
            self.invoke(attempts=1, cap=.02, count='releases')
        with self.assertRaisesRegex(RuntimeError, 'count'):
            self.invoke(attempts=1, cap=.1, count='unknown')


class StartIO(fake.RowsIO):
    def __init__(self, ctx, *, wizard=True, fault=None):
        super().__init__(ctx, fault=fault)
        self.wizard = wizard
        self.menu.update({'open': '0' if wizard else '1', 'game': 'Guard the Edge' if wizard else '',
                          'game.running': '1' if wizard else '0',
                          'notice.header': 'Edge · 4/4' if wizard else '',
                          'notice.body': 'CPU (1, 1) · 0% · P1 (0, 0)' if wizard else ''})

    def send(self, segment, until=None):
        if segment == 'tap-a1' and (self.wizard or self.menu['depth'] == '1'):
            self.actions.append(segment)
            if self.ctx.runner.setup_metrics is not None:
                self.ctx.runner.setup_metrics['inputs'] += 1
            if self.wizard:
                if self.fault != 'wizard_stuck':
                    self.menu['notice.header'] = ''
            else:
                if self.fault != 'no_ack':
                    self.menu['ack'] = str(int(self.menu['ack']) + 1)
                if self.fault != 'start_stuck':
                    self.menu.update({'open': '0', 'game': 'Guard the Edge', 'game.running': '1'})
        else:
            super().send(segment, until=until)


class StartTests(ObserveTests):
    def invoke_start(self, *, expect=None, cap=.5):
        with patch.object(routes, 'BridgePickerIO', return_value=self.io):
            result = routes.drill_start(self.ctx, game='Guard the Edge', expect=expect, cap=cap)
        json.dumps(result)
        return result

    def enable_planner(self):
        original = self.ctx.call

        def call(method, params):
            result = original(method, params)
            other = next(point for point in result['points'] if point['id'] == '99')
            for key in ('hits', 'matched', 'recorded'):
                other[key] = str(int(other[key]) - int(self.counters('5')['recorded']))
            result['points'].append({'id': '5', 'state': 'armed', 'mode': 'records',
                                     'target': 'libtraining_rooms.nro!planner_chose',
                                     'process_id': '81', 'address': '0x5678', **self.counters('5')})
            return result

        self.ctx.call = call

    def test_wizard_dismissed_into_wrong_stopped_game_is_failure(self):
        del self.ctx.instance['trace_jsonl']
        self.io = StartIO(self.ctx)
        send = self.io.send

        def wrong_game(segment, until=None):
            send(segment, until=until)
            self.io.menu.update({'game': 'Eggs-ercise', 'game.running': '0'})

        self.io.send = wrong_game
        with self.assertRaisesRegex(RuntimeError, r'expected Guard the Edge running after start; saw game=.*Eggs-ercise.*running=.*0'):
            self.invoke_start(cap=.05)
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-drill-start.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)

    def test_menu_start_into_wrong_stopped_game_is_failure(self):
        del self.ctx.instance['trace_jsonl']
        self.io = StartIO(self.ctx, wizard=False)
        send = self.io.send

        def wrong_game(segment, until=None):
            send(segment, until=until)
            if segment == 'tap-a1' and self.io.menu['open'] == '0':
                self.io.menu.update({'game': 'Eggs-ercise', 'game.running': '0'})

        self.io.send = wrong_game
        with self.assertRaisesRegex(RuntimeError, r'expected Guard the Edge running after start; saw game=.*Eggs-ercise.*running=.*0'):
            self.invoke_start(cap=.05)

    def test_start_rechecks_game_after_trace_before_receipt(self):
        self.io = StartIO(self.ctx)
        release = excerpt('cp1b-hero-attempt.jsonl')[0]
        self.pending = [json.dumps(release) + '\n']
        check_stop = self.ctx.check_stop

        def changed_after_start():
            check_stop()
            self.io.menu.update({'game': 'Eggs-ercise', 'game.running': '0'})

        self.ctx.check_stop = changed_after_start
        with self.assertRaisesRegex(RuntimeError, r'expected Guard the Edge running after start; saw game=.*Eggs-ercise.*running=.*0'):
            self.invoke_start(expect={'cpu_kind': 0x53})
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-drill-start.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)

    def test_pre_input_pyra_records_cannot_certify_start(self):
        self.io = StartIO(self.ctx)
        self.enable_planner()
        original = self.ctx.call
        injected = False

        def call(method, params):
            nonlocal injected
            if not injected:
                injected = True
                self.path.write_text(contiguous(excerpt('cp1b-pyra-attempt.jsonl')))
            return original(method, params)

        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, 'first release|deadline'):
            self.invoke_start(expect={'plan': [0, 38]}, cap=.2)

    def test_pre_input_markers_are_reported_but_not_selected(self):
        self.io = StartIO(self.ctx)
        old_release, plan, terminal = excerpt('cp1b-pyra-attempt.jsonl')
        later = dict(old_release, sequence='44871')
        self.pending = [json.dumps(later) + '\n']
        original = self.ctx.call
        injected = False

        def call(method, params):
            nonlocal injected
            if not injected:
                injected = True
                self.path.write_text(contiguous([old_release, plan, terminal]))
            return original(method, params)

        self.ctx.call = call
        result = self.invoke_start(expect={'cpu_kind': 0x5a})
        self.assertEqual(result['verified']['fence'], {'sequence': 44870,
                         'taken': 'immediately before the start input', 'state': 'wizard Preview'})
        self.assertEqual(result['verified']['first_release']['sequence'], 44871)
        self.assertEqual([mark['sequence'] for mark in result['verified']['markers_before_start']],
                         [44362, 44870])

    def test_start_counter_sum_must_cover_all_file_sequences(self):
        self.io = StartIO(self.ctx)
        release = excerpt('cp1b-pyra-attempt.jsonl')[0]
        self.pending = [json.dumps(release) + '\n']
        original = self.ctx.call

        def call(method, params):
            result = original(method, params)
            if self.path.stat().st_size:
                result['points'][1]['recorded'] = '0'
            return result

        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, 'trace counters do not account for file sequences'):
            self.invoke_start(expect={'cpu_kind': 0x5a})

    def test_start_damage_is_exact_and_tolerance_expect_is_rejected(self):
        self.io = StartIO(self.ctx)
        release = excerpt('cp1b-hero-attempt.jsonl')[0]
        damage = struct.unpack('<I', struct.pack('<f', .04))[0]
        release['registers']['x1'] = hex((int(release['registers']['x1'], 16) & ~0xffffffff) | damage)
        self.pending = [json.dumps(release) + '\n']
        with self.assertRaisesRegex(RuntimeError, 'first-release mismatch for damage'):
            self.invoke_start(expect={'damage': 0})
        with self.assertRaisesRegex(RuntimeError, 'invalid first-release expect keys'):
            self.invoke_start(expect={'tolerance': 1})

    def test_menu_start_fence_records_not_running_precondition(self):
        self.io = StartIO(self.ctx, wizard=False)
        release = excerpt('cp1b-hero-attempt.jsonl')[0]
        self.pending = [json.dumps(release) + '\n']
        result = self.invoke_start(expect={'cpu_kind': 0x53})
        self.assertEqual(result['verified']['fence'], {'sequence': 0,
                         'taken': 'immediately before the start input', 'state': 'not running'})

    def test_terminal_must_close_same_release_ordinal(self):
        self.io = StartIO(self.ctx)
        self.enable_planner()
        release, _, terminal = excerpt('cp1b-pyra-attempt.jsonl')
        terminal['registers']['x0'] = hex((int(terminal['registers']['x0'], 16) & ~0xffff) | 2)
        self.pending = [contiguous([release, terminal])]
        with self.assertRaisesRegex(RuntimeError, 'expected.*ordinal.*1.*saw.*2'):
            self.invoke_start(expect={'plan': 'none'})

    def test_terminal_must_match_release_game_and_perfect_arm(self):
        self.io = StartIO(self.ctx)
        self.enable_planner()
        release, plan, terminal = excerpt('cp1b-pyra-attempt.jsonl')
        terminal['registers']['x0'] = hex(int(terminal['registers']['x0'], 16) ^ (1 << 24) ^ (1 << 25))
        self.pending = [contiguous([release, plan, terminal])]
        with self.assertRaisesRegex(RuntimeError, r'terminal.*expected.*juggler.*saw'):
            self.invoke_start(expect={'juggler': False, 'perfect': True, 'plan': [0, 38]})
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-drill-start.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)

    def test_terminal_must_match_release_perfect_bit(self):
        self.io = StartIO(self.ctx)
        self.enable_planner()
        release, plan, terminal = excerpt('cp1b-pyra-attempt.jsonl')
        terminal['registers']['x0'] = hex(int(terminal['registers']['x0'], 16) ^ (1 << 25))
        self.pending = [contiguous([release, plan, terminal])]
        with self.assertRaisesRegex(RuntimeError, r'terminal.*expected.*perfect.*saw'):
            self.invoke_start(expect={'plan': [0, 38]})

    def test_first_release_must_match_requested_game(self):
        self.io = StartIO(self.ctx)
        release = excerpt('cp1b-hero-attempt.jsonl')[0]
        release['registers']['x0'] = hex(int(release['registers']['x0'], 16) | (1 << 24))
        self.pending = [json.dumps(release) + '\n']
        with self.assertRaisesRegex(RuntimeError, r'release.*sequence.*expected.*juggler.*False.*saw.*True'):
            self.invoke_start(expect={'cpu_kind': 0x53})

    def test_filtered_planner_hit_cannot_certify_none(self):
        self.io = StartIO(self.ctx)
        self.enable_planner()
        release, terminal = excerpt('cp1b-hero-level9-none.jsonl')
        self.pending = [contiguous([release, terminal])]
        original = self.ctx.call

        def call(method, params):
            result = original(method, params)
            if self.path.stat().st_size:
                point = next(point for point in result['points'] if point['id'] == '5')
                point['hits'] = '1'
            return result

        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, r'not observed \(planner hit 1 times, recorded 0\)'):
            self.invoke_start(expect={'plan': 'none'})

    def test_start_accepts_three_page_preview_by_body(self):
        del self.ctx.instance['trace_jsonl']
        self.io = StartIO(self.ctx)
        self.io.menu['notice.header'] = 'Edge · 3/3'
        result = self.invoke_start()
        self.assertTrue(result['ok'])

    def test_start_refuses_wrong_game_preview_without_input(self):
        del self.ctx.instance['trace_jsonl']
        self.io = StartIO(self.ctx)
        self.io.menu['game'] = 'Eggs-ercise'
        with self.assertRaisesRegex(RuntimeError, 'expected Guard the Edge'):
            self.invoke_start()
        self.assertEqual(self.io.actions, [])
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-drill-start.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)

    def test_wizard_bridge_only_without_trace(self):
        del self.ctx.instance['trace_jsonl']
        self.io = StartIO(self.ctx)
        result = self.invoke_start()
        self.assertEqual(self.io.actions, ['tap-a1'])
        self.assertEqual(result['verified']['first_release'], 'not checked: no trace in this boot')
        self.assertEqual(result['verified']['markers_before_release'], [])

    def test_records_prior_terminal_markers_before_first_release(self):
        self.io = StartIO(self.ctx)
        release, _, terminal = excerpt('cp1b-pyra-attempt.jsonl')
        first = dict(terminal, sequence='44360', frame='33040')
        second = dict(terminal, sequence='44361', frame='33041')
        second['registers'] = dict(terminal['registers'])
        meta = int(second['registers']['x0'], 16)
        second['registers']['x0'] = hex((meta & ~(255 << 16)) | (3 << 16))
        self.pending = [json.dumps(first) + '\n', contiguous([second, release])]
        result = self.invoke_start(expect={'cpu_kind': 0x5a})
        self.assertEqual(result['verified']['markers_before_release'], [
            {'sequence': 44360, 'frame': 33040, 'event': 2, 'terminal': 'ledge'},
            {'sequence': 44361, 'frame': 33041, 'event': 3, 'terminal': 'ground'},
        ])
        self.assertEqual(result['verified']['first_release']['sequence'], 44362)
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-drill-start.json').read_text())
        self.assertEqual(receipt['verified']['markers_before_release'], result['verified']['markers_before_release'])

    def test_prior_terminal_is_saved_even_when_release_expect_mismatches(self):
        self.io = StartIO(self.ctx)
        release, _, terminal = excerpt('cp1b-pyra-attempt.jsonl')
        earlier = dict(terminal, sequence='44361', frame='33041')
        self.pending = [contiguous([earlier, release])]
        with self.assertRaisesRegex(RuntimeError, 'mismatch'):
            self.invoke_start(expect={'cpu_kind': 0})
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-drill-start.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)
        self.assertEqual(receipt['observed']['markers_before_release'],
                         [{'sequence': 44361, 'frame': 33041, 'event': 2, 'terminal': 'ledge'}])

    def test_expect_without_trace_and_missing_planner_refuse_before_input(self):
        self.io = StartIO(self.ctx)
        del self.ctx.instance['trace_jsonl']
        with self.assertRaisesRegex(RuntimeError, 'trace'):
            self.invoke_start(expect={'cpu_kind': 0x5a})
        self.assertEqual(self.io.actions, [])
        self.ctx.instance['trace_jsonl'] = str(self.path)
        with self.assertRaisesRegex(RuntimeError, 'planner'):
            self.invoke_start(expect={'plan': [0, 38]})
        self.assertEqual(self.io.actions, [])

    def test_pyra_release_and_plan_wait_for_ledge_terminal(self):
        self.io = StartIO(self.ctx)
        self.enable_planner()
        release, plan, terminal = excerpt('cp1b-pyra-attempt.jsonl')
        self.pending = [contiguous([release, plan, terminal])]
        result = self.invoke_start(expect={'cpu_kind': 0x5a, 'stage_form': 0, 'perfect': True,
                                           'cpu': [-184, -50], 'plan': [0, 38]})
        self.assertEqual(result['verified']['first_release']['sequence'], 44362)
        self.assertEqual(result['verified']['plan']['sequence'], 44368)
        self.assertEqual(result['verified']['plan_terminal']['sequence'], 44870)
        self.assertEqual(result['verified']['planner'], {'hits': 1, 'matched': 1, 'recorded': 1, 'dropped': 0})

    def test_level9_none_plan_uses_reviewed_attempt(self):
        self.io = StartIO(self.ctx)
        self.enable_planner()
        release, terminal = excerpt('cp1b-hero-level9-none.jsonl')
        self.pending = [contiguous([release, terminal])]
        result = self.invoke_start(expect={'perfect': False, 'plan': 'none'})
        self.assertEqual(result['verified']['first_release']['sequence'], 312411)
        self.assertEqual(result['verified']['plan'], 'none')
        self.assertEqual(result['verified']['plan_terminal']['sequence'], 312685)

    def test_any_plan_reports_recorded_route_and_row(self):
        self.io = StartIO(self.ctx)
        self.enable_planner()
        release, plan, terminal = excerpt('cp1b-pyra-attempt.jsonl')
        self.pending = [contiguous([release, plan, terminal])]
        result = self.invoke_start(expect={'plan': 'any'})
        self.assertEqual((result['verified']['plan']['route'], result['verified']['plan']['row']), (0, 38))
        self.assertEqual(result['verified']['plan_terminal']['sequence'], 44870)
        self.assertEqual(result['verified']['planner']['recorded'], 1)

    def test_any_plan_reports_none_when_there_are_no_planner_hits(self):
        self.io = StartIO(self.ctx)
        self.enable_planner()
        release, terminal = excerpt('cp1b-hero-level9-none.jsonl')
        self.pending = [contiguous([release, terminal])]
        result = self.invoke_start(expect={'plan': 'any'})
        self.assertEqual(result['verified']['plan'], 'none')
        self.assertEqual(result['verified']['planner']['hits'], 0)

    def test_any_plan_reports_filtered_planner_hit_as_not_observed(self):
        self.io = StartIO(self.ctx)
        self.enable_planner()
        release, terminal = excerpt('cp1b-hero-level9-none.jsonl')
        self.pending = [contiguous([release, terminal])]
        original = self.ctx.call

        def call(method, params):
            result = original(method, params)
            if self.path.stat().st_size:
                next(point for point in result['points'] if point['id'] == '5')['hits'] = '1'
            return result

        self.ctx.call = call
        result = self.invoke_start(expect={'plan': 'any'})
        self.assertEqual(result['verified']['plan'], 'not observed (planner hit 1 times, recorded 0)')

    def test_any_plan_still_requires_the_planner_point(self):
        self.io = StartIO(self.ctx)
        with self.assertRaisesRegex(RuntimeError, 'planner'):
            self.invoke_start(expect={'plan': 'any'})
        self.assertEqual(self.io.actions, [])

    def test_mismatch_of_each_expect_key_saves_receipt_before_failure(self):
        cases = {'cpu_kind': 0, 'stage_form': 1, 'stage_id': 65535, 'perfect': False,
                 'juggler': True, 'damage': 0, 'cpu': [0, 0], 'cpu_words': ['0x00000000'] * 2,
                 'p1': [0, 0], 'p1_words': ['0x00000000'] * 2, 'plan': [1, 38]}
        for key, value in cases.items():
            with self.subTest(key=key):
                self.path.write_text('')
                self.io = StartIO(self.ctx)
                release, plan, terminal = excerpt('cp1b-pyra-attempt.jsonl')
                self.pending = [contiguous([release, plan, terminal])]
                original = self.ctx.call
                if key == 'plan':
                    self.enable_planner()
                with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                    self.invoke_start(expect={key: value})
                latest = max(Path(self.ctx.row_dir).glob('routes-drill-start*.json'),
                             key=lambda path: path.stat().st_mtime_ns)
                receipt = json.loads(latest.read_text())
                self.assertFalse(receipt['ok'])
                self.assertNotIn('verified', receipt)
                self.assertEqual(receipt['observed']['first_release']['sequence'], 44362)
                self.ctx.call = original

    def test_menu_start_requires_a_running_transition_and_no_existing_game(self):
        self.io = StartIO(self.ctx, wizard=False)
        del self.ctx.instance['trace_jsonl']
        result = self.invoke_start()
        self.assertEqual(result['verified']['running'], '1')
        self.assertEqual(self.io.menu['open'], '0')
        self.io = StartIO(self.ctx, wizard=False)
        self.io.menu['game.running'] = '1'
        with self.assertRaisesRegex(RuntimeError, 'already running'):
            self.invoke_start()
        self.assertEqual(self.io.actions, [])

    def test_header_stuck_trace_loss_and_epoch_change_fail(self):
        self.io = StartIO(self.ctx, fault='wizard_stuck')
        del self.ctx.instance['trace_jsonl']
        with self.assertRaisesRegex(RuntimeError, 'wizard'):
            self.invoke_start(cap=.02)
        self.ctx.instance['trace_jsonl'] = str(self.path)
        self.io = StartIO(self.ctx)
        self.pending = ['{"event":"loss","records":"1"}\n']
        with self.assertRaisesRegex(RuntimeError, 'loss'):
            self.invoke_start()
        self.io = StartIO(self.ctx, fault='epoch')
        with self.assertRaisesRegex(RuntimeError, 'connection'):
            self.invoke_start()

    def test_plan_requires_terminal_and_planner_counter_stays_constant(self):
        self.io = StartIO(self.ctx)
        self.enable_planner()
        release, plan, terminal = excerpt('cp1b-pyra-attempt.jsonl')
        self.pending = [contiguous([release, plan])]
        with self.assertRaisesRegex(RuntimeError, 'terminal'):
            self.invoke_start(expect={'plan': [0, 38]}, cap=.02)
        self.path.write_text('')
        self.io = StartIO(self.ctx)
        self.pending = [contiguous([release, plan, terminal])]
        original = self.ctx.call

        def call(method, params):
            result = original(method, params)
            if self.path.stat().st_size:
                next(point for point in result['points'] if point['id'] == '5')['dropped'] = '1'
            return result

        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, 'loss'):
            self.invoke_start(expect={'plan': [0, 38]})


if __name__ == '__main__':
    unittest.main()
