"""Read text-setter trace records; the caller supplies the point, so the reader is plugin-agnostic.

The default layout is a 256-byte pane object with its name at 0xb0, plus the requested UTF-16-LE text.
Ported from compact-strip's strip_trace.py and notice-ui's notice_steps.py (2026-11-17 / 11-22).
Arming is the caller's boot step: stub off, trace.configure, and symbols.load from the same-build ELF.
"""
import json
import re
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


def decimal(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]+', value):
        try:
            return int(value)
        except ValueError:
            pass
    return None


def pane_name(captures, name_at):
    if (not isinstance(captures, list) or not captures or
            not isinstance(captures[0], dict) or captures[0].get('error')):
        return None
    try:
        pane = bytes.fromhex(captures[0].get('hex', ''))
        name = pane[name_at:name_at + NAME_LEN].split(b'\0', 1)[0]
        if not name or any(byte < 0x20 or byte > 0x7e for byte in name):
            return None
        return name.decode('ascii')
    except (TypeError, ValueError):
        return None


def parse_key(raw):
    try:
        record = json.loads(raw)
        if not isinstance(record, dict):
            return None
        regs = record.get('registers')
        if not isinstance(regs, dict) or not isinstance(regs.get('x0'), str):
            return None
        return regs['x0'], record.get('point_id')
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None


def parse(raw, name_at=NAME_AT):
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    caps, regs = record.get('captures'), record.get('registers')
    name = pane_name(caps, name_at)
    if name is None:
        return None
    regs = regs if isinstance(regs, dict) else {}
    pane = regs.get('x0') if isinstance(regs.get('x0'), str) else None
    result = {'name': name, 'sequence': decimal(record.get('sequence')), 'frame': decimal(record.get('frame')),
              'wall_time_ns': record.get('wall_time_ns'), 'pane': pane, 'pc': regs.get('pc'),
              'point_id': record.get('point_id')}
    try:
        value = regs.get('x2')
        units = (int(value, 0) if isinstance(value, str) else value if type(value) is int else None)
        units = units & 0xffffffff if units is not None else None
    except ValueError:
        units = None
    if pane is None:
        return dict(result, text=None, known=False, why='no pane address')
    if units is None:
        return dict(result, text=None, known=False, why='no length register')
    if units > MAX_UNITS:
        return dict(result, text=None, known=False, why='text length exceeds limit')
    text = ''
    if units:
        capture = caps[1] if len(caps) > 1 and isinstance(caps[1], dict) else {}
        if capture.get('error'):
            return dict(result, text=None, known=False, why=f'text capture error: {capture["error"]}')
        try:
            data = bytes.fromhex(capture.get('hex', ''))
        except (TypeError, ValueError):
            data = b''
        if len(data) < 2 * units:
            return dict(result, text=None, known=False, why='text capture too short')
        text = data[:2 * units].decode('utf-16-le', 'replace')
    if result['sequence'] is None or result['frame'] is None:
        return dict(result, text=None, known=False, why='no sequence or frame')
    return dict(result, text=text, known=True)


def matches_point(record, point_id):
    return point_id is None or record['point_id'] is None or str(record['point_id']) == str(point_id)


def scan(path, name=None, since=0, name_at=NAME_AT, point_id=None, keys=()):
    try:
        stream = Path(path).open('rb')
    except FileNotFoundError:
        return [], 0
    found = []
    offset = since
    needle = name.encode().hex().encode() if name is not None else None
    keys = set(keys)
    key_needles = {key.encode() for key in keys}
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
            if (needle is not None and needle not in raw and b'"loss"' not in raw
                    and not any(key in raw for key in key_needles)):
                continue
            record = None
            if name is not None and b'"loss"' in raw:
                try:
                    source = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
                    source = None
                if isinstance(source, dict) and source.get('event') == 'loss':
                    record = {'name': name, 'text': None, 'known': False,
                              'why': f'trace loss ({source.get("records")} records)', 'loss': True,
                              'sequence': None, 'frame': None, 'pane': None, 'point_id': None,
                              'wall_time_ns': None, 'pc': None}
            if record is None:
                record = parse(raw, name_at)
            if record is None and name is not None and keys:
                key = parse_key(raw)
                if key and key[0] in keys:
                    source = json.loads(raw)
                    if pane_name(source.get('captures'), name_at) is None:
                        record = {'name': name, 'text': None, 'known': False, 'why': 'pane capture unreadable',
                                  'sequence': decimal(source.get('sequence')), 'frame': decimal(source.get('frame')),
                                  'wall_time_ns': source.get('wall_time_ns'), 'pane': key[0],
                                  'pc': source['registers'].get('pc'), 'point_id': key[1]}
            if record and (name is None or record['name'] == name) and matches_point(record, point_id):
                if point_id is not None and record['point_id'] is None and record['known']:
                    record = dict(record, text=None, known=False, why='no point id')
                found.append(dict(record, at=at))
                if name is not None and isinstance(record['pane'], str):
                    keys.add(record['pane'])
                    key_needles.add(record['pane'].encode())
    return found, offset


def records(path, name=None, since=0):
    return [record for record in scan(path, name, since)[0] if record['known']]


def current(path, name, *, name_at=NAME_AT, point_id=None):
    found = scan(path, name, name_at=name_at, point_id=point_id)[0]
    return found[-1] if found else None


def transition(found, since=0):
    final = found[-1]
    prior = [record for record in found[:-1] if record['known'] and record['pane'] == final['pane']]
    earlier = [record for record in prior if record['at'] >= since]
    older = [record for record in prior if record['at'] < since]
    previous = next((record for record in reversed(earlier) if record['text'] != final['text']), None)
    previous = ({key: previous[key] for key in ('text', 'sequence', 'frame', 'pane')} if previous else None)
    before_scope = ({key: older[-1][key] for key in ('text', 'sequence', 'frame', 'pane')} if older else None)
    return ('measured' if previous else 'unmeasured' if earlier else 'sampled'), previous, before_scope


def bound(path, name, since, wanted):
    found = scan(path, name)[0]
    window = [r for r in found if r['at'] >= since]
    older = found[:len(found) - len(window)]
    older_known = [record for record in older if record['known']]
    out = {'pane': name, 'records_before': len(older), 'records': len(window), 'ok': False, 'transition': None,
           'text_before': older_known[-1]['text'] if older_known else None, 'previous': None, 'before_scope': None}
    if not window:
        return dict(out, why='no record in the window')
    final = window[-1]
    kind, previous, before_scope = transition(found, since)
    out.update(text=final['text'], sequences=[window[0]['sequence'], final['sequence']],
               frames=[window[0]['frame'], final['frame']], pane_key=final['pane'], before_scope=before_scope)
    if not final['known']:
        return dict(out, why='the newest sample for this pane is unreadable',
                    unknown={key: final[key] for key in ('sequence', 'frame', 'why')})
    out['previous'] = previous
    if not wanted(final['text']):
        return dict(out, why='the newest text is not the expected one')
    return dict(out, ok=True, transition=kind, previous=previous)


def wait(path, name, wanted, cap, since=0, poll=0.1, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + cap
    while True:
        result = bound(path, name, since, wanted)
        if result['ok']:
            return result
        remaining = deadline - clock()
        if remaining <= 0:
            if 'unknown' in result:
                raise TimeoutError(f'{name}: timed out; newest sample unreadable: {result["unknown"]["sequence"]}')
            last = result.get('text', result['text_before'])
            raise TimeoutError(f'{name}: timed out; last text: {last!r}' if last is not None
                               else f'{name}: timed out; no record')
        sleep(min(poll, remaining))
