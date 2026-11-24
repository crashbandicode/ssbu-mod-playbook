"""Record screenshot provenance and detect images frozen across game frames."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


_BOOT = re.compile(rb'\bboot=(0x[0-9a-fA-F]+)\b')
_FRAME = re.compile(rb' frame=(\d+) wall=(\d+)$')
_READ_BYTES = 1024 * 1024


class FrameSource:
    def __init__(self, path):
        self.path = Path(path) if path is not None else None
        self._reset()

    def _reset(self):
        self.identity = None
        self.offset = 0
        self.partial = b''
        self.boot = self.frame = self.wall = None

    def _line(self, line):
        for value in _BOOT.findall(line):
            boot = value.decode('ascii')
            if boot != self.boot:
                self.boot = boot
                self.frame = self.wall = None
        if match := _FRAME.search(line.rstrip(b'\r')):
            self.frame, self.wall = int(match[1]), int(match[2])

    def read(self):
        if self.path is None:
            self._reset()
            return None
        for _ in range(2):
            try:
                with self.path.open('rb') as stream:
                    stat = os.fstat(stream.fileno())
                    identity = (stat.st_dev, stat.st_ino)
                    if identity != self.identity or stat.st_size < self.offset:
                        self._reset()
                        self.identity = identity
                    stream.seek(self.offset)
                    while self.offset < stat.st_size:
                        data = stream.read(min(_READ_BYTES, stat.st_size - self.offset))
                        if not data:
                            break
                        self.offset += len(data)
                        lines = (self.partial + data).split(b'\n')
                        self.partial = lines.pop()
                        for line in lines:
                            self._line(line)
                    current = self.path.stat()
                    if (self.offset < stat.st_size or (current.st_dev, current.st_ino) != identity
                            or current.st_size < self.offset):
                        self._reset()
                        continue
            except (OSError, TypeError, ValueError):
                self._reset()
                return None
            if self.frame is None:
                return None
            return {'boot': self.boot, 'frame': self.frame, 'wall': self.wall}
        return None


class Ledger:
    def __init__(self, out_dir, frames=180):
        self.out_dir = Path(out_dir)
        self.frames = frames
        self.runs = {}

    def record(self, on, path, reply, frame_source, *, commit=None):
        path = Path(path)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        source = frame_source if isinstance(frame_source, dict) else {}
        frame, wall, boot = source.get('frame'), source.get('wall'), source.get('boot')
        reply = reply if isinstance(reply, dict) else {}
        previous = self.runs.get(on)
        first_file, first_frame = path.name, frame
        unchanged = None
        reason = None
        if not isinstance(frame, int) or not isinstance(wall, int):
            status, reason = 'unverified', 'no game frame source'
            first_frame = None
        elif boot is None:
            status, reason = 'unverified', 'no game boot source'
            first_frame = None
        elif (previous is not None and previous['boot'] == boot
              and previous['frame'] is not None and frame < previous['frame']):
            status, reason = 'unverified', 'game frame went backwards'
        elif previous is None or digest != previous['sha256'] or (
                previous['boot'] is not None and boot != previous['boot']):
            status, unchanged = 'fresh', 0
        elif previous['frame'] is None or previous['first_frame'] is None:
            status, reason = 'unverified', 'no game frame source for identical run'
        else:
            first_file, first_frame = previous['first_file'], previous['first_frame']
            unchanged = frame - first_frame
            if unchanged >= self.frames:
                status = 'refused'
                reason = f'unchanged across {unchanged} game frames since {first_file}'
            else:
                status = 'same'
        record = {'on': on, 'file': path.name, 'path': str(path), 'sha256': digest, 'bytes': len(data),
                  'utc': datetime.now(timezone.utc).isoformat(), 'boot': boot, 'frame': frame, 'wall': wall,
                  'backend': reply.get('backend'), 'uniform': reply.get('uniform'), 'warning': reply.get('warning'),
                  'status': status, 'reason': reason, 'unchanged_frames': unchanged, 'first_identical': first_file}
        run = {'sha256': digest, 'boot': boot, 'frame': frame,
               'first_file': first_file, 'first_frame': first_frame}
        if commit is None:
            self.append(record)
            self.runs[on] = run
        else:
            commit(record, run)
        return record

    def append(self, record):
        with (self.out_dir / 'stills.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record) + '\n')


def refused(record):
    return isinstance(record, dict) and record.get('status') == 'refused'


def usable(record):
    return isinstance(record, dict) and record.get('status') in ('fresh', 'same', 'unverified')


def unusable_reason(record):
    if not isinstance(record, dict) or 'status' not in record:
        return 'no freshness record'
    return record.get('reason') or f'invalid freshness status: {record["status"]!r}'


def evidence_issue(record):
    prefix = 'still refused as evidence' if refused(record) else 'still not usable as evidence'
    return f'{prefix}: {unusable_reason(record)}'
