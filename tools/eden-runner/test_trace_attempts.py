"""Host probes of release-marker decoding and bounded trace observation."""

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import routes
import test_setup_blocks as fake


# Source: phase23/edge-juggle/evidence/cp1b-pyra38-1/trace/edge-juggle-p0-83eecc32.trace.jsonl
# SHA-256 bb83adcca818425248239fbe6fe575d0c382cb50fcb9be9b57cb5ff1e573a9df
# Source: phase23/edge-juggle/evidence/cp1b-hero32-3/trace/edge-juggle-p0-83eecc32.trace.jsonl
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
                        'target': 'libtraining_rooms.nro!edge_juggle_attempt'}],
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
        self.pending = [json.dumps(release) + '\n' + json.dumps(terminal) + '\n']
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
        for line in ('{"event":"loss","records":"1"}\n', '{"event":"entry","point_id":"8"}\n'):
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


if __name__ == '__main__':
    unittest.main()
