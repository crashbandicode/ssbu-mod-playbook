#!/usr/bin/env python3
"""Shared Eden test runner: plays recorded macro segments row by row, with capped gates and a receipt per row.

A test run writes only a row list (rows.json) and, at Up, an instances file. This tool does the rest:

  runner.py dry-run <rows.json>                      offline: files, hashes, gates, row chain; lists missing segments
  runner.py run <rows.json> --instances <inst.json>  play the rows on the live instances

rows.json (paths are relative to its folder):
  {
    "game_id": "01006A800016E000",
    "start": "main-menu",                      # screen the first row starts on
    "segments": {"to-training": {"file": "segments/to-training.edenmacro.json", "sha256": "...", "cap": 60,
                                 "recipe": [{"buttons": "a", "hold": 150, "gap": 0.6}, {"stick": [0, -1, 250]}]}},
    "gates": {"training": {"kind": "image", "reference": "refs/training.png", "sha256": "...",
                           "roi": [x0, y0, x1, y1], "tolerance": 0, "cap": 30},
              "pause-log": {"kind": "log", "source": "console_log", "pattern": "paused=1", "cap": 10},
              "sdi-saved": {"kind": "json", "source": "settings", "expect": {"menu.sdi_state": 1}, "cap": 5},
              "css": {"kind": "hook", "module": "checks/css_check.py", "function": "check", "cap": 20}},
    "rows": [{"id": "t1", "start": "main-menu", "end": "training",
              "steps": [{"play": "to-training"}, {"gate": "training"}, {"shot": "training"}],
              "evidence": ["training screenshot"]}]
  }

instances.json (written by whoever ran Up): {"a": {"socket": ".../control.sock", "console_log": ".../console.log",
  "settings": ".../TrainingRooms/settings.json"}}. Steps run on "on": "<key>", default the first key.

Steps: play <segment> (macro.play, wait for finished, input.reset; a segment whose file is missing but has a recipe is
recorded instead: macro.record, the recipe's inputs, input.reset, macro.stop, then its sha256 is written back to rows.json),
gate <gate>, shot <name> (screenshot into the row folder), mark <name> (screenshot a "before" for a change gate),
copy <instance key> [as <name>] (copy that instance file into the row folder), json_diff {source, before, allowed,
must_change, must_keep} (compare the file now with an earlier copy), wait_ms <ms> (only where no gate exists).

More gates:
  {"kind": "change", "mark": "hud-before", "roi": [...], "min_diff": 8, "stable": true, "cap": 10}
      the region differs from the row's mark and has stopped moving (HUD portrait swapped, menu value changed)
  {"kind": "log", "source": "detector_log", "pattern": "sync_end .*frame=(\\d+) .*desync_seen=(\\d+)",
   "on_all": ["a", "b"], "equal": true, "expect_groups": {"2": "0"}, "cap": 120}
      every listed instance logged a match this row, with the same captured values and desync_seen=0

Run rules (playbook docs/MACROS.md, "Test runs"): rows run back to back while their gates pass; the first failure writes
STOP-INPUT and the runner exits. A STOP file in the output folder stops it before its next input. If no row has finished
--pace-minutes after the first run started, it writes STOP-INPUT. Every control call goes to out/actions.jsonl; each row
writes out/<row>.json and its media under out/<row>/. Rerunning skips rows whose receipt passed and resumes at the next.
"""
import argparse
import hashlib
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

EDENCTL_DIR = '/home/intpa/Applications/Eden-GIF/current/tools'
GATE_KINDS = ('image', 'log', 'json', 'hook', 'change')
# Mean absolute difference per colour byte (0-255) an image gate accepts. Exact matches (0) fail on animated screens
# (the title's attract mode, menu shimmer, a blinking cursor); 2026-11-03 pkg4 lost two row-1 attempts to one.
DEFAULT_TOLERANCE = 8


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def dotted(document, key):
    value = document
    for part in key.split('.'):
        value = value[part]
    return value


def load_hook(base, gate):
    spec = importlib.util.spec_from_file_location('gate_hook_' + Path(gate['module']).stem, base / gate['module'])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, gate['function'])


def dry_run(manifest_path):
    """Offline only. Raises on a broken manifest; returns the report (missing segments are not an error)."""
    base = manifest_path.parent
    plan = json.loads(manifest_path.read_text())
    errors, missing, available = [], [], []
    for name, seg in plan.get('segments', {}).items():
        path = base / seg['file']
        if path.is_file():
            if seg.get('sha256') != digest(path):
                errors.append(f'segment {name}: sha256 mismatch')
                continue
            macro = json.loads(path.read_text())
            if plan.get('game_id') and macro.get('game_id') != plan['game_id']:
                errors.append(f'segment {name}: game_id {macro.get("game_id")}')
            if not macro.get('events'):
                errors.append(f'segment {name}: no events')
            available.append(name)
        elif seg.get('recipe'):
            missing.append(name)
        else:
            errors.append(f'segment {name}: file missing and no recipe')
    for name, gate in plan.get('gates', {}).items():
        kind = gate.get('kind')
        if kind not in GATE_KINDS:
            errors.append(f'gate {name}: kind {kind}')
            continue
        if not gate.get('cap', 0) > 0:
            errors.append(f'gate {name}: needs a cap > 0')
        try:
            if kind == 'image':
                ref = base / gate['reference']
                if digest(ref) != gate['sha256']:
                    errors.append(f'gate {name}: reference sha256 mismatch')
                if len(gate['roi']) != 4:
                    errors.append(f'gate {name}: roi needs 4 numbers')
                if gate.get('tolerance', DEFAULT_TOLERANCE) == 0 and not gate.get('static'):
                    errors.append(f'gate {name}: tolerance 0 needs "static": true (exact matches fail on animated screens)')
            elif kind == 'log':
                pattern = re.compile(gate['pattern'])
                for number in gate.get('expect_groups', {}):
                    if int(number) > pattern.groups:
                        errors.append(f'gate {name}: expect_groups {number} but the pattern has {pattern.groups} groups')
                if gate.get('equal') and pattern.groups == 0:
                    errors.append(f'gate {name}: "equal" needs capture groups in the pattern')
            elif kind == 'change':
                if len(gate['roi']) != 4 or not gate.get('mark'):
                    errors.append(f'gate {name}: change needs "mark" and a 4-number roi')
            elif kind == 'json':
                if not gate.get('expect'):
                    errors.append(f'gate {name}: empty expect')
            elif kind == 'hook':
                load_hook(base, gate)
        except Exception as error:  # report every broken gate, not just the first
            errors.append(f'gate {name}: {error!r}')
    state = plan['start']
    ids = set()
    for row in plan['rows']:
        if row['id'] in ids:
            errors.append(f'row {row["id"]}: duplicate id')
        ids.add(row['id'])
        if row['start'] != state:
            errors.append(f'row {row["id"]}: starts on {row["start"]} but the row before ends on {state}')
        if not row.get('steps'):
            errors.append(f'row {row["id"]}: no steps')
        marks, copies = set(), set()
        for step in row.get('steps', []):
            on = step.get('on', '*')
            if 'play' in step and step['play'] not in plan.get('segments', {}):
                errors.append(f'row {row["id"]}: unknown segment {step["play"]}')
            if 'gate' in step:
                gate = plan.get('gates', {}).get(step['gate'])
                if gate is None:
                    errors.append(f'row {row["id"]}: unknown gate {step["gate"]}')
                elif gate.get('kind') == 'change' and (on, gate.get('mark')) not in marks:
                    errors.append(f'row {row["id"]}: change gate {step["gate"]} before its mark {gate.get("mark")!r}')
            if 'mark' in step:
                marks.add((on, step['mark']))
            if 'copy' in step:
                copies.add((on, step.get('as', step['copy'])))
            if 'json_diff' in step and (on, step['json_diff'].get('before')) not in copies:
                errors.append(f'row {row["id"]}: json_diff before {step["json_diff"].get("before")!r} has no earlier copy')
        state = row['end']
    report = {'manifest': str(manifest_path), 'manifest_sha256': digest(manifest_path), 'rows': len(plan['rows']),
              'available_segments': available, 'missing_segments': missing, 'gates': len(plan.get('gates', {})),
              'errors': errors,
              'result': 'BROKEN' if errors else ('READY_TO_RECORD' if missing else 'READY')}
    return report


class StopRun(Exception):
    pass


class Runner:
    def __init__(self, manifest_path, instances, out, pace_minutes, client_factory=None):
        self.manifest_path = manifest_path
        self.base = manifest_path.parent
        self.plan = json.loads(manifest_path.read_text())
        self.instances = instances
        self.default = next(iter(instances))
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.row_dir = self.out
        if client_factory is None:
            sys.path.insert(0, EDENCTL_DIR)
            from edenctl import Client
            client_factory = lambda socket: Client(socket, 10)  # noqa: E731
        self.clients = {key: client_factory(inst['socket']) for key, inst in instances.items()}
        pace = self.out / 'pace.json'
        if not pace.exists():
            pace.write_text(json.dumps({'started': time.time(), 'first_row_done': None}) + '\n')
        self.pace = json.loads(pace.read_text())
        self.pace_seconds = pace_minutes * 60

    # --- control -------------------------------------------------------------------------------------------------
    def log(self, record):
        record['time'] = time.time()
        with (self.out / 'actions.jsonl').open('a') as f:
            f.write(json.dumps(record) + '\n')

    def check_stop(self):
        for name in ('STOP', 'STOP-INPUT'):
            if (self.out / name).exists():
                raise StopRun(f'{name} present: ' + (self.out / name).read_text().strip()[:200])
        if not self.pace['first_row_done'] and time.time() - self.pace['started'] > self.pace_seconds:
            raise StopRun(f'pace: no row finished {self.pace_seconds // 60} minutes after the run started')

    def call(self, on, method, params=None):
        if method not in ('macro.stop', 'input.reset'):
            self.check_stop()
        reply = self.clients[on].request(method, params or {})
        self.log({'on': on, 'method': method, 'params': params, 'ok': reply.get('ok')})
        if not reply.get('ok'):
            raise RuntimeError(f'{method} on {on}: {reply}')
        value = reply.get('result')
        if isinstance(value, dict) and value.get('accepted') and value.get('job'):
            until = time.monotonic() + 15
            while True:
                job = self.clients[on].request('jobs.get', {'job': value['job']})
                state = (job.get('result') or {}).get('state')
                if state in ('done', 'completed', 'finished'):
                    break
                if state in ('failed', 'error') or not job.get('ok'):
                    raise RuntimeError(f'{method} job on {on}: {job}')
                if time.monotonic() > until:
                    raise RuntimeError(f'{method} job on {on} still {state} after 15 s')
                time.sleep(.1)
        return value

    def shot(self, on, name):
        tree = self.call(on, 'ui.tree', {'brief': True, 'visible_only': True}) or []
        target = next((n['id'] for n in tree if n.get('class') == 'GRenderWindow'), 'all')
        path = self.row_dir / f'{on}-{time.strftime("%H%M%S")}-{time.time_ns() % 10**6:06d}-{name}.png'
        self.call(on, 'ui.screenshot', {'target': target, 'path': str(path)})
        return path

    # --- steps ---------------------------------------------------------------------------------------------------
    def wait_macro(self, on, cap):
        until = time.monotonic() + cap
        while time.monotonic() < until:
            status = self.call(on, 'macro.status') or {}
            if status.get('error'):
                raise RuntimeError(f'macro error on {on}: {status}')
            if status.get('state') == 'finished':
                self.call(on, 'input.reset')
                return
            time.sleep(.2)
        raise RuntimeError(f'macro on {on} not finished within {cap} s')

    def record(self, on, name, seg):
        path = self.base / seg['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        self.call(on, 'macro.record', {'name': name, 'path': str(path), 'player': seg.get('player', 0),
                                       'countdown_ms': 0})
        try:
            for action in seg['recipe']:
                if 'buttons' in action:
                    hold = action.get('hold', 150)
                    buttons = action['buttons'].split(',')
                    for b in buttons:
                        self.call(on, 'input.button', {'button': b, 'state': 'press', 'lease_ms': hold + 500})
                    time.sleep(hold / 1000)
                    for b in buttons:
                        self.call(on, 'input.button', {'button': b, 'state': 'release'})
                elif 'stick' in action:
                    x, y, ms = action['stick']
                    self.call(on, 'input.stick', {'stick': 'left', 'x': x, 'y': y, 'lease_ms': ms})
                    time.sleep(ms / 1000)
                    self.call(on, 'input.stick', {'stick': 'left', 'x': 0, 'y': 0, 'lease_ms': 100})
                time.sleep(action.get('gap', .6))
        finally:
            self.call(on, 'input.reset')
            time.sleep(.12)  # one neutral frame before the recording closes
            self.call(on, 'macro.stop')
        if not path.is_file():
            raise RuntimeError(f'Eden saved no recording for {name}')
        seg['sha256'] = digest(path)
        seg['recorded'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        self.manifest_path.write_text(json.dumps(self.plan, indent=2) + '\n')
        self.log({'recorded': name, 'path': str(path), 'sha256': seg['sha256']})

    def play(self, on, name):
        seg = self.plan['segments'][name]
        path = self.base / seg['file']
        if not path.is_file():
            self.record(on, name, seg)  # the recording itself reaches the segment's end screen
            return
        if digest(path) != seg['sha256']:
            raise RuntimeError(f'segment {name} changed since the dry run')
        self.call(on, 'macro.play', {'path': str(path)})
        self.wait_macro(on, seg.get('cap', 60))

    def source(self, on, key):
        return Path(self.instances[on][key]) if key in self.instances[on] else Path(key)

    def last_match(self, on, gate):
        """Last match of the gate's pattern in lines written during this row (or the whole file), or None."""
        source = self.source(on, gate['source'])
        if not source.is_file():
            return None
        offset = 0 if gate.get('whole_file') else self.row_offsets.get(str(source), 0)
        with source.open('rb') as f:
            f.seek(offset)
            text = f.read().decode('utf-8', 'replace')
        matches = list(re.finditer(gate['pattern'], text))
        return matches[-1] if matches else None

    @staticmethod
    def roi_diff(path_a, path_b, roi):
        from PIL import Image
        with Image.open(path_a) as im_a, Image.open(path_b) as im_b:
            a = im_a.convert('RGB').crop(tuple(roi)).tobytes()
            b = im_b.convert('RGB').crop(tuple(roi)).tobytes()
        if len(a) != len(b):
            return float('inf')
        return sum(abs(x - y) for x, y in zip(a, b)) / max(len(a), 1)

    def gate(self, on, name):
        gate = self.plan['gates'][name]
        kind, until = gate['kind'], time.monotonic() + gate['cap']
        if kind == 'log':
            # Lines from this row only: the line usually appears while the segment before the gate is playing.
            # "on_all": every listed instance must match; "equal": their captured groups must be the same;
            # "expect_groups": {"2": "0"} pins captured values (group number -> text).
            sides = gate.get('on_all') or [on]
            while time.monotonic() < until:
                self.check_stop()
                found = {side: self.last_match(side, gate) for side in sides}
                if all(found.values()):
                    groups = {side: m.groups() for side, m in found.items()}
                    for side, m in found.items():
                        for number, want in gate.get('expect_groups', {}).items():
                            if m.group(int(number)) != want:
                                raise RuntimeError(f'gate {name} on {side}: group {number} is {m.group(int(number))!r}, '
                                                   f'expected {want!r}: {m.group(0)[:200]}')
                    if gate.get('equal') and len(set(groups.values())) > 1:
                        time.sleep(.5)  # the other side may not have written its line yet
                        if time.monotonic() >= until:
                            raise RuntimeError(f'gate {name}: captured values differ across instances: {groups}')
                        continue
                    lines = {side: m.group(0)[:300] for side, m in found.items()}
                    self.log({'gate': name, 'on': sides, 'pass': True, 'lines': lines})
                    return lines
                time.sleep(.3)
        elif kind == 'change':
            # Passes when the region differs from the row's earlier "mark" shot and, with "stable", has stopped moving.
            before = self.marks.get((on, gate['mark']))
            if before is None:
                raise RuntimeError(f'gate {name}: no mark {gate["mark"]!r} taken on {on} earlier in this row')
            previous = None
            while time.monotonic() < until:
                path = self.shot(on, 'gate-' + name)
                changed = self.roi_diff(path, before, gate['roi']) > gate.get('min_diff', DEFAULT_TOLERANCE)
                settled = not gate.get('stable', True) or (
                    previous is not None and self.roi_diff(path, previous, gate['roi']) <= gate.get('tolerance', 2))
                if changed and settled:
                    self.log({'gate': name, 'on': on, 'pass': True, 'shot': str(path), 'mark': str(before)})
                    return str(path)
                if previous is not None and previous != before:
                    Path(previous).unlink(missing_ok=True)
                previous = path
                time.sleep(gate.get('interval', .5))
            self.shot(on, 'GATE-FAILED-' + name)
        elif kind == 'json':
            source = self.source(on, gate['source'])
            while time.monotonic() < until:
                self.check_stop()
                try:
                    document = json.loads(source.read_text())
                    if all(dotted(document, k) == v for k, v in gate['expect'].items()):
                        copy = self.row_dir / f'{name}-{source.name}'
                        copy.write_text(json.dumps(document, indent=2) + '\n')
                        self.log({'gate': name, 'on': on, 'pass': True, 'copy': str(copy)})
                        return str(copy)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                time.sleep(.25)
        else:
            hook = load_hook(self.base, gate) if kind == 'hook' else None
            while time.monotonic() < until:
                path = self.shot(on, 'gate-' + name)
                if hook is not None:
                    passed = bool(hook(str(path), gate))
                else:
                    diff = self.roi_diff(path, self.base / gate['reference'], gate['roi'])
                    passed = diff <= gate.get('tolerance', DEFAULT_TOLERANCE)
                if passed:
                    self.log({'gate': name, 'on': on, 'pass': True, 'shot': str(path)})
                    return str(path)
                path.unlink(missing_ok=True)  # keep only the passing shot and the final failing one
                time.sleep(gate.get('interval', 1))
            self.shot(on, 'GATE-FAILED-' + name)
        raise RuntimeError(f'gate {name} on {on} did not pass within {gate["cap"]} s')

    def json_diff(self, on, spec):
        """Compare a JSON file now with a copy taken earlier in the row ({"copy": key, "as": before}).

        spec: {"source": "settings", "before": "before", "allowed": [dotted keys that may change],
               "must_change": [...], "must_keep": [...]}. Writes the diff into the row folder.
        """
        before_path = self.copies.get((on, spec['before']))
        if before_path is None:
            raise RuntimeError(f'json_diff: no copy {spec["before"]!r} taken on {on} earlier in this row')

        def leaves(value, prefix=''):
            if isinstance(value, dict):
                out = {}
                for k, v in value.items():
                    out.update(leaves(v, f'{prefix}.{k}' if prefix else k))
                return out
            return {prefix: value}

        old = leaves(json.loads(before_path.read_text()))
        new = leaves(json.loads(self.source(on, spec['source']).read_text()))
        changes = {k: [old.get(k), new.get(k)] for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
        report = {'before': str(before_path), 'changes': changes}
        (self.row_dir / f'json-diff-{on}-{spec["before"]}.json').write_text(json.dumps(report, indent=2) + '\n')
        allowed = set(spec.get('allowed', [])) | set(spec.get('must_change', []))
        unexpected = sorted(set(changes) - allowed)
        if unexpected:
            raise RuntimeError(f'json_diff on {on}: unexpected changes {unexpected[:10]}')
        missing = [k for k in spec.get('must_change', []) if k not in changes]
        if missing:
            raise RuntimeError(f'json_diff on {on}: expected changes missing {missing}')
        kept = [k for k in spec.get('must_keep', []) if k in changes]
        if kept:
            raise RuntimeError(f'json_diff on {on}: must_keep keys changed {kept}')
        return report

    # --- rows ----------------------------------------------------------------------------------------------------
    def log_offsets(self):
        """Current size of every log a gate reads, so log gates see only lines written during the row."""
        offsets = {}
        for gate in self.plan.get('gates', {}).values():
            if gate.get('kind') != 'log':
                continue
            for inst in self.instances.values():
                source = Path(inst.get(gate['source'], gate['source']))
                if source.is_file():
                    offsets[str(source)] = source.stat().st_size
        return offsets

    def run(self, only=None):
        previous_end = self.plan['start']
        for row in self.plan['rows']:
            receipt_path = self.out / f'{row["id"]}.json'
            if receipt_path.exists() and json.loads(receipt_path.read_text()).get('status') == 'PASS':
                previous_end = row['end']
                continue
            if only and row['id'] not in only:
                continue
            if not only and row['start'] != previous_end:
                raise StopRun(f'row {row["id"]} starts on {row["start"]}, but the game is on {previous_end}')
            self.row_dir = self.out / row['id']
            self.row_dir.mkdir(exist_ok=True)
            self.row_offsets = self.log_offsets()
            self.marks, self.copies = {}, {}
            receipt = {'id': row['id'], 'start': row['start'], 'end': row['end'], 'evidence': row.get('evidence', []),
                       'manifest_sha256': digest(self.manifest_path), 'started': time.time(), 'results': []}
            try:
                for step in row['steps']:
                    on = step.get('on', self.default)
                    if 'play' in step:
                        self.play(on, step['play'])
                        receipt['results'].append({'play': step['play'], 'on': on})
                    elif 'gate' in step:
                        receipt['results'].append({'gate': step['gate'], 'on': on, 'evidence': self.gate(on, step['gate'])})
                    elif 'shot' in step:
                        receipt['results'].append({'shot': str(self.shot(on, step['shot']))})
                    elif 'mark' in step:
                        self.marks[(on, step['mark'])] = self.shot(on, 'mark-' + step['mark'])
                        receipt['results'].append({'mark': step['mark'], 'on': on})
                    elif 'copy' in step:
                        source = self.source(on, step['copy'])
                        target = self.row_dir / f'{step.get("as", step["copy"])}-{on}-{source.name}'
                        target.write_bytes(source.read_bytes())
                        self.copies[(on, step.get('as', step['copy']))] = target
                        receipt['results'].append({'copy': str(target)})
                    elif 'json_diff' in step:
                        receipt['results'].append({'json_diff': self.json_diff(on, step['json_diff'])})
                    elif 'wait_ms' in step:
                        self.check_stop()
                        time.sleep(step['wait_ms'] / 1000)
                    else:
                        raise RuntimeError(f'unknown step {step}')
                receipt['status'] = 'PASS'
                if not self.pace['first_row_done']:
                    self.pace['first_row_done'] = time.time()
                    (self.out / 'pace.json').write_text(json.dumps(self.pace) + '\n')
            except BaseException as error:
                receipt.update(status='FAILED', error=repr(error))
                if not isinstance(error, StopRun):
                    (self.out / 'STOP-INPUT').write_text(f'{row["id"]}: {error!r}\n')
                raise
            finally:
                receipt['ended'] = time.time()
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
                for key in self.clients:
                    try:
                        self.call(key, 'input.reset')
                    except Exception:
                        pass
                print(json.dumps({'row': row['id'], 'status': receipt['status'],
                                  'seconds': round(receipt['ended'] - receipt['started'], 1),
                                  'receipt': str(receipt_path)}), flush=True)
            previous_end = row['end']
        print(json.dumps({'status': 'DONE', 'out': str(self.out)}), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=('dry-run', 'run'))
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--instances', type=Path, help='instances.json written at Up (run mode)')
    parser.add_argument('--out', type=Path, help='output folder (default: <manifest folder>/out)')
    parser.add_argument('--rows', help='comma-separated row ids to run (default: all remaining)')
    parser.add_argument('--pace-minutes', type=float, default=15)
    args = parser.parse_args(argv)
    manifest = args.manifest.resolve()
    report = dry_run(manifest)
    out = (args.out or manifest.parent / 'out').resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / 'dry-run.json').write_text(json.dumps(report, indent=2) + '\n')
    if args.mode == 'dry-run' or report['errors']:
        print(json.dumps(report, indent=2))
        return 1 if report['errors'] else 0
    if not args.instances:
        parser.error('run needs --instances')
    instances = json.loads(args.instances.read_text())
    try:
        Runner(manifest, instances, out, args.pace_minutes).run(set(args.rows.split(',')) if args.rows else None)
    except StopRun as stop:
        print(json.dumps({'status': 'STOPPED', 'reason': str(stop)}), flush=True)
        return 2
    except Exception as error:
        print(json.dumps({'status': 'FAILED', 'error': repr(error), 'stop_input': str(out / 'STOP-INPUT')}), flush=True)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
