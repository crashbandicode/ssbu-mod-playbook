#!/usr/bin/env python3
"""Shared Eden test runner: plays recorded macro segments row by row, with capped gates and a receipt per row.

A test run writes only a row list (rows.json) and, at Up, an instances file. This tool does the rest:

  runner.py dry-run <rows.json>                      offline: files, hashes, gates, row chain; lists missing segments
  runner.py run <rows.json> --instances <inst.json> [--capture-fixtures]
                                                     play the rows on the live instances
  runner.py replay <rows.json> --fixture <dir>       replay the row's gates and step hooks on a captured fixture
  runner.py self-test <rows.json> --instances <inst.json>
                                                     check declared start states and recovery from each screen class
  runner.py capture-fixture <rows.json> --instances <inst.json> [--rows <id>]
                                                     run the selected rows and save a fixture from each pass
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
must_change, must_keep} (compare the file now with an earlier copy), wait_ms <ms> (only where no gate exists),
library <name> (play a shared-library segment by its index name), gif {name, preset, steps, out?} (record the nested
steps; presets "docs" and "evidence", or a preset the plan defines). A step has one kind. New kinds register on
Runner.step_handlers instead of another branch in the row loop.

Shared library: a segment or gate written as {"library": "<name>"} comes from library/index.json next to this file
(proven segments and references, with sha256 and start/end screens), so common routes aren't recorded again per run.

Known detours: a gate with "recover": "<segment>" plays that segment and checks again when it fails (at most
"recover_times", default 1). Use it for the attract-mode intro the game falls into when left idle, or a stray pop-up.

Recipe actions: {"buttons": "a", "hold": 150, "gap": 0.6}, {"buttons": "b", "with_stick": [0, -1]} (stick held
through the press, e.g. Down+B), {"stick": [x, y, ms]}. Add "frames": N (and "gap_frames": N) to hold for N game frames
of the recording instead of wall time; use it for any stick hold whose length matters (cursor acceleration on the CSS).
Eden's control API accepts lease_ms of 1..10000 only (CLIControl.md "Controller input"; edenctl.py copies lease_ms
onto input.button and input.stick). A new press replaces the previous expiry, so a hold longer than one lease —
including a frame hold, whose wall time is the game's — is renewed with another lease of at most 10000 ms. The
runner never asks for 10000 plus a margin. A recording that fails deletes the partial macro and writes no sha256.
Playing a segment file that has no sha256 fails that row with a clear error.

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
"call" takes only game.load/pause/resume, status, settings.get/set/save and the tracing methods (trace.arm/read/
counters/list/disarm, modules.list, symbols.load, tracepoint.*); "@key" reads instances.json. With eden-lifecycle's
per-instance "trace", instances.json has "trace_jsonl": a log gate on it waits for a traced function, e.g.
{"kind": "log", "source": "trace_jsonl", "pattern": "\"point_id\":\"2\".*\"event\":\"return\"", "cap": 30}.

More gates:
  {"kind": "change", "mark": "hud-before", "roi": [...], "min_diff": 8, "stable": true, "cap": 10}
      the region differs from the row's mark and has stopped moving (HUD portrait swapped, menu value changed)
  {"kind": "image", "scale": "fit", "frame": [1280, 720]}
      opt-in, on the gate or the plan. Scale the live frame to the reference size when the aspect ratio matches,
      then compare. The ROI stays in reference coordinates. A reference that is already the ROI has no frame of
      its own: "frame" is the size those coordinates were captured at, and the live frame is scaled to that
      before the crop. If the mean difference is still over the tolerance, both crops are compared again at
      half their size, so a 1280-wide shot of a 0.5x render (upscaled and blurrier) can match. Off unless set.
      The receipt records the scaling. Tolerances are unchanged. A pass that came only from hook gates
      returning True gets a receipt warning and
      still passes; return a line, a count or a small dict from the hook when that pass should count as evidence.
  {"kind": "log", "source": "detector_log", "pattern": "sync_end .*frame=(\\d+) .*desync_seen=(\\d+)",
   "on_all": ["a", "b"], "equal": true, "expect_groups": {"2": "0"}, "cap": 120}
      every listed instance logged a match this row, with the same captured values and desync_seen=0

Run rules (playbook docs/MACROS.md, "Test runs"): rows run back to back while their gates pass. On a failure the runner
writes the row's receipt as FAILED with evidence (a screenshot, the tails of the logs that row's gates read, and
trace.counters plus trace.read when that instance is tracing). It then follows on_failure: "continue" or "stop".
The default is continue when the next row has a start gate and a recover list, and stop otherwise, so a rows.json
with no recover still stops at the first failure (STOP-INPUT) as before. continue plays that next row's recover
(or the plan's recover list) and checks its start gate; the list may hold segments, a library route, a run hook,
or a ui or call step. If the start gate passes, the run carries on. If recovery fails, or status says the game is
not running (running false, or a crash), it writes STOP-INPUT and stops. The runner never reboots Eden.
Each failure is tagged harness-suspect (the screen never changed, recovery or the start
state failed, or evidence is missing), product-suspect (a screen gate passed and a later
effect, card file, or trace assertion failed), or unknown. out/summary.json groups them
by kind and cause. After early_stop failures of one cause (default 3; false disables it)
the run stops with "probable shared cause" instead of repeating that failure.
A passing row can be saved with --capture-fixtures and replayed offline. self-test, before
plugin rows, checks each declared start gate and the declared recovery from each
screen class back to that gate. Screen classes and their routes are plan data.
on_failure may be set on the plan, overridden per row, or passed as --on-failure. A plan or row may also set
failure_evidence and on_failure_hook (a project hook that adds evidence). out/summary.json lists every row as
pass, fail, skipped or not run, with each failure's reason and evidence paths. The exit code is nonzero if any
row failed. --rows failed reruns only rows whose receipt is FAILED. A STOP file in the output folder stops the
runner before its next input. If no row has finished --pace-minutes after the first run started, it writes
STOP-INPUT. Every control call goes to out/actions.jsonl; each row writes out/<row>.json and its media under
out/<row>/. Rerunning skips rows whose receipt passed and resumes at the next.
"""
import argparse
import hashlib
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import harness  # noqa: E402

EDENCTL_DIR = '/home/intpa/Applications/Eden-GIF/current/tools'
GATE_KINDS = ('image', 'log', 'json', 'hook', 'change', 'ui', 'status')
# Control methods a row may call directly with a {"call": ...} step. Input stays in recorded segments.
CALL_METHODS = ('game.load', 'game.pause', 'game.resume', 'status', 'settings.get', 'settings.set', 'settings.save',
                # Guest tracing (Eden persistent-tracing builds, docs/GUEST_TRACEPOINT.md); never input.
                'trace.arm', 'trace.read', 'trace.counters', 'trace.list', 'trace.disarm', 'modules.list',
                'symbols.load', 'tracepoint.arm', 'tracepoint.status', 'tracepoint.cancel')
# One verb per step. Runner.step_handlers maps each of these to a method; add a kind there, not a new if-chain.
STEP_KINDS = ('play', 'gate', 'shot', 'mark', 'copy', 'json_diff', 'ui', 'run', 'call', 'wait_ms', 'gif', 'library')
UI_OPS = ('trigger', 'click', 'set', 'key', 'select', 'items', 'context', 'close')
# Eden recording.start limits (tools/edenctl.py, docs/GIFRecording.md): fps 1-30, width 160-1280,
# duration_ms 100-120000. Presets below are the playbook's defaults (docs/GIFS.md), not a ceiling:
# a plan or row gif_presets entry, or fields on the gif step, override them. Leaving the operator's
# envelope (docs: 1280 wide and under 10 s; evidence: 640 wide, fps at most 15, at most 10 MB) needs a reason.
BUILTIN_GIF_PRESETS = {
    'docs': {'width': 1280, 'fps': 10, 'max_duration_s': 10},
    'evidence': {'width': 640, 'fps': 10, 'max_duration_s': 10, 'max_bytes': 10 * 1024 * 1024},
}
EVIDENCE_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_FAILURE_EVIDENCE = {'screenshot': True, 'log_tails': True, 'trace': 'auto', 'tail_lines': 80}
# Mean absolute difference per colour byte (0-255) an image gate accepts. Exact matches (0) fail on animated screens
# (the title's attract mode, menu shimmer, a blinking cursor); 2026-11-03 pkg4 lost two row-1 attempts to one.
DEFAULT_TOLERANCE = 8
# Eden control API (Eden-GIF docs/CLIControl.md, "Controller input"): a held input's lease_ms is 1..10000.
# edenctl.py copies lease_ms onto input.button, input.stick and the other held-input commands. A new press
# replaces the previous expiry, so a longer hold is renewed with another lease; it is not one request past the cap.
# Renew this long before expiry. Eden checks expiry on a 20 ms GUI timer.
LEASE_MAX_MS = 10000
LEASE_MIN_MS = 1
LEASE_RENEW_MARGIN_MS = 250


def lease_parts(total_ms, cap=LEASE_MAX_MS):
    """Split a hold into consecutive leases of 1..cap ms. Eden rejects a single lease outside that range."""
    total = int(total_ms)
    if total < LEASE_MIN_MS:
        raise ValueError(f'lease_ms {total} is below {LEASE_MIN_MS}')
    parts = []
    left = total
    while left > cap:
        parts.append(cap)
        left -= cap
    parts.append(left)
    return parts


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


class GifRefused(RuntimeError):
    """The captured GIF breaks its preset. detail is stored on the row receipt."""

    def __init__(self, message, detail):
        super().__init__(message)
        self.detail = detail


def step_kind(step):
    """The one verb in a step. Unknown shapes raise; the dry run still ignores a step with no known verb,
    and the run reports it, which is how an unrecognized future kind fails today."""
    if not isinstance(step, dict):
        raise RuntimeError(f'unknown step {step}')
    known = list(STEP_KINDS)
    extra = getattr(step_kind, 'extra', ())
    for key in extra:
        if key not in known:
            known.append(key)
    found = [key for key in known if key in step]
    if len(found) != 1:
        raise RuntimeError(f'unknown step {step}')
    return found[0]


def project_root(base):
    """Repo root when rows.json lives in a checkout; otherwise the rows folder. A plan "root" wins."""
    here = base.resolve()
    for parent in (here, *here.parents):
        if (parent / '.git').exists():
            return parent
    return here


def inside_root(root, rel):
    """A relative path that stays inside root. Absolute paths and escapes raise ValueError."""
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError('empty path')
    path = Path(rel)
    if path.is_absolute() or '..' in path.parts:
        # ".." is checked after resolve as well: "a/../../x" must not leave root.
        raise ValueError(f'path {rel!r} must stay inside {root}')
    resolved = (root / path).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f'path {rel!r} leaves {root_resolved}')
    return resolved


def library_index():
    index_path = LIBRARY / 'index.json'
    if not index_path.is_file():
        return {'segments': {}, 'gates': {}}
    return json.loads(index_path.read_text())


def merged_gif_presets(plan, row=None):
    """Builtin presets, then the plan's gif_presets, then the row's. Later fields replace earlier ones."""
    presets = {name: dict(spec) for name, spec in BUILTIN_GIF_PRESETS.items()}
    for source in (plan.get('gif_presets') or {}, (row or {}).get('gif_presets') or {}):
        if not isinstance(source, dict):
            raise ValueError('gif_presets must be an object')
        for name, spec in source.items():
            if not isinstance(spec, dict):
                raise ValueError(f'gif preset {name!r} must be an object')
            base = dict(presets.get(name, {}))
            base.update(spec)
            presets[name] = base
    return presets


def _gif_number(spec, *keys):
    for key in keys:
        if key in spec and spec[key] is not None:
            return spec[key]
    return None


def resolve_gif_spec(plan, row, gif):
    """Preset plus the step's width/fps/length/size overrides. Does not decide whether a reason is required."""
    if not isinstance(gif, dict):
        raise ValueError('gif step needs an object')
    if not gif.get('name') or not isinstance(gif['name'], str):
        raise ValueError('gif step needs a name')
    if not gif.get('preset'):
        raise ValueError('gif step needs a preset')
    presets = merged_gif_presets(plan, row)
    if gif['preset'] not in presets:
        raise ValueError(f'unknown gif preset {gif["preset"]!r}')
    spec = dict(presets[gif['preset']])
    if 'width' in gif:
        spec['width'] = gif['width']
    if 'fps' in gif:
        spec['fps'] = gif['fps']
    length = _gif_number(gif, 'length', 'max_duration_s', 'duration_s')
    if length is not None:
        spec['max_duration_s'] = length
    if 'duration_ms' in gif:
        spec['max_duration_s'] = gif['duration_ms'] / 1000
    if 'max_bytes' in gif:
        spec['max_bytes'] = gif['max_bytes']
    elif 'size' in gif:
        spec['max_bytes'] = gif['size']
    elif 'max_mb' in gif:
        spec['max_bytes'] = int(float(gif['max_mb']) * 1024 * 1024)
    if 'width' not in spec or 'fps' not in spec or 'max_duration_s' not in spec:
        raise ValueError(f'gif preset {gif["preset"]!r} needs width, fps and a duration')
    spec['width'] = int(spec['width'])
    spec['fps'] = int(spec['fps'])
    spec['max_duration_s'] = float(spec['max_duration_s'])
    if spec.get('max_bytes') is not None:
        spec['max_bytes'] = int(spec['max_bytes'])
    spec['preset'] = gif['preset']
    spec['reason'] = gif.get('reason') or spec.get('reason')
    return spec


def gif_api_error(spec):
    """None when recording.start would accept the numbers. A reason cannot widen Eden's own limits."""
    if not 1 <= spec['fps'] <= 30:
        return f'fps {spec["fps"]} is outside Eden\'s 1..30'
    if not 160 <= spec['width'] <= 1280:
        return f'width {spec["width"]} is outside Eden\'s 160..1280'
    duration_ms = spec['max_duration_s'] * 1000
    if not 100 <= duration_ms <= 120000:
        return f'duration {duration_ms:g} ms is outside Eden\'s 100..120000'
    return None


def gif_outside_operator_rules(spec):
    """True when the spec is outside both playbook envelopes and therefore needs a reason.

    docs (GIFS.md): full frame, 1280 wide, under 10 s. fps stays inside Eden's 1..30.
    evidence: 640 wide, fps at most 15, a size cap of at most 10 MB. Duration may run up to
    Eden's 120 s so a longer action still counts as evidence when the file stays small.
    """
    width, fps, duration = spec['width'], spec['fps'], spec['max_duration_s']
    size = spec.get('max_bytes')
    docs = width == 1280 and duration <= 10 and 1 <= fps <= 30
    evidence = (width == 640 and fps <= 15 and duration <= 120 and size is not None and size <= EVIDENCE_MAX_BYTES)
    return not (docs or evidence)


def gif_dest_path(root, row_dir, gif):
    if gif.get('out'):
        return inside_root(root, gif['out'])
    name = gif['name']
    if Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError(f'gif name {name!r} must stay inside the row folder')
    if not name.endswith('.gif'):
        name += '.gif'
    return inside_root(row_dir, name)


def recover_steps_of(plan, row):
    """The row's recover list, else the plan's. None when neither is declared (old rows.json)."""
    if isinstance(row, dict) and 'recover' in row:
        return row['recover']
    if isinstance(plan.get('recover'), list):
        return plan['recover']
    return None


def start_gate_of(plan, row):
    """The gate that proves the row's start screen: an explicit start_gate, or start when that name is a gate."""
    if row.get('start_gate'):
        return row['start_gate']
    if row.get('start') in plan.get('gates', {}):
        return row['start']
    return None


def failure_policy_of(plan, row, successor, cli=None):
    """cli, then the row, then the plan, else continue only when the next row can be recovered."""
    if cli:
        return cli
    if row.get('on_failure'):
        return row['on_failure']
    if plan.get('on_failure'):
        return plan['on_failure']
    if successor is not None and start_gate_of(plan, successor) and recover_steps_of(plan, successor) is not None:
        return 'continue'
    return 'stop'


def evidence_config_of(plan, row):
    raw = row.get('failure_evidence', plan.get('failure_evidence', DEFAULT_FAILURE_EVIDENCE))
    cfg = dict(DEFAULT_FAILURE_EVIDENCE)
    if isinstance(raw, list):
        cfg['screenshot'] = 'screenshot' in raw
        cfg['log_tails'] = 'log_tails' in raw or 'logs' in raw
        cfg['trace'] = True if 'trace' in raw else False
    elif isinstance(raw, dict):
        cfg.update(raw)
    else:
        raise ValueError('failure_evidence must be a list or an object')
    return cfg


def tracing_enabled(plan, instances):
    if plan.get('trace'):
        return True
    return any(isinstance(inst, dict) and (inst.get('trace_jsonl') or inst.get('trace')) for inst in instances.values())


def _check_known_step(plan, base, step, where, errors):
    """References a recover step or a gif's nested step points at. Play start-checks are the caller's job."""
    try:
        kind = step_kind(step)
    except RuntimeError:
        errors.append(f'{where}: unknown step {step!r}')
        return
    if kind == 'play' and step['play'] not in plan.get('segments', {}):
        errors.append(f'{where}: unknown segment {step["play"]}')
    elif kind == 'library':
        name = step['library']
        in_plan = any(seg_name == name or seg.get('library') == name for seg_name, seg in plan.get('segments', {}).items())
        if not in_plan and name not in library_index().get('segments', {}):
            errors.append(f'{where}: unknown library segment {name!r}')
    elif kind == 'gate' and step['gate'] not in plan.get('gates', {}):
        errors.append(f'{where}: unknown gate {step["gate"]}')
    elif kind == 'run':
        try:
            load_step(base, step['run'])
        except Exception as error:  # noqa: BLE001
            errors.append(f'{where}: step hook {step["run"]!r}: {error!r}')
    elif kind == 'call' and step['call'] not in CALL_METHODS:
        errors.append(f'{where}: call {step["call"]!r} is not one of {", ".join(CALL_METHODS)}')
    elif kind == 'ui' and step['ui'] not in UI_OPS:
        errors.append(f'{where}: ui op {step["ui"]!r} is not one of {", ".join(UI_OPS)}')
    elif kind == 'gif':
        _check_gif(plan, base, None, step, where, errors)
    elif kind == 'wait_ms' and not isinstance(step['wait_ms'], (int, float)):
        errors.append(f'{where}: wait_ms needs a number')


def _check_gif(plan, base, row, step, where, errors):
    gif = step.get('gif')
    try:
        spec = resolve_gif_spec(plan, row or {}, gif)
    except ValueError as error:
        errors.append(f'{where}: {error}')
        return
    if 'crop' in gif:
        errors.append(f'{where}: gif records the full frame and does not crop')
    api = gif_api_error(spec)
    if api:
        errors.append(f'{where}: gif {gif.get("name")}: {api}')
    elif gif_outside_operator_rules(spec) and not spec.get('reason'):
        errors.append(f'{where}: gif {gif.get("name")} leaves the docs/evidence rules and needs a "reason"')
    if gif.get('out'):
        root = Path(plan['root']) if plan.get('root') else project_root(base)
        try:
            inside_root(root, gif['out'])
        except ValueError as error:
            errors.append(f'{where}: gif out: {error}')
    try:
        if Path(gif.get('name', '')).is_absolute() or '..' in Path(gif.get('name', '')).parts:
            errors.append(f'{where}: gif name {gif.get("name")!r} must stay inside the row folder')
    except TypeError:
        errors.append(f'{where}: gif name must be a string')
    for nested in gif.get('steps') or []:
        _check_known_step(plan, base, nested, f'{where} gif {gif.get("name")}', errors)


def check_plan_policies(plan, base, errors):
    """Presets, recover lists and failure hooks. Old plans that omit them pass through."""
    try:
        presets = merged_gif_presets(plan)
    except ValueError as error:
        errors.append(str(error))
        presets = {}
    for name, spec in (plan.get('gif_presets') or {}).items() if isinstance(plan.get('gif_presets'), dict) else []:
        merged = presets.get(name, spec)
        try:
            probe = resolve_gif_spec({'gif_presets': {name: merged}}, None, {'name': name, 'preset': name})
        except ValueError as error:
            errors.append(f'gif preset {name}: {error}')
            continue
        api = gif_api_error(probe)
        if api:
            errors.append(f'gif preset {name}: {api}')
        elif gif_outside_operator_rules(probe) and not probe.get('reason'):
            errors.append(f'gif preset {name} leaves the docs/evidence rules and needs a "reason"')
    if 'recover' in plan and not isinstance(plan['recover'], list):
        errors.append('plan recover must be a list of steps (a gate\'s recover stays a segment name)')
    if isinstance(plan.get('recover'), list):
        for step in plan['recover']:
            _check_known_step(plan, base, step, 'plan recover', errors)
    if plan.get('on_failure') not in (None, 'continue', 'stop'):
        errors.append(f'plan on_failure {plan.get("on_failure")!r} must be "continue" or "stop"')
    hook = plan.get('on_failure_hook')
    if isinstance(hook, str):
        try:
            load_step(base, hook)
        except Exception as error:  # noqa: BLE001
            errors.append(f'on_failure_hook {hook!r}: {error!r}')
    elif isinstance(hook, dict) and hook.get('run'):
        try:
            load_step(base, hook['run'])
        except Exception as error:  # noqa: BLE001
            errors.append(f'on_failure_hook {hook["run"]!r}: {error!r}')
    try:
        if 'failure_evidence' in plan:
            evidence_config_of(plan, {})
    except ValueError as error:
        errors.append(str(error))
    harness.validate_declarations(plan, errors)


def publish_gif(raw_path, dest, spec):
    """Trim to the preset duration, scale the full frame to its width, and palette-encode.

    No crop: docs GIFs are the whole frame (GIFS.md). Returns frames, duration, size, sha256 and path.
    Raises GifRefused and deletes dest when the file still breaks the preset.
    """
    from PIL import Image, ImageSequence
    resample = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', Image.BICUBIC)
    limit = int(spec['max_duration_s'] * 1000)
    frames, durations, total = [], [], 0
    with Image.open(raw_path) as im:
        for frame in ImageSequence.Iterator(im):
            dur = int(frame.info.get('duration') or round(1000 / max(spec['fps'], 1)))
            if total >= limit:
                break
            # The first frame can already be longer than the cap (Eden, or Pillow merging identical
            # frames into one held frame). Shorten it; don't keep a clip past the preset.
            if total + dur > limit:
                dur = max(1, limit - total)
            frames.append(frame.convert('RGB'))
            durations.append(max(1, dur))
            total += durations[-1]
            if total >= limit:
                break
    if not frames:
        raise GifRefused(f'gif {spec.get("preset")} captured no frames', {'preset': spec.get('preset'), 'why': 'no frames'})
    width = spec['width']

    def scaled(source):
        out = []
        for frame in source:
            height = max(1, round(width * frame.height / max(frame.width, 1)))
            out.append(frame if frame.size == (width, height) else frame.resize((width, height), resample))
        return out

    def save(source, durs, colors):
        pictures = scaled(source)
        height = pictures[0].height
        sample = [0, len(pictures) // 2, len(pictures) - 1]
        mosaic = Image.new('RGB', (width, height * 3))
        for index, which in enumerate(sample):
            mosaic.paste(pictures[which], (0, index * height))
        palette = mosaic.quantize(colors=max(2, colors), method=Image.Quantize.MEDIANCUT)
        encoded = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in pictures]
        dest.parent.mkdir(parents=True, exist_ok=True)
        encoded[0].save(dest, save_all=True, append_images=encoded[1:], duration=durs[:len(encoded)],
                        loop=0, disposal=1)
        return dest.stat().st_size, len(encoded), pictures[0].size

    colors, step = 128, 1
    size, count, dimensions = save(frames, durations, colors)
    max_bytes = spec.get('max_bytes')
    kept, durs = frames, durations
    while max_bytes and size > max_bytes and (colors > 16 or step * 2 < len(frames)):
        if colors > 16:
            colors //= 2
        else:
            step *= 2
            kept = frames[::step] or frames[:1]
            durs = []
            for index in range(0, len(frames), step):
                durs.append(max(1, sum(durations[index:index + step])))
            durs = durs[:len(kept)]
        size, count, dimensions = save(kept, durs, colors)
    duration_s = round(sum(durs if step > 1 else durations[:count]) / 1000, 3)
    why = None
    if dimensions[0] != width:
        why = f'width is {dimensions[0]}, preset wants {width}'
    elif duration_s > spec['max_duration_s'] + 0.05:
        why = f'duration is {duration_s} s, preset allows {spec["max_duration_s"]} s'
    elif max_bytes and size > max_bytes:
        why = f'size is {size} bytes, preset allows {max_bytes}'
    if why:
        dest.unlink(missing_ok=True)
        raise GifRefused(f'gif breaks preset {spec.get("preset")}: {why}', {
            'preset': spec.get('preset'), 'why': why, 'frames': count, 'duration': duration_s, 'size': size})
    return {'frames': count, 'duration': duration_s, 'size': size, 'sha256': digest(dest), 'path': str(dest),
            'width': dimensions[0], 'fps': spec['fps']}


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
            if not seg.get('sha256'):
                errors.append(f'segment {name}: no sha256')
                continue
            if seg['sha256'] != digest(path):
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
    if 'scale' in plan and plan.get('scale') not in (None, False, 'off', 'fit'):
        errors.append(f'plan scale {plan.get("scale")!r} is not "fit" or "off"')
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
                scale = gate['scale'] if 'scale' in gate else plan.get('scale')
                if scale not in (None, False, 'off', 'fit'):
                    errors.append(f'gate {name}: scale {scale!r} is not "fit" or "off"')
                frame = gate.get('frame')
                if frame is not None and not (isinstance(frame, list) and len(frame) == 2
                                               and all(type(n) is int and n > 0 for n in frame)):
                    errors.append(f'gate {name}: frame must be [width, height]')
                elif gate.get('reference_is_roi') and scale == 'fit' and frame is None:
                    errors.append(f'gate {name}: an ROI reference with scale "fit" needs "frame": [width, height]')
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
    check_plan_policies(plan, base, errors)
    state = plan['start']
    ids = set()
    rows = plan['rows']
    for index, row in enumerate(rows):
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
            if 'ui' in step and step['ui'] not in UI_OPS:
                errors.append(f'row {row["id"]}: ui op {step["ui"]!r} is not one of {", ".join(UI_OPS)}')
            if 'mark' in step:
                marks.add((on, step['mark']))
            if 'copy' in step:
                copies.add((on, step.get('as', step['copy'])))
            if 'json_diff' in step and (on, step['json_diff'].get('before')) not in copies:
                errors.append(f'row {row["id"]}: json_diff before {step["json_diff"].get("before")!r} has no earlier copy')
            if isinstance(step.get('gif'), dict):
                _check_gif(plan, base, row, step, f'row {row["id"]}', errors)
                for nested in step['gif'].get('steps') or []:
                    if not isinstance(nested, dict):
                        errors.append(f'row {row["id"]}: gif step must be an object')
                        continue
                    non = nested.get('on', on)
                    if 'play' in nested and nested['play'] in plan.get('segments', {}):
                        seg = plan['segments'][nested['play']]
                        if not (seg.get('start_gate') or non in checked or nested.get('unchecked')):
                            errors.append(f'row {row["id"]}: play {nested["play"]} on {non} has no start check '
                                          '(a gate step before it, a "start_gate" on the segment, or "unchecked": true)')
                        checked.discard(non)
                    if 'gate' in nested:
                        checked.add(non)
                    if 'run' in nested:
                        checked.add(non) if nested.get('check') else checked.discard(non)
                    if 'library' in nested:
                        checked.discard(non)
            elif 'library' in step:
                _check_known_step(plan, base, step, f'row {row["id"]}', errors)
                checked.discard(on)
        if row.get('on_failure') not in (None, 'continue', 'stop'):
            errors.append(f'row {row["id"]}: on_failure {row.get("on_failure")!r} must be "continue" or "stop"')
        if row.get('start_gate') and row['start_gate'] not in plan.get('gates', {}):
            errors.append(f'row {row["id"]}: unknown start_gate {row["start_gate"]!r}')
        if 'recover' in row:
            if not isinstance(row['recover'], list):
                errors.append(f'row {row["id"]}: recover must be a list of steps')
            else:
                for recover_step in row['recover']:
                    _check_known_step(plan, base, recover_step, f'row {row["id"]} recover', errors)
        if 'gif_presets' in row:
            try:
                for name in merged_gif_presets(plan, row):
                    if name not in (row.get('gif_presets') or {}):
                        continue
                    probe = resolve_gif_spec(plan, row, {'name': name, 'preset': name,
                                                        **({'reason': row['gif_presets'][name].get('reason')}
                                                           if isinstance(row['gif_presets'][name], dict) else {})})
                    api = gif_api_error(probe)
                    if api:
                        errors.append(f'row {row["id"]} gif preset {name}: {api}')
                    elif gif_outside_operator_rules(probe) and not probe.get('reason'):
                        errors.append(f'row {row["id"]} gif preset {name} leaves the docs/evidence rules and needs a "reason"')
            except ValueError as error:
                errors.append(f'row {row["id"]}: {error}')
        hook = row.get('on_failure_hook')
        hook_spec = hook.get('run') if isinstance(hook, dict) else hook
        if isinstance(hook_spec, str):
            try:
                load_step(base, hook_spec)
            except Exception as error:  # noqa: BLE001
                errors.append(f'row {row["id"]}: on_failure_hook {hook_spec!r}: {error!r}')
        if 'failure_evidence' in row:
            try:
                evidence_config_of(plan, row)
            except ValueError as error:
                errors.append(f'row {row["id"]}: {error}')
        policy = row.get('on_failure') or plan.get('on_failure')
        if policy == 'continue' and index + 1 < len(rows):
            nxt = rows[index + 1]
            if not start_gate_of(plan, nxt):
                errors.append(f'row {row["id"]}: on_failure continue needs a start gate on {nxt["id"]}')
            if recover_steps_of(plan, nxt) is None:
                errors.append(f'row {row["id"]}: on_failure continue needs a recover list on {nxt["id"]} or the plan')
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


def node_text(node, key):
    """A ui.tree widget's text/title/tooltip: top level for buttons and labels, but only under
    properties.<name>.value for some widgets (QMessageBox text, window titles), as pkg4 found on 2026-11-03."""
    value = node.get(key)
    if value in (None, ''):
        props = node.get('properties') or {}
        for name in {'title': ('windowTitle', 'title'), 'text': ('text',), 'tooltip': ('toolTip', 'tooltip')}[key]:
            entry = props.get(name)
            value = entry.get('value') if isinstance(entry, dict) else entry
            if value not in (None, ''):
                break
    return '' if value is None else str(value)


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


class RecoveryFailed(RuntimeError):
    """Getting the next row back to its start gate failed. The run stops; Eden is not rebooted."""


class Runner:
    # Filled after the class body. A plugin hook can add a kind: step_handlers['kind'] = fn(self, on, step)
    # and step_kind.extra = tuple(step_handlers).
    step_handlers = {}

    def __init__(self, manifest_path, instances, out, pace_minutes, client_factory=None, on_failure=None):
        self.manifest_path = manifest_path
        self.base = manifest_path.parent
        self.plan = expand_library(json.loads(manifest_path.read_text()))
        self.instances = instances
        self.default = next(iter(instances))
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.row_dir = self.out
        self.on_failure_cli = on_failure
        self.outcomes = {}
        self.recovered_to = None
        self.current_on = self.default
        self.current_row = None
        if client_factory is None:
            client_factory = default_client_factory()
        self.clients = {key: client_factory(inst['socket']) for key, inst in instances.items()}
        pace = self.out / 'pace.json'
        if not pace.exists():
            pace.write_text(json.dumps({'started': time.time(), 'first_row_done': None}) + '\n')
        self.pace = json.loads(pace.read_text())
        self.pace_seconds = pace_minutes * 60
        self.capture_fixtures = False
        self.cause_counts = {}
        self.early_stop = None
        self.screen_reached = False
        self.last_gate_outcome = None
        self.gate_saw_evidence = []
        self.scales_used = []
        self._arm_frames = False
        self._gate_baseline = None
        self._gate_latest = None

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
        if self._arm_frames:
            try:
                data = path.read_bytes()
            except OSError:
                data = None
            if data is not None:
                if self._gate_baseline is None:
                    self._gate_baseline = data
                self._gate_latest = data
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

    def send_lease(self, on, method, params, lease_ms):
        """One held input whose lease is inside Eden's 1..10000 ms range. A value outside it is a runner bug."""
        lease_ms = int(lease_ms)
        if lease_ms < LEASE_MIN_MS or lease_ms > LEASE_MAX_MS:
            raise RuntimeError(f'{method} lease_ms {lease_ms} is outside {LEASE_MIN_MS}..{LEASE_MAX_MS}')
        body = dict(params)
        body['lease_ms'] = lease_ms
        self.call(on, method, body)

    def issue_hold(self, on, method, params, lease_ms, renew):
        """Send the first lease. A request past 10000 ms is split: the rest is renewed from hold()."""
        parts = lease_parts(lease_ms)
        self.send_lease(on, method, params, parts[0])
        if len(parts) > 1:
            renew.append((method, params))

    def hold(self, on, frames, ms, renew=None):
        """Wait for `frames` game frames of the recording (macro.status frame) if given, else for `ms` wall time.

        `renew` is held inputs to send again before the current lease expires. Eden replaces the previous expiry
        when the same input is pressed again (CLIControl.md), so this is how a hold outlasts 10000 ms."""
        renew = list(renew or [])
        if not frames and not renew:
            time.sleep(ms / 1000)
            return
        start = (self.call(on, 'macro.status') or {}).get('frame', 0) if frames else None
        deadline = None if frames else time.monotonic() + ms / 1000
        frame_deadline = time.monotonic() + max(5.0, frames / 10) if frames else None
        period = (LEASE_MAX_MS - LEASE_RENEW_MARGIN_MS) / 1000
        next_renew = time.monotonic() + period if renew else None
        while True:
            now = time.monotonic()
            if frames:
                if (self.call(on, 'macro.status') or {}).get('frame', 0) - start >= frames:
                    return
                if now > frame_deadline:
                    raise RuntimeError(
                        f'recording on {on} did not advance {frames} frames in time (game paused or stalled?)')
            elif now >= deadline:
                return
            if next_renew is not None and now >= next_renew:
                for method, params in renew:
                    self.send_lease(on, method, params, LEASE_MAX_MS)
                next_renew = time.monotonic() + period
            slice_s = 0.01 if frames else 0.05
            if next_renew is not None:
                slice_s = min(slice_s, max(0.0, next_renew - time.monotonic()))
            if deadline is not None:
                slice_s = min(slice_s, max(0.0, deadline - time.monotonic()))
            if slice_s > 0:
                time.sleep(slice_s)

    def drop_recording(self, on, path):
        """A failed recording leaves no macro file and no sha256. Eden may still have saved one on macro.stop."""
        for method in ('input.reset', 'macro.stop'):
            try:
                self.call(on, method)
            except Exception:
                pass
        path.unlink(missing_ok=True)

    def recipe_action(self, on, action):
        # "frames": hold for that many game frames (macro.status frame), not wall time: the game's speed
        # varies, so a 400 ms hold recorded 11 frames once and 35 another time (lr-03, 2026-11-03).
        # A frame hold's wall time can pass 10 s, so those inputs are renewed until the frame count arrives.
        frames = action.get('frames')
        renew = []
        if 'buttons' in action:
            hold_ms = action.get('hold', 150)
            lease = LEASE_MAX_MS if frames else hold_ms + 500
            buttons = [b for b in action['buttons'].split(',') if b]
            if 'with_stick' in action:  # stick held through the press, e.g. Down+B
                x, y = action['with_stick']
                params = {'stick': 'left', 'x': x, 'y': y}
                # The extra 200 ms used to be one lease of `lease + 200`. At the 10000 ms frame lease that is
                # 10200, which Eden rejects. Cap the first lease and renew when the hold outlasts it.
                self.issue_hold(on, 'input.stick', params, lease + 200, renew)
                if frames and ('input.stick', params) not in renew:
                    renew.append(('input.stick', params))
                time.sleep(action.get('stick_lead_ms', 50) / 1000)
            for button in buttons:
                params = {'button': button, 'state': 'press'}
                self.issue_hold(on, 'input.button', params, lease, renew)
                if frames and ('input.button', params) not in renew:
                    renew.append(('input.button', params))
            self.hold(on, frames, hold_ms, renew)
            for button in buttons:
                self.call(on, 'input.button', {'button': button, 'state': 'release'})
            if 'with_stick' in action:
                self.send_lease(on, 'input.stick', {'stick': 'left', 'x': 0, 'y': 0}, 100)
        elif 'stick' in action:
            x, y = action['stick'][:2]
            ms = action['stick'][2] if len(action['stick']) > 2 else 250
            lease = LEASE_MAX_MS if frames else ms
            params = {'stick': 'left', 'x': x, 'y': y}
            self.issue_hold(on, 'input.stick', params, lease, renew)
            if frames and ('input.stick', params) not in renew:
                renew.append(('input.stick', params))
            self.hold(on, frames, ms, renew)
            self.send_lease(on, 'input.stick', {'stick': 'left', 'x': 0, 'y': 0}, 100)
        self.hold(on, action.get('gap_frames'), action.get('gap', .6) * 1000)

    def record(self, on, name, seg):
        path = self.base / seg['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        self.call(on, 'macro.record', {'name': name, 'path': str(path), 'player': seg.get('player', 0),
                                       'countdown_ms': 0})
        try:
            for action in seg['recipe']:
                self.recipe_action(on, action)
            self.call(on, 'input.reset')
            time.sleep(.12)  # one neutral frame before the recording closes
            self.call(on, 'macro.stop')
        except Exception:
            self.drop_recording(on, path)
            raise
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
            if not seg.get('recipe'):
                raise RuntimeError(f'segment {name} is missing and has no recipe to record')
            self.record(on, name, seg)  # the recording itself reaches the segment's end screen
            return
        if not seg.get('sha256'):
            raise RuntimeError(
                f'segment {name} has no sha256; refusing to play an unhashed recording ({path.name})')
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
    def same_aspect(size_a, size_b):
        """True when two sizes are the same shape, with about a pixel of slack. A crop is not the frame's shape."""
        aw, ah = size_a
        bw, bh = size_b
        if min(aw, ah, bw, bh) < 1:
            return False
        return abs(aw * bh - bw * ah) <= max(ah, bh)

    @staticmethod
    def _mean_diff(left, right):
        """Mean absolute difference per colour byte. Infinity when the two images are not the same size."""
        first, second = left.tobytes(), right.tobytes()
        if len(first) != len(second) or not first:
            return float('inf')
        return sum(abs(x - y) for x, y in zip(first, second)) / len(first)

    @staticmethod
    def compare_region(path_a, path_b, roi, b_is_roi=False, scale=None, reference_frame=None, tolerance=None):
        """Mean difference of the ROI, and a note when the live frame was scaled to the reference.

        scale "fit" resizes the frame to the reference size when the aspect matches. The ROI is in reference
        coordinates, applied after that. A reference that is already the ROI (b_is_roi) does not carry the frame
        size: reference_frame is the size those coordinates were captured at, and the live frame is resized to
        that before the crop. When that compare is still over tolerance, both crops are compared again at half
        their size. A 0.5x render shown in a full-size, blurrier frame matches there; a different screen does not.
        Returns (difference, note or None)."""
        from PIL import Image
        with Image.open(path_a) as im_a, Image.open(path_b) as im_b:
            frame = im_a.convert('RGB')
            reference = im_b.convert('RGB')
            note = None
            live = [frame.size[0], frame.size[1]]
            if scale == 'fit' and b_is_roi and reference_frame:
                if not (isinstance(reference_frame, (list, tuple)) and len(reference_frame) == 2
                        and all(type(n) is int and n > 0 for n in reference_frame)):
                    raise RuntimeError(f'frame {reference_frame!r} is not [width, height]')
                target = (reference_frame[0], reference_frame[1])
                if frame.size != target and Runner.same_aspect(frame.size, target):
                    note = {'scale': 'fit', 'frame': live, 'reference': [target[0], target[1]]}
                    frame = frame.resize(target, Image.Resampling.BOX)
                region = frame.crop(tuple(roi))
                other = reference
            else:
                if (scale == 'fit' and not b_is_roi and frame.size != reference.size
                        and Runner.same_aspect(frame.size, reference.size)):
                    note = {'scale': 'fit', 'frame': live, 'reference': [reference.size[0], reference.size[1]]}
                    frame = frame.resize(reference.size, Image.Resampling.BOX)
                region = frame.crop(tuple(roi))
                other = reference if b_is_roi else reference.crop(tuple(roi))
            if region.size != other.size:
                return float('inf'), note
            diff = Runner._mean_diff(region, other)
            small = (max(1, region.size[0] // 2), max(1, region.size[1] // 2))
            # Half the ROI is the 0.5x content. Bilinear, not BOX: a linear upsample of that content
            # stays just over tolerance 8 under a box filter and matches under this one.
            if (scale == 'fit' and b_is_roi and reference_frame and tolerance is not None
                    and diff > tolerance and small != region.size):
                coarse = Runner._mean_diff(region.resize(small, Image.Resampling.BILINEAR),
                                            other.resize(small, Image.Resampling.BILINEAR))
                if coarse <= tolerance:
                    if note is None:
                        note = {'scale': 'fit', 'frame': live, 'reference': [reference_frame[0], reference_frame[1]]}
                    note['compared'] = [small[0], small[1]]
                    diff = coarse
            return diff, note

    @staticmethod
    def roi_diff(path_a, path_b, roi, b_is_roi=False):
        """Mean absolute difference per colour byte of the region in both images. With b_is_roi, image b already
        is just that region (library references keep only the compared region)."""
        return Runner.compare_region(path_a, path_b, roi, b_is_roi)[0]

    def image_scale(self, gate):
        """"fit" on the gate, or on the plan when the gate doesn't say. Anything else is off, except a typo."""
        value = gate['scale'] if 'scale' in gate else self.plan.get('scale')
        if value in (None, False, 'off'):
            return None
        if value == 'fit':
            return 'fit'
        raise RuntimeError(f'scale {value!r} is not "fit" or "off"')

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
        self._gate_evidence = True
        gate = self.plan['gates'][name]
        self._arm_frames = True
        self._gate_baseline = None
        self._gate_latest = None
        missing = harness.gate_inputs_missing(self, on, gate)
        try:
            result = self.check_gate_spec(on, gate, name)
        except RuntimeError as error:
            timed_out = 'did not pass within' in str(error) or 'never settled' in str(error)
            if timed_out:
                self.last_gate_outcome = harness.timeout_facts(
                    self, on, name, gate, missing, gate.get('tolerance', DEFAULT_TOLERANCE))
            else:
                self.last_gate_outcome = {'gate': name, 'on': on, 'role': harness.assertion_role(gate),
                                          'timed_out': False, 'screen_unchanged': None,
                                          'missing_evidence': missing, 'screen_reached': self.screen_reached}
            raise
        else:
            if harness.assertion_role(gate) == 'screen':
                self.screen_reached = True
            self.last_gate_outcome = {'gate': name, 'passed': True, 'role': harness.assertion_role(gate),
                                      'screen_reached': self.screen_reached}
            self.gate_saw_evidence.append(self._gate_evidence)
            return result
        finally:
            self._arm_frames = False

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
            scale = None if hook is not None else self.image_scale(gate)
            while time.monotonic() < until:
                path = self.shot(on, 'gate-' + name)
                note = None
                hook_evidence = None
                if hook is not None:
                    outcome = hook(str(path), gate)
                    passed = bool(outcome)
                    # Bare True checked the row and named nothing. Any other truthy result is the evidence.
                    if passed and outcome is not True:
                        try:
                            json.dumps(outcome)
                            hook_evidence = outcome
                        except TypeError:
                            hook_evidence = repr(outcome)[:300]
                    self._gate_evidence = hook_evidence is not None
                else:
                    diff, note = self.compare_region(
                        path, self.base / gate['reference'], gate['roi'], gate.get('reference_is_roi'), scale,
                        gate.get('frame'), gate.get('tolerance', DEFAULT_TOLERANCE))
                    passed = diff <= gate.get('tolerance', DEFAULT_TOLERANCE)
                if passed:
                    record = {'gate': name, 'on': on, 'pass': True, 'shot': str(path)}
                    if note:
                        record.update(note)
                        self.last_image_scale = {'gate': name, **note}
                        self.scales_used.append(self.last_image_scale)
                    if hook is not None:
                        record['hook_evidence'] = hook_evidence
                        self.last_hook_evidence = hook_evidence
                    self.log(record)
                    return str(path)
                path.unlink(missing_ok=True)  # keep only the passing shot and the final failing one
                time.sleep(gate.get('interval', 1))
            self.shot(on, 'GATE-FAILED-' + name)
            raise RuntimeError(f'gate {name} on {on} did not pass within {gate["cap"]} s')
        # Non-image gates (log, json, ui, status, change) save what the game showed when they gave up, so a
        # failure can be told apart afterwards: wrong gate, or a real error dialog (pkg4's guest join, 2026-11-03).
        self.failure_evidence(on, name)
        raise RuntimeError(f'gate {name} on {on} did not pass within {gate["cap"]} s')

    def failure_evidence(self, on, name):
        """Best effort: the widget tree (with every dialog's text) and a screenshot, into the row folder."""
        try:
            tree = self.clients[on].request('ui.tree', {})
            (self.row_dir / f'{on}-GATE-FAILED-{name}-tree.json').write_text(json.dumps(tree.get('result'), indent=1))
            dialogs = [{'class': n.get('class'), 'name': n.get('name'), 'title': node_text(n, 'title'),
                        'text': node_text(n, 'text')} for n in tree.get('result') or []
                       if n.get('visible') and (n.get('class') in ('QMessageBox', 'QDialog', 'QLabel')
                                                 or 'Dialog' in str(n.get('class')))]
            self.log({'gate': name, 'on': on, 'pass': False, 'visible_dialog_widgets': dialogs[:20]})
        except Exception as error:  # noqa: BLE001 - evidence is best effort; the gate failure is the result
            self.log({'gate': name, 'on': on, 'failure_evidence_error': repr(error)})
        try:
            self.shot(on, 'GATE-FAILED-' + name)
        except Exception as error:  # noqa: BLE001
            self.log({'gate': name, 'on': on, 'failure_shot_error': repr(error)})

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
                if not re.search(want, node_text(node, key).replace('&', '')):
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
    def snapshot_manifest(self):
        """Keep the exact rows.json bytes a receipt names: recording rewrites rows.json in place (new sha256s), so
        out/manifests/<sha256>.json holds every version a row started from (the lead's lr-04 gap)."""
        data = self.manifest_path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        target = self.out / 'manifests' / f'{sha}.json'
        if not target.exists():
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
        return sha

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

    def note(self, row_id, status, **extra):
        item = {'status': status}
        for key, value in extra.items():
            if value is not None and value != []:
                item[key] = value
        self.outcomes[row_id] = item

    def stop_input(self, row, text):
        (self.out / 'STOP-INPUT').write_text(text if str(text).endswith('\n') else f'{text}\n')

    def execute_step(self, step, into=None):
        try:
            kind = step_kind(step)
        except RuntimeError:
            raise RuntimeError(f'unknown step {step}') from None
        handler = self.step_handlers.get(kind)
        if handler is None:
            raise RuntimeError(f'unknown step {step}')
        on = step.get('on', self.default) if isinstance(step, dict) else self.default
        self.current_on = on
        result = handler(self, on, step)
        if into is not None and result is not None:
            into.append(result)
        return result

    def _step_play(self, on, step):
        self.play(on, step['play'])
        return {'play': step['play'], 'on': on}

    def _step_gate(self, on, step):
        self.last_image_scale = None
        self.last_hook_evidence = None
        evidence = self.gate(on, step['gate'])
        result = {'gate': step['gate'], 'on': on, 'evidence': evidence}
        if self.last_image_scale:
            result['scale'] = self.last_image_scale
        if self.last_hook_evidence is not None:
            result['hook_evidence'] = self.last_hook_evidence
        return result

    def _step_shot(self, on, step):
        return {'shot': str(self.shot(on, step['shot']))}

    def _step_mark(self, on, step):
        self.marks[(on, step['mark'])] = self.shot(on, 'mark-' + step['mark'])
        return {'mark': step['mark'], 'on': on}

    def _step_copy(self, on, step):
        source = self.source(on, step['copy'])
        target = self.row_dir / f'{step.get("as", step["copy"])}-{on}-{source.name}'
        target.write_bytes(source.read_bytes())
        self.copies[(on, step.get('as', step['copy']))] = target
        return {'copy': str(target)}

    def _step_json_diff(self, on, step):
        return {'json_diff': self.json_diff(on, step['json_diff'])}

    def _step_ui(self, on, step):
        self.ui(on, step)
        return {'ui': step['ui'], 'on': on, 'target': step.get('target')}

    def _step_run(self, on, step):
        hook = load_step(self.base, step['run'])
        value = hook(StepContext(self, on), **step.get('args', {}))
        if not isinstance(value, (str, int, float, bool, list, dict, type(None))):
            value = repr(value)
        return {'run': step['run'], 'on': on, 'value': value}

    def _step_call(self, on, step):
        # "@key" values come from instances.json, e.g. {"call": "game.load", "params": {"path": "@game"}}
        params = {k: (self.instances[on][v[1:]] if isinstance(v, str) and v.startswith('@') else v)
                  for k, v in step.get('params', {}).items()}
        self.call(on, step['call'], params, job_cap=step.get('job_cap', 15))
        return {'call': step['call'], 'on': on, 'params': params}

    def _step_wait(self, on, step):
        self.check_stop()
        time.sleep(step['wait_ms'] / 1000)
        return None

    def play_library(self, on, name):
        """Play a shared-library segment by index name, or a plan segment that already imported it."""
        for seg_name, seg in self.plan.get('segments', {}).items():
            if seg_name == name or seg.get('library') == name:
                self.play(on, seg_name)
                return
        entry = library_index().get('segments', {}).get(name)
        if entry is None:
            raise RuntimeError(f'library segment {name!r} is not in the plan or {LIBRARY / "index.json"}')
        path = LIBRARY / entry['file']
        if not path.is_file():
            raise RuntimeError(f'library segment {name} file missing: {path}')
        if entry.get('sha256') and digest(path) != entry['sha256']:
            raise RuntimeError(f'library segment {name} sha256 mismatch')
        if entry.get('start_gate') and entry['start_gate'] in self.plan.get('gates', {}):
            self.gate(on, entry['start_gate'])
        self.call(on, 'macro.play', {'path': str(path)})
        self.wait_macro(on, entry.get('cap', 60))

    def _step_library(self, on, step):
        self.play_library(on, step['library'])
        return {'library': step['library'], 'on': on}

    def _step_gif(self, on, step):
        gif = step['gif']
        row = self.current_row or {}
        spec = resolve_gif_spec(self.plan, row, gif)
        api = gif_api_error(spec)
        if api:
            raise GifRefused(f'gif {gif.get("name")}: {api}', {'why': api, 'preset': spec.get('preset')})
        if 'crop' in gif:
            why = 'gif records the full frame and does not crop'
            raise GifRefused(f'gif {gif.get("name")}: {why}', {'why': why, 'preset': spec.get('preset')})
        if gif_outside_operator_rules(spec) and not spec.get('reason'):
            why = 'override outside the GIF rules needs a reason'
            raise GifRefused(f'gif {gif.get("name")}: {why}', {'why': why, 'preset': spec.get('preset')})
        root = Path(self.plan['root']) if self.plan.get('root') else project_root(self.base)
        try:
            dest = gif_dest_path(root, self.row_dir, gif)
        except ValueError as error:
            raise GifRefused(f'gif {gif.get("name")}: {error}', {'why': str(error), 'preset': spec.get('preset')}) from error
        slug = re.sub(r'[^A-Za-z0-9._-]+', '-', gif['name']).strip('-') or 'clip'
        raw = self.row_dir / f'{slug}.{time.time_ns()}.capture.gif'
        self.call(on, 'recording.start', {'path': str(raw), 'fps': spec['fps'], 'width': spec['width'],
                                          'duration_ms': int(spec['max_duration_s'] * 1000)})
        nested = []
        try:
            for child in gif.get('steps') or []:
                self.execute_step(child, nested)
        finally:
            try:
                self.call(on, 'recording.stop', {})
            except Exception as error:  # noqa: BLE001 - a failed moment still has to release the recorder
                self.log({'gif': gif.get('name'), 'stop_error': repr(error)})
        status = self.call(on, 'recording.status', {}) or {}
        if isinstance(status, dict) and (status.get('state') == 'error' or status.get('error')):
            why = f'recording status {status.get("state")}: {status.get("error")}'
            raise GifRefused(f'gif {gif.get("name")}: {why}', {'why': why, 'preset': spec.get('preset')})
        if not raw.is_file():
            why = 'Eden wrote no gif'
            raise GifRefused(f'gif {gif.get("name")}: {why}', {'why': why, 'preset': spec.get('preset')})
        info = publish_gif(raw, dest, spec)
        info['steps'] = nested
        info['preset'] = spec['preset']
        if spec.get('reason'):
            info['reason'] = spec['reason']
        if isinstance(status, dict):
            if status.get('frames') is not None:
                info['captured_frames'] = status['frames']
            if status.get('dropped_frames') is not None:
                info['dropped_frames'] = status['dropped_frames']
        return {'gif': info}

    def game_running(self, on):
        """False when status says the game stopped or crashed, or the control call itself failed."""
        try:
            status = self.call(on, 'status') or {}
        except Exception as error:  # noqa: BLE001
            self.log({'on': on, 'status_error': repr(error)})
            return False
        if not isinstance(status, dict):
            return True
        if status.get('running') is False:
            return False
        if status.get('crashed') or status.get('crash') or status.get('state') in ('crashed', 'crash'):
            return False
        return True

    def recover_to(self, row):
        """Play the row or plan recover list on the row's instance, then require its start gate.

        A shared recover list names no instance, so each step is bound to the row's own
        instance before it runs. Does not reboot Eden.
        """
        gate_name = start_gate_of(self.plan, row)
        if not gate_name:
            raise RecoveryFailed(f'recovery to {row["id"]}: no start gate')
        steps = recover_steps_of(self.plan, row)
        if steps is None:
            raise RecoveryFailed(f'recovery to {row["id"]}: no recover list')
        on = row.get('on', self.default)
        saved_row = self.current_row
        saved_on = self.current_on
        self.current_row = row
        self.current_on = on
        try:
            for step in steps:
                self.execute_step(harness.bind_step(step, on))
            self.gate(on, gate_name)
        except RecoveryFailed:
            raise
        except StopRun:
            raise
        except Exception as error:
            raise RecoveryFailed(f'recovery to {row["id"]} failed: {error}') from error
        finally:
            self.current_row = saved_row
            self.current_on = saved_on

    def _gate_names(self, row):
        def walk(steps):
            for step in steps or []:
                if not isinstance(step, dict):
                    continue
                if 'gate' in step:
                    yield step['gate']
                gif = step.get('gif')
                if isinstance(gif, dict):
                    yield from walk(gif.get('steps'))
        return list(walk(row.get('steps')))

    def collect_failure_evidence(self, row, error):
        """Screenshot, log tails for the gates this row read, trace counters and a trace read when tracing is on.
        on_failure_hook may add paths. Best effort: a failure here does not hide the row's own error."""
        cfg = evidence_config_of(self.plan, row)
        paths = []
        on = self.current_on or self.default
        if cfg.get('screenshot', True):
            try:
                paths.append(str(self.shot(on, 'FAIL')))
            except StopRun:
                raise
            except Exception as shot_error:  # noqa: BLE001
                self.log({'on': on, 'failure_shot_error': repr(shot_error)})
        if cfg.get('log_tails', True):
            tail = int(cfg.get('tail_lines', 80) or 0)
            seen = set()
            for gate_name in self._gate_names(row):
                gate = self.plan.get('gates', {}).get(gate_name) or {}
                if gate.get('kind') != 'log' or not gate.get('source') or gate['source'] in seen:
                    continue
                seen.add(gate['source'])
                for side in self.instances:
                    source = self.source(side, gate['source'])
                    if not source.is_file():
                        continue
                    lines = source.read_text(errors='replace').splitlines()[-tail:]
                    dest = self.row_dir / f'log-tail-{side}-{gate["source"]}.txt'
                    dest.write_text('\n'.join(lines) + ('\n' if lines else ''))
                    paths.append(str(dest))
        want_trace = cfg.get('trace', 'auto')
        traced = [key for key, inst in self.instances.items() if isinstance(inst, dict) and (inst.get('trace_jsonl') or inst.get('trace'))]
        if want_trace is True or (want_trace == 'auto' and (traced or self.plan.get('trace'))):
            targets = traced or ([on] if self.plan.get('trace') or want_trace is True else [])
            for side in targets:
                for method in ('trace.counters', 'trace.read'):
                    try:
                        value = self.call(side, method, {})
                    except Exception as trace_error:  # noqa: BLE001
                        value = {'error': repr(trace_error)}
                    dest = self.row_dir / f'{side}-{method.replace(".", "-")}.json'
                    dest.write_text(json.dumps(value, indent=2, default=str) + '\n')
                    paths.append(str(dest))
        hook = row.get('on_failure_hook', self.plan.get('on_failure_hook'))
        if hook:
            spec = hook.get('run') if isinstance(hook, dict) else hook
            args = hook.get('args', {}) if isinstance(hook, dict) else {}
            try:
                ctx = StepContext(self, on)
                ctx.failure = error
                value = load_step(self.base, spec)(ctx, **args)
            except StopRun:
                raise
            except Exception as hook_error:  # noqa: BLE001
                self.log({'on_failure_hook_error': repr(hook_error)})
                value = None
            extra = [value] if isinstance(value, (str, Path)) else value if isinstance(value, list) else []
            for item in extra:
                if item:
                    paths.append(str(item))
        return paths

    def classify_failure(self, row, error):
        """Tag one failure and count its cause. Early stop is a result, not another reboot."""
        facts = dict(self.last_gate_outcome or {})
        text = str(error)
        if 'starts on' in text and 'but the game is on' in text:
            facts['start_expected'] = row.get('start')
            facts['start_actual'] = text.rsplit(' on ', 1)[-1]
        if isinstance(error, RecoveryFailed):
            facts['recovery'] = True
            facts['recovery_target'] = row.get('id')
        if isinstance(error, StopRun) and not facts.get('start_expected'):
            kind, cause = 'harness-suspect', 'runner-stop'
        else:
            kind, cause = harness.classify_failure(error, facts)
        self.cause_counts[cause] = self.cause_counts.get(cause, 0) + 1
        count = self.cause_counts[cause]
        limit = harness.early_stop_limit(self.plan)
        early = None
        if cause != 'runner-stop' and limit is not None and count >= limit:
            early = {'result': f'probable shared cause: {cause}', 'cause': cause, 'kind': kind, 'count': count}
            self.early_stop = early
        return kind, cause, early

    def apply_failure(self, row, receipt, error, evidence=None):
        kind, cause, early = self.classify_failure(row, error)
        if receipt is not None:
            receipt['failure_kind'] = kind
            receipt['failure_cause'] = cause
            if early:
                receipt['early_stop'] = early
        self.note(row['id'], 'fail', reason=repr(error), evidence=evidence,
                  failure_kind=kind, failure_cause=cause)
        return early

    def replay_checks(self, row_id):
        """Run gate steps and project step hooks for one row. Plays are not repeated."""
        row = next(item for item in self.plan['rows'] if item['id'] == row_id)
        self.current_row = row
        self.row_dir = self.out / str(row_id)
        self.row_dir.mkdir(parents=True, exist_ok=True)
        self.row_offsets = {}
        self.screen_reached = False
        checks = []
        for step in row.get('steps') or []:
            if not isinstance(step, dict):
                continue
            try:
                kind = step_kind(step)
            except RuntimeError:
                checks.append({'step': 'unknown', 'name': None, 'ok': False, 'error': f'unknown step {step!r}'})
                continue
            if kind not in ('gate', 'run'):
                continue
            try:
                self.execute_step(step)
                checks.append({'step': kind, 'name': step.get(kind), 'ok': True})
            except Exception as error:  # noqa: BLE001 - replay reports the check; it does not boot Eden
                checks.append({'step': kind, 'name': step.get(kind), 'ok': False, 'error': str(error)})
        return {'ok': bool(checks) and all(item['ok'] for item in checks), 'row': row_id, 'checks': checks}

    def self_test(self):
        return harness.self_test_boot(self)

    def write_summary(self):
        rows = []
        failed = 0
        by_kind, by_cause = {}, {}
        for row in self.plan['rows']:
            info = {'id': row['id'], **self.outcomes.get(row['id'], {'status': 'not run'})}
            if info['status'] == 'fail':
                failed += 1
                by_kind.setdefault(info.get('failure_kind', 'unknown'), []).append(row['id'])
                by_cause.setdefault(info.get('failure_cause', 'unclassified'), []).append(row['id'])
            rows.append(info)
        payload = {'rows': rows, 'failed': failed, 'by_kind': by_kind, 'by_cause': by_cause}
        if self.early_stop:
            payload['early_stop'] = self.early_stop
            payload['result'] = self.early_stop['result']
        (self.out / 'summary.json').write_text(json.dumps(payload, indent=2) + '\n')
        return failed

    def run(self, only=None):
        self.outcomes = {}
        self.recovered_to = None
        self.cause_counts = {}
        self.early_stop = None
        try:
            return self._run_rows(only)
        finally:
            self.write_summary()

    def _run_rows(self, only):
        previous_end = self.plan['start']
        rows = self.plan['rows']
        review = None
        for index, row in enumerate(rows):
            self.current_row = row
            receipt_path = self.out / f'{row["id"]}.json'
            prior_status = None
            prior = {}
            if receipt_path.exists():
                prior = json.loads(receipt_path.read_text())
                prior_status = prior.get('status')
            if prior_status == 'PASS':
                self.note(row['id'], 'pass')
                previous_end = row['end']
                continue
            if only is not None and row['id'] not in only:
                self.note(row['id'], 'skipped', receipt=prior_status)
                continue
            if only is None and row['start'] != previous_end and self.recovered_to != row['id']:
                mismatch = StopRun(f'row {row["id"]} starts on {row["start"]}, but the game is on {previous_end}')
                early = self.apply_failure(row, None, mismatch)
                if early:
                    raise harness.EarlyStop(early['result'])
                raise mismatch
            self.row_dir = self.out / row['id']
            self.row_dir.mkdir(exist_ok=True)
            self.row_offsets = self.log_offsets()
            self.marks, self.copies, self.acquired = {}, {}, []
            self.scales_used, self.gate_saw_evidence = [], []
            self.last_image_scale = None
            self.last_hook_evidence = None
            self.screen_reached = False
            self.last_gate_outcome = None
            receipt = {'id': row['id'], 'start': row['start'], 'end': row['end'], 'evidence': row.get('evidence', []),
                       'manifest_sha256': self.snapshot_manifest(), 'started': time.time(), 'results': []}
            try:
                if (self.recovered_to != row['id'] and prior_status == 'FAILED'
                        and start_gate_of(self.plan, row) and recover_steps_of(self.plan, row) is not None):
                    self.recover_to(row)
                for step in row['steps']:
                    self.execute_step(step, receipt['results'])
                receipt['status'] = 'PASS'
                if self.scales_used:
                    receipt['scale'] = self.scales_used
                if self.gate_saw_evidence and not any(self.gate_saw_evidence):
                    receipt['warnings'] = ['passed with no screen or log evidence from its gates']
                self.note(row['id'], 'pass')
                if self.capture_fixtures or self.plan.get('capture_fixtures'):
                    receipt['fixture'] = str(harness.capture_passing_row(self, row, self.out / 'fixtures' / row['id']))
                if not self.pace['first_row_done']:
                    self.pace['first_row_done'] = time.time()
                    (self.out / 'pace.json').write_text(json.dumps(self.pace) + '\n')
            except StopRun as error:
                receipt.update(status='FAILED', error=repr(error))
                early = self.apply_failure(row, receipt, error)
                if early:
                    self.stop_input(row, early['result'])
                    raise harness.EarlyStop(early['result'])
                raise
            except Exception as error:
                receipt.update(status='FAILED', error=repr(error))
                if isinstance(error, GifRefused):
                    receipt['gif_refused'] = error.detail
                evidence = []
                try:
                    evidence = self.collect_failure_evidence(row, error)
                except StopRun:
                    raise
                except Exception as evidence_error:  # noqa: BLE001
                    self.log({'failure_evidence_error': repr(evidence_error)})
                receipt['evidence_paths'] = evidence
                early = self.apply_failure(row, receipt, error, evidence=evidence)
                if early:
                    self.stop_input(row, early['result'])
                    raise harness.EarlyStop(early['result'])
                successor = rows[index + 1] if index + 1 < len(rows) else None
                policy = failure_policy_of(self.plan, row, successor, self.on_failure_cli)
                if isinstance(error, RecoveryFailed) or policy != 'continue' or successor is None:
                    self.stop_input(row, f'{row["id"]}: {error!r}')
                    raise
                if not self.game_running(self.current_on or self.default):
                    receipt['recovery'] = {'ok': False, 'error': 'game is not running'}
                    self.stop_input(row, f'{row["id"]}: {error!r}')
                    raise
                saved_dir = self.row_dir
                self.row_dir = self.out / successor['id']
                self.row_dir.mkdir(exist_ok=True)
                try:
                    self.recover_to(successor)
                except Exception as recovered:
                    receipt['recovery'] = {'to': successor['id'], 'ok': False, 'error': repr(recovered)}
                    self.stop_input(row, f'{row["id"]}: {error!r}; recovery to {successor["id"]} failed: {recovered!r}')
                    raise recovered
                finally:
                    self.row_dir = saved_dir
                receipt['recovery'] = {'to': successor['id'], 'ok': True}
                self.recovered_to = successor['id']
            except BaseException as error:
                receipt.update(status='FAILED', error=repr(error))
                self.stop_input(row, f'{row["id"]}: {error!r}')
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
            if receipt['status'] == 'PASS':
                previous_end = row['end']
                if self.acquired:
                    # A new reference is a claim about what the screen should look like: a model looks once, then reruns.
                    print(json.dumps({'status': 'REVIEW', 'row': row['id'], 'acquired': self.acquired,
                                      'next': 'look at each new reference; if one is wrong, delete it and this row\'s '
                                              'receipt and fix the route; then run again'}), flush=True)
                    review = 'REVIEW'
                    break
        failed = any(item.get('status') == 'fail' for item in self.outcomes.values())
        if review:
            return review
        status = 'FAILED' if failed else 'DONE'
        print(json.dumps({'status': status, 'out': str(self.out),
                          'failed': sum(1 for item in self.outcomes.values() if item.get('status') == 'fail')}), flush=True)
        return status


Runner.step_handlers = {
    'play': Runner._step_play,
    'gate': Runner._step_gate,
    'shot': Runner._step_shot,
    'mark': Runner._step_mark,
    'copy': Runner._step_copy,
    'json_diff': Runner._step_json_diff,
    'ui': Runner._step_ui,
    'run': Runner._step_run,
    'call': Runner._step_call,
    'wait_ms': Runner._step_wait,
    'gif': Runner._step_gif,
    'library': Runner._step_library,
}
step_kind.extra = tuple(Runner.step_handlers)


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
    parser.add_argument('mode', choices=('dry-run', 'run', 'replay', 'self-test', 'capture-fixture',
                                         'record-start', 'record-stop'))
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--instances', type=Path, help='instances.json written at Up (run mode)')
    parser.add_argument('--out', type=Path, help='output folder (default: <manifest folder>/out)')
    parser.add_argument('--fixture', type=Path, help='replay: directory containing fixture.json')
    parser.add_argument('--capture-fixtures', action='store_true',
                        help='save an offline fixture for each passing row')
    parser.add_argument('--rows', help='comma-separated row ids, or "failed" to rerun rows whose receipt is FAILED')
    parser.add_argument('--on-failure', choices=('continue', 'stop'),
                        help='override the plan/row on_failure policy for this run')
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
    if args.mode == 'replay':
        if report['errors']:
            print(json.dumps(report, indent=2))
            return 1
        if not args.fixture:
            parser.error('replay needs --fixture')
        result = harness.replay_fixture(manifest, args.fixture, out / 'replay', Runner)
        for line in result.get('lines') or []:
            print(line, file=sys.stderr)
        print(json.dumps(result, indent=2))
        return 0 if result.get('ok') else 1
    if args.mode == 'dry-run' or report['errors']:
        print(json.dumps(report, indent=2))
        return 1 if report['errors'] else 0
    if not args.instances:
        parser.error(f'{args.mode} needs --instances')
    instances = json.loads(args.instances.read_text())
    if args.mode == 'self-test':
        result = Runner(manifest, instances, out, args.pace_minutes).self_test()
        (out / 'self-test.json').write_text(json.dumps(result, indent=2) + '\n')
        for line in result.get('lines') or []:
            print(line, file=sys.stderr)
        print(json.dumps(result, indent=2))
        return 0 if result.get('ok') else 1
    only = None
    if args.rows == 'failed':
        only = set()
        for row in json.loads(manifest.read_text()).get('rows', []):
            receipt = out / f'{row["id"]}.json'
            if receipt.is_file() and json.loads(receipt.read_text()).get('status') == 'FAILED':
                only.add(row['id'])
    elif args.rows:
        only = set(args.rows.split(','))
    try:
        tool = Runner(manifest, instances, out, args.pace_minutes, on_failure=args.on_failure)
        tool.capture_fixtures = bool(args.capture_fixtures or args.mode == 'capture-fixture')
        result = tool.run(only)
        if result == 'REVIEW':
            return 4
        if result == 'FAILED':
            return 3
    except harness.EarlyStop as stop:
        print(json.dumps({'status': 'EARLY-STOP', 'result': str(stop), 'probable_shared_cause': True}), flush=True)
        return 5
    except StopRun as stop:
        print(json.dumps({'status': 'STOPPED', 'reason': str(stop)}), flush=True)
        return 2
    except Exception as error:
        print(json.dumps({'status': 'FAILED', 'error': repr(error), 'stop_input': str(out / 'STOP-INPUT')}), flush=True)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
