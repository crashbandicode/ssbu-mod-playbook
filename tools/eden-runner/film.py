"""Portable 1080p film capture and ffmpeg-only frame analysis."""

import argparse
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import host_os

FILM_SIZE = (1920, 1080)


def ffmpeg_path():
    env = os.environ.get('EDEN_FFMPEG')
    found = shutil.which('ffmpeg')
    candidates = [env, found, str(Path.home() / 'eden-runs/_eden-test/ffmpeg'),
                  str(Path.home() / '.local/bin/ffmpeg.exe')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError(f'ffmpeg not found: EDEN_FFMPEG={env!r}, PATH={found!r}, '
                       f'{candidates[2]}, {candidates[3]}')


def capture_command(ffmpeg, output, seconds, fps=60, display=None, windows=None):
    windows = host_os.WINDOWS if windows is None else windows
    prefix = [str(ffmpeg), '-hide_banner', '-loglevel', 'warning', '-y']
    if windows:
        if display is not None:
            raise ValueError('Windows capture does not use a display')
        source = ['-f', 'lavfi', '-i', f'ddagrab=framerate={fps}:draw_mouse=0,hwdownload,format=bgra']
    else:
        if not isinstance(display, str) or not re.fullmatch(r':\d+', display):
            raise ValueError('Linux capture requires a display :N')
        source = ['-f', 'x11grab', '-framerate', str(fps), '-video_size', '1920x1080', '-draw_mouse', '0',
                  '-i', f'{display}+0,0']
    return [*prefix, *source, '-t', str(seconds), '-an', '-c:v', 'mjpeg', '-q:v', '3',
            '-pix_fmt', 'yuvj420p', str(output)]


def _write_status(path, status):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(status) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def _record(spec_path):
    spec = json.loads(Path(spec_path).read_text(encoding='utf-8'))
    started = time.time()
    stopped = False
    process = None
    terminate = threading.Event()
    old_handler = None
    if threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGTERM, lambda *_: terminate.set())
    try:
        with Path(spec['log']).open('ab') as log:
            process = subprocess.Popen(spec['command'], stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=0x08000000 if os.name == 'nt' else 0)
            _write_status(spec['status'], {'pid': process.pid, 'started': started})
            while process.poll() is None:
                if terminate.is_set():
                    process.terminate()
                    break
                if Path(spec['stop']).exists():
                    stopped = True
                    try:
                        process.stdin.write(b'q')
                        process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                    break
                time.sleep(0.05)
            try:
                rc = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                rc = process.wait()
        ended = time.time()
        _write_status(spec['status'], {'pid': process.pid, 'started': started, 'ended': ended,
                                       'wall': ended - started, 'returncode': rc, 'stopped_early': stopped})
        return rc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if old_handler is not None:
            signal.signal(signal.SIGTERM, old_handler)


class Recorder:
    def __init__(self, output, seconds, fps=60, display=None, ffmpeg=None):
        # The interactive Windows task runs in the output directory, not the caller's cwd.
        self.output = Path(output).absolute()
        self.seconds = seconds
        self.fps = fps
        self.display = display
        self.ffmpeg = str(ffmpeg or ffmpeg_path())
        self.backend = 'ddagrab' if host_os.WINDOWS else 'x11grab'
        self.spec = self.output.with_suffix('.spec.json')
        self.status = self.output.with_suffix('.recorder.json')
        self.stop = self.output.with_suffix('.stop')
        self.log = self.output.with_suffix('.log')
        self.process = None
        self.task = None
        self.task_xml = None

    def _status(self):
        try:
            return json.loads(self.status.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def running(self):
        return 'returncode' not in self._status() and (self.process is None or self.process.poll() is None)

    def start(self):
        command = capture_command(self.ffmpeg, self.output, self.seconds, self.fps, self.display)
        for path in (self.status, self.stop, self.output):
            path.unlink(missing_ok=True)
        self.spec.write_text(json.dumps({'command': command, 'log': str(self.log), 'status': str(self.status),
                                         'stop': str(self.stop)}) + '\n', encoding='utf-8')
        if host_os.WINDOWS:
            self.task = 'EdenFilm-' + uuid.uuid4().hex[:12]
            self.task_xml = self.output.parent / f'{self.task}.task.xml'
            try:
                host_os.interactive_task_start(
                    self.task, [str(Path(sys.executable).with_name('pythonw.exe')), '-X', 'utf8',
                                str(Path(__file__).resolve()), '_record', str(self.spec)],
                    cwd=self.output.parent, limit_s=self.seconds + 60)
            except Exception:
                self.kill()
                raise
        else:
            self.process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()),
                                             '_record', str(self.spec)])
        until = time.monotonic() + (12 if host_os.WINDOWS else 6)
        while time.monotonic() < until:
            if self.output.is_file() and self.output.stat().st_size:
                return self
            if not self.running():
                break
            time.sleep(0.05)
        self.kill()
        raise RuntimeError(f'1080p capture did not start: {self.output}')

    def stop_early(self):
        self.stop.touch()

    def kill(self):
        if self.task:
            host_os.interactive_task_end(self.task)
            if self.task_xml:
                self.task_xml.unlink(missing_ok=True)
            self.task = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def finish(self, timeout):
        until = time.monotonic() + timeout
        try:
            while time.monotonic() < until:
                status = self._status()
                if 'returncode' in status:
                    if self.process:
                        self.process.wait(timeout=10)
                    return status
                time.sleep(0.05)
            raise RuntimeError(f'1080p capture exceeded its limit: {self.output}')
        finally:
            self.kill()


def film_probe(path, ffmpeg=None, timeout=600):
    path = Path(path)
    command = [str(ffmpeg or ffmpeg_path()), '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(path),
               '-map', '0:v:0', '-f', 'framemd5', '-']
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'ffmpeg framemd5 failed: {exc}') from exc
    if result.returncode:
        raise RuntimeError(f'ffmpeg framemd5 failed: {result.stderr}')
    tb = dimensions = None
    frames = 0
    previous = None
    buckets = []
    last = None
    for line in result.stdout.splitlines():
        if match := re.match(r'#tb 0:\s*(\d+)/(\d+)', line):
            tb = tuple(map(int, match.groups()))
        elif match := re.match(r'#dimensions 0:\s*(\d+)x(\d+)', line):
            dimensions = tuple(map(int, match.groups()))
        elif line and not line.startswith('#'):
            parts = [part.strip() for part in line.split(',')]
            if len(parts) < 6 or tb is None:
                raise RuntimeError('invalid ffmpeg framemd5 output')
            pts, duration = int(parts[2]), int(parts[3])
            second = pts * tb[0] // tb[1]
            while len(buckets) <= second:
                buckets.append(0)
            if parts[5] != previous:
                buckets[second] += 1
            previous = parts[5]
            frames += 1
            last = (pts + duration) * tb[0] / tb[1]
    if not frames or not tb or not dimensions or not last or last <= 0:
        raise RuntimeError('ffmpeg framemd5 produced no frames or usable timing')
    whole = buckets[:int(last)]
    return {'duration': f'{last:.6f}', 'size': str(path.stat().st_size), 'frames': frames,
            'width': dimensions[0], 'height': dimensions[1], 'captured_fps': round(frames / last, 2),
            'distinct_per_second': buckets, 'distinct_fps_min': min(whole) if whole else None,
            'distinct_fps_median': statistics.median(whole) if whole else None, 'tool': 'ffmpeg framemd5'}


@contextmanager
def take(ctx, name, cap, before=0.8, after=1.5, stop_after_body=False, fps=60, on_started=None):
    import routes
    run = Path(ctx.runner.out).parent
    try:
        journal = json.loads((run / 'state' / f'run-{ctx.on}' / 'run.json').read_text())
        if not journal.get('owned_pids'):
            raise ValueError('no owned pids')
    except (OSError, ValueError, AttributeError):
        raise RuntimeError('capture requires the owned run') from None
    display = None
    if not host_os.WINDOWS:
        config = json.loads((run / 'config.json').read_text())
        instance = next((item for item in config.get('instances', []) if item.get('key') == ctx.on), None)
        display = instance.get('display') if instance is not None else None
        if not display:
            raise RuntimeError('capture requires the instance display in config.json')
    output = Path(ctx.row_dir) / f'{name}-1080p-capture.mkv'
    recorder = Recorder(output, cap, fps, display)
    recorder.start()
    origin = time.monotonic()
    cues = []
    completed = False
    try:
        if on_started is not None:
            on_started(display)
        def cue(label):
            cues.append({'label': label, 'seconds': round(time.monotonic() - origin, 3)})
        time.sleep(before)
        yield cue
        time.sleep(after)
        completed = True
    finally:
        ended_early = stop_after_body and not recorder.running()
        if stop_after_body and not ended_early:
            recorder.stop_early()
        status = recorder.finish(15 if stop_after_body else cap + 12)
        try:
            probe = film_probe(output)
            error = None
        except (RuntimeError, OSError, ValueError) as exc:
            probe, error = None, str(exc)
        receipt = {'display': display, 'size': list(FILM_SIZE), 'fps': fps, 'source': str(output),
                   'cues_after_first_bytes': cues, 'capture_status': status['returncode'], 'cap_s': cap,
                   'backend': recorder.backend, 'host_os': 'windows' if host_os.WINDOWS else 'linux',
                   'recorder': status, 'film_probe': probe,
                   'length_vs_cap': round(float(probe['duration']) / cap, 4) if probe else None}
        if error is not None:
            receipt['film_probe_error'] = error
        routes._save(ctx, f'{name}-take', receipt)
        if ended_early and completed:
            raise RuntimeError(f'1080p capture ended before the rehearsal body: {output}')
        if status['returncode']:
            raise RuntimeError(f"1080p capture failed: {output.with_suffix('.log')}")


def fullscreen(ctx):
    def read():
        nodes = ctx.call('ui.tree', {'target': 'action_Fullscreen'}) or []
        action = next((node for node in nodes if 'checked' in node.get('properties', {})), None)
        if action is None:
            raise RuntimeError('fullscreen action is not readable')
        return action['properties']['checked']['value'] is True
    was = read()
    if not was:
        ctx.call('ui.trigger', {'target': 'action_Fullscreen'})
    if not read():
        raise RuntimeError('fullscreen did not stay on')
    return {'fullscreen': True, 'was': was}


def record(ctx, name, seconds, play=None):
    import routes
    with take(ctx, name, seconds) as cue:
        if play is not None:
            cue(play)
            routes.play(ctx, play)
    receipt = json.loads((Path(ctx.row_dir) / f'{name}-take.json').read_text())
    probe = receipt['film_probe']
    if not probe or (probe['width'], probe['height']) != FILM_SIZE:
        raise RuntimeError('1080p capture has no valid 1920x1080 probe')
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    probe = sub.add_parser('probe')
    probe.add_argument('films', nargs='+')
    smoke = sub.add_parser('smoke')
    smoke.add_argument('--out', required=True)
    smoke.add_argument('--seconds', type=int, default=3)
    smoke.add_argument('--display')
    worker = sub.add_parser('_record')
    worker.add_argument('spec')
    args = parser.parse_args(argv)
    if args.action == '_record':
        return _record(args.spec)
    if args.action == 'probe':
        for path in args.films:
            print(json.dumps(film_probe(path)))
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(out / 'smoke-1080p-capture.mkv', args.seconds, display=args.display)
    recorder.start()
    status = recorder.finish(args.seconds + 12)
    result = film_probe(recorder.output)
    print(json.dumps({'backend': recorder.backend, 'status': status, 'film_probe': result}))
    return 0 if status['returncode'] == 0 and (result['width'], result['height']) == FILM_SIZE else 1


if __name__ == '__main__':
    sys.exit(main())
