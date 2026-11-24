"""Offline route replay against unmodified recorded bridge exchanges."""
import gzip
import json
import re
import tempfile
import types
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import routes
import state_gate


FIXTURES = Path(__file__).parent / 'fixtures' / 'recorded-rooms'
RUNS = {
    'xps-stop-1': dict(boot='0x022b82b302fcdd39', room_list=618, stage_first=840,
                       stage_wait_last=1783, lost_a=847, tiles=(6617, 7141, 13973),
                       start_before_tile=13964, working_a=13986, css=14061, lines=2686),
    'xps-1': dict(boot='0x41ebfad78f601a4b', room_list=772, stage_first=992,
                  stage_wait_last=1931, lost_a=1004, tiles=(7230, 12845),
                  start_before_tile=12837, working_a=12862, css=12936, lines=2971),
}
BEGIN = re.compile(r'\[Bridge\] state begin name=(\S+).* frame=(\d+) wall=\d+$')
FENCE = re.compile(r'\[Bridge\] req providers result=ok .* boot=(0x[0-9a-f]{16})(?: |$)')


def exchanges(name):
    data = gzip.decompress((FIXTURES / f'{name}.bridge.log.gz').read_bytes())
    lines = data.decode('utf-8').splitlines(keepends=True)
    found, fence, start = [], None, None
    for index, line in enumerate(lines):
        if match := FENCE.search(line):
            fence, start = match[1], index
            while start and '[Bridge] provider name=' in lines[start - 1]:
                start -= 1
        match = BEGIN.search(line.rstrip('\r\n'))
        if not match:
            continue
        provider, frame = match[1], int(match[2])
        end = next((i for i in range(index + 1, len(lines))
                    if f'[Bridge] state end name={provider} ' in lines[i]), None)
        if end is None or fence is None:
            raise ValueError(f'incomplete {provider} exchange at line {index + 1}')
        block = state_gate.latest(''.join(lines[index:end + 1]), provider)
        if block is None or block['boot'] != fence:
            raise ValueError(f'invalid {provider} exchange at line {index + 1}')
        found.append({'provider': provider, 'frame': frame, 'boot': fence, 'values': block['values'],
                      'lines': ''.join(lines[start:end + 1])})
    return lines, found


class Replay:
    def __init__(self, root, recorded, start, end, inputs):
        self.path = root / 'bridge.log'
        self.path.write_text('')
        self.recorded, self.start, self.end = recorded, start, end
        self.inputs = list(inputs)
        self.floor, self.now = start, 0.0
        self.sends, self.served, self.divergences = [], Counter(), []
        self.ctx = types.SimpleNamespace(
            on='a', instance={'bridge_log': str(self.path)}, runner=types.SimpleNamespace(),
            row_dir=root, check_stop=lambda: None, call=self.call, log=lambda event: None,
        )

    def position(self):
        frame = max(self.floor, self.start + int(60 * self.now))
        if self.inputs:
            frame = min(frame, self.inputs[0][0] - 1)
        return min(self.end, frame)

    def call(self, method, *_args):
        if method == 'macro.status':
            return {'state': 'idle', 'error': ''}
        if method == 'status':
            return {}
        raise AssertionError(f'unexpected call {method}')

    def bridge(self, _ctx, *requests):
        request = requests[-1]
        if not request.startswith('state '):
            raise AssertionError(f'unexpected bridge request {requests!r}')
        provider, position = request[6:], self.position()
        candidates = [entry for entry in self.recorded
                      if entry['provider'] == provider and position - 120 <= entry['frame'] <= position]
        if candidates:
            entry = max(candidates, key=lambda item: item['frame'])
            self.served[(provider, entry['frame'])] += 1
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(entry['lines'])

    def play(self, _ctx, macro):
        position = self.position()
        if not self.inputs or macro != self.inputs[0][1]:
            self.divergences.append((macro, position, list(self.inputs)))
            raise AssertionError(f'unrecorded effective input {macro!r} at frame {position}')
        frame, _ = self.inputs.pop(0)
        self.sends.append((macro, position))
        self.floor = max(position, frame)

    def sleep(self, seconds):
        self.now += seconds

    def run(self):
        with mock.patch.object(routes, 'bridge_v2', side_effect=self.bridge), \
                mock.patch.object(routes, 'play', side_effect=self.play), \
                mock.patch.object(routes.time, 'monotonic', side_effect=lambda: self.now), \
                mock.patch.object(routes.time, 'sleep', side_effect=self.sleep):
            return routes.wireless_room(self.ctx)


class RecordedRoomsTests(unittest.TestCase):
    def stopped(self, name, start, end, inputs):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        _, recorded = exchanges(name)
        replay = Replay(Path(temp.name), recorded, start, end, inputs)
        with self.assertRaises(routes.closed_loop.RouteStopped) as stopped:
            replay.run()
        self.assertEqual(replay.divergences, [])
        receipt = json.loads((Path(temp.name) / 'routes-wireless-room-stop.json').read_text())
        return replay, stopped.exception, receipt

    def test_the_fixtures_match_their_provenance(self):
        for name, facts in RUNS.items():
            with self.subTest(run=name):
                lines, blocks = exchanges(name)
                self.assertEqual(len(lines), facts['lines'])
                self.assertEqual({entry['boot'] for entry in blocks}, {facts['boot']})
                ui = [entry for entry in blocks if entry['provider'] == 'ssbu.ui']
                rooms = [entry for entry in ui if entry['values'].get('screen') == 'ldn.room_list']
                self.assertEqual([entry['frame'] for entry in rooms], [facts['room_list']])
                self.assertEqual(rooms[0]['values'].get('focus.id'), 'unknown')
                stage = [entry['frame'] for entry in ui if entry['values'].get('screen') == 'stage_select']
                self.assertEqual(stage[0], facts['stage_first'])
                self.assertEqual(max(frame for frame in stage if frame < facts['tiles'][0] - 1),
                                 facts['stage_wait_last'])
                self.assertIn(facts['start_before_tile'], stage)
                tiles = [entry for entry in blocks if entry['provider'] == 'tile.cursor']
                self.assertEqual(tuple(entry['frame'] for entry in tiles), facts['tiles'])
                for entry in tiles:
                    tile = entry['values']
                    self.assertEqual((tile.get('screen'), tile.get('settled'),
                                      tile.get('p1.token'), tile.get('local.token')),
                                     ('sss', '1', 'held', 'held'))
                    self.assertEqual(tile.get('at'), tile.get('hovered'))
                self.assertEqual(next(entry['frame'] for entry in ui
                                      if entry['values'].get('screen') == 'css.smash'), facts['css'])
                self.assertLess(facts['stage_first'], facts['lost_a'])
                self.assertLess(facts['lost_a'], facts['stage_wait_last'])
                self.assertLess(facts['tiles'][-1], facts['working_a'])
                self.assertLess(facts['working_a'], facts['css'])

    def test_the_recorded_early_stage_select_window_gets_no_press(self):
        for name, facts in RUNS.items():
            with self.subTest(run=name):
                replay, error, receipt = self.stopped(name, facts['stage_first'], facts['stage_wait_last'], [])
                self.assertEqual(error.step, 'stage-a')
                self.assertEqual(error.receipt['entered_at'], 'stage-a')
                self.assertEqual(receipt['entered_at'], 'stage-a')
                self.assertEqual(replay.sends, [])
                self.assertEqual(receipt['step'], 'stage-a')
                self.assertEqual(next(step for step in receipt['steps'] if step['name'] == 'stage-a')['sends'], 0)
                self.assertEqual(receipt['press_gate'], 'tile.cursor (no input.ready published)')
                self.assertEqual(receipt['stalls'], [])

    def test_the_recorded_qualifying_tile_read_gets_one_a_and_reaches_css(self):
        for name, facts in RUNS.items():
            with self.subTest(run=name):
                replay, error, receipt = self.stopped(
                    name, facts['start_before_tile'], facts['css'], [(facts['working_a'], 'lobby-a')])
                self.assertEqual(len(replay.sends), 1)
                self.assertEqual(replay.sends[0][0], 'lobby-a')
                self.assertGreaterEqual(replay.sends[0][1], facts['tiles'][-1])
                self.assertLessEqual(replay.sends[0][1], facts['working_a'])
                self.assertEqual(replay.inputs, [])
                self.assertIn('css.smash', receipt['screens'])
                self.assertEqual(next(step for step in receipt['steps'] if step['name'] == 'stage-a')['sends'], 1)
                self.assertFalse(any(event['step'] == 'stage-a' for event in receipt['stalls']))
                self.assertEqual(error.step, 'css-hand')
                self.assertEqual(next(step for step in receipt['steps'] if step['name'] == 'css-hand')['sends'], 0)

    def test_the_recorded_room_list_has_no_create_row_focus(self):
        for name, facts in RUNS.items():
            with self.subTest(run=name):
                replay, error, receipt = self.stopped(name, facts['room_list'], facts['room_list'], [])
                self.assertEqual(error.step, 'open-create')
                self.assertEqual(replay.sends, [])
                self.assertEqual(receipt['unsupported_reader'], 'ldn.room_list focus')


if __name__ == '__main__':
    unittest.main()
