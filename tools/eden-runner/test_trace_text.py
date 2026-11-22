#!/usr/bin/env python3
"""Offline tests for trace_text.py using an excerpt of a real text-setter trace."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trace_text  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / 'fixtures/trace-text/set-text-excerpt.jsonl'
CRUMB = 'Fighters & Stages  >  CPU Character   page 1 of 1'
EXPECTED = [
    (82, 'tr_crumb', ''),
    (311, 'tr_crumb', ''),
    (505, 'tr_rl_00', 'Mash'),
    (507, 'tr_rv_00', '0 of 30  >'),
    (744, 'tr_rl_00', 'Save States'),
    (746, 'tr_rv_00', '[ON]'),
    (987, 'tr_rl_00', 'SAVE STATES  (1)'),
    (989, 'tr_rv_00', ''),
    (1730, 'tr_crumb', CRUMB),
    (1731, 'tr_crumb', CRUMB),
    (3437, 'TrModDisp0Header', 'CPU Character'),
    (3621, 'TrModDisp0Header', 'Fighters'),
    (3623, 'TrModDisp0Long', 'Loading 4/10\n· Ready Fighters 10/30: 19\nstay cold'),
    (3722, 'TrIvChG00', '\ue0e0'),
]


def synthetic(name=b'pane', text='OK', name_at=0xb0):
    pane = bytearray(256)
    pane[name_at:name_at + len(name)] = name
    data = text.encode('utf-16-le')
    return {'sequence': '1', 'frame': '2', 'wall_time_ns': '3',
            'registers': {'x0': '0x1000', 'x2': hex(len(data) // 2), 'pc': '0x2000'},
            'captures': [{'hex': pane.hex()}, {'hex': data.hex()}]}


class TraceTextTests(unittest.TestCase):
    def setUp(self):
        self.lines = FIXTURE.read_bytes().splitlines(keepends=True)

    def test_point_arm_spec(self):
        self.assertEqual(trace_text.point('module!symbol'), {
            'target': 'module!symbol', 'mode': 'records', 'registers': ['x0', 'x1', 'x2', 'pc'],
            'captures': [{'expression': 'x0', 'type': 'bytes', 'size': 256},
                         {'expression': 'x1', 'type': 'bytes', 'size': 640}],
            'max_hits': 0, 'rate_limit': 1000})

    def test_real_fixture_names_text_and_error(self):
        empty = trace_text.parse(self.lines[0])
        self.assertEqual((empty['sequence'], empty['name'], empty['known'], empty['text']),
                         (75, 'tr_tab_00', True, ''))
        parsed = [trace_text.parse(line) for line in self.lines[1:]]
        self.assertEqual([(r['sequence'], r['name'], r['text']) for r in parsed], EXPECTED)
        self.assertEqual(trace_text.parse(self.lines[1].decode()), parsed[0])
        self.assertEqual((parsed[0]['frame'], parsed[0]['wall_time_ns']), (5284, '1795357858865109228'))
        self.assertIn('pane', parsed[0])
        self.assertIn('pc', parsed[0])

    def test_zero_units_are_empty_regardless_of_text_capture(self):
        original = json.loads(self.lines[0])
        self.assertEqual((original['registers']['x1'], original['registers']['x2']), ('0x2', '0x0'))
        for capture in ({'error': 'memory_unavailable'}, {}, {'hex': ''}, {'hex': 'bad-hex'}, None):
            with self.subTest(capture=capture):
                record = copy.deepcopy(original)
                record['captures'][1] = capture
                parsed = trace_text.parse(json.dumps(record))
                self.assertEqual((parsed['known'], parsed['text'], parsed['sequence']), (True, '', 75))
        for field in ('sequence', 'frame'):
            record = copy.deepcopy(original)
            record.pop(field)
            self.assertIsNone(trace_text.parse(json.dumps(record)))
        for x2 in (None, 'not-hex'):
            record = copy.deepcopy(original)
            if x2 is None:
                record['registers'].pop('x2')
            else:
                record['registers']['x2'] = x2
            self.assertIsNone(trace_text.parse(json.dumps(record)))

    def test_parse_rejects_malformed_records(self):
        good = synthetic()
        variants = [b'{', b'[]', b'null']
        for change in (
            lambda r: r.update(captures=[]),
            lambda r: r['captures'][0].update(error='memory_unavailable'),
            lambda r: r['registers'].pop('x0'),
            lambda r: r['registers'].pop('x2'),
            lambda r: r['registers'].update(x2='not-hex'),
            lambda r: r['captures'][0].update(hex='not-hex'),
        ):
            item = copy.deepcopy(good)
            change(item)
            variants.append(json.dumps(item))
        variants += [json.dumps(synthetic(name=b'')), json.dumps(synthetic(name=b'bad\x1fname')),
                     json.dumps(synthetic(name=b'bad\xffname'))]
        for raw in variants:
            with self.subTest(raw=str(raw)[:50]):
                self.assertIsNone(trace_text.parse(raw))

    def test_parse_supports_offset_and_masks_length(self):
        item = synthetic(name=b'custom', name_at=0x90)
        item['registers']['x2'] = '0x100000002'
        self.assertIsNone(trace_text.parse(json.dumps(item)))
        parsed = trace_text.parse(json.dumps(item), name_at=0x90)
        self.assertEqual((parsed['name'], parsed['text']), ('custom', 'OK'))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'trace.jsonl'
            path.write_text(json.dumps(item) + '\n')
            self.assertIsNone(trace_text.current(path, 'custom'))
            self.assertEqual(trace_text.current(path, 'custom', name_at=0x90)['text'], 'OK')

    def test_records_filter_since_and_missing(self):
        all_records = trace_text.records(FIXTURE)
        self.assertEqual([(r['sequence'], r['name'], r['text']) for r in all_records],
                         [(75, 'tr_tab_00', '')] + EXPECTED)
        crumb = trace_text.records(FIXTURE, name='tr_crumb')
        self.assertEqual([r['sequence'] for r in crumb], [82, 311, 1730, 1731])
        self.assertEqual([r['sequence'] for r in trace_text.records(FIXTURE, 'tr_crumb', since=crumb[2]['at'])],
                         [1730, 1731])
        self.assertEqual([r['sequence'] for r in trace_text.records(FIXTURE, 'tr_crumb', since=crumb[2]['at'] + 1)],
                         [1731])
        self.assertEqual(trace_text.records(FIXTURE.parent / 'missing.jsonl'), [])
        self.assertIsNone(trace_text.current(FIXTURE, 'missing'))
        self.assertEqual(trace_text.current(FIXTURE, 'tr_crumb')['sequence'], 1731)

    def test_partial_last_line_is_consumed_only_after_newline(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'trace.jsonl'
            path.write_bytes(self.lines[1] + self.lines[9][:-1])
            self.assertEqual([r['sequence'] for r in trace_text.records(path)], [82])
            with path.open('ab') as f:
                f.write(b'\n')
            self.assertEqual([r['sequence'] for r in trace_text.records(path)], [82, 1730])

    def test_bound_measured_unmeasured_and_failure_cases(self):
        crumb = trace_text.records(FIXTURE, 'tr_crumb')
        measured = trace_text.bound(FIXTURE, 'tr_crumb', crumb[2]['at'], lambda text: text == CRUMB)
        self.assertEqual((measured['ok'], measured['transition'], measured['text_before'], measured['text']),
                         (True, 'measured', '', CRUMB))
        self.assertEqual((measured['records_before'], measured['records']), (2, 2))
        self.assertEqual((measured['sequences'], measured['frames']), ([1730, 1731], [5523, 5523]))
        repeated = trace_text.bound(FIXTURE, 'tr_crumb', crumb[3]['at'], lambda text: text == CRUMB)
        self.assertEqual((repeated['ok'], repeated['transition'], repeated['text_before']), (True, 'measured', CRUMB))
        empty = trace_text.bound(FIXTURE, 'tr_crumb', FIXTURE.stat().st_size, lambda text: True)
        self.assertEqual((empty['ok'], empty['transition'], empty['text_before'], empty['why']),
                         (False, None, CRUMB, 'no record in the window'))
        wrong = trace_text.bound(FIXTURE, 'tr_crumb', 0, lambda text: text == 'other')
        self.assertEqual((wrong['ok'], wrong['text'], wrong['why']),
                         (False, CRUMB, 'the newest text is not the expected one'))

    def test_bound_detects_a_change_within_the_window(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'trace.jsonl'
            lines = [(json.dumps(synthetic(name=b'tr_crumb', text=text)) + '\n').encode()
                     for text in (CRUMB, '', CRUMB)]
            path.write_bytes(b''.join(lines))
            result = trace_text.bound(path, 'tr_crumb', len(lines[0]), lambda text: text == CRUMB)
            self.assertEqual((result['ok'], result['transition'], result['text_before']),
                             (True, 'measured', CRUMB))

    def test_wait_succeeds_after_fake_sleep_and_times_out(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'trace.jsonl'
            path.write_bytes(b'')
            now = [0.0]

            def sleep(delay):
                now[0] += delay
                if not path.stat().st_size:
                    path.write_bytes(self.lines[9])

            result = trace_text.wait(path, 'tr_crumb', lambda text: text == CRUMB, 1,
                                     clock=lambda: now[0], sleep=sleep)
            self.assertTrue(result['ok'])
            self.assertEqual(result['text'], CRUMB)
            with self.assertRaises(TimeoutError) as caught:
                trace_text.wait(path, 'tr_crumb', lambda text: False, .3,
                                clock=lambda: now[0], sleep=sleep)
            self.assertIn('tr_crumb', str(caught.exception))
            self.assertIn(CRUMB, str(caught.exception))

    def test_wait_timeout_without_records_names_the_absence(self):
        now = [0.0]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'missing.jsonl'
            with self.assertRaises(TimeoutError) as caught:
                trace_text.wait(path, 'pane', lambda text: True, .2, clock=lambda: now[0],
                                sleep=lambda delay: now.__setitem__(0, now[0] + delay))
        self.assertIn('pane', str(caught.exception))
        self.assertIn('no record', str(caught.exception))


class UnreadableTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'trace.jsonl'
        self.lines = FIXTURE.read_bytes().splitlines(keepends=True)
        self.valid = json.loads(self.lines[9])
        self.empty = json.loads(self.lines[1])

    def write(self, *records):
        self.path.write_text(''.join(json.dumps(record) + '\n' for record in records))

    def damaged(self, capture=1, sequence='1732'):
        record = copy.deepcopy(self.valid)
        record['sequence'], record['frame'] = sequence, '5524'
        record['captures'][capture] = {'error': 'memory_unavailable'}
        return record

    def test_unreadable_text_is_a_named_unknown_even_without_metadata(self):
        record = self.damaged()
        self.assertGreater(int(record['registers']['x2'], 0), 0)
        parsed = trace_text.parse(json.dumps(record))
        self.assertEqual((parsed['name'], parsed['text'], parsed['known'], parsed['sequence'], parsed['frame']),
                         ('tr_crumb', None, False, 1732, 5524))
        self.assertIn('memory_unavailable', parsed['why'])
        record.pop('sequence')
        record['frame'] = 'not-a-frame'
        parsed = trace_text.parse(json.dumps(record))
        self.assertEqual((parsed['known'], parsed['sequence'], parsed['frame']), (False, None, None))
        record['registers'].pop('x2')
        self.assertIsNone(trace_text.parse(json.dumps(record)))
        for update in ({'hex': ''},):
            short = copy.deepcopy(self.valid)
            short['captures'][1] = update
            self.assertFalse(trace_text.parse(json.dumps(short))['known'])
        too_long = copy.deepcopy(self.valid)
        too_long['registers']['x2'] = hex(trace_text.MAX_UNITS + 1)
        self.assertFalse(trace_text.parse(json.dumps(too_long))['known'])

    def test_known_metadata_requires_decimal_integer_values(self):
        for field in ('sequence', 'frame'):
            for value in (None, '', '-1', -1, True, 2.5, '2.0', '１２'):
                with self.subTest(field=field, value=value):
                    item = copy.deepcopy(self.valid)
                    item[field] = value
                    self.assertIsNone(trace_text.parse(json.dumps(item)))
            missing = copy.deepcopy(self.valid)
            del missing[field]
            self.assertIsNone(trace_text.parse(json.dumps(missing)))
        parsed = trace_text.parse(json.dumps(self.valid))
        self.assertEqual((parsed['known'], parsed['sequence'], parsed['frame']), (True, 1730, 5523))
        self.valid.update(sequence=0, frame=7)
        parsed = trace_text.parse(json.dumps(self.valid))
        self.assertEqual((parsed['sequence'], parsed['frame']), (0, 7))

    def test_scan_current_detects_pane_loss_on_a_learned_key(self):
        pane_error = self.damaged(capture=0)
        unrelated = self.damaged(capture=0, sequence='1733')
        unrelated['registers']['x0'] = '0xdeadbeef'
        self.write(self.valid, unrelated, pane_error)
        self.assertEqual(trace_text.parse_key(json.dumps(pane_error)),
                         (self.valid['registers']['x0'], self.valid['point_id']))
        with mock.patch.object(trace_text, 'parse', wraps=trace_text.parse) as parse:
            found, _ = trace_text.scan(self.path, 'tr_crumb', point_id=1)
        self.assertEqual([record['sequence'] for record in found], [1730, 1732])
        self.assertEqual((found[-1]['known'], found[-1]['why']), (False, 'pane capture unreadable'))
        self.assertNotIn('1733', [json.loads(call.args[0])['sequence'] for call in parse.call_args_list])
        self.assertFalse(trace_text.current(self.path, 'tr_crumb', point_id=1)['known'])
        self.assertEqual([record['sequence'] for record in trace_text.records(self.path, 'tr_crumb')], [1730])
        recovered = copy.deepcopy(self.valid)
        recovered['sequence'] = '1734'
        with self.path.open('a') as f:
            f.write(json.dumps(recovered) + '\n')
        self.assertEqual(trace_text.current(self.path, 'tr_crumb')['sequence'], 1734)

    def test_unreadable_key_keeps_missing_metadata_and_obeys_point_binding(self):
        damaged = self.damaged(capture=0)
        damaged.pop('sequence')
        damaged['frame'] = 'bad'
        self.write(self.valid, damaged)
        sample = trace_text.current(self.path, 'tr_crumb', point_id=1)
        self.assertEqual((sample['known'], sample['sequence'], sample['frame']), (False, None, None))
        damaged['point_id'] = '2'
        self.write(self.valid, damaged)
        sample = trace_text.current(self.path, 'tr_crumb', point_id=1)
        self.assertEqual((sample['known'], sample['sequence']), (True, 1730))
        self.assertFalse(trace_text.current(self.path, 'tr_crumb')['known'])

    def test_bound_classifies_same_key_history_and_foreign_keys(self):
        self.write(self.valid)
        result = trace_text.bound(self.path, 'tr_crumb', 0, lambda text: text == CRUMB)
        self.assertEqual((result['transition'], result['previous']), ('sampled', None))
        self.write(self.valid, self.valid)
        result = trace_text.bound(self.path, 'tr_crumb', 0, lambda text: text == CRUMB)
        self.assertEqual((result['transition'], result['previous']), ('unmeasured', None))
        self.write(self.empty, self.valid)
        result = trace_text.bound(self.path, 'tr_crumb', 0, lambda text: text == CRUMB)
        self.assertEqual(result['transition'], 'measured')
        self.assertEqual(result['previous'], {'text': '', 'sequence': 82, 'frame': 5284,
                                               'pane': self.valid['registers']['x0']})
        wrong = trace_text.bound(self.path, 'tr_crumb', 0, lambda text: False)
        self.assertEqual((wrong['transition'], wrong['previous']['sequence']), (None, 82))
        foreign = copy.deepcopy(self.empty)
        foreign['registers']['x0'] = '0xdeadbeef'
        self.write(foreign, self.valid)
        result = trace_text.bound(self.path, 'tr_crumb', 0, lambda text: text == CRUMB)
        self.assertEqual((result['transition'], result['previous']), ('sampled', None))

    def test_bound_and_wait_report_unknown_latest_sample(self):
        self.write(self.valid, self.damaged())
        result = trace_text.bound(self.path, 'tr_crumb', 0, lambda text: text == CRUMB)
        self.assertEqual((result['ok'], result['why']), (False, 'the newest sample for this pane is unreadable'))
        self.assertEqual((result['unknown']['sequence'], result['unknown']['frame']), (1732, 5524))
        now = [0.0]
        with self.assertRaisesRegex(TimeoutError, 'unreadable'):
            trace_text.wait(self.path, 'tr_crumb', lambda text: text == CRUMB, .2, clock=lambda: now[0],
                            sleep=lambda delay: now.__setitem__(0, now[0] + delay))

    def test_wait_recovers_when_a_new_known_sample_arrives(self):
        self.write(self.valid, self.damaged())
        now = [0.0]
        recovered = copy.deepcopy(self.valid)
        recovered['sequence'] = '1734'

        def sleep(delay):
            now[0] += delay
            with self.path.open('a') as f:
                f.write(json.dumps(recovered) + '\n')

        result = trace_text.wait(self.path, 'tr_crumb', lambda text: text == CRUMB, 1,
                                 clock=lambda: now[0], sleep=sleep)
        self.assertEqual((result['ok'], result['sequences'][-1]), (True, 1734))


class TraceScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'trace.jsonl'
        self.lines = FIXTURE.read_bytes().splitlines(keepends=True)

    def test_scan_continues_after_complete_lines_and_waits_for_partial(self):
        partial = len(self.lines[9]) // 2
        first = self.lines[0] + self.lines[1]
        self.path.write_bytes(first + self.lines[9][:partial])
        records, offset = trace_text.scan(self.path, 'tr_crumb')
        self.assertEqual([r['sequence'] for r in records], [82])
        self.assertEqual(offset, len(first))
        self.assertEqual(trace_text.scan(self.path, 'tr_crumb', since=offset), ([], offset))
        with self.path.open('ab') as f:
            f.write(self.lines[9][partial:])
        records, next_offset = trace_text.scan(self.path, 'tr_crumb', since=offset)
        self.assertEqual([r['sequence'] for r in records], [1730])
        self.assertEqual(next_offset, self.path.stat().st_size)
        self.assertEqual(trace_text.scan(self.path, 'tr_crumb', since=next_offset), ([], next_offset))

    def test_midline_offset_and_missing_file(self):
        self.assertEqual(trace_text.scan(self.path, since=25), ([], 0))
        self.path.write_bytes(self.lines[1][:80])
        self.assertEqual(trace_text.scan(self.path, since=10), ([], 10))
        with self.path.open('ab') as f:
            f.write(self.lines[1][80:] + self.lines[9])
        found, offset = trace_text.scan(self.path, 'tr_crumb', since=10)
        self.assertEqual([r['sequence'] for r in found], [1730])
        self.assertEqual(offset, self.path.stat().st_size)
        self.assertEqual(trace_text.scan(self.path, since=offset + 10), ([], offset + 10))

    def test_name_prefilter_does_not_parse_other_panes(self):
        with mock.patch.object(trace_text, 'parse', wraps=trace_text.parse) as parse:
            found, offset = trace_text.scan(FIXTURE, 'tr_crumb')
        self.assertEqual([r['sequence'] for r in found], [82, 311, 1730, 1731])
        self.assertEqual([json.loads(call.args[0])['sequence'] for call in parse.call_args_list],
                         ['82', '311', '1730', '1731'])
        self.assertEqual(offset, FIXTURE.stat().st_size)

    def test_point_id_uses_real_request_and_write_ids(self):
        request, _ = trace_text.scan(FIXTURE, 'tr_crumb', point_id=1)
        write, _ = trace_text.scan(FIXTURE, 'tr_crumb', point_id='2')
        self.assertEqual([r['sequence'] for r in request], [82, 311, 1730])
        self.assertEqual([r['sequence'] for r in write], [1731])
        self.assertEqual({str(r['point_id']) for r in request}, {'1'})
        self.assertEqual(str(write[0]['point_id']), '2')
        self.assertEqual(trace_text.current(FIXTURE, 'tr_crumb', point_id='1')['sequence'], 1730)


if __name__ == '__main__':
    unittest.main(verbosity=2)
