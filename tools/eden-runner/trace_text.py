"""Read text-setter trace records; the caller supplies the point, so the reader is plugin-agnostic.

The default layout is a 256-byte pane object with its name at 0xb0, plus the requested UTF-16-LE text.
Ported from compact-strip's strip_trace.py and notice-ui's notice_steps.py (2026-11-17 / 11-22).
Arming is the caller's boot step: stub off, trace.configure, and symbols.load from the same-build ELF.
"""
import json
import time
from pathlib import Path

NAME_AT = 0xb0
NAME_LEN = 25
MAX_UNITS = 320


def point(target):
    return {'target': target, 'mode': 'records', 'registers': ['x0', 'x1', 'x2', 'pc'],
            'captures': [{'expression': 'x0', 'type': 'bytes', 'size': 256},
                         {'expression': 'x1', 'type': 'bytes', 'size': 640}],
            'max_hits': 0, 'rate_limit': 1000}


def parse(raw, name_at=NAME_AT):
    try:
        record = json.loads(raw)
        if not isinstance(record, dict):
            return None
        caps, regs = record.get('captures'), record.get('registers')
        if (not isinstance(caps, list) or len(caps) != 2 or
                any(not isinstance(cap, dict) or cap.get('error') for cap in caps) or
                not isinstance(regs, dict) or not isinstance(regs.get('x0'), str) or 'x2' not in regs):
            return None
        pane = bytes.fromhex(caps[0].get('hex', ''))
        name = pane[name_at:name_at + NAME_LEN].split(b'\0', 1)[0]
        units = int(regs['x2'], 0) & 0xffffffff
        data = bytes.fromhex(caps[1].get('hex', ''))
        if not name or any(byte < 0x20 or byte > 0x7e for byte in name) or units > MAX_UNITS or len(data) < 2 * units:
            return None
        return {'name': name.decode('ascii'), 'text': data[:2 * units].decode('utf-16-le', 'replace'),
                'sequence': record.get('sequence'), 'frame': record.get('frame'),
                'wall_time_ns': record.get('wall_time_ns'), 'pane': regs['x0'], 'pc': regs.get('pc'),
                'point_id': record.get('point_id')}
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None


def scan(path, name=None, since=0, name_at=NAME_AT, point_id=None):
    try:
        stream = Path(path).open('rb')
    except FileNotFoundError:
        return [], 0
    found = []
    offset = since
    needle = name.encode().hex().encode() if name is not None else None
    with stream:
        if since:
            stream.seek(since - 1)
            if stream.read(1) != b'\n':
                if not stream.readline().endswith(b'\n'):
                    return [], since
                offset = stream.tell()
        while True:
            at = stream.tell()
            raw = stream.readline()
            if not raw or not raw.endswith(b'\n'):
                break
            offset = stream.tell()
            if needle is not None and needle not in raw:
                continue
            record = parse(raw, name_at)
            if (record and (name is None or record['name'] == name) and
                    (point_id is None or (record['point_id'] is not None
                                          and str(record['point_id']) == str(point_id)))):
                found.append(dict(record, at=at))
    return found, offset


def records(path, name=None, since=0):
    return scan(path, name, since)[0]


def current(path, name, *, name_at=NAME_AT, point_id=None):
    found = scan(path, name, name_at=name_at, point_id=point_id)[0]
    return found[-1] if found else None


def bound(path, name, since, wanted):
    found = records(path, name)
    window = [r for r in found if r['at'] >= since]
    older = found[:len(found) - len(window)]
    out = {'pane': name, 'records_before': len(older), 'records': len(window), 'ok': False, 'transition': None,
           'text_before': older[-1]['text'] if older else None}
    if not window:
        return dict(out, why='no record in the window')
    final = window[-1]
    out.update(text=final['text'], sequences=[window[0]['sequence'], final['sequence']],
               frames=[window[0]['frame'], final['frame']])
    if not wanted(final['text']):
        return dict(out, why='the newest text is not the expected one')
    changed = not older or older[-1]['text'] != final['text'] or any(r['text'] != final['text'] for r in window)
    return dict(out, ok=True, transition='measured' if changed else 'unmeasured')


def wait(path, name, wanted, cap, since=0, poll=0.1, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + cap
    while True:
        result = bound(path, name, since, wanted)
        if result['ok']:
            return result
        remaining = deadline - clock()
        if remaining <= 0:
            last = result.get('text', result['text_before'])
            raise TimeoutError(f'{name}: timed out; last text: {last!r}' if last is not None
                               else f'{name}: timed out; no record')
        sleep(min(poll, remaining))
