#!/usr/bin/env python3
"""One command for an Eden test run: eden-lifecycle + eden-runner + harvest + a short RESULTS.md.

Run it ON the host that runs Eden (the Surface locally; the Yoga with `ssh yoga python3 ~/ssbu-mod-playbook/...`,
after `git -C ~/ssbu-mod-playbook pull --ff-only` there and copying the rows folder over).

  eden_test.py new  --from <previous config.json> --label lr-14 --rows <rows folder>   new run folder + config
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
import json
import shutil
import subprocess
import sys
import time
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
    rows_src = Path(args.rows)
    run.mkdir(parents=True)
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
    shutil.copytree(rows_src, run / 'rows', symlinks=True)
    config['rows'] = {'path': str(run / 'rows' / 'rows.json'), 'sha256': sha256(run / 'rows' / 'rows.json')}
    # The lifecycle dry-runs the rows with the runner it pins: always this playbook's runner, never a staged copy.
    config['runner'] = {'path': str(RUNNER), 'sha256': sha256(RUNNER)}
    (run / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
    stamp(run, 'new')
    print(json.dumps({'run': str(run), 'label': args.label, 'apps': sorted({i['app']['path'] for i in config['instances']})}))
    return run


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
                child = f'{path}.{key}' if path else key
                if key not in left or key not in right:
                    found.append(child)
                else:
                    found.extend(diff(left[key], right[key], child))
            return found
        if isinstance(left, list):
            if len(left) != len(right):
                return [path]
            return [item for i, (a, b) in enumerate(zip(left, right))
                    for item in diff(a, b, f'{path}.{i}' if path else str(i))]
        return [] if left == right else [path]
    return sorted(diff(fixture, reviewed, ''))


def fixture_comparison(item, base):
    reviewed = item.get('reviewed')
    result = {'state': item.get('state'), 'file': item.get('file'), 'reviewed': reviewed,
              'differing_paths': [], 'differs_only': item.get('differs_only', []), 'ok': False}
    problems = []
    state = item.get('state', '?')
    if not item.get('file'):
        return result, [f'{state}: reviewed requires file']
    if not isinstance(reviewed, dict) or not reviewed.get('path'):
        return result, [f'{state}: reviewed needs path and sha256']
    allowed = result['differs_only']
    if not isinstance(allowed, list) or any(not isinstance(path, str) for path in allowed):
        return result, [f'{state}: differs_only must be a list of paths']
    missing = object()
    values = []
    for name, source in (('fixture', item['file']), ('reviewed', reviewed['path'])):
        try:
            path = Path(source)
            data = (path if path.is_absolute() else Path(base) / path).read_bytes()
            value = json.loads(data)
        except (OSError, ValueError, TypeError) as error:
            problems.append(f'{state}: {name} {source}: {type(error).__name__}')
            values.append(missing)
            continue
        if name == 'reviewed' and (not isinstance(reviewed.get('sha256'), str)
                                   or reviewed['sha256'].lower() != hashlib.sha256(data).hexdigest()):
            problems.append(f'{state}: reviewed sha256 mismatch: {source}')
        values.append(value)
    if len(values) == 2 and all(value is not missing for value in values):
        result['differing_paths'] = fixture_diff(*values)
        offending = [path for path in result['differing_paths']
                     if not any(path == allow or path.startswith(allow + '.') for allow in allowed)]
        if offending:
            problems.append(f'{state}: {len(offending)} differing paths: '
                            + ', '.join(path or '<root>' for path in offending[:12]))
    result['ok'] = not problems
    return result, problems


def fixture_problems(item, base):
    return fixture_comparison(item, base)[1] if 'reviewed' in item else []


def setup_plan_problems(spec, base=None):
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
        if base is not None and isinstance(item, dict):
            problems.extend(f'setup[{i}]: {problem}' for problem in fixture_problems(item, base))
    return problems


def cmd_up(run, skip_setup_plan=False):
    rows = run / 'rows' / 'rows.json'
    spec = json.loads(rows.read_text())
    problems = setup_plan_problems(spec, rows.parent)
    setup = spec.get('setup', []) if isinstance(spec, dict) else []
    comparisons = [(i, fixture_comparison(item, rows.parent)) for i, item in enumerate(setup)
                   if isinstance(item, dict) and 'reviewed' in item] if isinstance(setup, list) else []
    (run / 'setup-fixtures.json').write_text(json.dumps([result for _, (result, _) in comparisons], indent=2) + '\n')
    if skip_setup_plan:
        problems = [f'setup[{i}]: {problem}' for i, (_, found) in comparisons for problem in found]
    if problems:
        raise SystemExit('setup plan: ' + '; '.join(problems) + ' (MACROS rule 13; --skip-setup-plan only to re-run '
                         'rows that already have their segments)')
    if sh([sys.executable, '-B', '-I', str(RUNNER), 'dry-run', str(rows), '--out', str(run / 'out')],
          run / 'dry-run.log') != 0:
        raise SystemExit('dry run failed; fix rows.json before Up')
    stamp(run, 'up_start')
    code = lifecycle(run, 'up')
    stamp(run, 'up_done')
    if code != 0:
        raise SystemExit(f'lifecycle up failed ({code}); see {run / "lifecycle-up.log"}')


def cmd_run(run, retry=False):
    stop = run / 'out' / 'STOP-INPUT'
    if retry and stop.exists():
        stop.rename(run / 'out' / f'STOP-INPUT.{int(time.time())}')
    stamp(run, 'run_start')
    code = sh([sys.executable, '-B', '-I', str(RUNNER), 'run', str(run / 'rows' / 'rows.json'),
               '--instances', str(run / 'state' / 'instances.json'), '--out', str(run / 'out')], run / 'runner.log')
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
    runs = [(start, end) for start, end in timing.get('runs', []) if end]
    rows_seconds = sum(end - start for start, end in runs) if runs else timing.get('run_end', 0) - timing.get('run_start', 0)
    rows_path = run / 'rows' / 'rows.json'
    spec = json.loads(rows_path.read_text()) if rows_path.exists() else []
    rows = spec.get('rows', []) if isinstance(spec, dict) else spec
    durations = {'setup': 0, 'test': 0}
    for row in rows:
        path = run / 'out' / f'{row["id"]}.json'
        if not path.exists():
            continue
        receipt = json.loads(path.read_text())
        if receipt.get('started') is None or receipt.get('ended') is None:
            continue
        role = receipt.get('role', 'setup' if row.get('start') != row.get('end') else 'test')
        if role in durations:
            durations[role] += receipt['ended'] - receipt['started']
    up = timing['up_done'] - timing['up_start'] if 'up_done' in timing and 'up_start' in timing else 0
    down = timing['down_done'] - timing['down_start'] if 'down_done' in timing and 'down_start' in timing else 0
    between = max(0, rows_seconds - durations['setup'] - durations['test'])
    harness = up + durations['setup'] + between + down
    total = harness + durations['test']
    return {'up': up, 'setup': durations['setup'], 'test': durations['test'], 'between_rows': between,
            'down': down, 'harness': harness, 'test_share': round(durations['test'] / total, 3) if total else 0}


def results_md(run, config):
    passed, failed, unrun, retained = row_accounting(run)
    journal = json.loads((run / 'state' / 'lifecycle.json').read_text()) if (run / 'state' / 'lifecycle.json').exists() else {}
    timing = json.loads((run / 'timing.json').read_text())
    runs = [(start, end) for start, end in timing.get('runs', []) if end]
    rows_seconds = sum(end - start for start, end in runs) if runs else timing.get('run_end', 0) - timing.get('run_start', 0)
    split = timing.get('split') or time_split(run)
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
              f'- Timing: up {timing.get("up_done", 0) - timing.get("up_start", 0):.0f} s, rows {rows_seconds:.0f} s over '
              f'{max(len(runs), 1)} run call(s), down {timing.get("down_done", 0) - timing.get("down_start", 0):.0f} s; '
              f'harness {split["harness"]:.0f} s, test {split["test"]:.0f} s ({split["test_share"] * 100:.1f} %)',
              '- Next: ']
    (run / 'RESULTS.md').write_text('\n'.join(lines[:20]) + '\n')
    print('\n'.join(lines[:20]))


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
    sub.add_parser('pace').add_argument('run', type=Path)
    p = sub.add_parser('down')
    p.add_argument('run', type=Path)
    p.add_argument('--keep-state', action='store_true',
                   help='keep work/ and the state snapshots for debugging (only when asked); a later down prunes them')
    p = sub.add_parser('up')
    p.add_argument('run', type=Path)
    p.add_argument('--skip-setup-plan', action='store_true',
                   help='re-running rows whose starting states are already recorded (no "setup" list yet)')
    p = sub.add_parser('run')
    p.add_argument('run', type=Path)
    p.add_argument('--retry', action='store_true')
    args = parser.parse_args(argv)
    if args.mode == 'new':
        cmd_new(args)
    elif args.mode == 'up':
        cmd_up(args.run.resolve(), args.skip_setup_plan)
    elif args.mode == 'run':
        return cmd_run(args.run.resolve(), args.retry)
    elif args.mode == 'down':
        cmd_down(args.run.resolve(), args.keep_state)
    elif args.mode == 'pace':
        return cmd_pace(args.run.resolve())
    elif args.mode == 'all':
        run = cmd_new(args)
        cmd_up(run)
        try:
            cmd_run(run)
        finally:
            cmd_down(run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
