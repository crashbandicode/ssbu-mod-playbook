#!/usr/bin/env python3
"""Offline tests for routes.py and routes_online.py: a fake Eden and fake plugin logs, no emulator.

Run: python3 -I test_routes.py
"""
import json
import os
import sys
import tempfile
import time
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


class StageTransitionTests(unittest.TestCase):
    def test_transition_deadline_changes_only_the_css_wait(self):
        from unittest.mock import Mock, patch
        ctx = Mock()
        for cap in (60, 180):
            with patch.multiple(routes, wait_sss=Mock(), TileWalker=Mock(), play=Mock(),
                                gate=Mock(), remember=Mock(), _save=Mock()):
                if cap == 60:
                    routes.pick_stage(ctx, "Battlefield")
                else:
                    routes.pick_stage(ctx, "Battlefield", transition_cap=cap)
                routes.gate.assert_called_once_with(ctx, "css", cap)
                routes.wait_sss.assert_called_once_with(ctx)
                routes.TileWalker.return_value.walk_to.assert_called_once_with(1)
                routes.play.assert_called_once_with(ctx, "tap-a1")


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
    BRIDGE_LINES = [
        '[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=v1 port=6970 guards=0x7f frame=0 wall=10',
        '[TrainingRooms] cpu_ids FIGHTER_STATUS_KIND_WAIT=0 FIGHTER_STATUS_KIND_CAPTURE_PULLED=63 frame=1 wall=11',
        '[Bridge] stage id=3 frame=182 wall=12',
        '[Bridge] roster entry=0 kind=0 nana=false frame=182 wall=12',
        '[Bridge] roster entry=1 kind=84 nana=false frame=182 wall=12',
        '[Bridge] item entry=0 held=true kind=429 frame=651 wall=13',
        '[Bridge] speed fps=59.94 ratio=0.9990 frames=599 ms=9994 frame=700 wall=14',
        '[TrainingRooms] state pos entry=0 kind=0 x=-35.999996 y=0.010259508 lr=1 dmg=0 status=0 situation=0 frame=701 wall=15',
        '[TrainingRooms] state pos entry=1 kind=84 x=36 y=0.010259508 lr=-1 dmg=12.5 status=63 situation=2 frame=701 wall=15',
    ]

    def test_bridge_state_parses_every_native_read(self):
        import switch_hw
        state = switch_hw.parse_bridge_state(self.BRIDGE_LINES)
        self.assertEqual((state['frame'], state['wall_ms']), (701, 15))
        self.assertEqual(state['stage'], 3)
        self.assertEqual(state['roster'], {0: {'kind': 0, 'nana': False}, 1: {'kind': 84, 'nana': False}})
        self.assertEqual(state['items'], {0: {'held': True, 'kind': 429}})
        self.assertEqual(state['speed'], {'fps': 59.94, 'ratio': 0.999, 'frames': 599, 'ms': 9994})
        self.assertEqual(state['pos'][1], {'kind': 84, 'x': 36.0, 'y': 0.010259508, 'lr': -1.0, 'dmg': 12.5, 'status': 63,
                                           'situation': 2, 'frame': 701})
        self.assertEqual(state['status_ids'], {'FIGHTER_STATUS_KIND_WAIT': 0, 'FIGHTER_STATUS_KIND_CAPTURE_PULLED': 63})

    def test_bridge_state_newest_line_wins(self):
        import switch_hw
        state = switch_hw.parse_bridge_state(self.BRIDGE_LINES + ['[Bridge] stage id=8 frame=900 wall=20',
                                                                  '[Bridge] item entry=0 held=false kind=-1 frame=901 wall=21'])
        self.assertEqual((state['stage'], state['items'][0]['held'], state['frame']), (8, False, 901))

    def test_bridge_state_fresh_reads_wait_for_their_answers(self):
        import tempfile
        import threading
        import switch_hw
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'bridge').mkdir()
            log = run / 'bridge.log'
            log.write_text(self.BRIDGE_LINES[0] + '\n')

            def bridge():  # the capture: a request file in the spool becomes the bridge's answer in bridge.log
                for _ in range(100):
                    if (run / 'bridge' / 'diag-pos.req').exists() and (run / 'bridge' / 'bridge-speed.req').exists():
                        with log.open('a') as f:
                            f.write('[Bridge] req pos result=ok entries=0x3 frame=700 wall=14\n')
                            f.write('\n'.join(self.BRIDGE_LINES[6:]) + '\n')
                        return
                    time.sleep(0.02)

            worker = threading.Thread(target=bridge)
            worker.start()
            state = switch_hw.bridge_state(run, fresh=['pos', 'speed'], cap=5)
            worker.join()
            self.assertEqual(sorted(state['pos']), [0, 1])
            self.assertEqual(state['speed']['fps'], 59.94)
            with self.assertRaisesRegex(RuntimeError, r"no answer for \['roster'\]"):
                switch_hw.bridge_state(run, fresh=['roster'], cap=0.3)

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

    def test_up_refuses_when_obs_cannot_split_per_row(self):
        import contextlib
        import io
        import switch_hw
        folder = Path(tempfile.mkdtemp())
        split = {'ok': False, 'why': 'simple_file_output.split_file is None: manual file splitting is off'}
        calls = []

        class Client:
            def request(self, method, params=None):
                calls.append(method)
                if method == 'ui.screenshot':
                    Path(params['path']).write_bytes(b'png')
                if method == 'recording.split_check':
                    return {'ok': True, 'result': dict(split)}
                return {'ok': True, 'result': {'version': 'x'}}

            def close(self):
                pass
        saved = (switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen)
        switch_hw.CLAIM = folder / 'claim'
        switch_hw.holders = lambda device: []
        switch_hw._client = lambda device: Client()
        switch_hw.subprocess.Popen = lambda *a, **k: type('P', (), {'pid': 4242})()
        try:
            with self.assertRaises(SystemExit) as cm:
                switch_hw.up(folder / 'run', '/dev/null', 'h:1', 'test')
            self.assertIn('split-enable', str(cm.exception))
            self.assertFalse((folder / 'claim').exists())  # released on the refusal
            self.assertNotIn('SetOutputSettings', calls)    # read-only: up never changes OBS
            with contextlib.redirect_stdout(io.StringIO()):
                switch_hw.up(folder / 'run2', '/dev/null', 'h:1', 'test', allow_no_split=True)
            journal = json.loads((folder / 'run2' / 'journal.json').read_text())
            self.assertFalse(journal['obs_split']['ok'])
            (folder / 'claim').unlink()
            split['ok'], split['why'] = True, None
            with contextlib.redirect_stdout(io.StringIO()):
                switch_hw.up(folder / 'run3', '/dev/null', 'h:1', 'test')
            self.assertTrue(json.loads((folder / 'run3' / 'journal.json').read_text())['obs_split']['ok'])
        finally:
            switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen = saved

    def test_bridge_spool_turns_eden_request_files_into_bridge_requests(self):
        import base64
        import time
        import switch_hw
        spool = Path(tempfile.mkdtemp())
        for name, data in [('diag-settings-next.json', b'{"menu":{}}'), ('diag-pos.req', b''), ('diag-item.req', b' 7\n'),
                           ('bridge-file.req', b'save_states.json\nlibrary/a.json\n'), ('diag-save.req', b''),
                           ('diag-load.req', b''), ('settings.json', b'{}')]:
            (spool / name).write_bytes(data)
            time.sleep(0.002)
        lines = switch_hw.spool_requests(spool)
        self.assertEqual(lines, [b'set ' + base64.b64encode(b'{"menu":{}}'), b'pos', b'item 7', b'file save_states.json',
                                 b'file library/a.json', b'save', b'load'])
        self.assertEqual(sorted(p.name for p in spool.iterdir()), ['settings.json'])  # requests consumed, the mirror kept
        self.assertEqual(switch_hw.spool_requests(spool / 'missing'), [])

    def test_bridge_mirror_writes_settings_and_keeps_lines_whole(self):
        import base64
        import hashlib
        import io
        import switch_hw
        run = Path(tempfile.mkdtemp())
        blog, clog = io.BytesIO(), io.BytesIO()
        mirror = switch_hw.BridgeMirror(run, [blog, clog])
        body = b'{"menu": {"input_delay": [3]}}' * 20
        for seq, start in enumerate(range(0, len(body), 384)):
            mirror.line(b'[Bridge] file_data name=settings.json seq=%d b64=' % seq + base64.b64encode(body[start:start + 384]))
        digest = hashlib.sha256(body).hexdigest().encode()
        mirror.line(b'[Bridge] file_end name=settings.json bytes=%d sha256=%s result=ok frame=9 wall=10\r' % (len(body), digest))
        self.assertEqual((run / 'bridge' / 'settings.json').read_bytes(), body)
        self.assertEqual((run / 'bridge' / 'files' / 'settings.json').read_bytes(), body)
        mirror.line(b'[Bridge] file_end name=x.json bytes=1 sha256=' + b'0' * 64 + b' result=ok')
        mirror.line(b'[Bridge] file_data name=cut.json seq=0 b64=Zm9vYm')  # a chunk cut short
        mirror.line(b'[Bridge] file_end name=cut.json bytes=4 sha256=' + hashlib.sha256(b'foob').hexdigest().encode() + b' result=ok')
        self.assertFalse((run / 'bridge' / 'files' / 'cut.json').exists())
        self.assertFalse((run / 'bridge' / 'files' / 'x.json').exists())
        mirror.line(b'[TrainingRooms] settings.json written by the writer thread frame=12 wall=13')
        mirror.line(b'[Ui] press A page=3 cursor=0 depth=1 screen=Page row=None recording= frame=14 wall=15')
        self.assertEqual(mirror.wanted, [b'file settings.json'])
        text = blog.getvalue().decode()
        self.assertEqual(text, clog.getvalue().decode())
        self.assertNotIn('file_data', text)
        self.assertIn(' mirrored=true\n', text)
        self.assertIn('x.json bytes=1 sha256=' + '0' * 64 + ' result=ok mirrored=false\n', text)
        self.assertTrue(text.endswith('recording= frame=14 wall=15\n'))

    def test_capture_bridge_sends_hello_and_spool_then_reconnects(self):
        import switch_hw
        run = Path(tempfile.mkdtemp())
        console = run / 'skyline.log'
        (run / 'bridge').mkdir()
        (run / 'bridge' / 'diag-pos.req').write_bytes(b'')
        sent, calls = [], []
        chunks = [[b'[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 frame=0 wall=1\n[TrainingRooms] state pos entry=0 kind=0 ',
                   b'x=1 y=2 lr=1 dmg=0 status=0 situation=0 frame=5 wall=6\n']]

        class Sock:
            def __init__(self, data):
                self.data = list(data)

            def settimeout(self, t):
                pass

            def sendall(self, b):
                sent.append(b)

            def recv(self, n):
                return self.data.pop(0) if self.data else b''

            def close(self):
                pass

        def connect(addr, timeout):
            calls.append(addr)
            if not chunks:
                raise OSError('refused')
            return Sock(chunks.pop(0))
        switch_hw.capture_bridge(run, 'h', 6970, console, stop=lambda: len(calls) >= 2, connect=connect, retry_s=0)
        self.assertEqual(sent[0], b'backup\nfile settings.json\n')
        self.assertIn(b'pos\n', sent)
        log = (run / 'bridge.log').read_text()
        self.assertIn('[TrainingRooms] state pos entry=0 kind=0 x=1 y=2 lr=1 dmg=0 status=0 situation=0 frame=5 wall=6\n', log)
        self.assertEqual(console.read_text(), log)
        self.assertEqual(json.loads((run / 'bridge.requests.jsonl').read_text().splitlines()[0])['request'], 'pos')
        import fcntl
        with (run / 'bridge.lock').open('w') as held:  # a second capture of the same run is refused
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(SystemExit):
                switch_hw.capture_bridge(run, 'h', 6970, console, stop=lambda: True, connect=connect, retry_s=0)

    def test_read_state_runs_unchanged_on_a_bridge_instance(self):
        import threading
        run = Path(tempfile.mkdtemp())
        (run / 'bridge').mkdir()
        log = run / 'bridge.log'
        log.write_text('[TrainingRooms] state pos entry=0 kind=9 x=0 y=0 lr=1 dmg=0 status=0 situation=0 frame=1 wall=1\n')
        instance = {'socket': 'usb:///dev/ttyACM0', 'diag_log': str(log), 'settings': str(run / 'bridge' / 'settings.json')}
        ctx = type('Ctx', (), {'instance': instance, 'check_stop': lambda self: None})()

        def bridge():
            for _ in range(100):
                if (run / 'bridge' / 'diag-pos.req').exists():
                    with log.open('a') as f:
                        f.write('[TrainingRooms] state pos entry=1 kind=66 x=-65.37304 y=0 lr=-1 dmg=12.5 status=27 situation=2 frame=8 wall=9\n')
                        f.write('[TrainingRooms] state pos entry=0 kind=0 x=6.5219345 y=0 lr=1 dmg=0 status=0 situation=0 frame=8 wall=9\n')
                    return
                threading.Event().wait(0.02)
        t = threading.Thread(target=bridge)
        t.start()
        bodies = routes.read_state(ctx, cap=3)
        t.join()
        self.assertEqual(bodies[0]['kind'], 0)  # the stale entry-0 line before the request is not used
        self.assertEqual((bodies[1]['kind'], bodies[1]['x'], bodies[1]['lr'], bodies[1]['status']), (66, -65.37304, -1.0, 27))

    def test_up_with_a_bridge_gives_the_instance_eden_keys(self):
        import contextlib
        import io
        import switch_hw
        folder = Path(tempfile.mkdtemp())
        popen = []

        class Client:
            def request(self, method, params=None):
                if method == 'ui.screenshot':
                    Path(params['path']).write_bytes(b'png')
                return {'ok': True, 'result': {'version': 'x', 'ok': True}}

            def close(self):
                pass
        saved = (switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen)
        switch_hw.CLAIM = folder / 'claim'
        switch_hw.holders = lambda device: []
        switch_hw._client = lambda device: Client()
        switch_hw.subprocess.Popen = lambda args, **k: popen.append(args) or type('P', (), {'pid': 4242 + len(popen)})()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                switch_hw.up(folder / 'run', '/dev/null', 'h:1', 'test', bridge_host='192.168.0.224:6970')
            sw = json.loads((folder / 'run' / 'instances.json').read_text())['sw']
            self.assertEqual(sw['diag_log'], str(folder / 'run' / 'bridge.log'))
            self.assertEqual(Path(sw['settings']).parent, folder / 'run' / 'bridge')
            self.assertTrue(Path(sw['bridge_files']).is_dir())
            self.assertIn('capture-bridge', popen[1])
            self.assertEqual(popen[1][popen[1].index('--console') + 1], sw['console_log'])
            self.assertEqual(json.loads((folder / 'run' / 'journal.json').read_text())['bridge_pid'], 4244)
        finally:
            switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen = saved

    def test_an_unrestored_backup_is_restored_before_a_new_one(self):
        import io
        import switch_hw
        mirror = switch_hw.BridgeMirror(Path(tempfile.mkdtemp()), [io.BytesIO()])
        mirror.line(b'[Bridge] req backup result=refused reason=backup_open frame=0 wall=1')
        self.assertEqual(mirror.wanted, [b'restore', b'backup'])
        mirror.line(b'[Bridge] req backup result=refused reason=backup_open frame=0 wall=2')
        self.assertEqual(mirror.wanted, [b'restore', b'backup'], 'once per connection, never a loop')

    def test_down_restores_the_fixture_and_keeps_a_receipt(self):
        import switch_hw
        run = Path(tempfile.mkdtemp())
        sent = []

        class Sock:
            def __init__(self):
                self.data = [b'[Bridge] hello x\n[Bridge] req restore result=ok files=3 written=1 removed=0 frame=1 wall=2\n']

            def settimeout(self, t):
                pass

            def sendall(self, b):
                sent.append(b)

            def recv(self, n):
                return self.data.pop(0) if self.data else b''

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        receipt = switch_hw.restore_fixture(run, {'bridge_host': 'h:6970'}, connect=lambda a, timeout: Sock())
        self.assertTrue(receipt['ok'])
        self.assertEqual(sent, [b'restore\n'])
        self.assertTrue(json.loads((run / 'bridge-restore.json').read_text())['ok'])
        failed = switch_hw.bridge_request('h', 1, 'restore', cap=0.5, connect=lambda a, timeout: (_ for _ in ()).throw(OSError()))
        self.assertIsNone(failed)

    def test_a_failed_restore_keeps_the_claim_and_fails_loudly(self):
        from unittest import mock
        import switch_hw
        run = Path(tempfile.mkdtemp())
        claim = run / 'claim.json'
        claim.write_text(json.dumps({'run': str(run)}))
        (run / 'journal.json').write_text(json.dumps({'device': 'x', 'claim': str(claim), 'bridge_host': 'h:6970'}))
        with mock.patch.object(switch_hw, '_client', side_effect=OSError('no pad')), \
                mock.patch.object(switch_hw, 'restore_fixture', return_value={'ok': False, 'restore': None}), \
                mock.patch.object(switch_hw.time, 'sleep'):
            with self.assertRaises(SystemExit):
                switch_hw.down(run)
        self.assertTrue(claim.is_file(), 'the console stays claimed')
        self.assertIn('fixture restore incomplete', json.loads(claim.read_text())['dirty'])
        self.assertFalse(json.loads((run / 'journal.json').read_text())['down_result']['released'])
        with mock.patch.object(switch_hw, '_client', side_effect=OSError('no pad')), \
                mock.patch.object(switch_hw, 'restore_fixture', return_value={'ok': True, 'restore': 'ok'}), \
                mock.patch.object(switch_hw.time, 'sleep'):
            switch_hw.down(run)
        self.assertFalse(claim.is_file(), 'a verified restore releases it')

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


class PreloadNoticeTests(unittest.TestCase):
    """TR's preloader notice (switch-hw-session-1 T-01, 15:03Z: an R lost at "Fighters / Loading 4/7"): the
    classifier and the preload_clear gate, on frames built from the committed notice references only."""

    BOX = (310, 290, 970, 387)

    @staticmethod
    def ref(name):
        from PIL import Image
        entry = json.loads((routes.ROUTES / 'refs' / 'hw-refs.json').read_text())[name]
        return Image.open(routes.ROUTES / 'refs' / f'{name}.png').convert('RGB'), entry['roi']

    def frame(self, box=True, header=True, word='Loading', dx=0, dim=1.0, menu=False):
        from PIL import Image, ImageDraw
        im = Image.new('RGB', (1280, 720), (13, 13, 13) if menu else (212, 212, 212))
        draw = ImageDraw.Draw(im)
        if not menu:
            for x in range(0, 1280, 48):  # the Training stage grid
                draw.line([(x, 0), (x, 720)], fill=(150, 150, 150), width=2)
            for y in range(0, 720, 48):
                draw.line([(0, y), (1280, y)], fill=(150, 150, 150), width=2)
        else:
            draw.text((60, 140), 'Reactions (4)   Mash   0 of 30', fill=(230, 230, 230))
        if box:
            draw.rectangle(self.BOX, fill=(13, 13, 13))
            if header:
                head, roi = self.ref('hw-notice-fighters')
                im.paste(head, tuple(roi[:2]))
            if word:
                img, roi = self.ref('hw-notice-loading')
                if word == 'Load':  # "Loaded n/m" and "All loaded" share it: blank "ing" (from column 37)
                    img = img.copy()
                    ImageDraw.Draw(img).rectangle((37, 0, img.size[0], img.size[1]), fill=(13, 13, 13))
                elif word == 'Loaded':  # "Load" + an "e"-like (the "o") + the "d", glyph-aligned
                    base = img.copy()
                    img = base.copy()
                    ImageDraw.Draw(img).rectangle((37, 0, img.size[0], img.size[1]), fill=(13, 13, 13))
                    img.paste(base.crop((9, 0, 19, base.size[1])), (38, 0))
                    img.paste(base.crop((28, 0, 37, base.size[1])), (49, 0))
                elif word == 'other':  # some other second line ("Ready 7/7", "Swap P1 once to load")
                    img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                im.paste(img, (roi[0] + dx, roi[1]))
        if dim != 1.0:
            im = im.point(lambda v: int(v * dim))
        return im

    def save(self, im, folder, name):
        path = Path(folder) / f'{name}.png'
        im.save(path)
        return path

    def test_classifier(self):
        folder = tempfile.mkdtemp()
        cases = {
            'loading': dict(), 'loading-right': dict(dx=24), 'loading-left': dict(dx=-30),
            'loading-dim': dict(dim=0.33),
            'final-load': dict(word='Load'), 'final-loaded': dict(word='Loaded'), 'final-other': dict(word='other'),
            'final-blank': dict(word=None), 'final-dim': dict(word='Load', dim=0.35),
            'other-notice': dict(header=False, word='other'), 'stage': dict(box=False),
            'stage-dim': dict(box=False, dim=0.33), 'menu': dict(box=False, menu=True),
            'menu-with-box-pixels': dict(menu=True)}
        want = {'loading': 'loading', 'loading-right': 'loading', 'loading-left': 'loading', 'loading-dim': 'loading',
                'final-load': 'fighters', 'final-loaded': 'fighters', 'final-other': 'fighters',
                'final-blank': 'fighters', 'final-dim': 'fighters', 'other-notice': 'other', 'stage': None,
                'stage-dim': None, 'menu': None, 'menu-with-box-pixels': None}
        got = {name: routes.preload_notice(self.save(self.frame(**kw), folder, name)) for name, kw in cases.items()}
        self.assertEqual(got, want)

    def test_word_margin(self):
        """The "Loading" word stays far from its look-alikes: the limit (0.3) sits between them."""
        word, _ = self.ref('hw-notice-loading')
        ref = routes._ink(word)
        self.assertEqual(routes.ink_mismatch(ref, ref), 0.0)
        for kw in (dict(word='Load'), dict(word='Loaded')):
            im = self.frame(**kw)
            _, roi = self.ref('hw-notice-loading')
            self.assertGreater(routes.ink_mismatch(routes._ink(im.crop(tuple(roi))), ref), 0.4, kw)

    def ctx_with_frames(self, frames, on_look=None):
        out = Path(tempfile.mkdtemp())
        log = out / 'skyline.log'
        log.write_text('[NRO hook] Loaded richter. BindFlag: Lazy\n')
        fake = FakeRunner({'socket': 'usb:///dev/ttyACM0', 'console_log': str(log)}, None)
        fake.out = str(out)
        row = out / 'S-BOOT'
        row.mkdir()
        ctx = FakeCtx(fake, row)
        seq = list(frames)
        looks = []

        def shot(name):
            kw = seq.pop(0) if len(seq) > 1 else seq[0]
            looks.append(kw)
            if on_look:
                on_look(len(looks), log)
            return self.save(self.frame(**kw), row, f'{name}-{len(looks)}')
        ctx.shot = shot
        return ctx, log, looks

    def test_gate_waits_through_gaps_for_the_final_notice(self):
        none, loading, final = dict(box=False), dict(), dict(word='Load')
        # HUD before the preloader starts, Loading, a gap between steps, Loading again, the final notice.
        ctx, log, looks = self.ctx_with_frames([none, loading, none, none, loading, final])
        result = routes.preload_clear(ctx, cap=30, appear=60)
        self.assertEqual(result['preload'], 'final notice')
        self.assertEqual(len(looks), 6)
        self.assertEqual([k for _, k in result['looks']], [None, 'loading', None, 'loading', 'fighters'])
        record = json.loads((Path(ctx.runner.out) / 'routes-preload.json').read_text())
        self.assertEqual(record['size'], log.stat().st_size)
        # Same scene: passes at once, no look.
        again = routes.preload_clear(ctx, cap=30, appear=60)
        self.assertEqual(again['preload'], 'done earlier')
        self.assertEqual(len(looks), 6)
        # Unloads (helpers given back) don't re-arm it; a new load (new scene, fighter change) does.
        with log.open('a') as f:
            f.write('[NRO hook] Unloaded lucas.\n')
        self.assertEqual(routes.preload_clear(ctx, cap=30, appear=60)['preload'], 'done earlier')
        with log.open('a') as f:
            f.write('[NRO hook] Loaded mario. BindFlag: Lazy\n')
        self.assertEqual(routes.preload_clear(ctx, cap=30, appear=60)['preload'], 'final notice')
        self.assertEqual(len(looks), 7)

    def test_no_notice_needs_the_appear_window_and_a_quiet_log(self):
        def loads(n, log):
            if n <= 4:
                with log.open('a') as f:
                    f.write(f'[NRO hook] Loaded fighter{n}. BindFlag: Lazy\n')
        ctx, _, looks = self.ctx_with_frames([dict(box=False)], on_look=loads)
        result = routes.preload_clear(ctx, cap=30, appear=1.0)
        self.assertEqual(result['preload'], 'no notice')
        self.assertGreaterEqual(len(looks), 5)  # not before the loads stopped
        self.assertGreaterEqual(result['seconds'], 1.0)

    def test_a_standing_other_notice_is_no_preload(self):
        ctx, _, _ = self.ctx_with_frames([dict(header=False, word='other')])
        self.assertEqual(routes.preload_clear(ctx, cap=30, appear=0.6)['preload'], 'no notice')

    def test_gate_fails_while_loading_never_ends(self):
        ctx, _, _ = self.ctx_with_frames([dict()])
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            routes.preload_clear(ctx, cap=1, appear=60)

    def test_refs_are_pinned(self):
        for name in ('hw-notice-fighters', 'hw-notice-loading'):
            self.assertIsNotNone(routes._notice_ref(name)[0])


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

    class SplitClient:
        """recording.session_split like switch-remote's OBS client: a new file per acknowledged split."""

        def __init__(self, folder, ack=lambda label: True):
            self.folder, self.ack, self.calls, self.n, self.rec_ms, self.cur = folder, ack, [], 0, 0, None

        def new_file(self):
            self.n += 1
            path = self.folder / f'rec-{self.n}.mp4'
            path.write_bytes(b'mp4' * self.n)
            return str(path)

        def request(self, method, params=None):
            self.calls.append(method)
            params = params or {}
            if method == 'recording.session_start':
                self.cur = self.new_file()
                return {'ok': True, 'result': {'started_wall': 1.0, 'already_active': False, 'path': self.cur}}
            if method == 'recording.session_split':
                self.rec_ms += 1000
                base = {'active': True, 'rec_ms': self.rec_ms, 'wall': 1.0, 'closed': [], 'late_paths': []}
                if not self.ack(params['label']):
                    return {'ok': True, 'result': {**base, 'split_ok': False, 'error': 'split_unacknowledged'}}
                prev, self.cur = self.cur, self.new_file()
                return {'ok': True, 'result': {**base, 'split_ok': True, 'new_path': self.cur, 'prev_path': prev,
                                               'file_rec_ms': self.rec_ms + 40,
                                               'closed': [{'path': prev, 'finalized': True}]}}
            if method == 'recording.session_mark':
                self.rec_ms += 1000
                return {'ok': True, 'result': {'active': True, 'rec_ms': self.rec_ms, 'wall': 1.0}}
            if method == 'recording.session_stop':
                return {'ok': True, 'result': {'path': self.cur, 'finalized': True, 'stopped_wall': 2.0,
                                               'late_paths': []}}
            if method == 'ui.tree':
                return {'ok': True, 'result': [{'id': 'w', 'class': 'GRenderWindow'}]}
            if method == 'ui.screenshot':
                Path(params['path']).write_bytes(b'png')
            return {'ok': True, 'result': {}}

    def run_split_plan(self, rows, ack=lambda label: True):
        folder = Path(tempfile.mkdtemp())
        client = self.SplitClient(folder, ack)
        plan = {'start': 's', 'gates': {'never': {'kind': 'log', 'source': 'console_log', 'pattern': 'never', 'cap': 0.3}},
                'session_recording': {'marks': ['row-start', 'row-end', 'shot'], 'per_row_files': True},
                'rows': [{'id': rid, 'start': 's', 'end': 's', 'steps': steps} for rid, steps in rows]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        log = folder / 'console.log'
        log.write_text('')
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'x', 'console_log': str(log)}}, folder / 'out', 15,
                          client_factory=lambda s: client)
        try:
            r.run()
        except BaseException:
            pass
        out = folder / 'out'
        events = [json.loads(l) for l in (out / 'recording.jsonl').read_text().splitlines()]
        return folder, out, events, client

    def test_per_row_files_split_at_each_row_start(self):
        folder, out, events, client = self.run_split_plan([('a', [{'shot': 'one'}]), ('b', [])])
        self.assertEqual([(e['event'], e.get('row')) for e in events],
                         [('start', None), ('row-start', 'a'), ('clip', None), ('shot', 'a'), ('row-end', 'a'),
                          ('row-start', 'b'), ('clip', 'a'), ('row-end', 'b'), ('stop', None), ('clip', 'b')])
        self.assertEqual(client.calls.count('recording.session_split'), 2)
        self.assertEqual(client.calls.count('recording.session_mark'), 3)  # shot, row-end x2 stay plain marks
        start_a = events[1]
        self.assertTrue(start_a['split']['ok'])
        self.assertEqual((start_a['rec_ms'], start_a['split']['requested_rec_ms']), (1040, 1000))
        pre = events[2]
        self.assertEqual((pre['path'], pre['clip']), (str(folder / 'rec-1.mp4'), None))  # before any row: no clip
        for row, n in (('a', 2), ('b', 3)):
            receipt = json.loads((out / f'{row}.json').read_text())
            self.assertEqual(receipt['clip'], str(out / row / 'clip.mp4'))
            self.assertEqual(receipt['clip_source'], str(folder / f'rec-{n}.mp4'))
            self.assertTrue(receipt['clip_finalized'])
            # a hard link: the same file, never a move (the original recording stays)
            self.assertTrue((folder / f'rec-{n}.mp4').is_file())
            self.assertTrue((out / row / 'clip.mp4').samefile(folder / f'rec-{n}.mp4'))
        self.assertEqual(events[-1]['clip'], str(out / 'b' / 'clip.mp4'))
        self.assertEqual(client.calls.count('recording.session_start'), 1)
        self.assertNotIn('recording.start', client.calls)
        self.assertNotIn('recording.stop', client.calls)

    def test_unacknowledged_split_fails_loudly_and_the_row_has_no_clip(self):
        folder, out, events, client = self.run_split_plan([('a', []), ('b', []), ('c', [])],
                                                           ack=lambda label: not label.startswith('b:'))
        start_b = [e for e in events if e['event'] == 'row-start' and e['row'] == 'b'][0]
        self.assertEqual((start_b['split']['ok'], start_b['split']['error']), (False, 'split_unacknowledged'))
        clips = {e['row']: e for e in events if e['event'] == 'clip'}
        self.assertNotIn('b', clips)
        self.assertNotIn('clip', json.loads((out / 'b.json').read_text()))
        # a's file went on through b: a's clip says so
        self.assertEqual(clips['a']['rows'], ['a', 'b'])
        self.assertEqual(json.loads((out / 'a.json').read_text())['clip_spans_rows'], ['a', 'b'])
        self.assertEqual(clips['c']['path'], str(folder / 'rec-3.mp4'))
        actions = (out / 'actions.jsonl').read_text()
        self.assertIn('"session_split": "failed"', actions)
        # never a Start/Stop substitute mid-session
        self.assertEqual((client.calls.count('recording.session_start'), client.calls.count('recording.session_stop')),
                         (1, 1))

    def test_failed_rows_get_their_clip_and_the_stop_closes_the_last(self):
        # b fails and the run stops there (rows after it never run): the stop closes b's file
        folder, out, events, client = self.run_split_plan([('a', []), ('b', [{'gate': 'never'}]), ('c', [])])
        self.assertFalse((out / 'c.json').exists())
        receipt = json.loads((out / 'b.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertEqual(receipt['clip'], str(out / 'b' / 'clip.mp4'))
        self.assertTrue((out / 'b' / 'clip.mp4').samefile(folder / 'rec-3.mp4'))
        self.assertEqual([e['event'] for e in events[-2:]], ['stop', 'clip'])
        self.assertEqual(events[-1]['row'], 'b')
        self.assertEqual(json.loads((out / 'a.json').read_text())['clip'], str(out / 'a' / 'clip.mp4'))

    def test_dry_run_validates_per_row_files(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'session_recording': {'marks': ['shot'], 'per_row_files': 'yes', 'split_cap_s': 0},
                'rows': [{'id': 'a', 'start': 's', 'end': 's', 'steps': [{'shot': 'x'}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        errors = runner.dry_run(folder / 'rows.json')['errors']
        self.assertTrue(any('per_row_files must be' in e for e in errors), errors)
        self.assertTrue(any('split_cap_s' in e for e in errors), errors)
        plan['session_recording'] = {'marks': ['shot'], 'per_row_files': True}
        (folder / 'rows.json').write_text(json.dumps(plan))
        errors = runner.dry_run(folder / 'rows.json')['errors']
        self.assertTrue(any('keep it in marks' in e for e in errors), errors)

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
