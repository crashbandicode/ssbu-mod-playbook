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


class RecipeTests(unittest.TestCase):
    def test_combo_in_one_frame_with_min_hold(self):
        import recipe_macro
        doc = recipe_macro.compile_recipe('open', [{'buttons': 'b+up', 'hold': 10, 'gap': 0.1}])
        self.assertEqual([(e['frame'], e['buttons']) for e in doc['events']], [(0, 8194), (2, 0)])
        self.assertEqual(doc['duration_frames'], 2 + 6)
        doc = recipe_macro.compile_recipe('x', [{'buttons': 'plus', 'hold': 100}, {'stick': [0, -1, 250], 'gap': 0}])
        self.assertEqual([(e['frame'], e['buttons'], e['sticks'][1]) for e in doc['events']],
                         [(0, 1024, 0), (6, 0, 0), (42, 0, -1.0), (57, 0, 0)])
        with self.assertRaises(ValueError):
            recipe_macro.compile_recipe('h', [{'buttons': 'home'}])

    def test_plan_compiles_to_a_ready_dry_run(self):
        import recipe_macro
        folder = Path(tempfile.mkdtemp())
        plan = {'game_id': '01006A800016E000', 'start': 's',
                'gates': {'g': {'kind': 'log', 'source': 'console_log', 'pattern': 'x', 'cap': 1}},
                'segments': {'menu': {'recipe': [{'buttons': 'b+up', 'hold': 100}]}},
                'rows': [{'id': 'r', 'start': 's', 'end': 's', 'steps': [{'gate': 'g'}, {'play': 'menu'}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        self.assertEqual(recipe_macro.compile_plan(folder / 'rows.json'), ['menu'])
        report = runner.dry_run(folder / 'rows.json')
        self.assertEqual(report['result'], 'READY', report)
        self.assertEqual(recipe_macro.compile_plan(folder / 'rows.json'), [])  # already compiled


class SwitchHwTests(unittest.TestCase):
    def test_line_writer_keeps_lines_whole(self):
        import io
        import switch_hw
        buf = io.BytesIO()
        w = switch_hw.LineWriter(buf)
        w.feed(b'plugin=training_rooms/0.2.0 game=13.0.5 res')
        self.assertEqual(buf.getvalue(), b'')
        w.feed(b'ult=installed\r\nnext')
        self.assertEqual(buf.getvalue(), b'plugin=training_rooms/0.2.0 game=13.0.5 result=installed\n')
        w.close()
        self.assertTrue(buf.getvalue().endswith(b'next\n'))

    def test_capture_reconnects_and_stops(self):
        import switch_hw
        chunks = [[b'a\nb', b'c\n'], [b'd\n']]
        calls = []

        class Sock:
            def __init__(self, data):
                self.data = list(data)

            def settimeout(self, t):
                pass

            def recv(self, n):
                return self.data.pop(0) if self.data else b''

            def close(self):
                pass

        def connect(addr, timeout):
            calls.append(addr)
            if not chunks:
                raise OSError('refused')
            return Sock(chunks.pop(0))
        path = Path(tempfile.mkdtemp()) / 'skyline.log'
        switch_hw.capture_log(path, 'h', 1, stop=lambda: len(calls) >= 3, connect=connect, retry_s=0)
        self.assertEqual(path.read_text(), 'a\nbc\nd\n')

    def test_factory_routes_usb_to_switch_remote(self):
        folder = Path(tempfile.mkdtemp())
        (folder / 'switch_remote.py').write_text('def make_client(s):\n    return ("fake", s)\n')
        old = os.environ.get('SWITCH_REMOTE_DIR')
        os.environ['SWITCH_REMOTE_DIR'] = str(folder)
        sys.modules.pop('switch_remote', None)
        try:
            self.assertEqual(runner.default_client_factory()('usb:///dev/ttyACM0'), ('fake', 'usb:///dev/ttyACM0'))
        finally:
            sys.modules.pop('switch_remote', None)
            if old is None:
                os.environ.pop('SWITCH_REMOTE_DIR', None)
            else:
                os.environ['SWITCH_REMOTE_DIR'] = old


class SwitchGateTests(unittest.TestCase):
    def test_hw_gates_read_stills(self):
        from PIL import Image
        folder = Path(tempfile.mkdtemp())
        spec = routes._switch_spec('tr-menu')
        frame = Image.new('RGB', (1280, 720))
        frame.paste(Image.open(spec['reference']).convert('RGB'), tuple(spec['roi'][:2]))
        good, blank = folder / 'good.png', folder / 'blank.png'
        frame.save(good)
        Image.new('RGB', (1280, 720)).save(blank)
        shots = []

        class R:
            plan = {'gates': {}}

            @staticmethod
            def compare_region(*a):
                return runner.Runner.compare_region(*a)

        ctx = FakeCtx.__new__(FakeCtx)
        ctx.runner, ctx.on, ctx.row_dir = R(), 'sw', folder
        ctx.instance = {'socket': 'usb:///dev/ttyACM0'}
        counter = []

        def shot(name):  # a fresh file per still, as runner.shot makes (the gate deletes failed stills)
            counter.append(1)
            path = folder / f'shot-{len(counter)}.png'
            path.write_bytes(Path(shots.pop(0)).read_bytes())
            return str(path)
        ctx.shot = shot
        self.assertTrue(routes.on_switch(ctx))
        shots[:] = [str(blank), str(good)]
        passed = routes.gate(ctx, 'tr-menu', cap=5)
        self.assertEqual(Path(passed).read_bytes(), good.read_bytes())
        shots[:] = [str(blank)] * 50
        self.assertFalse(routes.passes(ctx, 'tr-menu', 0.5))
        with self.assertRaises(RuntimeError):
            routes.gate(ctx, 'native-pause', cap=1)
        # The console's auto-dim (about 1/3 the luminance) still passes; a dark unrelated frame doesn't.
        dim = folder / 'dim.png'
        frame.point(lambda v: int(v * 0.33)).save(dim)
        shots[:] = [str(dim)]
        self.assertTrue(routes.passes(ctx, 'tr-menu', 1))
        gray = folder / 'gray.png'
        Image.new('RGB', (1280, 720), (20, 20, 20)).save(gray)
        shots[:] = [str(gray)] * 50
        self.assertFalse(routes.passes(ctx, 'tr-menu', 0.5))


class SwitchDiscriminationTests(unittest.TestCase):
    """Every real-Switch gate passes its own screen, normal and dimmed (the console's ~1/3 auto-dim), and fails
    every other gate's screen content placed at its ROI, normal and dimmed: HOME vs main vs TR menu vs HUD and the
    rest. Built from the committed references only (no game stills in the repo)."""

    @staticmethod
    def frame_with(spec, content, factor):
        from PIL import Image
        frame = Image.new('RGB', (1280, 720))
        x0, y0, x1, y1 = spec['roi']
        frame.paste(content.resize((x1 - x0, y1 - y0)), (x0, y0))
        return frame.point(lambda v: int(v * factor)) if factor != 1 else frame

    def test_every_gate_only_matches_its_own_screen(self):
        from PIL import Image
        folder = Path(tempfile.mkdtemp())
        gates = sorted(routes.SWITCH_REFS)
        self.assertIn('home', gates)
        refs = {g: Image.open(routes._switch_spec(g)['reference']).convert('RGB') for g in gates}
        wrong = []
        for g in gates:
            spec = routes._switch_spec(g)
            for factor in (1, 0.33, 0.35):
                own = folder / f'{g}-own-{factor}.png'
                self.frame_with(spec, refs[g], factor).save(own)
                diff, note = routes.match_frame(own, spec, runner.Runner.compare_region)
                self.assertLessEqual(diff, spec['tolerance'], f'{g} own x{factor}: {diff} {note}')
                for h in gates:
                    if h == g:
                        continue
                    other = folder / f'{g}-as-{h}-{factor}.png'
                    self.frame_with(spec, refs[h], factor).save(other)
                    diff, _ = routes.match_frame(other, spec, runner.Runner.compare_region)
                    if diff <= spec['tolerance']:
                        wrong.append((g, h, factor, round(diff, 1)))
        self.assertEqual(wrong, [], 'a gate passed another screen')


class SessionRecordingTests(unittest.TestCase):
    """plan "session_recording": one continuous recording, marks around every executed row (failed ones too), a
    mark per shot, the stop in run()'s finally; a backend without it runs on without one."""

    class Client:
        def __init__(self, supports=True):
            self.supports, self.calls, self.rec_ms = supports, [], 0

        def request(self, method, params=None):
            self.calls.append(method)
            if method.startswith('recording.session'):
                if not self.supports:
                    return {'ok': False, 'error': {'code': 'unknown_method'}}
                self.rec_ms += 1000
                return {'ok': True, 'result': {'rec_ms': self.rec_ms, 'active': True, 'wall': 1.0,
                                               'path': '/rec.mp4' if method.endswith('stop') else None}}
            if method == 'ui.tree':
                return {'ok': True, 'result': [{'id': 'w', 'class': 'GRenderWindow'}]}
            if method == 'ui.screenshot':
                Path(params['path']).write_bytes(b'png')
            return {'ok': True, 'result': {}}

    def run_plan(self, client, **extra):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'gates': {'never': {'kind': 'log', 'source': 'console_log', 'pattern': 'never', 'cap': 0.3}},
                'session_recording': {'marks': ['row-start', 'row-end', 'shot'], 'chapters': True},
                'rows': [{'id': 'a', 'start': 's', 'end': 's', 'steps': [{'shot': 'one'}]},
                         {'id': 'b', 'start': 's', 'end': 's', 'steps': [{'gate': 'never'}]}], **extra}
        (folder / 'rows.json').write_text(json.dumps(plan))
        log = folder / 'console.log'
        log.write_text('')
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'x', 'console_log': str(log)}}, folder / 'out', 15,
                          client_factory=lambda s: client)
        try:
            r.run()
        except BaseException:
            pass
        return folder / 'out'

    def test_marks_and_stop_even_after_a_failure(self):
        client = self.Client()
        out = self.run_plan(client)
        events = [json.loads(l)['event'] for l in (out / 'recording.jsonl').read_text().splitlines()]
        self.assertEqual(events, ['start', 'row-start', 'shot', 'row-end', 'row-start', 'row-end', 'stop'])
        self.assertEqual(json.loads((out / 'b.json').read_text())['status'], 'FAILED')
        self.assertEqual(len(json.loads((out / 'b.json').read_text())['rec_ms']), 2)
        self.assertEqual(client.calls.count('recording.session_start'), 1)
        self.assertEqual(client.calls[-1] if client.calls[-1].startswith('recording') else
                         [c for c in client.calls if c.startswith('recording')][-1], 'recording.session_stop')

    def test_hardware_gif_is_marked_not_recorded(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'session_recording': {'marks': ['row-start', 'row-end']},
                'rows': [{'id': 'g', 'start': 's', 'end': 's',
                          'steps': [{'gif': {'name': 'clip', 'preset': 'evidence', 'steps': [{'shot': 'mid'}]}}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        client = self.Client()
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'usb:///dev/ttyACM0'}}, folder / 'out', 15,
                          client_factory=lambda s: client)
        r.run()
        events = [json.loads(l) for l in (folder / 'out' / 'recording.jsonl').read_text().splitlines()]
        self.assertEqual([e['event'] for e in events], ['start', 'row-start', 'gif-start', 'gif-end', 'row-end', 'stop'])
        self.assertIn('"gif":"clip"', events[2]['label'])
        self.assertNotIn('recording.start', client.calls)
        self.assertNotIn('recording.stop', client.calls)
        receipt = json.loads((folder / 'out' / 'g.json').read_text())
        self.assertEqual(receipt['status'], 'PASS')
        self.assertIn('deferred', receipt['results'][0]['gif'])

    def test_hardware_gif_without_session_is_refused(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'rows': [{'id': 'g', 'start': 's', 'end': 's',
                                        'steps': [{'gif': {'name': 'clip', 'preset': 'evidence', 'steps': []}}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'usb:///dev/ttyACM0'}}, folder / 'out', 15,
                          client_factory=lambda s: self.Client())
        try:
            r.run()
        except BaseException:
            pass
        receipt = json.loads((folder / 'out' / 'g.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertIn('session recording', receipt['error'])

    def test_backend_without_it_runs_on(self):
        client = self.Client(supports=False)
        out = self.run_plan(client)
        self.assertFalse((out / 'recording.jsonl').exists())
        self.assertEqual(json.loads((out / 'a.json').read_text())['status'], 'PASS')

    def test_dry_run_validates_it(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'session_recording': {'marks': ['bogus'], 'log': '../x'},
                'rows': [{'id': 'a', 'start': 's', 'end': 's', 'steps': [{'shot': 'x'}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        errors = runner.dry_run(folder / 'rows.json')['errors']
        self.assertTrue(any('marks' in e for e in errors), errors)
        self.assertTrue(any('log' in e for e in errors), errors)


class SwitchBootTests(unittest.TestCase):
    def run_boot(self, screens):
        """screens: what each look sees, in order ('main', 'profile', 'launch-error' or None)."""
        folder = Path(tempfile.mkdtemp())
        log = folder / 'skyline.log'
        log.write_text('')
        calls, played = [], []
        seq = list(screens)
        state = {'now': None}

        def passes(ctx, name, cap):
            if name == 'main':  # every look starts with main: take the next screen
                state['now'] = seq.pop(0) if seq else None
            return state['now'] == name

        ctx = FakeCtx.__new__(FakeCtx)
        ctx.runner, ctx.on, ctx.row_dir = type('R', (), {'plan': {'gates': {}}})(), 'sw', folder
        ctx.instance = {'socket': 'usb:///dev/ttyACM0', 'console_log': str(log)}
        ctx.call = lambda method, params=None, job_cap=15: calls.append(method) or {'ok': True}
        ctx.check_stop = lambda: None
        ctx.shot = lambda name: str(folder / f'{name}.png')
        old_passes, old_play = routes.passes, routes.play
        routes.passes, routes.play = passes, lambda c, name: played.append(name)
        try:
            return routes.switch_boot(ctx, cap=5), calls, played
        finally:
            routes.passes, routes.play = old_passes, old_play

    def test_profile_then_start_then_main(self):
        out, calls, played = self.run_boot([None, 'profile', None, 'main'])
        self.assertEqual(calls, ['switch.launch'])
        self.assertEqual(played, ['tap-b1', 'tap-a1', 'attract-cancel'])
        self.assertEqual(out['looks'][-1], 'main')

    def test_launch_error_relaunches_once_then_fails(self):
        out, calls, _ = self.run_boot([None, 'launch-error', 'main'])
        self.assertEqual(calls, ['switch.launch', 'switch.dismiss', 'switch.launch'])
        self.assertIn('erpt 2009-0005', out['notes'][0])
        with self.assertRaises(RuntimeError):
            self.run_boot([None, 'launch-error', 'launch-error'])


class ExpectTests(unittest.TestCase):
    def test_expectations_survive_a_new_runner_process(self):
        folder = Path(tempfile.mkdtemp())

        class R:
            out = folder
        first = FakeCtx.__new__(FakeCtx)
        first.runner = R()
        routes.remember(first, stage_id=3)
        routes.remember(first, p1_kind=0)
        second = FakeCtx.__new__(FakeCtx)
        second.runner = R()  # a retry: a new runner object, nothing in memory
        self.assertEqual(routes.expected(second), {'stage_id': 3, 'p1_kind': 0})


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
