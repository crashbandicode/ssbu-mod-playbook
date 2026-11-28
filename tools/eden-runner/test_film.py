import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import chdir
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import film
import host_os
import routes
import runner


class FilmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_capture_command_linux(self):
        self.assertEqual(film.capture_command('ffmpeg', 'a.mkv', 3, 60, ':23', False),
                         ['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y', '-f', 'x11grab',
                          '-framerate', '60', '-video_size', '1920x1080', '-draw_mouse', '0', '-i',
                          ':23+0,0', '-t', '3', '-an', '-c:v', 'mjpeg', '-q:v', '3', '-pix_fmt',
                          'yuvj420p', 'a.mkv'])
        for value in (None, ':x', ':1+2,3'):
            with self.assertRaises(ValueError):
                film.capture_command('ffmpeg', 'a.mkv', 3, display=value, windows=False)

    def test_capture_command_windows(self):
        self.assertEqual(film.capture_command('ffmpeg', 'a.mkv', 3, 60, None, True),
                         ['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y', '-f', 'lavfi', '-i',
                          'ddagrab=framerate=60:draw_mouse=0,hwdownload,format=bgra', '-t', '3',
                          '-an', '-c:v', 'mjpeg', '-q:v', '3', '-pix_fmt', 'yuvj420p', 'a.mkv'])
        with self.assertRaises(ValueError):
            film.capture_command('ffmpeg', 'a.mkv', 3, display=':1', windows=True)

    def test_probe_parses_framemd5(self):
        path = self.root / 'film.mkv'
        path.write_bytes(b'abc')
        header = '#tb 0: 1/60\n#dimensions 0: 1920x1080\n'
        hashes = [str(i if i < 60 or i >= 120 else 60 + (i - 60) // 2) for i in range(150)]
        text = header + ''.join(f'0, {i}, {i}, 1, 100, {value}\n' for i, value in enumerate(hashes))
        with mock.patch.object(film.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, text, '')):
            result = film.film_probe(path, 'ffmpeg')
        self.assertEqual((result['frames'], result['duration'], result['captured_fps']), (150, '2.500000', 60.0))
        self.assertEqual(result['distinct_per_second'], [60, 30, 30])
        self.assertEqual((result['distinct_fps_min'], result['distinct_fps_median']), (30, 45))
        self.assertEqual((result['width'], result['height'], result['size']), (1920, 1080, '3'))
        text = '#tb 0: 1/1000\n#dimensions 0: 320x180\n' + ''.join(
            f'0, {i * 20}, {i * 20}, 20, 1, {i}\n' for i in range(75))
        with mock.patch.object(film.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, text, '')):
            result = film.film_probe(path, 'ffmpeg')
        self.assertEqual((result['duration'], result['captured_fps'], result['distinct_per_second']),
                         ('1.500000', 50.0, [50, 25]))
        self.assertEqual((result['distinct_fps_min'], result['distinct_fps_median']), (50, 50))

    def test_probe_failure_and_no_frames(self):
        path = self.root / 'film.mkv'
        path.write_bytes(b'x')
        with mock.patch.object(film.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'bad film')):
            with self.assertRaisesRegex(RuntimeError, 'bad film'):
                film.film_probe(path, 'ffmpeg')
        with mock.patch.object(film.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '#tb 0: 1/60\n', '')):
            with self.assertRaisesRegex(RuntimeError, 'no frames'):
                film.film_probe(path, 'ffmpeg')

    def test_probe_real_film(self):
        try:
            ffmpeg = film.ffmpeg_path()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        path = self.root / 'test.mkv'
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                        '-i', 'testsrc=size=320x180:rate=60', '-t', '2', '-c:v', 'mjpeg',
                        '-threads', '1', '-pix_fmt', 'yuvj420p', str(path)], check=True, timeout=60)
        result = film.film_probe(path, ffmpeg)
        self.assertEqual((result['frames'], result['width'], result['height']), (120, 320, 180))
        self.assertEqual(result['distinct_per_second'], [60, 60])

    def context(self):
        out = self.root / 'out'
        row = out / 'row'
        row.mkdir(parents=True)
        state = self.root / 'state' / 'run-offline'
        state.mkdir(parents=True)
        (state / 'run.json').write_text(json.dumps({'owned_pids': [{'pid': 123}]}))
        (self.root / 'config.json').write_text(json.dumps({'instances': [{'key': 'offline', 'display': ':123'}]}))
        return SimpleNamespace(row_dir=row, on='offline', runner=SimpleNamespace(out=out), check_stop=lambda: None)

    def fake_ffmpeg(self, fails=False):
        path = self.root / 'ffmpeg'
        if fails:
            text = '#!/usr/bin/python3\nimport sys\nsys.exit(1)\n'
        else:
            text = '''#!/usr/bin/python3
import select, sys, time
from pathlib import Path
Path(sys.argv[-1]).write_bytes(b'film')
end = time.monotonic() + float(sys.argv[sys.argv.index('-t') + 1])
while time.monotonic() < end:
    ready, _, _ = select.select([sys.stdin], [], [], min(0.05, end - time.monotonic()))
    if ready and sys.stdin.read(1) == 'q':
        break
'''
        path.write_text(text)
        path.chmod(0o755)
        return path

    @unittest.skipIf(os.name == 'nt', 'POSIX fake ffmpeg executable')
    def test_take_flow_with_a_fake_ffmpeg(self):
        ctx = self.context()
        fake = self.fake_ffmpeg()
        probe = {'duration': '0.300000', 'width': 1920, 'height': 1080}
        with mock.patch.dict(os.environ, {'EDEN_FFMPEG': str(fake)}), \
                mock.patch.object(film, 'film_probe', return_value=probe):
            with film.take(ctx, 'clip', 0.3, before=0, after=0) as cue:
                cue('one')
                cue('two')
        receipt = json.loads((ctx.row_dir / 'clip-take.json').read_text())
        self.assertEqual(set(receipt), {'display', 'size', 'fps', 'source', 'cues_after_first_bytes',
                                       'capture_status', 'cap_s', 'backend', 'host_os', 'recorder',
                                       'film_probe', 'length_vs_cap'})
        self.assertEqual((receipt['display'], receipt['size'], receipt['fps'], receipt['capture_status']),
                         (':123', [1920, 1080], 60, 0))
        self.assertEqual([c['label'] for c in receipt['cues_after_first_bytes']], ['one', 'two'])
        self.assertEqual(receipt['source'], str(ctx.row_dir / 'clip-1080p-capture.mkv'))
        self.assertTrue((ctx.row_dir / 'clip-1080p-capture.log').is_file())
        self.assertEqual(receipt['length_vs_cap'], 1)

    @unittest.skipIf(os.name == 'nt', 'POSIX fake ffmpeg executable')
    def test_take_stop_after_body_ends_the_film_early(self):
        ctx = self.context()
        fake = self.fake_ffmpeg()
        start = time.monotonic()
        with mock.patch.dict(os.environ, {'EDEN_FFMPEG': str(fake)}), \
                mock.patch.object(film, 'film_probe', return_value={'duration': '1', 'width': 1920, 'height': 1080}):
            with film.take(ctx, 'early', 30, before=0, after=0, stop_after_body=True):
                pass
        receipt = json.loads((ctx.row_dir / 'early-take.json').read_text())
        self.assertTrue(receipt['recorder']['stopped_early'])
        self.assertEqual(receipt['capture_status'], 0)
        self.assertLess(time.monotonic() - start, 5)

    def test_take_refuses_a_run_it_does_not_own(self):
        ctx = self.context()
        (self.root / 'state' / 'run-offline' / 'run.json').write_text('{"owned_pids":[]}')
        with self.assertRaisesRegex(RuntimeError, 'capture requires the owned run'):
            with film.take(ctx, 'refused', 3):
                pass

    def test_take_refuses_missing_display(self):
        ctx = self.context()
        with mock.patch.object(host_os, 'WINDOWS', False):
            for instances in ([], [{'key': 'offline'}]):
                (self.root / 'config.json').write_text(json.dumps({'instances': instances}))
                with self.assertRaisesRegex(RuntimeError, '^capture requires the instance display in config.json$'):
                    with film.take(ctx, 'refused', 3):
                        pass

    @unittest.skipIf(os.name == 'nt', 'POSIX fake ffmpeg executable')
    def test_take_reports_a_recorder_that_never_starts(self):
        ctx = self.context()
        with mock.patch.dict(os.environ, {'EDEN_FFMPEG': str(self.fake_ffmpeg(fails=True))}):
            with self.assertRaisesRegex(RuntimeError, '1080p capture did not start'):
                with film.take(ctx, 'bad', 3, before=0, after=0):
                    pass

    def test_windows_recorder_goes_through_the_interactive_task(self):
        output = self.root / 'film.mkv'
        calls = []
        class FakeProcess:
            pid = 42
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0
        def popen(command, **kwargs):
            Path(command[-1]).write_bytes(b'film')
            return FakeProcess()
        def start(name, argv, cwd, limit_s):
            calls.append((name, argv, cwd, limit_s))
            xml = Path(cwd) / f'{name}.task.xml'
            xml.write_text('task')
            thread = threading.Thread(target=film._record, args=(argv[-1],))
            thread.start()
            thread.join()
            return xml
        with mock.patch.object(host_os, 'WINDOWS', True), \
                mock.patch.object(host_os, 'interactive_task_start', side_effect=start), \
                mock.patch.object(host_os, 'interactive_task_running'), \
                mock.patch.object(host_os, 'interactive_task_end') as end, \
                mock.patch.object(film.subprocess, 'Popen', side_effect=popen):
            recorder = film.Recorder(output, 3, ffmpeg='ffmpeg')
            recorder.start()
            status = recorder.finish(5)
        name, argv, cwd, limit = calls[0]
        self.assertRegex(name, r'^EdenFilm-[0-9a-f]{12}$')
        self.assertEqual(Path(argv[0]).name, 'pythonw.exe')
        self.assertEqual(argv[1:3], ['-X', 'utf8'])
        self.assertEqual((cwd, limit, status['returncode']), (self.root, 63, 0))
        end.assert_called_once_with(name)
        self.assertFalse((self.root / f'{name}.task.xml').exists())

    def test_windows_relative_smoke_output_is_readable_from_task_cwd(self):
        out = self.root / 'takes'
        out.mkdir()
        def start(name, argv, cwd, limit_s):
            with chdir(cwd):
                spec = json.loads(Path(argv[-1]).read_text())
                self.assertEqual(Path(spec['command'][-1]).resolve(), out / 'clip.mkv')
                Path(spec['command'][-1]).write_bytes(b'movie')
                Path(spec['status']).write_text('{"returncode": 0}')
        with chdir(self.root), mock.patch.object(host_os, 'WINDOWS', True), \
                mock.patch.object(host_os, 'interactive_task_start', side_effect=start), \
                mock.patch.object(host_os, 'interactive_task_end'):
            rec = film.Recorder('takes/clip.mkv', 3, ffmpeg='ffmpeg')
            rec.start()
            self.assertEqual(rec.finish(1)['returncode'], 0)

    def test_fullscreen_hook(self):
        class Ctx:
            def __init__(self, checked=False):
                self.checked = checked
                self.calls = []
            def call(self, method, params):
                self.calls.append((method, params))
                if method == 'ui.tree':
                    return [{'name': 'action_Fullscreen', 'properties': {'checked': {'value': self.checked}}}]
                self.checked = True
        ctx = Ctx()
        self.assertEqual(film.fullscreen(ctx), {'fullscreen': True, 'was': False})
        self.assertEqual(ctx.calls, [('ui.tree', {'target': 'action_Fullscreen'}),
                                     ('ui.trigger', {'target': 'action_Fullscreen'}),
                                     ('ui.tree', {'target': 'action_Fullscreen'})])
        ctx.calls.clear()
        self.assertEqual(film.fullscreen(ctx), {'fullscreen': True, 'was': True})
        self.assertEqual(ctx.calls, [('ui.tree', {'target': 'action_Fullscreen'}),
                                     ('ui.tree', {'target': 'action_Fullscreen'})])
        missing = SimpleNamespace(call=lambda method, params: [])
        with self.assertRaisesRegex(RuntimeError, '^fullscreen action is not readable$'):
            film.fullscreen(missing)
        stuck = SimpleNamespace(call=lambda method, params: [{'properties': {'checked': {'value': False}}}])
        with self.assertRaisesRegex(RuntimeError, '^fullscreen did not stay on$'):
            film.fullscreen(stuck)

    def test_shared_hook_registration(self):
        self.assertIn('film', runner.SHARED_HOOKS)
        self.assertEqual(runner.load_step(self.root, 'film:record').__name__, 'record')


if __name__ == '__main__':
    unittest.main()
