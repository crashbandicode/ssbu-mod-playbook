#!/usr/bin/env python3
"""Offline tests for routes.py and routes_online.py: a fake Eden and fake plugin logs, no emulator.

Run: python3 -I test_routes.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import routes  # noqa: E402
import routes_online  # noqa: E402
import runner  # noqa: E402


class LogCursorTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / 'practice-diagnostics.log'
        self.path.write_text('old 1\nold 2\n')

    def test_only_new_lines(self):
        cursor = routes.LogCursor(self.path)
        with self.path.open('a') as f:
            f.write('new 1\nnew')
        self.assertEqual(cursor.read(), ['new 1'])
        with self.path.open('a') as f:
            f.write(' 2\n')
        self.assertEqual(cursor.read(), ['new 2'])

    @unittest.skipIf(os.name == 'nt', 'Windows uses the unheld cursor; a held handle blocks the rename')
    def test_rotation_drains_old_file_then_reads_new(self):
        cursor = routes.LogCursor(self.path)
        with self.path.open('a') as f:
            f.write('before rename\n')
        os.replace(self.path, self.dir / 'practice-diagnostics.previous.log')
        with (self.dir / 'practice-diagnostics.previous.log').open('a') as f:
            f.write('late write to old\n')
        self.path.write_text('first of new\n')
        self.assertEqual(cursor.read(), ['before rename', 'late write to old', 'first of new'])
        self.assertEqual(cursor.rotations, 1)
        with self.path.open('a') as f:
            f.write('second of new\n')
        self.assertEqual(cursor.read(), ['second of new'])

    def test_unheld_mode_for_windows(self):
        cursor = routes.LogCursor(self.path, hold=False)
        self.assertIsNone(cursor.handle)
        with self.path.open('a') as f:
            f.write('a\n')
        self.assertEqual(cursor.read(), ['a'])
        os.replace(self.path, self.dir / 'practice-diagnostics.previous.log')  # works: no handle is held
        self.path.write_text('new\n')
        self.assertEqual(cursor.read(), ['new'])
        self.assertEqual(cursor.rotations, 1)
        self.path.write_text('t\n')  # truncated below the cursor's offset
        self.assertEqual(cursor.read(), ['t'])

    def test_truncation_reads_from_start(self):
        cursor = routes.LogCursor(self.path)
        self.path.write_text('x\n')
        self.assertEqual(cursor.read(), ['x'])

    def test_missing_file_then_created(self):
        missing = self.dir / 'later.log'
        cursor = routes.LogCursor(missing)
        self.assertEqual(cursor.read(), [])
        missing.write_text('hello\n')
        self.assertEqual(cursor.read(), ['hello'])

    def test_wait_reports_lines_seen(self):
        cursor = routes.LogCursor(self.path)
        with self.path.open('a') as f:
            f.write('nothing here\n')
        with self.assertRaises(RuntimeError) as raised:
            cursor.wait('never', 0.2)
        self.assertIn('nothing here', str(raised.exception))


class FakeTileGame:
    """A tile grid with the tile cursor's log lines. widths: tiles per row."""

    def __init__(self, log, widths, screen='sss', start=0, holding=True):
        self.log, self.widths, self.screen, self.at, self.holding = log, widths, screen, start, holding
        self.rows = []
        n = 0
        for w in widths:
            self.rows.append(list(range(n, n + w)))
            n += w

    def where(self, tile):
        for r, row in enumerate(self.rows):
            if tile in row:
                return r, row.index(tile)
        raise KeyError(tile)

    def step(self, direction):
        if not self.holding:
            return
        r, c = self.where(self.at)
        target = None
        if direction == 'left' and c > 0:
            target = self.rows[r][c - 1]
        elif direction == 'right' and c + 1 < len(self.rows[r]):
            target = self.rows[r][c + 1]
        elif direction == 'up' and r > 0:
            target = self.rows[r - 1][min(c, len(self.rows[r - 1]) - 1)]
        elif direction == 'down' and r + 1 < len(self.rows):
            target = self.rows[r + 1][min(c, len(self.rows[r + 1]) - 1)]
        with self.log.open('a') as f:
            f.write(f'1 step screen={self.screen} dir={direction} from={self.at} to={target if target is not None else "none"} x=0.0 y=0.0\n')
            if target is not None:
                self.at = target
                hovered = target if self.screen == 'css' else 'none'
                f.write(f'2 settled screen={self.screen} at={target} hovered={hovered} x=0.0 y=0.0\n')


class FakeRunner:
    def __init__(self, instance, game):
        self.instances = {'s': instance}
        self.plan = {'gates': {}}
        self.game = game
        self.played = []

    def call(self, on, method, params=None, job_cap=15):
        if method == 'macro.play':
            name = Path(params['path']).name.split('.')[0]
            self.played.append(name)
            if name.startswith('tile-'):
                self.game.step(name.split('-', 1)[1])
        return {}

    def wait_macro(self, on, cap):
        return None

    def log(self, record):
        pass

    def check_stop(self):
        pass


class FakeCtx:
    def __init__(self, runner_obj, row_dir):
        self.runner, self.on = runner_obj, 's'
        self.instance = runner_obj.instances['s']
        self.row_dir = row_dir

    def call(self, method, params=None, job_cap=15):
        return self.runner.call(self.on, method, params, job_cap)

    def log(self, record):
        pass

    def check_stop(self):
        pass

    def shot(self, name):
        return f'{name}.png'


class TileWalkerTests(unittest.TestCase):
    def walker(self, widths, start, screen='sss'):
        folder = Path(tempfile.mkdtemp())
        log = folder / 'events.log'
        log.write_text('')
        game = FakeTileGame(log, widths, screen, start)
        ctx = FakeCtx(FakeRunner({'tile_log': str(log)}, game), folder)
        return routes.TileWalker(ctx, screen), game, ctx

    def test_walks_to_target_by_rows_read_from_the_log(self):
        walker, game, ctx = self.walker([11] * 10 + [6], start=0)
        out = walker.walk_to(4)
        self.assertEqual(out['at'], 4)
        self.assertEqual(game.at, 4)
        walker, game, ctx = self.walker([11] * 10 + [6], start=37)
        self.assertEqual(walker.walk_to(57)['at'], 57)
        self.assertEqual(game.at, 57)

    def test_last_short_row(self):
        walker, game, ctx = self.walker([13] * 6 + [9], start=0, screen='css')
        self.assertEqual(walker.walk_to(85)['at'], 85)

    def test_plain_presses_are_never_sent(self):
        walker, game, ctx = self.walker([11] * 3, start=5)
        walker.walk_to(14)
        self.assertTrue(all(name.startswith('tile-') for name in ctx.runner.played))

    def test_settled_mismatch_fails(self):
        walker, game, ctx = self.walker([11] * 3, start=0)
        log = Path(ctx.instance['tile_log'])

        def lying(direction):
            with log.open('a') as f:
                f.write(f'1 step screen=sss dir={direction} from=0 to=1 x=0.0 y=0.0\n'
                        '3 settled screen=sss at=99 hovered=none\n')
        game.step = lying
        with self.assertRaises(RuntimeError):
            walker.step('right')


class TableTests(unittest.TestCase):
    def test_tables_come_from_trainingrooms(self):
        t = routes.tables()
        self.assertEqual(routes.fighter_entry('Mario')['tile'], 0)
        self.assertEqual(routes.fighter_entry('Lucas')['kind'], 0x28)
        self.assertEqual(routes.fighter_entry('0x28')['tile'], 39)
        self.assertEqual(routes.fighter_entry('Steve')['tile'], 78)
        self.assertEqual(routes.stage_entry('Battlefield')['tile'], 1)
        self.assertEqual(routes.stage_entry('final destination')['id'], 3)
        self.assertEqual(routes.stage_entry('Kongo Jungle')['tile'], 6)
        self.assertTrue(routes.stage_entry('Kongo Jungle').get('proven'))
        self.assertIn('commit', t['source'])
        with self.assertRaises(KeyError):
            routes.fighter_entry('Pokemon Trainer')

    def test_macro_index_hashes_match(self):
        for name in routes.routes_index()['macros']:
            path, sha, cap = routes.macro_file(name)
            self.assertTrue(path.is_file(), name)

    def test_chord_macros_press_in_one_frame(self):
        reset = json.loads(routes.macro_file('reset-lra')[0].read_text())
        self.assertIn(1 | 64 | 128, [e['buttons'] for e in reset['events']])
        tile = json.loads(routes.macro_file('tile-right')[0].read_text())
        self.assertEqual([e['buttons'] for e in tile['events']], [16, 16 | 16384, 0])


class SettledTests(unittest.TestCase):
    def body(self, x, status=0, situation=0):
        return {'x': x, 'y': 0.01, 'status': status, 'situation': situation, 'kind': 0, 'lr': 1.0, 'dmg': 0.0}

    def test_two_equal_grounded_reads(self):
        a = {0: self.body(-40.0), 1: self.body(40.0)}
        self.assertTrue(routes.settled(a, {0: self.body(-40.0), 1: self.body(40.0)}))
        self.assertFalse(routes.settled(None, a))
        self.assertFalse(routes.settled(a, {0: self.body(-39.0), 1: self.body(40.0)}))
        self.assertFalse(routes.settled(a, {0: self.body(-40.0, status=7), 1: self.body(40.0)}))
        self.assertFalse(routes.settled(a, {0: self.body(-40.0), 1: self.body(40.0, situation=2)}))


class LoadStepTests(unittest.TestCase):
    def test_shared_hook_names_resolve_next_to_the_runner(self):
        fn = runner.load_step(Path('/nonexistent'), 'routes:to_main')
        self.assertEqual(fn.__name__, 'to_main')
        fn = runner.load_step(Path('/nonexistent'), 'routes_online:lan_pair')
        self.assertEqual(fn.__name__, 'lan_pair')


class OnlineTests(unittest.TestCase):
    A1 = ('[auto] broker started title=01006A800016E000 lan=192.168.0.222 local_only=false\n'
          '[coord] leading epoch=137 tcp=192.168.0.222:35095\n'
          '[coord] member joined pid=1031977419 vip=10.253.109.237 members=1\n'
          '[auto] ready pid=1031977419 vip=10.253.109.237\n')
    B = ('[auto] broker started title=01006A800016E000 lan=192.168.0.166 local_only=false\n'
         '[auto] ready pid=1709175818 vip=10.253.175.95\n')

    def test_leader_then_pair(self):
        self.assertTrue(routes_online.leader_ok(self.A1, '192.168.0.222'))
        self.assertFalse(routes_online.leader_ok(self.A1, '192.168.0.1'))
        a2 = self.A1 + '[coord] member joined pid=1709175818 vip=10.253.175.95 members=2\n'
        self.assertFalse(routes_online.pair_joined(self.A1, self.B))
        self.assertTrue(routes_online.pair_joined(a2, self.B))

    def test_two_rooms_fail(self):
        b_leads = self.B + '[coord] leading epoch=9 tcp=192.168.0.166:1\n'
        a2 = self.A1 + '[coord] member joined pid=1709175818 vip=10.253.175.95 members=2\n'
        self.assertFalse(routes_online.pair_joined(a2, b_leads))
        self.assertFalse(routes_online.pair_joined(self.A1 + '[coord] member joined pid=5 members=2\n', self.B))


if __name__ == '__main__':
    unittest.main()
