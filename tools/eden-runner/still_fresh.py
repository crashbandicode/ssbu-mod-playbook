"""Record screenshot provenance and detect images frozen across game frames."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


_BOOT = re.compile(rb'\bboot=(0x[0-9a-fA-F]+)\b')
_FRAME = re.compile(rb' frame=(\d+) wall=(\d+)$')
_TAIL_BYTES = 64 * 1024


def game_frame(bridge_log):
    if bridge_log is None:
        return None
    try:
        with Path(bridge_log).open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - _TAIL_BYTES)
            stream.seek(start)
            lines = stream.read().splitlines()
    except (OSError, TypeError, ValueError):
        return None
    boot, frame = None, None
    for line in lines:
        for value in _BOOT.findall(line):
            boot = value.decode('ascii')
        if match := _FRAME.search(line):
            frame = (int(match[1]), int(match[2]))
    if frame is None:
        return None
    return {'boot': boot, 'frame': frame[0], 'wall': frame[1]}


class Ledger:
    def __init__(self, out_dir, frames=180):
        self.out_dir = Path(out_dir)
        self.frames = frames
        self.runs = {}

    def record(self, on, path, reply, frame_source):
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
        with (self.out_dir / 'stills.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record) + '\n')
        self.runs[on] = {'sha256': digest, 'boot': boot, 'frame': frame,
                         'first_file': first_file, 'first_frame': first_frame}
        return record


def refused(record):
    return isinstance(record, dict) and record.get('status') == 'refused'
