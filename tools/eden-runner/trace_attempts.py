"""Read the Guard Edge attempt and plan tracepoints identified by the boot packet."""

import json
import math
import os
import struct
import time
from pathlib import Path


TERMINALS = {1: 'KO', 2: 'ledge', 3: 'ground', 4: 'unresolved',
             5: 'ground', 6: 'ledge', 7: 'KO'}


def _decimal(value):
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError('missing decimal')
    return int(value)


def _registers(row):
    registers = row['registers']
    words = [int(registers[f'x{i}'], 16) for i in range(4)]
    if any(word < 0 or word >= 1 << 64 for word in words):
        raise ValueError('register outside u64')
    return words


def _position(pair):
    words = [pair & 0xffffffff, pair >> 32]
    values = [struct.unpack('<f', struct.pack('<I', word))[0] for word in words]
    if not all(math.isfinite(value) for value in values):
        raise ValueError('nonfinite position')
    return values, [f'0x{word:08x}' for word in words]


def attempt(row):
    try:
        if row['schema'] != 1 or row['event'] != 'entry' or not row['process_id']:
            raise ValueError('not an entry')
        meta, clock_damage, cpu_pair, p1_pair = _registers(row)
        event = (meta >> 16) & 255
        if event and event not in TERMINALS:
            raise ValueError('unknown terminal')
        cpu, cpu_words = _position(cpu_pair)
        p1, p1_words = _position(p1_pair)
        damage = struct.unpack('<f', struct.pack('<I', clock_damage & 0xffffffff))[0]
        if not math.isfinite(damage):
            raise ValueError('nonfinite damage')
        return {
            'sequence': _decimal(row['sequence']), 'frame': _decimal(row['frame']),
            'process': str(row['process_id']), 'ordinal': meta & 0xffff,
            'event': event, 'terminal': TERMINALS.get(event),
            'juggler': bool(meta & (1 << 24)), 'perfect': bool(meta & (1 << 25)),
            'stage_form': (meta >> 28) & 15, 'cpu_kind': (meta >> 32) & 0xffff,
            'stage': meta >> 48, 'stage_id': meta >> 48, 'elapsed_frames': clock_damage >> 32,
            'damage': damage, 'cpu': cpu, 'cpu_words': cpu_words, 'p1': p1, 'p1_words': p1_words,
        }
    except (KeyError, TypeError, ValueError, OverflowError, struct.error) as exc:
        raise RuntimeError(f'unreadable attempt marker: {exc}') from exc


def plan(row):
    try:
        if row['schema'] != 1 or row['event'] != 'entry' or not row['process_id']:
            raise ValueError('not an entry')
        route, fighter, crossable, selected = _registers(row)
        return {'sequence': _decimal(row['sequence']), 'frame': _decimal(row['frame']),
                'process': str(row['process_id']), 'route': route & 0xffffffff,
                'fighter': fighter & 0xffffffff, 'crossable': crossable & 0xffffffff,
                'row': selected & 0xffffffff}
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f'unreadable plan marker: {exc}') from exc


def point_id(status, symbol):
    try:
        points = [point for point in status['points']
                  if isinstance(point, dict) and str(point.get('target', '')).endswith('!' + symbol)]
        if (len(points) != 1 or points[0].get('state') != 'armed'
                or points[0].get('mode') != 'records'):
            raise ValueError('missing or ambiguous armed records point')
        return str(_decimal(points[0]['id']))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f'trace point {symbol} is unavailable: {exc}') from exc


class Stream:
    def __init__(self, path, point, planner=None):
        self.path = Path(path)
        self.point = str(point)
        self.planner = None if planner is None else str(planner)
        try:
            info = self.path.stat()
            self.identity = (info.st_dev, info.st_ino)
            self.offset = info.st_size
            with self.path.open('rb') as source:
                opened = os.fstat(source.fileno())
                if (opened.st_dev, opened.st_ino) != self.identity or opened.st_size < self.offset:
                    raise RuntimeError('trace continuity changed')
                if self.offset:
                    source.seek(self.offset - 1)
                    self.partial = source.read(1) != b'\n'
                else:
                    self.partial = False
        except OSError as exc:
            raise RuntimeError(f'trace file unavailable: {exc}') from exc
        self.sequence = None
        self.process = None

    def read(self, deadline=None):
        try:
            info = self.path.stat()
            if (info.st_dev, info.st_ino) != self.identity or info.st_size < self.offset:
                raise RuntimeError('trace continuity changed')
            result = []
            with self.path.open('rb') as source:
                opened = os.fstat(source.fileno())
                if (opened.st_dev, opened.st_ino) != self.identity or opened.st_size < self.offset:
                    raise RuntimeError('trace continuity changed')
                source.seek(self.offset)
                while source.tell() < info.st_size:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    raw = source.readline(info.st_size - source.tell())
                    if not raw or not raw.endswith(b'\n'):
                        break
                    self.offset = source.tell()
                    if self.partial:
                        self.partial = False
                        continue
                    try:
                        row = json.loads(raw)
                        if not isinstance(row, dict):
                            raise ValueError('not an object')
                        if row.get('event') == 'loss':
                            raise RuntimeError('trace loss')
                        seq = _decimal(row['sequence'])
                        if self.sequence is not None and seq != self.sequence + 1:
                            raise RuntimeError('trace sequence gap')
                        self.sequence = seq
                        point = str(row.get('point_id'))
                        if point == self.point or point == self.planner:
                            if row.get('event') not in ('entry', 'return'):
                                raise RuntimeError('unreadable attempt or plan marker event')
                            if row['event'] == 'entry':
                                mark = plan(row) if point == self.planner else attempt(row)
                                mark['record'] = 'plan' if point == self.planner else 'attempt'
                                if self.process is not None and mark['process'] != self.process:
                                    raise RuntimeError('attempt trace process changed')
                                self.process = mark['process']
                                result.append(mark)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RuntimeError(f'unreadable trace marker or record: {exc}') from exc
            return result
        except OSError as exc:
            raise RuntimeError(f'trace file unavailable: {exc}') from exc
