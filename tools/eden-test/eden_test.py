#!/usr/bin/env python3
"""One command for an Eden test run: eden-lifecycle + eden-runner + harvest + a short RESULTS.md.

  eden_test.py go --host <host> --label <label> --nro <file> --rows <rows>  stage, Up, run, report (no Down)
  eden_test.py run <run> --rows <id>   rerun one row; eden_test.py report <run>   show the updated report

Run it ON the host that runs Eden (the Surface locally; the Yoga with `ssh yoga python3 ~/ssbu-mod-playbook/...`,
after `git -C ~/ssbu-mod-playbook pull --ff-only` there and copying the rows folder over).

  eden_test.py new  --from <previous config.json> --label lr-14 --rows <rows folder>   new run folder + config
  eden_test.py check <run folder>      check setup, pinned fixtures and volatile inputs without starting anything
  eden_test.py up   <run folder>        offline dry run, then lifecycle up
  eden_test.py run  <run folder> [--retry]   runner (with --retry: clear STOP-INPUT first; passed rows skip)
  eden_test.py down <run folder> [--keep-state]   lifecycle down (and its prune), harvest, RESULTS.md, timing.json
  eden_test.py all  --from ... --label ... --rows ...   new + up + run + down (down always runs)

A run folder is /home/intpa/eden-runs/<label>/ with config.json, rows/ (a copy of the rows folder), state/ and
work/ (fresh every run), out/ (runner output) and RESULTS.md. A clean `down` copies the files the run wrote into
out/post-run-<key>/ and prunes work/ and the state snapshots (about 3 GB a run); state/ keeps the journal and the
run-<key>/ logs, and RESULTS.md's `Prune:` line gives the bytes. `--keep-state` skips the prune, only when asked.
Pins (Eden AppImage, plugins, files, game) carry over
from the previous config unchanged; `new` checks each pinned file still has its sha256, `--app` swaps in a
different Eden AppImage and `--plugin <new build>` swaps the pinned plugin with the same file name (resolved path;
sha256 read; the swap is recorded in config.json `plugin_swaps`). `--defer-load` leaves the frontend ready so rows
can configure boot tracing before game.load. Same-boot fixing (MACROS rule 8): after a failed row,
fix rows.json or a hook, then `run --retry`; `down` when the batch is done.
"""
import argparse
import copy
import hashlib
import heapq
import json
import math
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PLAYBOOK = Path(__file__).resolve().parents[2]
LIFECYCLE = PLAYBOOK / 'tools/eden-lifecycle/lifecycle.py'
RUNNER = PLAYBOOK / 'tools/eden-runner/runner.py'
HARVEST = PLAYBOOK / 'tools/eden-runner/harvest.py'
RUNS = Path('/home/intpa/eden-runs')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def stamp(run, key):
    """Record a time in timing.json. run_start/run_end also append to `runs`, so retries add up instead of overwriting."""
    timing_path = run / 'timing.json'
    timing = json.loads(timing_path.read_text()) if timing_path.exists() else {}
    now = time.time()
    timing[key] = now
    if key == 'run_start':
        timing.setdefault('runs', []).append([now, None])
    elif key == 'run_end' and timing.get('runs') and timing['runs'][-1][1] is None:
        timing['runs'][-1][1] = now
    timing_path.write_text(json.dumps(timing, indent=2) + '\n')


def sh(argv, log=None):
    """Run a tool, echo its output, keep a copy in the run folder; returns the exit code."""
    done = subprocess.run(argv, capture_output=True, text=True)
    out = done.stdout + done.stderr
    sys.stdout.write(out)
    if log:
        with open(log, 'a') as f:
            f.write(f'$ {" ".join(map(str, argv))}\n{out}\n')
    return done.returncode


def cmd_new(args):
    previous = json.loads(Path(args.from_config).read_text())
    run = RUNS / args.label
    if run.exists():
        raise SystemExit(f'{run} exists; pick a new label')
    config = copy.deepcopy(previous)
    config['label'] = args.label
    config['state_dir'] = str(run / 'state')
    config['work_root'] = str(run / 'work')
    if args.owner:
        config['owner'] = args.owner
    if args.defer_load:  # pkg4: leave the frontend ready so rows can configure boot tracing before game.load
        config['defer_load'] = True
    swapped = []
    for new in args.plugin:  # swap a pinned plugin for a new build with the same file name, in every instance
        new_path = Path(new).resolve()
        hits = [pin for item in config['instances'] for pin in item.get('plugins', [])
                if Path(pin['path']).name == new_path.name]
        if not hits:
            raise SystemExit(f'--plugin {new_path.name}: no pinned plugin with that file name')
        for pin in hits:
            swapped.append({'from': pin['path'], 'from_sha256': pin['sha256'], 'to': str(new_path)})
            pin.update(path=str(new_path), sha256=sha256(new_path))
    if swapped:
        config['plugin_swaps'] = swapped
    for item in config['instances']:
        if args.app:
            item['app'] = {'path': str(Path(args.app).resolve()), 'sha256': sha256(Path(args.app).resolve())}
        for pin in [item['app'], *item.get('plugins', []), *item.get('files', [])]:
            actual = sha256(pin['path'])
            if actual != pin['sha256']:
                raise SystemExit(f'pin changed: {pin["path"]} is {actual[:12]}…, config says {pin["sha256"][:12]}…')
    return _finish_new(run, config, Path(args.rows))


def _finish_new(run, config, rows_src):
    run.mkdir(parents=True, exist_ok=True)
    # A pinned Qt fixture is applied after lifecycle's source-config rewrite.
    # Derive a new pinned fixture; never change the inherited input in place.
    for item in config['instances']:
        for pin in item.get('files', []):
            if pin.get('to') != 'user/config/qt-config.ini':
                continue
            source = Path(pin['path'])
            text = source.read_text()
            lines, counts = [], {'nand_directory': 0, 'sdmc_directory': 0}
            profile = Path(config['work_root']) / 'profiles' / ('profile-' + item['key'])
            for line in text.splitlines(keepends=True):
                key, equal, value = line.partition('=')
                if equal and key.strip() in counts:
                    name = key.strip()
                    counts[name] += 1
                    folder = 'nand' if name == 'nand_directory' else 'sdmc'
                    ending = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
                    line = key + '=' + (profile / 'user' / folder).as_posix() + ending
                lines.append(line)
            if any(count != 1 for count in counts.values()):
                raise SystemExit(f'Qt fixture needs exactly one NAND and SDMC directory: {source}')
            derived = run / 'inputs' / ('qt-' + item['key'] + '.ini')
            derived.parent.mkdir(exist_ok=True)
            derived.write_text(''.join(lines))
            pin.update(path=str(derived), sha256=sha256(derived))
            (derived.with_suffix('.provenance.json')).write_text(json.dumps({
                'source': str(source), 'source_sha256': sha256(source),
                'derived_sha256': pin['sha256'], 'changed_keys': list(counts)}, indent=2) + '\n')
    if rows_src.is_dir():
        shutil.copytree(rows_src, run / 'rows', symlinks=True)
    else:
        (run / 'rows').mkdir()
        shutil.copy2(rows_src, run / 'rows' / 'rows.json')
    config['rows'] = {'path': str(run / 'rows' / 'rows.json'), 'sha256': sha256(run / 'rows' / 'rows.json')}
    # The lifecycle dry-runs the rows with the runner it pins: always this playbook's runner, never a staged copy.
    config['runner'] = {'path': str(RUNNER), 'sha256': sha256(RUNNER)}
    (run / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
    stamp(run, 'new')
    print(json.dumps({'run': str(run), 'label': config['label'],
                      'apps': sorted({i['app']['path'] for i in config['instances']})}))
    return run


def _go_trace(args):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.label):
        raise SystemExit(f'invalid run label {args.label!r}')
    names = [Path(nro).name for nro in args.nro]
    if len(names) != len(set(names)):
        raise SystemExit('each --nro must have a distinct file name')
    if args.trace_point and not args.elf:
        raise SystemExit('--trace-point requires --elf')
    if not args.elf:
        return None
    elf = Path(args.elf)
    if elf.suffix != '.so':
        raise SystemExit(f'--elf must be a .so file: {elf.name}')
    match = next((Path(nro).name for nro in args.nro if Path(nro).stem == elf.stem), None)
    if match is None:
        raise SystemExit(f'--elf {elf.name} has no matching --nro among: '
                         + ', '.join(Path(nro).name for nro in args.nro))
    return match


def _go_base_trace(config, args):
    if any('trace' in item for item in config['instances']) and (not args.elf or not args.trace_point):
        raise SystemExit("this base is a trace run: pass --elf <the build's libtraining_rooms.so> and at least one "
                         "--trace-point <module!symbol> (the lifecycle needs 1..256 trace points); the two the drill "
                         "blocks use are libtraining_rooms.nro!edge_juggle_attempt and libtraining_rooms.nro!planner_chose")


def go_new(args):
    base = Path(args.from_config) if args.from_config else RUNS / '_eden-test' / 'base.json'
    config = copy.deepcopy(json.loads(base.read_text()))
    if config.get('host') != args.host:
        raise SystemExit(f'base {base} is for host {config.get("host")!r}, not {args.host!r}')
    _go_base_trace(config, args)
    module = _go_trace(args)
    run = RUNS / args.label
    if run.exists():
        raise SystemExit(f'{run} exists; pick a new label')
    config.update(label=args.label, owner=args.owner,
                  state_dir=str(run / 'state'), work_root=str(run / 'work'), defer_load=True)
    for key in ('rows', 'runner', 'authorization'):
        config.pop(key, None)
    trace_run = any('trace' in item for item in config['instances']) or bool(args.elf)
    if trace_run:
        for item in config['instances']:
            profile = Path(item['source']) / 'user/config/qt-config.ini'
            if profile.is_file() and any(line.strip() == 'use_gdbstub=true'
                                         for line in profile.read_text().splitlines()):
                raise SystemExit(f'trace run: the GDB stub must be off before game.load, but the source profile '
                                 f'{profile} has use_gdbstub=true. Turn it off there '
                                 '(or use a base without a trace section for a stub-on run).')
    config['stub_rule'] = (
        'trace run: GDB stub off (source profile checked; the boot row sets core.use_gdbstub=false before game.load)'
        if trace_run else
        'no trace section: stub left to the rows (routes:configure_boot)')
    inputs = run / 'inputs'
    inputs.mkdir(parents=True)
    swapped = []
    for nro in args.nro:
        original = Path(nro).resolve()
        target = inputs / original.name
        shutil.copy2(original, target)
        digest = sha256(target)
        for item in config['instances']:
            pins = item.setdefault('plugins', [])
            hits = [pin for pin in pins if pin.get('name', Path(pin['path']).name) == original.name]
            for pin in hits:
                swapped.append({'from': pin['path'], 'from_sha256': pin['sha256'], 'to': str(target)})
                pin.update(path=str(target), sha256=digest)
            if not hits:
                pins.append({'name': original.name, 'path': str(target), 'sha256': digest})
    if swapped:
        config['plugin_swaps'] = swapped
    for item in config['instances']:
        for pin in [item['app'], *item.get('plugins', []), *item.get('files', [])]:
            actual = sha256(pin['path'])
            if actual != pin['sha256']:
                raise SystemExit(f'pin changed: {pin["path"]} is {actual[:12]}…, config says {pin["sha256"][:12]}…')
    if args.elf:
        elf = inputs / Path(args.elf).name
        shutil.copy2(args.elf, elf)
        for item in config['instances']:
            trace = item.setdefault('trace', {})
            trace.setdefault('symbols', []).append({'module': module, 'elf': str(elf)})
            trace.setdefault('points', []).extend({'target': point, 'mode': 'records',
                                                   'registers': ['x0', 'x1', 'x2', 'x3']}
                                                  for point in args.trace_point)
    port_pins = [pin for item in config['instances'] for pin in item.get('files', [])
                 if pin.get('to', '').endswith('ultimate/ssbu-hw-bridge/port.txt')]
    if port_pins:
        sys.path.insert(0, str(RUNNER.parent))
        try:
            import routes
            port = routes.free_port()
        finally:
            sys.path.pop(0)
        port_path = inputs / 'port.txt'
        port_path.write_text(f'{port}\n')
        digest = sha256(port_path)
        for pin in port_pins:
            pin.update(path=str(port_path), sha256=digest)
        config['bridge_port'] = port
    return _finish_new(run, config, Path(args.rows))


def lifecycle(run, mode, *extra):
    config = json.loads((run / 'config.json').read_text())
    if config.get('host') not in ('surface', 'yoga', 'xps', 'ally'):
        raise SystemExit('config host must be surface, yoga, xps or ally')
    argv = [sys.executable, '-B', '-I', str(LIFECYCLE), mode, str(run / 'config.json'), '--execute', *extra]
    return sh(argv, run / f'lifecycle-{mode}.log')


SETUP_VIA = ('fixture', 'segment', 'record', 'hook')


def fixture_diff(fixture, reviewed):
    def diff(left, right, path):
        if type(left) is not type(right):
            return [path]
        if isinstance(left, dict):
            found = []
            for key in left.keys() | right.keys():
                child = path + [key]
                if key not in left or key not in right:
                    found.append(child)
                else:
                    found.extend(diff(left[key], right[key], child))
            return found
        if isinstance(left, list):
            if len(left) != len(right):
                return [path]
            return [item for i, (a, b) in enumerate(zip(left, right))
                    for item in diff(a, b, path + [i])]
        return [] if left == right else [path]
    return sorted(diff(fixture, reviewed, []),
                  key=lambda path: [(0, item) if isinstance(item, str) else (1, item) for item in path])


def fixture_path(path):
    if not path:
        return '<root>'
    if any(isinstance(part, str) and '.' in part for part in path):
        return json.dumps(path, ensure_ascii=False)
    return '.'.join(str(part) for part in path)


def fixture_comparison(item, base, config):
    reviewed = item.get('reviewed')
    problems = []
    state = item.get('state', '?')
    if 'file' in item:
        problems.append(f'{state}: use "to" instead of "file"')
    if 'reviewed' not in item:
        return [], problems
    destination = item.get('to')
    if not isinstance(destination, str) or not destination:
        return [], problems + [f'{state}: reviewed requires "to"']
    destination = posixpath.normpath(destination)
    selected = []
    for instance in config.get('instances', []):
        if not isinstance(instance, dict):
            continue
        if item.get('instance') is not None and instance.get('key') != item['instance']:
            continue
        pins = [pin for pin in instance.get('files', []) if isinstance(pin, dict)
                and isinstance(pin.get('to'), str) and posixpath.normpath(pin['to']) == destination]
        if pins or item.get('instance') is not None:
            selected.append((instance.get('key'), pins[-1] if pins else None))
    if not selected:
        return [], problems + [f'{state}: no instance selected for {destination}']
    allowed = item.get('differs_only', [])
    if (not isinstance(allowed, list) or any(
            not isinstance(path, str) and
            not (isinstance(path, list) and all(isinstance(part, str) or type(part) is int for part in path))
            for path in allowed)):
        problems.append(f'{state}: differs_only must be a list of paths')
        allowed = []
    allowed = [[int(part) if part.isdecimal() else part for part in path.split('.')]
               if isinstance(path, str) else path for path in allowed]
    if any(not path or any(part == '' for part in path) for path in allowed):
        problems.append(f'{state}: differs_only entries must name at least one key')
        allowed = [path for path in allowed if path and all(part != '' for part in path)]
    missing = object()
    reviewed_value = missing
    if not isinstance(reviewed, dict) or not reviewed.get('path'):
        problems.append(f'{state}: reviewed needs path and sha256')
    else:
        source = reviewed['path']
        try:
            path = Path(source)
            data = (path if path.is_absolute() else Path(base) / path).read_bytes()
        except (OSError, ValueError, TypeError) as error:
            problems.append(f'{state}: reviewed {source}: {type(error).__name__}')
        else:
            if (not isinstance(reviewed.get('sha256'), str)
                    or reviewed['sha256'].lower() != hashlib.sha256(data).hexdigest()):
                problems.append(f'{state}: reviewed sha256 mismatch: {source}')
            try:
                reviewed_value = json.loads(data)
            except (ValueError, TypeError) as error:
                problems.append(f'{state}: reviewed {source}: {type(error).__name__}')
    common_problems = list(problems)
    results = []
    for instance, pin in selected:
        result = {'state': item.get('state'), 'instance': instance, 'to': destination,
                  'pin': {'path': pin.get('path'), 'sha256': pin.get('sha256')} if pin else None,
                  'reviewed': reviewed, 'differing_paths': [], 'differs_only': item.get('differs_only', []),
                  'ok': False}
        found = []
        prefix = f'{state} instance {instance}'
        if pin is None:
            found.append(f'{prefix}: no pin for {destination}')
        else:
            source = pin.get('path')
            try:
                data = Path(source).read_bytes()
            except (OSError, ValueError, TypeError) as error:
                found.append(f'{prefix}: pin {source}: {type(error).__name__}')
            else:
                if (not isinstance(pin.get('sha256'), str)
                        or pin['sha256'].lower() != hashlib.sha256(data).hexdigest()):
                    found.append(f'{prefix}: pin sha256 mismatch: {source}')
                try:
                    value = json.loads(data)
                except (ValueError, TypeError) as error:
                    found.append(f'{prefix}: pin {source}: {type(error).__name__}')
                else:
                    if reviewed_value is not missing:
                        result['differing_paths'] = fixture_diff(value, reviewed_value)
                        offending = [path for path in result['differing_paths']
                                     if not any(path[:len(allow)] == allow for allow in allowed)]
                        if offending:
                            found.append(f'{prefix}: {len(offending)} differing paths: '
                                         + ', '.join(fixture_path(path) for path in offending[:12]))
        result['ok'] = not common_problems and not found
        results.append(result)
        problems.extend(found)
    return results, problems


def fixture_problems(item, base, config):
    return fixture_comparison(item, base, config)[1]


def setup_plan_problems(spec, base=None, config=None):
    """MACROS rule 13: rows.json names how each starting state is reached, before Up.
    "setup": [{"state": "Training, P1 Kirby, CPU Kirby", "via": "fixture", "ref": "files: settings.json"}, ...]"""
    plan = spec.get('setup') if isinstance(spec, dict) else None
    if not isinstance(plan, list) or not plan:
        return ['rows.json has no "setup" list: name each starting state and how it is reached '
                f'(via one of {", ".join(SETUP_VIA)}) before Up']
    problems = []
    for i, item in enumerate(plan):
        if not isinstance(item, dict) or not item.get('state') or item.get('via') not in SETUP_VIA or not item.get('ref'):
            problems.append(f'setup[{i}] needs "state", "via" ({"/".join(SETUP_VIA)}) and "ref"')
        if base is not None and config is not None and isinstance(item, dict):
            problems.extend(f'setup[{i}]: {problem}' for problem in fixture_problems(item, base, config))
    return problems


def iter_pin_nodes(node):
    if isinstance(node, dict):
        path, digest_value = node.get('path'), node.get('sha256')
        if isinstance(path, str) and isinstance(digest_value, str) and re.fullmatch(r'[0-9a-f]{64}', digest_value):
            yield node
        for key, child in node.items():
            if key not in ('path', 'sha256'):
                yield from iter_pin_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from iter_pin_nodes(child)


def volatile_pins(config, run):
    root = Path(os.path.abspath(run.parent))
    found = set()
    paths = [pin['path'] for pin in iter_pin_nodes(config)]
    for instance in config.get('instances', []):
        if isinstance(instance, dict):
            paths.extend(instance.get(key) for key in ('source', 'game'))
    for value in paths:
        if isinstance(value, str) and Path(value).is_absolute():
            try:
                parts = Path(os.path.normpath(value)).relative_to(root).parts
            except ValueError:
                continue
            if parts and parts[0] != run.name and not parts[0].startswith('_'):
                found.add((value, parts[0]))
    return sorted(found)


def check_run(run, skip_setup_plan=False, allow_volatile_pins=False):
    rows = run / 'rows' / 'rows.json'
    spec = json.loads(rows.read_text())
    config = json.loads((run / 'config.json').read_text())
    problems = [] if skip_setup_plan else setup_plan_problems(spec)
    setup = spec.get('setup', []) if isinstance(spec, dict) else []
    comparisons = []
    for i, item in enumerate(setup if isinstance(setup, list) else []):
        if isinstance(item, dict) and ('reviewed' in item or 'file' in item):
            results, found = fixture_comparison(item, rows.parent, config)
            comparisons.extend(results)
            problems.extend(f'setup[{i}]: {problem}' for problem in found)
    (run / 'setup-fixtures.json').write_text(json.dumps(comparisons, indent=2) + '\n')
    if not allow_volatile_pins:
        pins = volatile_pins(config, run)
        if pins:
            paths = ', '.join(path for path, _ in pins[:8])
            problems.append(f'volatile pins: {paths}; shared inputs belong in {run.parent}/_<name>/ '
                            'because the host janitor keeps only those '
                            '(configs that pin inputs-* folders: move them to _<name>/ or pass --allow-volatile-pins)')
    return problems


def cmd_check(run, skip_setup_plan=False, allow_volatile_pins=False):
    problems = check_run(run, skip_setup_plan, allow_volatile_pins)
    for problem in problems:
        print(problem)
    return 2 if problems else 0


def cmd_up(run, skip_setup_plan=False, allow_volatile_pins=False):
    problems = check_run(run, skip_setup_plan, allow_volatile_pins)
    if problems:
        raise SystemExit('; '.join(problems))
    rows = run / 'rows' / 'rows.json'
    if sh([sys.executable, '-B', '-I', str(RUNNER), 'dry-run', str(rows), '--out', str(run / 'out')],
          run / 'dry-run.log') != 0:
        raise SystemExit('dry run failed; fix rows.json before Up')
    stamp(run, 'up_start')
    code = lifecycle(run, 'up')
    stamp(run, 'up_done')
    if code != 0:
        raise SystemExit(f'lifecycle up failed ({code}); see {run / "lifecycle-up.log"}')


def set_aside(run, rows):
    """Keep a row's earlier receipt and files as out/<id>.rerun-<n>.json and out/<id>.rerun-<n>/, so the runner runs
    the row again (it skips a row whose receipt passed) and the report still shows the earlier attempt."""
    out = run / 'out'
    for row in rows:
        receipt = out / f'{row}.json'
        if not receipt.exists():
            continue
        n = 1
        while (out / f'{row}.rerun-{n}.json').exists():
            n += 1
        receipt.rename(out / f'{row}.rerun-{n}.json')
        if (out / row).is_dir():
            (out / row).rename(out / f'{row}.rerun-{n}')


def cmd_run(run, retry=False, rows=None):
    stop = run / 'out' / 'STOP-INPUT'
    if (retry or rows) and stop.exists():
        stop.rename(run / 'out' / f'STOP-INPUT.{int(time.time())}')
    if rows:
        set_aside(run, rows.split(','))
    stamp(run, 'run_start')
    code = sh([sys.executable, '-B', '-I', str(RUNNER), 'run', str(run / 'rows' / 'rows.json'),
               '--instances', str(run / 'state' / 'instances.json'), '--out', str(run / 'out'),
               *(['--rows', rows] if rows else [])], run / 'runner.log')
    stamp(run, 'run_end')
    cmd_pace(run)  # informational after every run call; `pace` alone exits 2 when over budget
    return code


# MACROS rule 13: a boot reaches its starting state through a fixture or a recorded segment, not by an agent steering
# menus one input at a time. These budgets make that visible while the boot is still up.
MANUAL_INPUT_BUDGET = 40
NO_ROW_MINUTES = 15
CAP_MARGIN_MINUTES = 5


def control_inputs(run):
    """(inputs outside macro recordings, their timestamps) summed over the run's instances' control.jsonl."""
    count, times = 0, []
    for path in sorted((run / 'state').glob('run-*/control.jsonl')):
        recording = False
        for line in path.read_text(errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('event') != 'request':
                continue
            method = event.get('method', '')
            if method.startswith('macro.record'):
                recording = True
            elif method == 'macro.stop':
                recording = False
            elif method in ('input.button', 'input.stick') and not recording:
                count += 1
                times.append(event.get('time'))
    return count, times


def runner_inputs(run):
    """Inputs the runner itself sent (its own call log): scripted hook presses and sticks, not hand-driving."""
    path = run / 'out' / 'actions.jsonl'
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(errors='replace').splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if str(record.get('method', '')).startswith('input.'):
            count += 1
    return count


def pace_report(run, now=None):
    """Hand-driven inputs and row progress since Up; 'over' says which budget is spent."""
    now = time.time() if now is None else now
    timing = json.loads((run / 'timing.json').read_text()) if (run / 'timing.json').exists() else {}
    up = timing.get('up_done')
    outside, _ = control_inputs(run)
    manual = max(0, outside - runner_inputs(run))
    passed, _failed, unrun, _retained = row_accounting(run) if (run / 'rows' / 'rows.json').exists() else ([], [], [], 0)
    receipts = [run / 'out' / f'{row}.json' for row in passed]
    last_pass = max((p.stat().st_mtime for p in receipts if p.exists()), default=None)
    since = (now - (last_pass or up)) / 60 if up else 0.0
    # The lifecycle's runtime cap (config "runtime", seconds) stops Eden with SIGTERM, and Eden has crashed on that
    # signal (SEGV, 2026-11-03/04). Down well before it, whatever the rows' progress.
    config = json.loads((run / 'config.json').read_text()) if (run / 'config.json').exists() else {}
    cap_left = (up + config.get('runtime', 5400) - now) / 60 if up else None
    over = []
    if manual > MANUAL_INPUT_BUDGET:
        over.append(f'{manual} hand-driven inputs (budget {MANUAL_INPUT_BUDGET})')
    if up and unrun and since > NO_ROW_MINUTES:
        over.append(f'{since:.0f} min without a newly passed row (budget {NO_ROW_MINUTES})')
    if cap_left is not None and cap_left <= CAP_MARGIN_MINUTES:
        over.append(f'{max(cap_left, 0):.0f} min to the runtime cap (Down at {CAP_MARGIN_MINUTES} min left)')
    return {'manual_inputs': manual, 'runner_inputs': runner_inputs(run), 'rows_passed': len(passed),
            'rows_left': len(unrun), 'minutes_since_progress': round(since, 1),
            'minutes_to_cap': None if cap_left is None else round(cap_left, 1), 'over': over}


def cmd_pace(run):
    report = pace_report(run)
    print('pace: ' + json.dumps(report))
    if report['over']:
        print('STOP: ' + '; '.join(report['over']) + '. Down now if the cap is near. Otherwise reach the starting state with a '
              'fixture or record the route once as a segment (MACROS rule 13), or Down and fix the rows; don\'t keep '
              'steering by hand.')
        return 2
    return 0


def unit_results(run):
    journal_path = run / 'state' / 'lifecycle.json'
    if not journal_path.exists():
        return []
    journal = json.loads(journal_path.read_text())
    results = []
    for item in journal.get('instances', []):
        unit = item.get('unit')
        if not unit:
            continue
        done = subprocess.run(['systemctl', '--user', 'show', unit, '--property=Result,ExecMainCode,ExecMainStatus'],
                              capture_output=True, text=True)
        props = dict(line.split('=', 1) for line in done.stdout.splitlines() if '=' in line)
        results.append(f'{item.get("key")}: {classify_exit(props)}')
    return results


def classify_exit(props):
    """systemd ExecMainCode: 1 exited, 2 killed, 3 dumped (core). Our Down ends a live Eden with SIGKILL after SIGTERM,
    so killed/9 is the expected forced stop; a core dump (SEGV, BUS, ABRT…) is a CRASH whatever else passed."""
    result, code, status = props.get('Result', '?'), props.get('ExecMainCode', '?'), props.get('ExecMainStatus', '?')
    if code == '3':
        verdict = f'CRASH (core dumped, signal {status})'
    elif code == '2' and status == '9':
        verdict = 'forced stop (SIGKILL), not a crash'
    elif code == '2':
        verdict = f'killed by signal {status}'
    else:
        verdict = f'exited {status}'
    if result == 'timeout':
        verdict += '; the runtime cap fired'
    return f'{verdict} [Result={result} code={code} status={status}]'


def one_line(text, limit=140):
    return ' '.join(str(text).split())[:limit]


def row_accounting(run):
    """Row receipts are out/<id>.json for the ids in the final rows.json; other JSON in out/ is not a row.
    Renamed attempts (out/<id>.FAILED.<why>.json) are retained failures, counted apart from the final status."""
    out = run / 'out'
    spec = json.loads((run / 'rows' / 'rows.json').read_text())
    ids = [row['id'] for row in (spec['rows'] if isinstance(spec, dict) else spec)]
    passed, failed, unrun = [], [], []
    for row_id in ids:
        receipt = out / f'{row_id}.json'
        if not receipt.exists():
            unrun.append(row_id)
            continue
        status = json.loads(receipt.read_text())
        if status.get('status') == 'PASS':
            passed.append(row_id)
        else:
            failed.append((row_id, status.get('error', '')))
    retained = len(list(out.glob('*.FAILED.*.json')))  # includes attempts of rows renamed or dropped since
    return passed, failed, unrun, retained


def harvest_summary(run):
    path = run / 'harvest.json'
    if not path.exists():
        return 'not run'
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return one_line(path.read_text(), 160)
    if not isinstance(data, dict):
        return one_line(json.dumps(data), 160)
    return ', '.join(f'{key} {len(value) if isinstance(value, list) else value}' for key, value in data.items())


def prune_summary(run, journal):
    """The Down's prune receipt (the journal's `prune`, written by lifecycle.py): what it freed, where the files the
    run wrote went, or why work/ and state/ are still there."""
    prune = journal.get('prune')
    if not prune:
        return 'not run: work/ and state/ stay until a clean Down'
    if prune.get('kept') and not prune.get('done'):
        return 'kept (--keep-state): work/ and state/ stay until a Down without it'
    where = Path(prune.get('evidence', '?'))
    where = where.relative_to(run) if run in where.parents else where
    if not prune.get('done'):
        saved = f'; the run\'s files are in {where}/post-run-*/' if prune.get('evidence_saved') else ''
        return 'FAILED: ' + one_line(prune.get('error'), 120) + saved
    df = (prune['disk_free_after'] - prune['disk_free_before']) / 10 ** 9
    return (f'{prune["unique_bytes"] / 10 ** 9:.1f} GB freed of {prune["bytes"] / 10 ** 9:.1f} GB of copies (df {df:+.1f} GB); '
            f'{prune["copied_files"]} files the run wrote in {where}/post-run-*/, {prune["listed_files"]} listed only'
            + (f'; {len(prune["skipped"])} skipped' if prune.get('skipped') else ''))


def time_split(run):
    timing_path = run / 'timing.json'
    timing = json.loads(timing_path.read_text()) if timing_path.exists() else {}
    rows_path = run / 'rows' / 'rows.json'
    try:
        rows_data = rows_path.read_bytes()
    except FileNotFoundError:
        rows_data = None
    spec = json.loads(rows_data) if rows_data is not None else []
    current_sha = hashlib.sha256(rows_data).hexdigest() if rows_data is not None else None
    rows = spec.get('rows', []) if isinstance(spec, dict) else spec
    rows_by_id = {row['id']: row for row in rows}
    out = run / 'out'
    candidates = []

    def numeric(value):
        return type(value) in (int, float) and math.isfinite(value)

    journal = out / 'attempts.jsonl'
    if journal.is_file():
        for line in journal.read_text().splitlines():
            try:
                attempt = json.loads(line)
            except ValueError:
                continue
            if isinstance(attempt, dict) and isinstance(attempt.get('id'), str):
                candidates.append(attempt)
    for path in sorted(out.glob('*.json')) if out.is_dir() else []:
        try:
            receipt = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(receipt, dict) or not isinstance(receipt.get('id'), str):
            continue
        row_id = receipt['id']
        if ((path.name == f'{row_id}.json' or path.name.startswith(f'{row_id}.FAILED.'))
                and numeric(receipt.get('started')) and 'start' in receipt and 'end' in receipt):
            candidates.append(receipt)

    def valid_interval(attempt):
        start, end = attempt.get('started'), attempt.get('ended')
        return numeric(start) and numeric(end) and end >= start

    attempts_by_start, skipped = {}, 0
    for attempt in candidates:
        start = attempt.get('started')
        start_key = ('numeric', start) if numeric(start) else ('invalid', json.dumps(start, sort_keys=True))
        key = (attempt['id'], start_key)
        prior = attempts_by_start.get(key)
        if prior is None or (not valid_interval(prior) and valid_interval(attempt)):
            attempts_by_start[key] = attempt
        elif valid_interval(prior) and valid_interval(attempt):
            for field in ('role', 'role_source', 'manifest_sha256', 'start', 'end'):
                if prior.get(field) is None and attempt.get(field) is not None:
                    prior[field] = attempt[field]

    manifests = {}

    def manifest_row(attempt):
        manifest_sha = attempt.get('manifest_sha256')
        if not isinstance(manifest_sha, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', manifest_sha):
            return None
        manifest_sha = manifest_sha.lower()
        if manifest_sha == current_sha:
            return rows_by_id.get(attempt['id'])
        if manifest_sha not in manifests:
            path = out / 'manifests' / f'{manifest_sha}.json'
            try:
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != manifest_sha:
                    raise ValueError('manifest hash mismatch')
                manifest = json.loads(data)
                manifest_rows = manifest.get('rows', []) if isinstance(manifest, dict) else manifest
                manifests[manifest_sha] = {row['id']: row for row in manifest_rows
                                           if isinstance(row, dict) and isinstance(row.get('id'), str)}
            except (OSError, ValueError, TypeError):
                manifests[manifest_sha] = {}
        return manifests[manifest_sha].get(attempt['id'])

    intervals = []
    for kind in ('up', 'down'):
        start, end = timing.get(kind + '_start'), timing.get(kind + '_done')
        if numeric(start) and numeric(end) and end >= start:
            intervals.append((start, end, kind))
    attempts, attempts_unroled, roles_defaulted, roles_from_manifest = 0, 0, 0, 0
    attempt_ids_not_in_manifest = set()
    for attempt in attempts_by_start.values():
        if not valid_interval(attempt):
            skipped += 1
            continue
        start, end = attempt['started'], attempt['ended']
        role = attempt.get('role')
        inferred = False
        from_manifest = False
        if role is None:
            row = manifest_row(attempt)
            if row is not None and 'role' in row:
                role = row['role']
                from_manifest = True
            elif isinstance(attempt.get('start'), str) and isinstance(attempt.get('end'), str):
                role = 'setup' if attempt['start'] != attempt['end'] else 'test'
                inferred = True
            else:
                role = 'unroled'
        if role not in ('setup', 'test', 'unroled'):
            skipped += 1
            continue
        if inferred or attempt.get('role_source') == 'default':
            roles_defaulted += 1
        roles_from_manifest += from_manifest
        attempts += 1
        attempts_unroled += role == 'unroled'
        if attempt['id'] not in rows_by_id:
            attempt_ids_not_in_manifest.add(attempt['id'])
        intervals.append((start, end, role))

    first, last = timing.get('up_start'), timing.get('down_done')
    window = last - first if numeric(first) and numeric(last) and last >= first else None
    durations = {'up': 0, 'setup': 0, 'test': 0, 'unroled': 0, 'down': 0}
    events = []
    if window is not None:
        events.extend([(first, 0, None), (last, 0, None)])
    for i, (start, end, _) in enumerate(intervals):
        begin, finish = (max(start, first), min(end, last)) if window is not None else (start, end)
        if finish > begin:
            events.extend([(begin, 1, i), (finish, -1, i)])
    events.sort()
    active, earliest = set(), []
    overlap, unclassified, previous = 0, 0, None
    for at, change, i in events:
        if previous is not None and at > previous:
            duration = at - previous
            if active:
                while earliest[0][1] not in active:
                    heapq.heappop(earliest)
                durations[intervals[earliest[0][1]][2]] += duration
                if len(active) > 1:
                    overlap += duration
            elif window is not None:
                unclassified += duration
        if change == 1:
            active.add(i)
            heapq.heappush(earliest, (intervals[i][0], i))
        elif change == -1:
            active.remove(i)
        previous = at
    total = sum(durations.values())
    return {'window': window, **durations, 'unclassified': unclassified if window is not None else None,
            'incomplete': window is None, 'overlap': overlap, 'attempts': attempts, 'attempts_skipped': skipped,
            'attempts_unroled': attempts_unroled, 'roles_defaulted': roles_defaulted,
            'roles_from_manifest': roles_from_manifest, 'attempt_ids_not_in_manifest': sorted(attempt_ids_not_in_manifest),
            'test_share': round(durations['test'] / total, 3) if total else 0}


def results_md(run, config):
    passed, failed, unrun, retained = row_accounting(run)
    journal = json.loads((run / 'state' / 'lifecycle.json').read_text()) if (run / 'state' / 'lifecycle.json').exists() else {}
    timing = json.loads((run / 'timing.json').read_text())
    runs = [(start, end) for start, end in timing.get('runs', []) if end]
    rows_seconds = sum(end - start for start, end in runs) if runs else timing.get('run_end', 0) - timing.get('run_start', 0)
    up_seconds = timing['up_done'] - timing['up_start'] if 'up_done' in timing and 'up_start' in timing else 0
    down_seconds = timing['down_done'] - timing['down_start'] if 'down_done' in timing and 'down_start' in timing else 0
    split = timing.get('split')
    if not isinstance(split, dict) or not {'unclassified', 'incomplete', 'unroled'} <= split.keys():
        split = time_split(run)
    unclassified = '?' if split['unclassified'] is None else f'{split["unclassified"]:.0f} s'
    rows_now = sha256(run / 'rows' / 'rows.json')
    rows_note = rows_now[:12] + '…' + ('' if rows_now == config['rows']['sha256'] else
                                       f' (changed during the boot; Up had {config["rows"]["sha256"][:12]}…)')
    manifests = sorted((run / 'out' / 'manifests').glob('*.json')) if (run / 'out' / 'manifests').is_dir() else []
    apps = sorted({f'{i["app"]["path"]} ({i["app"]["sha256"][:12]}…)' for i in config['instances']})
    lines = [f'# {config["label"]} — {time.strftime("%Y-%m-%d", time.gmtime())}',
             f'- Eden: {"; ".join(apps)}',
             f'- Plugins: ' + ', '.join(f'{Path(p["path"]).name} {p["sha256"][:8]}' for p in config['instances'][0].get('plugins', [])),
             f'- Rows: {len(passed)} PASS, {len(failed)} FAILED, {len(unrun)} unrun; {retained} earlier failed attempts retained '
             f'(receipts in `out/`); final rows.json {rows_note}; {len(manifests)} runner manifests in `out/manifests/`']
    for row, error in failed[:6]:
        lines.append(f'  - FAILED {row}: {one_line(error)}')
    if unrun:
        lines.append(f'  - unrun: {one_line(", ".join(unrun), 160)}')
    lines += [one_line(f'- Down: cleanup_failures={journal.get("cleanup_failures")}, released={journal.get("released")}; '
                       + '; '.join(unit_results(run)), 240),
              one_line(f'- Prune: {prune_summary(run, journal)}', 240),
              f'- Harvest: {harvest_summary(run)}',
              f'- Timing: up {up_seconds:.0f} s, rows {rows_seconds:.0f} s over '
              f'{max(len(runs), 1)} run call(s), down {down_seconds:.0f} s; '
              f'classified: up+setup+down {split["up"] + split["setup"] + split["down"]:.0f} s, '
              f'test {split["test"]:.0f} s'
              + (f', unroled {split["unroled"]:.0f} s' if split['unroled'] else '')
              + f', unclassified {unclassified}'
              + (' (incomplete timing)' if split['incomplete'] else '')
              + (f' ({split["roles_defaulted"]} roles defaulted)' if split['roles_defaulted'] else ''),
              '- Next: ']
    (run / 'RESULTS.md').write_text('\n'.join(lines[:20]) + '\n')
    print('\n'.join(lines[:20]))


PLUGINS = 'user/sdmc/atmosphere/contents/01006A800016E000/romfs/skyline/plugins'  # lifecycle.py PLUGINS
RAW_LINE = re.compile(r'\[(?:Bridge|TrainingRooms|Ui|Adapter)\] ')
MEDIA = ('.png', '.jpg', '.gif', '.mp4', '.webm', '.mkv')


def cell(text, limit):
    text = one_line(text, 10 ** 6)
    return (text if len(text) <= limit else text[:limit - 1] + '…').replace('|', '\\|')


def raw_lines(node):
    """The raw log lines a receipt carries (any string holding a `[Bridge] ` / `[TrainingRooms] ` / `[Ui] ` line)."""
    if isinstance(node, str):
        return [node] if RAW_LINE.search(node) else []
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    return [line for child in children for line in raw_lines(child)]


def nro_lines(run, config):
    """One line per plugin: the sha256 of the file in the Eden profile (what the game loads), checked against the
    config pin; the pins themselves, said as pins, once the profile is gone."""
    instances_path = run / 'state' / 'instances.json'
    instances = json.loads(instances_path.read_text()) if instances_path.exists() else {}
    lines = []
    for item in config.get('instances', []):
        pins = {pin.get('name', Path(pin['path']).name): pin['sha256'] for pin in item.get('plugins', [])}
        profile = (instances.get(item.get('key')) or {}).get('profile')
        folder = Path(profile) / PLUGINS if profile else None
        if folder is None or not folder.is_dir():
            lines += [f'  - {name} `{digest}` as pinned in config.json (the profile is gone or not on this machine, '
                      'so the loaded file was not re-hashed)' for name, digest in pins.items()]
            continue
        for path in sorted(folder.glob('*.nro')):
            digest = sha256(path)
            verdict = ('NOT in the config' if path.name not in pins else 'matches the config pin'
                       if pins[path.name] == digest else f'DIFFERS from the config pin {pins[path.name]}')
            lines.append(f'  - {path.name} `{digest}` in the profile; {verdict}')
        lines += [f'  - {name}: pinned `{digest}` but MISSING from the profile' for name, digest in pins.items()
                  if not (folder / name).exists()]
    return lines


def row_report(run, row, history):
    """(result, reason, proof) for one row from its receipt: PASS only for a receipt whose status is PASS."""
    out = run / 'out'
    row_id = row['id']
    receipt_path = out / f'{row_id}.json'
    earlier = history.get(row_id, [])
    asides = sorted(path.name for path in out.glob(f'{row_id}.rerun-*.json'))
    if not receipt_path.exists():
        folder = out / row_id
        recovery = folder / 'recovery'
        files = sorted(path for path in folder.rglob('*') if path.is_file()) if folder.is_dir() else []
        wrote = [path for path in files if not path.is_relative_to(recovery)]
        note = f'no receipt out/{row_id}.json'
        if not wrote and any(path.is_relative_to(recovery) for path in files):
            note += f'; a recovery to this row ran (out/{row_id}/recovery/)'
        if earlier:
            note += f'; attempts on record: {", ".join(earlier)}'
        if wrote:  # its steps ran, the runner ended before the row's verdict: attempted, never a pass and never unrun
            return ('FAIL', f'attempted without a verdict: its steps wrote {len(wrote)} file(s) but the runner ended '
                    f'before the row receipt ({note}); see the runner line above the table',
                    [f'out/{path.relative_to(out)}' for path in wrote[:4]] + [f'out/{name}' for name in asides])
        return 'NOT RUN', note, [f'out/{name}' for name in asides]
    receipt = json.loads(receipt_path.read_text())
    status = receipt.get('status')
    proof = [f'out/{row_id}.json']
    if status == 'PASS':
        result, parts = 'PASS', []
        for step in receipt.get('results', []):
            name = str(step.get('run') or step.get('gate') or step.get('library') or next(iter(step), '?'))
            value = step.get('value')
            if isinstance(value, dict) and 'verified' in value:
                value = {'verified': value['verified']}
            parts.append(name.split(':')[-1] + ('' if value is None else ' ' + cell(json.dumps(value, default=str), 110)))
        reason = '; '.join(parts) or 'every step ran'
    else:
        result = 'FAIL'
        reason = one_line(receipt.get('error') or '', 400) or f'status {status}'
        if status != 'FAILED':
            reason = f'{status}: ' + reason if receipt.get('error') else f'status {status} (ran, not verified)'
    if len(earlier) > 1:
        reason += f' (earlier attempts: {", ".join(earlier[:-1])})'
    proof += [f'`{cell(line, 230)}`' for line in dict.fromkeys(raw_lines(receipt))][:3]
    files = [Path(path) for path in receipt.get('evidence_paths', [])]
    files = [out / str(path).split('/out/', 1)[1] if '/out/' in str(path) else path for path in files]
    if (out / row_id).is_dir():
        files += [path for path in sorted((out / row_id).rglob('*')) if path.suffix.lower() in MEDIA]
    for path in dict.fromkeys(files):
        shown = f'out/{path.relative_to(out)}' if path.is_relative_to(out) else str(path)
        proof.append(f'{shown} ({path.stat().st_size} bytes)' if path.is_file() else f'{shown} (missing here)')
    return result, reason, proof + [f'out/{name}' for name in asides]


def report_rows(run):
    run = Path(run)
    spec = json.loads((run / 'rows' / 'rows.json').read_text())
    rows = spec['rows'] if isinstance(spec, dict) else spec
    history = {}
    attempts = run / 'out' / 'attempts.jsonl'
    for line in attempts.read_text().splitlines() if attempts.exists() else []:
        try:
            attempt = json.loads(line)
        except ValueError:
            continue
        history.setdefault(attempt.get('id'), []).append(str(attempt.get('status')))
    return [(row, *row_report(run, row, history)) for row in rows]


def report_counts(run):
    counts = {'PASS': 0, 'FAIL': 0, 'NOT RUN': 0}
    for _, result, _, _ in report_rows(run):
        counts[result] += 1
    return counts


def report_md(run):
    """The run's report: the NRO hashes, then every row as PASS, FAIL or NOT RUN with the reason and the proof, read
    from the run folder alone. Works while Eden is still up and again after Down."""
    run = Path(run)
    config = json.loads((run / 'config.json').read_text())
    rows = report_rows(run)
    timing = json.loads((run / 'timing.json').read_text()) if (run / 'timing.json').exists() else {}

    def utc(key):
        return time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(timing[key])) if key in timing else '?'

    host, me = config.get('host', '?'), f'/usr/bin/python3 -I -B {Path(__file__).resolve()}'
    if 'down_done' in timing:
        journal_path = run / 'state' / 'lifecycle.json'
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}
        state = (f'DOWN on {host} at {utc("down_done")} (released={journal.get("released")}, '
                 f'cleanup_failures={journal.get("cleanup_failures")})')
    elif 'up_done' in timing:
        state = f'EDEN UP on {host} since {utc("up_done")}; no Down recorded'
    else:
        state = f'no Up recorded on {host}'
    table, counts = [], {'PASS': 0, 'FAIL': 0, 'NOT RUN': 0}
    for row, result, reason, proof in rows:
        counts[result] += 1
        blocks = ', '.join(str(step.get('run', next(iter(step), '?'))).split(':')[-1] for step in row.get('steps', []))
        table.append(f'| {row["id"]} | {cell(blocks, 60)} | {result} | {cell(reason, 420)} | {"<br>".join(proof)} |')
    split = time_split(run)
    ends = []  # how each runner call ended: its last status line that is not a row's
    runner_log = run / 'runner.log'
    for line in runner_log.read_text(errors='replace').splitlines() if runner_log.exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and 'status' in event and 'row' not in event:
            detail = event.get('error') or event.get('reason') or event.get('result') or ''
            ends.append(f'{event["status"]}' + (f' `{cell(detail, 300)}`' if detail else ''))
    lines = [f'# {config.get("label", run.name)}: test report',
             f'- State: {state}',
             f'- Runner: {len(ends)} call(s) ended: ' + ('; '.join(ends) if ends else 'no end line in runner.log'),
             '- NROs:', *nro_lines(run, config),
             f'- Rows: {counts["PASS"]} PASS, {counts["FAIL"]} FAIL, {counts["NOT RUN"]} NOT RUN of {len(rows)} '
             f'(proof paths are relative to `{run}`)',
             '', '| Row | Steps | Result | Reason | Proof |', '| --- | --- | --- | --- | --- |', *table, '',
             f'- Times: Up {utc("up_start")} to {utc("up_done")}; rows {utc("run_start")} to {utc("run_end")} over '
             f'{len(timing.get("runs", [])) or 1} runner call(s); setup rows {split["setup"]:.0f} s, test rows '
             f'{split["test"]:.0f} s']
    if 'down_done' not in timing:
        lines += [f'- Re-run rows in this boot: `{me} run {run} --rows <id>[,<id>]` then `{me} report {run}`',
                  f'- Down: `{me} down {run}`']
    return '\n'.join(lines) + '\n'


def cmd_report(run):
    text = report_md(run)
    (run / 'REPORT.md').write_text(text)
    print(text, end='')
    return 0


def _go_sh(argv):
    print('$ ' + shlex.join(str(arg) for arg in argv), flush=True)
    return sh(argv)


def _pwsh_arg(value):
    value = str(value)
    if re.fullmatch(r'[A-Za-z0-9_./:@!=+-]+', value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _go_remote(args):
    hosts = RUNS / '_eden-test' / 'hosts.json'
    mapping = json.loads(hosts.read_text()) if hosts.is_file() else {}
    host = args.ssh or mapping.get(args.host)
    if not host:
        raise SystemExit(f'no SSH host for {args.host!r}; use --ssh or add it to {hosts}')
    if host.startswith('-'):
        raise SystemExit(f'invalid SSH host {host!r}')
    stage = f'/home/intpa/eden-runs/_eden-test/stage/{args.label}'
    commands = [
        ['rsync', '-a', '--delete', '--mkpath', str(PLAYBOOK / 'tools') + '/', f'{host}:{stage}/tools/'],
        *[['rsync', '-a', '--mkpath', str(nro), f'{host}:{stage}/nro/'] for nro in args.nro],
    ]
    if args.elf:
        commands.append(['rsync', '-a', '--mkpath', str(args.elf), f'{host}:{stage}/elf/'])
    rows = Path(args.rows)
    if rows.is_dir():
        commands.append(['rsync', '-a', '--mkpath', str(rows) + '/', f'{host}:{stage}/rows/'])
    else:
        commands.append(['rsync', '-a', '--mkpath', str(rows), f'{host}:{stage}/rows/rows.json'])
    for command in commands:
        if code := _go_sh(command):
            raise SystemExit(f'go staging failed ({code}): {shlex.join(command)}')
    script = f'{stage}/tools/eden-test/eden_test.py'
    remote = ['/usr/bin/python3', '-I', '-B', script, 'go', '--host', args.host, '--label', args.label]
    for nro in args.nro:
        remote.extend(('--nro', f'{stage}/nro/{Path(nro).name}'))
    remote.extend(('--rows', f'{stage}/rows'))
    if args.elf:
        remote.extend(('--elf', f'{stage}/elf/{Path(args.elf).name}'))
    for point in args.trace_point:
        remote.extend(('--trace-point', point))
    remote.extend(('--owner', args.owner))
    token = uuid.uuid4().hex
    remote.extend(('--token', token))
    ssh_code = _go_sh(['ssh', '-o', 'BatchMode=yes', host, *(_pwsh_arg(arg) for arg in remote)])
    run = f'/home/intpa/eden-runs/{args.label}'
    prefix = f'ssh -o BatchMode=yes {host} /usr/bin/python3 -I -B {script}'
    with tempfile.TemporaryDirectory(prefix='eden-go-') as directory:
        result_file = Path(directory) / 'go-result.json'
        result_error = _go_sh(['scp', f'{host}:{run}/go-result.json', str(result_file)])
        try:
            result = json.loads(result_file.read_text()) if not result_error else {}
            code = result['code']
            if (result.get('token') != token or type(code) is not int or code not in (0, 3)
                    or not isinstance(result['counts'], dict)):
                raise ValueError('invalid or mismatched result')
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise SystemExit(f'remote go produced no result for this call (ssh exit {ssh_code}); '
                             'nothing was copied; see the output above') from error
    report = f'./REPORT-{args.label}.md'
    if report_error := _go_sh(['scp', f'{host}:{run}/REPORT.md', report]):
        print(f'Down: {prefix} down {run}')
        raise SystemExit(f'copying remote report failed ({report_error})')
    print(f'Report: {Path(report).resolve()}')
    print(f'Re-run: {prefix} run {run} --rows <id>; then {prefix} report {run}')
    print(f'Down: {prefix} down {run}')
    return code


def cmd_go(args):
    # Match the owner rule in eden-lifecycle/lifecycle.py:624.
    if not re.fullmatch(r'(OpenCode|Codex|Claude|Cursor)\s+\S+', args.owner):
        raise SystemExit('--owner must be "<Engine> <session>/<stream>" (OpenCode|Codex|Claude|Cursor), '
                         f'e.g. "Claude devin-<lane>/<label>"; got: {args.owner!r}')
    base = Path(args.from_config) if args.from_config else RUNS / '_eden-test' / 'base.json'
    if not base.is_file() or json.loads(base.read_text()).get('host') != args.host:
        _go_trace(args)
        return _go_remote(args)
    run = go_new(args)
    try:
        cmd_up(run, skip_setup_plan=True, allow_volatile_pins=True)
    except BaseException:
        timing = run / 'timing.json'
        if timing.is_file() and 'up_start' in json.loads(timing.read_text()):
            print(f'Down: /usr/bin/python3 -I -B {Path(__file__).resolve()} down {run}')
        raise
    code = 3
    try:
        code = cmd_run(run)
    except (Exception, SystemExit) as error:
        print(f'run failed: {error}')
    finally:
        cmd_report(run)
        print(f'Report: {run / "REPORT.md"}')
    counts = report_counts(run)
    code = 0 if code == 0 and not counts['FAIL'] and not counts['NOT RUN'] else 3
    (run / 'go-result.json').write_text(json.dumps({'token': args.token, 'code': code, 'counts': counts}, indent=2) + '\n')
    return code


def stop_capture(run):
    """End the detached bridge capture `routes:bridge_on` started for this run (out/bridge-run/capture.json)."""
    folder = run / 'out' / 'bridge-run'
    if not (folder / 'capture.json').is_file():
        return None
    pid = json.loads((folder / 'capture.json').read_text()).get('pid')
    try:
        command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except OSError:
        return {'pid': pid, 'stopped': False, 'why': 'not running'}
    if b'capture-bridge' not in command or str(folder).encode() not in command:
        return {'pid': pid, 'stopped': False, 'why': 'that pid is not this run\'s capture'}
    os.kill(pid, signal.SIGTERM)
    return {'pid': pid, 'stopped': True}


def restore_up_config(run):
    """Down cleans up what Up created, so it uses the Up-time config the lifecycle journal recorded. A config.json
    edited after Up (it shouldn't be: rows.json and hooks are what change mid-boot) is kept aside, never merged into
    the journal; the lifecycle refuses a journal/config mismatch by design."""
    journal_path = run / 'state' / 'lifecycle.json'
    config_path = run / 'config.json'
    if not journal_path.exists() or not config_path.exists():
        return None
    up_config = json.loads(journal_path.read_text()).get('config')
    if not up_config or json.loads(config_path.read_text()) == up_config:
        return None
    aside = run / f'config.after-up.{int(time.time())}.json'
    config_path.rename(aside)
    config_path.write_text(json.dumps(up_config, indent=2) + '\n')
    print(f'config.json changed after Up; Down uses the Up-time config (edited copy kept as {aside.name})')
    return aside


def cmd_down(run, keep_state=False):
    restore_up_config(run)
    config = json.loads((run / 'config.json').read_text())
    capture = stop_capture(run)  # before Eden stops: the capture would otherwise keep reconnecting to a dead port
    if capture:
        print('bridge capture: ' + json.dumps(capture))
    stamp(run, 'down_start')
    # A clean Down copies the files the run wrote to out/post-run-<key>/, then prunes work/ and the state snapshots.
    code = lifecycle(run, 'down', '--evidence-dir', str(run / 'out'), *(['--keep-state'] if keep_state else []))
    stamp(run, 'down_done')
    done = subprocess.run([sys.executable, '-B', '-I', str(HARVEST), str(run / 'rows' / 'rows.json'),
                           '--label', config['label'], '--out', str(run / 'out')], capture_output=True, text=True)
    (run / 'harvest.json').write_text(done.stdout.strip() + '\n')
    timing_path = run / 'timing.json'
    timing = json.loads(timing_path.read_text())
    timing['split'] = time_split(run)
    timing_path.write_text(json.dumps(timing, indent=2) + '\n')
    results_md(run, config)
    (run / 'REPORT.md').write_text(report_md(run))
    if code != 0:
        raise SystemExit(f'lifecycle down failed ({code}); the claim stays held; see {run / "lifecycle-down.log"}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='mode', required=True)
    for name in ('new', 'all'):
        p = sub.add_parser(name)
        p.add_argument('--from', dest='from_config', required=True)
        p.add_argument('--label', required=True)
        p.add_argument('--rows', required=True, help='folder with rows.json, segments, refs and hooks')
        p.add_argument('--app', help='use this Eden AppImage instead of the previous config\'s')
        p.add_argument('--owner')
        p.add_argument('--plugin', action='append', default=[],
                       help='swap the pinned plugin with this file name for this build (repeatable; sha256 is read)')
        p.add_argument('--defer-load', action='store_true',
                       help='leave the frontend ready so rows can configure boot tracing before game.load')
        if name == 'all':
            p.add_argument('--skip-setup-plan', action='store_true',
                           help='re-running rows whose starting states are already recorded')
            p.add_argument('--allow-volatile-pins', action='store_true',
                           help='allow pins inside another run folder')
    p = sub.add_parser('go', help='stage a local or remote run, Up, run and report without Down')
    p.add_argument('--host', required=True)
    p.add_argument('--label', required=True)
    p.add_argument('--nro', action='append', required=True)
    p.add_argument('--rows', required=True, help='rows.json or a folder containing rows.json')
    p.add_argument('--elf')
    p.add_argument('--trace-point', action='append', default=[])
    p.add_argument('--owner', required=True,
                   help='ledger owner, "<Engine> <session>/<stream>" with Engine one of OpenCode, Codex, '
                        'Claude, Cursor — e.g. "Claude devin-<lane>/<label>"')
    p.add_argument('--from', dest='from_config')
    p.add_argument('--ssh')
    p.add_argument('--token', help=argparse.SUPPRESS)
    sub.add_parser('pace').add_argument('run', type=Path)
    sub.add_parser('report').add_argument('run', type=Path)
    p = sub.add_parser('down')
    p.add_argument('run', type=Path)
    p.add_argument('--keep-state', action='store_true',
                   help='keep work/ and the state snapshots for debugging (only when asked); a later down prunes them')
    for name in ('check', 'up'):
        p = sub.add_parser(name)
        p.add_argument('run', type=Path)
        p.add_argument('--skip-setup-plan', action='store_true',
                       help='re-running rows whose starting states are already recorded (no "setup" list yet)')
        p.add_argument('--allow-volatile-pins', action='store_true',
                       help='allow pins inside another run folder')
    p = sub.add_parser('run')
    p.add_argument('run', type=Path)
    p.add_argument('--retry', action='store_true')
    p.add_argument('--rows', help='comma-separated row ids to run again in this boot, passed or failed; the earlier '
                                  'receipt is kept as out/<id>.rerun-<n>.json')
    args = parser.parse_args(argv)
    if args.mode == 'go':
        return cmd_go(args)
    if args.mode == 'new':
        cmd_new(args)
    elif args.mode == 'check':
        return cmd_check(args.run.resolve(), args.skip_setup_plan, args.allow_volatile_pins)
    elif args.mode == 'up':
        cmd_up(args.run.resolve(), args.skip_setup_plan, args.allow_volatile_pins)
    elif args.mode == 'run':
        return cmd_run(args.run.resolve(), args.retry, args.rows)
    elif args.mode == 'report':
        return cmd_report(args.run.resolve())
    elif args.mode == 'down':
        cmd_down(args.run.resolve(), args.keep_state)
    elif args.mode == 'pace':
        return cmd_pace(args.run.resolve())
    elif args.mode == 'all':
        run = cmd_new(args)
        cmd_up(run, args.skip_setup_plan, args.allow_volatile_pins)
        try:
            cmd_run(run)
        finally:
            cmd_down(run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
