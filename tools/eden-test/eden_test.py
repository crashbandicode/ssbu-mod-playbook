#!/usr/bin/env python3
"""One command for an Eden test run: eden-lifecycle + eden-runner + harvest + a short RESULTS.md.

Run it ON the host that runs Eden (the Surface locally; the Yoga with `ssh yoga python3 ~/ssbu-mod-playbook/...`,
after `git -C ~/ssbu-mod-playbook pull --ff-only` there and copying the rows folder over).

  eden_test.py new  --from <previous config.json> --label lr-14 --rows <rows folder>   new run folder + config
  eden_test.py up   <run folder>        offline dry run, then lifecycle up
  eden_test.py run  <run folder> [--retry]   runner (with --retry: clear STOP-INPUT first; passed rows skip)
  eden_test.py down <run folder>        lifecycle down, harvest, RESULTS.md, timing.json
  eden_test.py all  --from ... --label ... --rows ...   new + up + run + down (down always runs)

A run folder is /home/intpa/eden-runs/<label>/ with config.json, rows/ (a copy of the rows folder), state/ and
work/ (fresh every run), out/ (runner output) and RESULTS.md. Pins (Eden AppImage, plugins, files, game) carry over
from the previous config unchanged; `new` checks each pinned file still has its sha256, and `--app` swaps in a
different Eden AppImage (resolved path; its sha256 is read). Same-boot fixing (MACROS rule 8): after a failed row,
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
    timing_path = run / 'timing.json'
    timing = json.loads(timing_path.read_text()) if timing_path.exists() else {}
    timing[key] = time.time()
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


def lifecycle(run, mode):
    config = json.loads((run / 'config.json').read_text())
    if config.get('host') not in ('surface', 'yoga', 'ally'):
        raise SystemExit('config host must be surface, yoga or ally')
    argv = [sys.executable, '-B', '-I', str(LIFECYCLE), mode, str(run / 'config.json'), '--execute']
    return sh(argv, run / f'lifecycle-{mode}.log')


def cmd_up(run):
    rows = run / 'rows' / 'rows.json'
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
    return code


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
        done = subprocess.run(['systemctl', '--user', 'show', unit, '--property=Result,ExecMainStatus'],
                              capture_output=True, text=True)
        props = dict(line.split('=', 1) for line in done.stdout.splitlines() if '=' in line)
        results.append(f'{item.get("key")}: Result={props.get("Result", "?")} status={props.get("ExecMainStatus", "?")}')
    return results


def results_md(run, config):
    out = run / 'out'
    receipts = sorted(p for p in out.glob('*.json') if p.name not in ('dry-run.json', 'pace.json'))
    rows = [(p.stem, json.loads(p.read_text())) for p in receipts]
    passed = [r for r, j in rows if j.get('status') == 'PASS']
    failed = [(r, j.get('error', '')) for r, j in rows if j.get('status') != 'PASS']
    journal = json.loads((run / 'state' / 'lifecycle.json').read_text()) if (run / 'state' / 'lifecycle.json').exists() else {}
    harvest = (run / 'harvest.json').read_text().strip() if (run / 'harvest.json').exists() else 'not run'
    timing = json.loads((run / 'timing.json').read_text())
    apps = sorted({f'{i["app"]["path"]} ({i["app"]["sha256"][:12]}…)' for i in config['instances']})
    lines = [f'# {config["label"]} — {time.strftime("%Y-%m-%d", time.gmtime())}',
             f'- Eden: {"; ".join(apps)}',
             f'- Plugins: ' + ', '.join(f'{Path(p["path"]).name} {p["sha256"][:8]}' for p in config['instances'][0].get('plugins', [])),
             f'- Rows: {len(passed)} PASS, {len(failed)} FAILED (receipts in `out/`); rows.json sha256 {config["rows"]["sha256"][:12]}…']
    for row, error in failed[:8]:
        lines.append(f'  - FAILED {row}: {str(error)[:140]}')
    lines += [f'- Down: cleanup_failures={journal.get("cleanup_failures")}, released={journal.get("released")}; '
              + '; '.join(unit_results(run)),
              f'- Harvest: {harvest[:160]}',
              f'- Timing: up {timing.get("up_done", 0) - timing.get("up_start", 0):.0f} s, rows '
              f'{timing.get("run_end", 0) - timing.get("run_start", 0):.0f} s, down {timing.get("down_done", 0) - timing.get("down_start", 0):.0f} s',
              '- Next: ']
    (run / 'RESULTS.md').write_text('\n'.join(lines[:20]) + '\n')
    print('\n'.join(lines[:20]))


def cmd_down(run):
    config = json.loads((run / 'config.json').read_text())
    stamp(run, 'down_start')
    code = lifecycle(run, 'down')
    stamp(run, 'down_done')
    done = subprocess.run([sys.executable, '-B', '-I', str(HARVEST), str(run / 'rows' / 'rows.json'),
                           '--label', config['label'], '--out', str(run / 'out')], capture_output=True, text=True)
    (run / 'harvest.json').write_text(done.stdout.strip() + '\n')
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
    for name in ('up', 'down'):
        sub.add_parser(name).add_argument('run', type=Path)
    p = sub.add_parser('run')
    p.add_argument('run', type=Path)
    p.add_argument('--retry', action='store_true')
    args = parser.parse_args(argv)
    if args.mode == 'new':
        cmd_new(args)
    elif args.mode == 'up':
        cmd_up(args.run.resolve())
    elif args.mode == 'run':
        return cmd_run(args.run.resolve(), args.retry)
    elif args.mode == 'down':
        cmd_down(args.run.resolve())
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
