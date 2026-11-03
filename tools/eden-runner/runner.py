#!/usr/bin/env python3
"""Shared Eden test runner: plays recorded macro segments row by row, with capped gates and a receipt per row.

A test run writes only a row list (rows.json) and, at Up, an instances file. This tool does the rest:

  runner.py dry-run <rows.json>                      offline: files, hashes, gates, row chain; lists missing segments
  runner.py run <rows.json> --instances <inst.json>  play the rows on the live instances
  runner.py record-start|record-stop <rows.json> --instances <inst.json> --segment <name> [--start-gate <gate>]
                                                     record while exploring by hand; the segment lands in rows.json

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

instances.json (written by tools/eden-lifecycle at Up): {"a": {"socket": ".../control.sock", "console_log": ".../console.log",
  "settings": ".../TrainingRooms/settings.json"}}. Steps run on "on": "<key>", default the first key. Standard file keys:
  console_log, control_log, diag_log, events_log, tile_log, detector_log, settings; a gate's "source" may drop the
  "_log" suffix ("diag" finds diag_log), so library gates work with any run's instances file.

Every play needs a start check: a gate step on the same instance since its last play, a "start_gate" on the segment
(checked before each play), or "unchecked": true on the step. The dry run refuses a play without one.

Steps: play <segment> (macro.play, wait for finished, input.reset; a segment whose file is missing but has a recipe is
recorded instead: macro.record, the recipe's inputs, input.reset, macro.stop, then its sha256 is written back to rows.json),
gate <gate>, shot <name> (screenshot into the row folder), mark <name> (screenshot a "before" for a change gate),
copy <instance key> [as <name>] (copy that instance file into the row folder), json_diff {source, before, allowed,
must_change, must_keep} (compare the file now with an earlier copy), wait_ms <ms> (only where no gate exists).

Shared library: a segment or gate written as {"library": "<name>"} comes from library/index.json next to this file
(proven segments and references, with sha256 and start/end screens), so common routes aren't recorded again per run.

Known detours: a gate with "recover": "<segment>" plays that segment and checks again when it fails (at most
"recover_times", default 1). Use it for the attract-mode intro the game falls into when left idle, or a stray pop-up.

Recipe actions: {"buttons": "a", "hold": 150, "gap": 0.6}, {"buttons": "b", "with_stick": [0, -1]} (stick held
through the press, e.g. Down+B), {"stick": [x, y, ms]}. Add "frames": N (and "gap_frames": N) to hold for N game frames
of the recording instead of wall time; use it for any stick hold whose length matters (cursor acceleration on the CSS).

Reference images not taken yet: an image gate with "acquire": true and no reference file captures one the first time
the run reaches it (once the region stops changing), writes its sha256 into rows.json, and the runner stops after that
row with status REVIEW (exit 4). A model looks at the new references once, then runs again.

Project step hooks (the plugin's own menus and checks, kept in the plugin's repo): {"run": "eden/steps.py:open_defaults",
"args": {...}, "check": true|false}. The function gets a StepContext (call, press, stick, play, gate, ui, shot,
wait_log, log, instance, row_dir) and runs inside the row: STOP files, the pace cap and actions.jsonl still apply.
"check": true means the hook verifies the screen it leaves, so it counts as the start check for the next play.

Eden's own frontend (menus, dialogs, the multiplayer room window): step {"ui": "trigger"|"click"|"set"|"key"|"select"|
"items"|"context"|"close", "target": "<unique objectName>" or {"class": "QPushButton", "text": "^Host Room$"}, ...extra
params such as "property"/"value"}. A dict target is polled in ui.tree until exactly one widget matches (or "index").
Gate {"kind": "ui", "match": {...}, "cap": 30} waits for a matching widget. Online pairs set up their room this way,
BEFORE the game loads (Eden refuses room setup while a game runs): eden-lifecycle "defer_load": true leaves the
frontend ready, then a row does {"call": "game.load", "params": {"path": "@game"}, "job_cap": 180} and a gate
{"kind": "status", "expect": {"running": true, "loading_complete": true}, "max_frame_age_ms": 1500, "cap": 180}.
"call" takes only game.load/pause/resume, status and settings.get/set/save; "@key" reads instances.json.

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
GATE_KINDS = ('image', 'log', 'json', 'hook', 'change', 'ui', 'status')
# Control methods a row may call directly with a {"call": ...} step. Input stays in recorded segments.
CALL_METHODS = ('game.load', 'game.pause', 'game.resume', 'status', 'settings.get', 'settings.set', 'settings.save')
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


LIBRARY = Path(__file__).resolve().parent / 'library'


def expand_library(plan):
    """Fill in segments and gates that name a shared library entry ({"library": "title-main"}).

    The library (library/index.json) holds proven segments and gate references for the game, each with its sha256 and
    start/end screens. Paths become absolute, so they resolve from any rows.json folder. Fields set in rows.json win.
    """
    index_path = LIBRARY / 'index.json'
    index = json.loads(index_path.read_text()) if index_path.is_file() else {'segments': {}, 'gates': {}}
    for kind in ('segments', 'gates'):
        for name, item in plan.get(kind, {}).items():
            if 'library' not in item:
                continue
            entry = index.get(kind, {}).get(item['library'])
            if entry is None:
                raise KeyError(f'{kind[:-1]} {name}: no library entry {item["library"]!r} in {index_path}')
            merged = dict(entry)
            for field in ('file', 'reference', 'module'):
                if field in merged:
                    merged[field] = str(LIBRARY / merged[field])
            merged.update({k: v for k, v in item.items() if k not in ('file', 'reference', 'module', 'sha256')})
            plan[kind][name] = merged
    return plan


def dry_run(manifest_path):
    """Offline only. Raises on a broken manifest; returns the report (missing segments are not an error)."""
    base = manifest_path.parent
    try:
        plan = expand_library(json.loads(manifest_path.read_text()))
    except KeyError as error:
        return {'manifest': str(manifest_path), 'errors': [str(error)], 'result': 'BROKEN', 'rows': 0,
                'missing_segments': [], 'references_to_acquire': [], 'available_segments': []}
    errors, missing, available, to_acquire = [], [], [], []
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
        if gate.get('recover') and gate['recover'] not in plan.get('segments', {}):
            errors.append(f'gate {name}: recover segment {gate["recover"]!r} is not in segments')
        try:
            if kind == 'image':
                ref = base / gate['reference']
                if not ref.is_file() and gate.get('acquire'):
                    to_acquire.append(name)  # captured the first time the run reaches the screen
                elif digest(ref) != gate.get('sha256'):
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
            elif kind == 'ui':
                if not isinstance(gate.get('match'), dict) or not gate['match']:
                    errors.append(f'gate {name}: ui needs a "match" object')
                else:
                    for key in ('text', 'title', 'tooltip'):
                        if key in gate['match']:
                            re.compile(gate['match'][key])
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
        marks, copies, checked = set(), set(), set()
        for step in row.get('steps', []):
            on = step.get('on', '*')
            if 'play' in step and step['play'] not in plan.get('segments', {}):
                errors.append(f'row {row["id"]}: unknown segment {step["play"]}')
            elif 'play' in step:
                # Never play or record onto a screen nobody checked (MACROS "Hybrid", rule 3). 2026-11-03 lr-03
                # recorded title->main while the game sat in the attract-mode intro movie.
                seg = plan['segments'][step['play']]
                if not (seg.get('start_gate') or on in checked or step.get('unchecked')):
                    errors.append(f'row {row["id"]}: play {step["play"]} on {on} has no start check '
                                  '(a gate step before it, a "start_gate" on the segment, or "unchecked": true)')
                if seg.get('start_gate') and seg['start_gate'] not in plan.get('gates', {}):
                    errors.append(f'segment {step["play"]}: unknown start_gate {seg["start_gate"]}')
                checked.discard(on)
            if 'gate' in step:
                checked.add(on)
            if 'gate' in step:
                gate = plan.get('gates', {}).get(step['gate'])
                if gate is None:
                    errors.append(f'row {row["id"]}: unknown gate {step["gate"]}')
                elif gate.get('kind') == 'change' and (on, gate.get('mark')) not in marks:
                    errors.append(f'row {row["id"]}: change gate {step["gate"]} before its mark {gate.get("mark")!r}')
            if 'run' in step:
                try:
                    load_step(base, step['run'])
                except Exception as error:  # noqa: BLE001 - report any hook that won't load
                    errors.append(f'row {row["id"]}: step hook {step["run"]!r}: {error!r}')
                if step.get('check'):
                    checked.add(on)  # the hook verifies the screen it leaves
                else:
                    checked.discard(on)  # a hook may move the game; check again before the next play
            if 'call' in step and step['call'] not in CALL_METHODS:
                errors.append(f'row {row["id"]}: call {step["call"]!r} is not one of {", ".join(CALL_METHODS)}')
            if 'ui' in step and step['ui'] not in Runner.UI_OPS:
                errors.append(f'row {row["id"]}: ui op {step["ui"]!r} is not one of {", ".join(Runner.UI_OPS)}')
            if 'mark' in step:
                marks.add((on, step['mark']))
            if 'copy' in step:
                copies.add((on, step.get('as', step['copy'])))
            if 'json_diff' in step and (on, step['json_diff'].get('before')) not in copies:
                errors.append(f'row {row["id"]}: json_diff before {step["json_diff"].get("before")!r} has no earlier copy')
        state = row['end']
    report = {'manifest': str(manifest_path), 'manifest_sha256': digest(manifest_path), 'rows': len(plan['rows']),
              'available_segments': available, 'missing_segments': missing, 'references_to_acquire': to_acquire,
              'gates': len(plan.get('gates', {})), 'errors': errors,
              'result': 'BROKEN' if errors else ('READY_TO_RECORD' if missing or to_acquire else 'READY')}
    return report


class StopRun(Exception):
    pass


class StepContext:
    """What a project step hook gets: the runner's own tools, bound to one instance and the current row.

    A hook is plain Python in the plugin's repo (rows.json: {"run": "eden/steps.py:open_defaults", "args": {...}}).
    It can call control methods (input included), take screenshots, play segments, check gates and operate the
    frontend. Everything goes through the runner, so STOP/STOP-INPUT, the pace cap and actions.jsonl still apply.
    """

    def __init__(self, runner, on):
        self.runner, self.on = runner, on
        self.instance = runner.instances[on]
        self.row_dir = runner.row_dir

    def call(self, method, params=None, job_cap=15):
        return self.runner.call(self.on, method, params, job_cap=job_cap)

    def shot(self, name):
        return self.runner.shot(self.on, name)

    def play(self, segment):
        return self.runner.play(self.on, segment)

    def gate(self, name):
        return self.runner.gate(self.on, name)

    def ui(self, op, target=None, **params):
        return self.runner.ui(self.on, {'ui': op, **({'target': target} if target is not None else {}), **params})

    def press(self, button, ms=120):
        """A scripted press (a route step in code, not a model looking after each input)."""
        self.call('input.button', {'button': button, 'state': 'tap', 'duration_ms': ms, 'lease_ms': ms + 500})
        time.sleep(ms / 1000 + .05)

    def stick(self, x, y, ms=250):
        self.call('input.stick', {'stick': 'left', 'x': x, 'y': y, 'lease_ms': ms})
        time.sleep(ms / 1000)
        self.call('input.stick', {'stick': 'left', 'x': 0, 'y': 0, 'lease_ms': 100})

    def wait_log(self, source, pattern, cap=10):
        """Wait for a regex in an instance file (lines written during this row), like a log gate."""
        lines = self.runner.check_gate_spec(self.on, {'kind': 'log', 'source': source, 'pattern': pattern, 'cap': cap},
                                            'hook-log')
        return lines[self.on]  # the matched text

    def log(self, record):
        self.runner.log({'hook': True, 'on': self.on, **record})

    def check_stop(self):
        self.runner.check_stop()


def load_step(base, spec):
    """'eden/steps.py:open_defaults' -> the function (path relative to the rows.json folder, or absolute)."""
    path, _, function = spec.partition(':')
    if not function:
        raise ValueError(f'step hook {spec!r} needs "<file>.py:<function>"')
    module_path = Path(path) if Path(path).is_absolute() else base / path
    module_spec = importlib.util.spec_from_file_location('step_hook_' + module_path.stem, module_path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return getattr(module, function)


class PipeClient:
    """Eden control over a Windows named pipe (the Ally: \\\\.\\pipe\\<name>), same JSON-lines protocol as edenctl's
    Unix-socket Client: one {"id", "method", "params"} line per connection, one reply line with the same id."""

    def __init__(self, path, timeout=10.0, opener=open):
        self.path, self.timeout, self.opener = path, timeout, opener

    def request(self, method, params=None):
        import uuid
        request_id = uuid.uuid4().hex
        payload = json.dumps({'id': request_id, 'method': method, 'params': params or {}},
                             allow_nan=False, separators=(',', ':')).encode() + b'\n'
        deadline = time.monotonic() + self.timeout
        while True:  # the pipe server may be between connections: retry the open until the deadline
            try:
                pipe = self.opener(self.path, 'r+b', buffering=0)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.05)
        with pipe:
            pipe.write(payload)
            data = bytearray()
            while b'\n' not in data:
                if time.monotonic() > deadline:
                    raise TimeoutError('control request deadline exceeded')
                chunk = pipe.read(65536)
                if not chunk:
                    raise RuntimeError('Eden closed the pipe before replying')
                data.extend(chunk)
        response = json.loads(data.split(b'\n', 1)[0])
        if not isinstance(response, dict) or response.get('id') != request_id:
            raise RuntimeError('invalid response or mismatched request id')
        return response


def default_client_factory():
    """Unix socket path -> edenctl Client; \\\\.\\pipe\\name -> PipeClient (Windows hosts such as the Ally)."""
    def make(socket_path):
        if str(socket_path).startswith('\\\\.\\pipe\\'):
            return PipeClient(socket_path)
        sys.path.insert(0, EDENCTL_DIR)
        from edenctl import Client
        return Client(socket_path, 10)
    return make


class Runner:
    def __init__(self, manifest_path, instances, out, pace_minutes, client_factory=None):
        self.manifest_path = manifest_path
        self.base = manifest_path.parent
        self.plan = expand_library(json.loads(manifest_path.read_text()))
        self.instances = instances
        self.default = next(iter(instances))
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.row_dir = self.out
        if client_factory is None:
            client_factory = default_client_factory()
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

    def call(self, on, method, params=None, job_cap=15, running_ok=False):
        """One control call. An accepted job is followed to completion within job_cap seconds. With running_ok, a
        job still running at the cap is fine: a ui click that opens a modal dialog (Host Room, a confirm box) stays
        running until that dialog is handled, by design (CLIControl.md); the next gate checks the outcome."""
        if method not in ('macro.stop', 'input.reset'):
            self.check_stop()
        reply = self.clients[on].request(method, params or {})
        self.log({'on': on, 'method': method, 'params': params, 'ok': reply.get('ok')})
        if not reply.get('ok'):
            raise RuntimeError(f'{method} on {on}: {reply}')
        value = reply.get('result')
        if isinstance(value, dict) and value.get('accepted') and value.get('job'):
            until = time.monotonic() + job_cap
            while True:
                job = self.clients[on].request('jobs.get', {'job': value['job']})
                state = (job.get('result') or {}).get('state')
                if state in ('done', 'completed', 'finished'):
                    break
                if state in ('failed', 'error') or not job.get('ok'):
                    raise RuntimeError(f'{method} job on {on}: {job}')
                if time.monotonic() > until:
                    if running_ok and state == 'running':
                        self.log({'on': on, 'method': method, 'job': value['job'], 'left_running': True})
                        break
                    raise RuntimeError(f'{method} job on {on} still {state} after {job_cap} s')
                time.sleep(.1)
        return value

    def shot(self, on, name):
        # "screenshot_target" in instances.json overrides the window lookup: the Ally needs "screen" (a per-window
        # grab is black under Vulkan there). Image gates are per host resolution; log gates are portable.
        target = self.instances[on].get('screenshot_target')
        if not target:
            tree = self.call(on, 'ui.tree', {'brief': True, 'visible_only': True}) or []
            target = next((n['id'] for n in tree if n.get('class') == 'GRenderWindow'), 'all')
        path = self.row_dir / f'{on}-{time.strftime("%H%M%S", time.gmtime())}Z-{time.time_ns() % 10**6:06d}-{name}.png'
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

    FRAME_LEASE_MS = 10000  # input lease while holding for a frame count; released explicitly when the count is reached

    def hold(self, on, frames, ms):
        """Wait for `frames` game frames of the recording (macro.status frame) if given, else for `ms` wall time."""
        if not frames:
            time.sleep(ms / 1000)
            return
        start = (self.call(on, 'macro.status') or {}).get('frame', 0)
        until = time.monotonic() + max(5.0, frames / 10)  # even a 10 fps game gets there in time
        while time.monotonic() < until:
            if (self.call(on, 'macro.status') or {}).get('frame', 0) - start >= frames:
                return
            time.sleep(.01)  # under a frame at 60 fps; the recording keeps whole display frames anyway
        raise RuntimeError(f'recording on {on} did not advance {frames} frames in time (game paused or stalled?)')

    def record(self, on, name, seg):
        path = self.base / seg['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        self.call(on, 'macro.record', {'name': name, 'path': str(path), 'player': seg.get('player', 0),
                                       'countdown_ms': 0})
        try:
            for action in seg['recipe']:
                # "frames": hold for that many game frames (macro.status frame), not wall time: the game's speed
                # varies, so a 400 ms hold recorded 11 frames once and 35 another time (lr-03, 2026-11-03).
                frames = action.get('frames')
                if 'buttons' in action:
                    hold = action.get('hold', 150)
                    lease = self.FRAME_LEASE_MS if frames else hold + 500
                    buttons = action['buttons'].split(',')
                    if 'with_stick' in action:  # stick held through the press, e.g. Down+B
                        x, y = action['with_stick']
                        self.call(on, 'input.stick', {'stick': 'left', 'x': x, 'y': y, 'lease_ms': lease + 200})
                        time.sleep(action.get('stick_lead_ms', 50) / 1000)
                    for b in buttons:
                        self.call(on, 'input.button', {'button': b, 'state': 'press', 'lease_ms': lease})
                    self.hold(on, frames, hold)
                    for b in buttons:
                        self.call(on, 'input.button', {'button': b, 'state': 'release'})
                    if 'with_stick' in action:
                        self.call(on, 'input.stick', {'stick': 'left', 'x': 0, 'y': 0, 'lease_ms': 100})
                elif 'stick' in action:
                    x, y = action['stick'][:2]
                    ms = action['stick'][2] if len(action['stick']) > 2 else 250
                    lease = self.FRAME_LEASE_MS if frames else ms
                    self.call(on, 'input.stick', {'stick': 'left', 'x': x, 'y': y, 'lease_ms': lease})
                    self.hold(on, frames, ms)
                    self.call(on, 'input.stick', {'stick': 'left', 'x': 0, 'y': 0, 'lease_ms': 100})
                self.hold(on, action.get('gap_frames'), action.get('gap', .6) * 1000)
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
        if seg.get('start_gate'):
            self.gate(on, seg['start_gate'])  # the segment's start screen, checked before any input
        path = self.base / seg['file']
        if not path.is_file():
            self.record(on, name, seg)  # the recording itself reaches the segment's end screen
            return
        if digest(path) != seg['sha256']:
            raise RuntimeError(f'segment {name} changed since the dry run')
        self.call(on, 'macro.play', {'path': str(path)})
        self.wait_macro(on, seg.get('cap', 60))

    def source(self, on, key):
        """An instance file by key. "diag" and "diag_log" name the same file, so library gates work with any
        instances.json (eden-lifecycle writes console_log, control_log, diag_log, detector_log, settings)."""
        inst = self.instances[on]
        for name in (key, key + '_log', key[:-4] if key.endswith('_log') else None):
            if name and name in inst:
                return Path(inst[name])
        return Path(key)

    def last_match(self, on, gate):
        """Last match of the gate's pattern in lines written during this row (or the whole file), or None."""
        source = self.source(on, gate['source'])
        if not source.is_file():
            return None
        offset, inode = (0, None) if gate.get('whole_file') else self.row_offsets.get(str(source), (0, None))
        stat = source.stat()
        if stat.st_ino != inode and inode is not None or stat.st_size < offset:
            # The file was rotated (new inode) or truncated since the row started: everything in it now was
            # written during this row (lr-04's practice-diagnostics.log rotated mid-row, 2026-11-03).
            offset = 0
        with source.open('rb') as f:
            f.seek(offset)
            text = f.read().decode('utf-8', 'replace')
        matches = list(re.finditer(gate['pattern'], text))
        return matches[-1] if matches else None

    @staticmethod
    def roi_diff(path_a, path_b, roi, b_is_roi=False):
        """Mean absolute difference per colour byte of the region in both images. With b_is_roi, image b already
        is just that region (library references keep only the compared region)."""
        from PIL import Image
        with Image.open(path_a) as im_a, Image.open(path_b) as im_b:
            a = im_a.convert('RGB').crop(tuple(roi)).tobytes()
            b = (im_b.convert('RGB') if b_is_roi else im_b.convert('RGB').crop(tuple(roi))).tobytes()
        if len(a) != len(b):
            return float('inf')
        return sum(abs(x - y) for x, y in zip(a, b)) / max(len(a), 1)

    def gate(self, on, name):
        """Check a gate. With "recover": "<segment>" a failed gate plays that segment and checks again, at most
        "recover_times" (default 1) times: for known detours such as the attract-mode intro or a stray pop-up."""
        gate = self.plan['gates'][name]
        for attempt in range(gate.get('recover_times', 1) + 1 if gate.get('recover') else 1):
            try:
                return self.check_gate(on, name)
            except StopRun:
                raise
            except RuntimeError as error:
                if not gate.get('recover') or attempt == gate.get('recover_times', 1):
                    raise
                self.log({'gate': name, 'on': on, 'recover': gate['recover'], 'after': repr(error)[:200]})
                seg = self.plan['segments'][gate['recover']]
                self.call(on, 'macro.play', {'path': str(self.base / seg['file'])})
                self.wait_macro(on, seg.get('cap', 60))

    def check_gate(self, on, name):
        return self.check_gate_spec(on, self.plan['gates'][name], name)

    def check_gate_spec(self, on, gate, name):
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
        elif kind == 'ui':
            # A frontend widget state, e.g. the room window's member count: {"match": {"class": "QLabel",
            # "text": "2 / 8"}}. Passes when at least one visible widget matches.
            while time.monotonic() < until:
                self.check_stop()
                tree = self.call(on, 'ui.tree', {}) or []
                hits = [n for n in tree if self.ui_matches(n, gate['match'])]
                if hits:
                    self.log({'gate': name, 'on': on, 'pass': True, 'widget': {k: hits[0].get(k) for k in
                                                                                 ('id', 'class', 'name', 'text')}})
                    return {k: hits[0].get(k) for k in ('id', 'class', 'name', 'text')}
                time.sleep(.5)
        elif kind == 'status':
            # Eden's own status, e.g. a game that finished loading: {"expect": {"running": true,
            # "loading_complete": true}, "max_frame_age_ms": 1500} (the same test eden-lifecycle's up uses).
            while time.monotonic() < until:
                self.check_stop()
                status = self.call(on, 'status') or {}
                fresh = ('max_frame_age_ms' not in gate or
                         (isinstance(status.get('last_frame_age_ms'), (int, float))
                          and status['last_frame_age_ms'] < gate['max_frame_age_ms']))
                if fresh and not status.get('modal') and all(status.get(k) == v for k, v in gate['expect'].items()):
                    self.log({'gate': name, 'on': on, 'pass': True})
                    return {k: status.get(k) for k in gate['expect']}
                time.sleep(.5)
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
        elif kind == 'image' and gate.get('acquire') and not (self.base / gate['reference']).is_file():
            return self.acquire(on, name, gate, until)
        else:
            hook = load_hook(self.base, gate) if kind == 'hook' else None
            while time.monotonic() < until:
                path = self.shot(on, 'gate-' + name)
                if hook is not None:
                    passed = bool(hook(str(path), gate))
                else:
                    diff = self.roi_diff(path, self.base / gate['reference'], gate['roi'], gate.get('reference_is_roi'))
                    passed = diff <= gate.get('tolerance', DEFAULT_TOLERANCE)
                if passed:
                    self.log({'gate': name, 'on': on, 'pass': True, 'shot': str(path)})
                    return str(path)
                path.unlink(missing_ok=True)  # keep only the passing shot and the final failing one
                time.sleep(gate.get('interval', 1))
            self.shot(on, 'GATE-FAILED-' + name)
        raise RuntimeError(f'gate {name} on {on} did not pass within {gate["cap"]} s')

    def acquire(self, on, name, gate, until):
        """First visit of a screen with no reference yet: wait until the region stops changing, save that shot as the
        reference, write its sha256 into rows.json, and flag the row so the runner stops for a review after it."""
        previous = None
        while time.monotonic() < until:
            path = self.shot(on, 'acquire-' + name)
            if previous is not None and self.roi_diff(path, previous, gate['roi']) <= gate.get('settle', 2):
                reference = self.base / gate['reference']
                reference.parent.mkdir(parents=True, exist_ok=True)
                reference.write_bytes(path.read_bytes())
                gate['sha256'] = digest(reference)
                gate['acquired'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                self.manifest_path.write_text(json.dumps(self.plan, indent=2) + '\n')
                self.acquired.append({'gate': name, 'reference': str(reference), 'shot': str(path)})
                self.log({'gate': name, 'on': on, 'acquired': str(reference), 'sha256': gate['sha256']})
                return str(reference)
            if previous is not None:
                previous.unlink(missing_ok=True)
            previous = path
            time.sleep(gate.get('interval', .5))
        raise RuntimeError(f'gate {name} on {on}: screen never settled within {gate["cap"]} s to acquire a reference')

    UI_OPS = ('trigger', 'click', 'set', 'key', 'select', 'items', 'context', 'close')

    @staticmethod
    def ui_matches(node, match):
        """match: exact "class"/"name"/"id", regex "text"/"title"/"tooltip" (& accelerators ignored), "visible"
        (default true), "enabled"."""
        for key, want in match.items():
            if key in ('class', 'name', 'id'):
                if node.get(key) != want:
                    return False
            elif key in ('text', 'title', 'tooltip'):
                if not re.search(want, str(node.get(key, '')).replace('&', '')):
                    return False
            elif key in ('visible', 'enabled'):
                if bool(node.get(key)) != bool(want):
                    return False
        return match.get('visible', True) is False or node.get('visible', True)

    def find_ui(self, on, match, cap):
        """Poll ui.tree until exactly one widget matches (dialogs open asynchronously); returns its session id."""
        until = time.monotonic() + cap
        found = []
        while time.monotonic() < until:
            self.check_stop()
            tree = self.call(on, 'ui.tree', {}) or []
            found = [n for n in tree if self.ui_matches(n, match)]
            if len(found) == 1 or (found and 'index' in match):
                return found[match.get('index', 0)]['id']
            time.sleep(.3)
        raise RuntimeError(f'ui target {match} on {on}: {len(found)} matches after {cap} s (need exactly one)')

    def ui(self, on, step):
        """Operate Eden's Qt frontend (menus, dialogs, the multiplayer room window) through the control socket."""
        params = {k: v for k, v in step.items() if k not in ('ui', 'target', 'on', 'cap', 'job_cap')}
        target = step.get('target')
        if isinstance(target, dict):
            params['target'] = self.find_ui(on, target, step.get('cap', 10))
        elif target is not None:
            params['target'] = target  # an objectName the server resolves (must be unique) or a session id
        # A ui job may stay running while a modal it opened is up (pkg4's Host Room click, 2026-11-03): give it a
        # short look ("job_cap", default 2 s) and let the next gate judge the result.
        result = self.call(on, 'ui.' + step['ui'], params, job_cap=step.get('job_cap', 2), running_ok=True)
        self.log({'ui': step['ui'], 'on': on, 'params': params})
        return result

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
        """Size and inode of every file a log gate or a hook's wait_log may read (every instance file and every
        log gate source), so they see only lines written during the row, also across rotation or truncation."""
        paths = set()
        for on, inst in self.instances.items():
            paths.update(str(v) for v in inst.values() if isinstance(v, str) and v.startswith('/'))
            for gate in self.plan.get('gates', {}).values():
                if gate.get('kind') == 'log':
                    paths.add(str(self.source(on, gate['source'])))
        offsets = {}
        for path in paths:
            try:
                stat = Path(path).stat()
            except OSError:
                continue
            if Path(path).is_file():
                offsets[path] = (stat.st_size, stat.st_ino)
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
            self.marks, self.copies, self.acquired = {}, {}, []
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
                    elif 'ui' in step:
                        self.ui(on, step)
                        receipt['results'].append({'ui': step['ui'], 'on': on, 'target': step.get('target')})
                    elif 'run' in step:
                        hook = load_step(self.base, step['run'])
                        value = hook(StepContext(self, on), **step.get('args', {}))
                        receipt['results'].append({'run': step['run'], 'on': on,
                                                   'value': value if isinstance(value, (str, int, float, bool, list,
                                                                                        dict, type(None))) else repr(value)})
                    elif 'call' in step:
                        # "@key" values come from instances.json, e.g. {"call": "game.load", "params": {"path": "@game"}}
                        params = {k: (self.instances[on][v[1:]] if isinstance(v, str) and v.startswith('@') else v)
                                  for k, v in step.get('params', {}).items()}
                        self.call(on, step['call'], params, job_cap=step.get('job_cap', 15))
                        receipt['results'].append({'call': step['call'], 'on': on, 'params': params})
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
                if self.acquired:
                    receipt['acquired_references'] = self.acquired
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
            if self.acquired:
                # A new reference is a claim about what the screen should look like: a model looks once, then reruns.
                print(json.dumps({'status': 'REVIEW', 'row': row['id'], 'acquired': self.acquired,
                                  'next': 'look at each new reference; if one is wrong, delete it and this row\'s '
                                          'receipt and fix the route; then run again'}), flush=True)
                return 'REVIEW'
        print(json.dumps({'status': 'DONE', 'out': str(self.out)}), flush=True)
        return 'DONE'


def record_command(mode, manifest, instances, name, on, start_gate=None, client_factory=None):
    """Record while exploring: record-start opens an Eden recording into segments/<name>.edenmacro.json; drive the
    game any way you like (step.ps1, gv.py press, the GUI); record-stop closes it and writes the segment with its
    sha256 (and optional start gate) into rows.json, ready for play steps and harvest."""
    manifest = manifest.resolve()
    plan = json.loads(manifest.read_text()) if manifest.is_file() else {'start': 'unknown', 'segments': {}, 'gates': {},
                                                                         'rows': []}
    on = on or next(iter(instances))
    client = (client_factory or default_client_factory())(instances[on]['socket'])
    path = manifest.parent / 'segments' / f'{name}.edenmacro.json'
    state_file = manifest.parent / 'segments' / f'.{name}.recording.json'
    if mode == 'record-start':
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise SystemExit(f'{path} exists; pick a new segment name (segments are immutable once recorded)')
        reply = client.request('macro.record', {'name': name, 'path': str(path), 'player': 0, 'countdown_ms': 0})
        if not reply.get('ok'):
            raise SystemExit(f'macro.record failed: {reply}')
        state_file.write_text(json.dumps({'segment': name, 'on': on, 'started': time.time()}) + '\n')
        return {'recording': name, 'on': on, 'path': str(path), 'next': f'drive the game, then record-stop {name}'}
    client.request('input.reset', {})
    time.sleep(.12)  # one neutral frame before the recording closes
    reply = client.request('macro.stop', {})
    if not reply.get('ok') or not path.is_file():
        raise SystemExit(f'macro.stop failed or no file was saved: {reply}')
    seg = plan.setdefault('segments', {}).setdefault(name, {})
    seg.update(file=f'segments/{name}.edenmacro.json', sha256=digest(path),
               recorded=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    if start_gate:
        seg['start_gate'] = start_gate
    manifest.write_text(json.dumps(plan, indent=2) + '\n')
    state_file.unlink(missing_ok=True)
    return {'recorded': name, 'sha256': seg['sha256'], 'rows_json': str(manifest)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=('dry-run', 'run', 'record-start', 'record-stop'))
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--instances', type=Path, help='instances.json written at Up (run mode)')
    parser.add_argument('--out', type=Path, help='output folder (default: <manifest folder>/out)')
    parser.add_argument('--rows', help='comma-separated row ids to run (default: all remaining)')
    parser.add_argument('--pace-minutes', type=float, default=15)
    parser.add_argument('--segment', help='record-start/record-stop: the segment name')
    parser.add_argument('--on', help='record-start/record-stop: the instance key (default: the first)')
    parser.add_argument('--start-gate', help='record-stop: the gate that checks the segment\'s start screen')
    args = parser.parse_args(argv)
    if args.mode in ('record-start', 'record-stop'):
        if not (args.instances and args.segment):
            parser.error(f'{args.mode} needs --instances and --segment')
        print(json.dumps(record_command(args.mode, args.manifest, json.loads(args.instances.read_text()),
                                        args.segment, args.on, args.start_gate)))
        return 0
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
        result = Runner(manifest, instances, out, args.pace_minutes).run(set(args.rows.split(',')) if args.rows else None)
        if result == 'REVIEW':
            return 4
    except StopRun as stop:
        print(json.dumps({'status': 'STOPPED', 'reason': str(stop)}), flush=True)
        return 2
    except Exception as error:
        print(json.dumps({'status': 'FAILED', 'error': repr(error), 'stop_input': str(out / 'STOP-INPUT')}), flush=True)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
