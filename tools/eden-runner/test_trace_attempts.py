"""Host probes of release-marker decoding and bounded trace observation."""

import importlib
import json
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
DATA = Path(__file__).parent / 'test-data'


def excerpt(name):
    return [json.loads(line) for line in (DATA / name).read_text().splitlines()]


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


class ObserveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'trace.jsonl'
        self.path.write_text('')
        self.ctx = SimpleNamespace(runner=SimpleNamespace(), row_dir=directory.name, on='s',
                                   instance={'trace_jsonl': str(self.path)})
        self.ctx.call = lambda method, params: {
            'points': [{'id': '8', 'state': 'armed', 'mode': 'records',
                        'target': 'libtraining_rooms.nro!edge_juggle_attempt', 'dropped': '0',
                        'process_id': '81', 'address': '0x1234'}],
            'output': str(self.path), 'output_error': '', 'boot_error': '', 'file_lost': '0'}
        self.pending = []

        def check_stop():
            if self.pending:
                with self.path.open('a') as stream:
                    stream.write(self.pending.pop(0))

        self.ctx.check_stop = check_stop
        self.io = fake.MenuIO(self.ctx)
        self.io.menu.update({'game': 'Guard the Edge', 'game.running': '1'})

    def invoke(self, **kwargs):
        with patch.object(routes, 'BridgePickerIO', return_value=self.io):
            return routes.drill_observe(self.ctx, **kwargs)

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
        self.pending = [json.dumps(terminal) + '\n']
        result = self.invoke(attempts=1, cap=.5, allow_stopped=True)
        self.assertEqual(result['verified']['running'], '0')

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

    def test_terminal_written_during_preflight_is_not_missed(self):
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
        result = self.invoke(attempts=1, cap=.5)
        self.assertEqual(result['verified']['last_sequence'], 58100)

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


if __name__ == '__main__':
    unittest.main()
