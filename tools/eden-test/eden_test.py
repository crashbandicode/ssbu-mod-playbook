#!/usr/bin/env python3
"""One command for an Eden test run: eden-lifecycle + eden-runner + harvest + a short RESULTS.md.

  eden_test.py go --host <host> --label <label> --nro <file> --rows <rows> [--card-lineup <dir> | --no-card-lineup <reason>]  stage, Up, run, report (no Down)
  eden_test.py run <run> --rows <id>   rerun one row; eden_test.py report <run>   show the updated report

Run it ON the host that runs Eden (the Surface locally; the Yoga with `ssh tr-yoga python3 ~/ssbu-mod-playbook/...`,
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
import base64
import copy
from contextlib import redirect_stderr, redirect_stdout
import errno
import hashlib
import heapq
import importlib.util
import json
import math
import os
import posixpath
import re
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import uuid
from pathlib import Path, PureWindowsPath

try:
    import fcntl
except ImportError:
    fcntl = None

PLAYBOOK = Path(__file__).resolve().parents[2]
LIFECYCLE = PLAYBOOK / 'tools/eden-lifecycle/lifecycle.py'
_lifecycle_spec = importlib.util.spec_from_file_location('_eden_test_lifecycle', LIFECYCLE)
_lifecycle_module = importlib.util.module_from_spec(_lifecycle_spec)
_lifecycle_spec.loader.exec_module(_lifecycle_module)
SshMux = _lifecycle_module.SshMux
_host_os_spec = importlib.util.spec_from_file_location('_eden_test_host_os', PLAYBOOK / 'tools/eden-runner/host_os.py')
host_os = importlib.util.module_from_spec(_host_os_spec)
_host_os_spec.loader.exec_module(host_os)
RUNNER = PLAYBOOK / 'tools/eden-runner/runner.py'
HARVEST = PLAYBOOK / 'tools/eden-runner/harvest.py'
RUNS = host_os.RUNS_ROOT
CARD_LINEUPS = Path('/home/intpa/smash-training-lobby/artifacts/phase23/card-lineup')
NET_SYSFS = Path('/sys/class/net')
X11_SOCKET_DIR = Path('/tmp/.X11-unix')
WINDOWS_HOSTS = ('ally',)
RELAY_PREFIX = '@@eden-test-relay@@ '


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


class _ExitCode(int):
    def __new__(cls, code, stderr='', diagnosis=None, timed_out=False):
        result = super().__new__(cls, code)
        result.stderr, result.diagnosis, result.timed_out = stderr, diagnosis, timed_out
        return result


_GO_LOG = None


def sh(argv, log=None):
    """Run a tool, echo its output, keep a copy in the run folder; returns the exit code."""
    if _GO_LOG is not None:
        _GO_LOG.flush()
        offset = os.fstat(_GO_LOG.fileno()).st_size
        done = subprocess.run(argv, stdout=_GO_LOG, stderr=subprocess.STDOUT, text=True)
        with open(_GO_LOG.name, 'rb') as stream:
            stream.seek(offset)
            out = stream.read().decode('utf-8', errors='replace')
        stderr = out if done.returncode else ''
    else:
        done = subprocess.run(argv, capture_output=True, text=True)
        out = done.stdout + done.stderr
        sys.stdout.write(out)
        stderr = done.stderr or ''
    if log:
        with open(log, 'a') as f:
            f.write(f'$ {" ".join(map(str, argv))}\n{out}\n')
    return _ExitCode(done.returncode, stderr)


def _target_commit(commit):
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('target commit must be 40 lower-case hex characters')


def _staged_target_arg(commit):
    try:
        _target_commit(commit)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return commit


def _target_label(label):
    if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{1,63}', label):
        raise ValueError('invalid target stage label')


def _target_git(tree, *args, timeout=600, env=None):
    result = subprocess.run(['git', '--no-optional-locks', '-C', str(tree), *args],
                            capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f'git exit {result.returncode}')
    return result.stdout.strip()


def _strict_target(tree, commit):
    tree = Path(tree)
    try:
        if tree.is_symlink() or not tree.is_dir() or not (tree / '.git').is_dir() or (tree / '.git').is_symlink():
            raise ValueError('not a checkout directory')
        env = _target_build_environment()
        head = _target_git(tree, 'rev-parse', 'HEAD', timeout=120, env=env)
        if head != commit:
            raise ValueError(f'is at {head}, not {commit}')
        status = _target_git(tree, 'status', '--porcelain', '--untracked-files=all', '--ignored', timeout=120, env=env)
        if status:
            raise ValueError(f'changed after staging: {status.splitlines()[0]}')
        _target_git(tree, 'cat-file', '-e', f'{commit}^{{commit}}', timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f'cannot be read: {error}') from error


def _target_build_environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
    return env


def _readonly_target(root):
    for directory, names, files in os.walk(root, topdown=False, followlinks=False):
        for name in [*files, *names]:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(path.stat().st_mode & ~0o222)
        Path(directory).chmod(Path(directory).stat().st_mode & ~0o222)


def _remove_target_tree(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f'not an owned target directory: {root}')
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        path = Path(directory)
        path.chmod(path.stat().st_mode | 0o700)
        names[:] = [name for name in names if not (path / name).is_symlink()]
        for name in files:
            host_os.remove_file(path / name)
    shutil.rmtree(root)


def _target_size(root):
    files, size = 0, 0
    for directory, _, names in os.walk(root, followlinks=False):
        for name in names:
            files += 1
            size += (Path(directory) / name).lstat().st_size
    return files, size


def _owned_target_directory(runs, parts, create=False):
    path = Path(runs)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f'symlink in target directory: {path}')
        if create:
            path.mkdir(exist_ok=True)
        elif not path.exists():
            return None
        if not path.is_dir():
            raise ValueError(f'not a target directory: {path}')
    return path


def _incoming_target(commit, label, runs, create=False):
    parent = _owned_target_directory(runs, ('_eden-test', 'stage', label, 'targets', '.incoming'), create=create)
    if parent is None:
        return None
    pack = parent / f'{commit}.pack'
    if pack.is_symlink():
        raise ValueError(f'symlink in target pack: {pack}')
    return pack


def pack_target(tree, commit, out_dir):
    _target_commit(commit)
    tree, out_dir = Path(tree), Path(out_dir)
    if not tree.is_absolute() or not tree.is_dir():
        raise ValueError(f'target tree is not an existing absolute directory: {tree}')
    _target_git(tree, 'cat-file', '-e', f'{commit}^{{commit}}', timeout=20)
    out_dir.mkdir(parents=True, exist_ok=True)
    pack = out_dir / f'{commit}.pack'
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    rev = subprocess.Popen(['nice', '-n', '19', 'git', '-C', str(tree),
                            'rev-list', '--objects', '--no-walk', commit],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    try:
        with pack.open('wb') as stream:
            writer = subprocess.Popen(['nice', '-n', '19', 'git', '-C', str(tree), 'pack-objects', '--stdout'],
                                      stdin=rev.stdout, stdout=stream, stderr=subprocess.PIPE, env=environment)
            rev.stdout.close()
            rev.stdout = None
            try:
                _, pack_error = writer.communicate(timeout=600)
                _, rev_error = rev.communicate(timeout=10)
            except BaseException:
                writer.kill()
                rev.kill()
                writer.wait()
                rev.wait()
                raise
        if rev.returncode or writer.returncode:
            detail = (rev_error + pack_error).decode('utf-8', errors='replace').strip()
            raise ValueError(f'packing {commit} failed: {detail or f"git exits {rev.returncode}/{writer.returncode}"}')
        return {'commit': commit, 'pack_file': str(pack), 'pack_sha256': sha256(pack), 'bytes': pack.stat().st_size}
    except BaseException:
        if rev.poll() is None:
            rev.kill()
            rev.wait()
        pack.unlink(missing_ok=True)
        raise


def probe_target(commit, runs=RUNS):
    _target_commit(commit)
    parent = _owned_target_directory(runs, ('_eden-test', 'target-cache'))
    if parent is None:
        return False
    cache = parent / commit
    if not cache.exists() and not cache.is_symlink():
        return False
    try:
        _strict_target(cache, commit)
        return True
    except (ValueError, OSError):
        return False


def stage_target(commit, label, pack=None, runs=RUNS):
    _target_commit(commit)
    _target_label(label)
    runs = Path(runs)
    cache_parent = _owned_target_directory(runs, ('_eden-test', 'target-cache'), create=True)
    cache = cache_parent / commit
    targets = _owned_target_directory(runs, ('_eden-test', 'stage', label, 'targets'), create=True)
    staged = targets / commit
    if pack is not None and Path(pack) == targets / '.incoming' / f'{commit}.pack':
        _incoming_target(commit, label, runs)
    if staged.exists() or staged.is_symlink():
        try:
            _strict_target(staged, commit)
        except ValueError as error:
            raise ValueError(f'staged tree {staged} {error}') from error
    built = False
    if cache.exists() or cache.is_symlink():
        try:
            _strict_target(cache, commit)
        except ValueError:
            suffix = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + f'-{time.time_ns()}'
            cache.rename(cache_parent / f'.rejected-{commit}-{suffix}')
    if not cache.exists():
        if pack is None:
            raise ValueError(f'no valid cache or pack for target {commit}')
        temporary = cache_parent / f'.tmp-{commit}-{os.getpid()}'
        temporary.mkdir()
        try:
            env = _target_build_environment()
            subprocess.run(['git', 'init', '-q', '--template=', str(temporary)], capture_output=True, text=True,
                           check=True, timeout=120, env=env)
            with Path(pack).open('rb') as stream:
                subprocess.run(['git', '-C', str(temporary), 'index-pack', '--stdin'], stdin=stream,
                               capture_output=True, check=True, timeout=600, env=env)
            if _target_git(temporary, 'cat-file', '-t', commit, env=env) != 'commit':
                raise ValueError(f'pack does not contain commit {commit}')
            (temporary / '.git' / 'shallow').write_text(commit + '\n')
            _target_git(temporary, 'checkout', '-q', '--detach', commit, env=env)
            _strict_target(temporary, commit)
            _readonly_target(temporary)
            temporary.rename(cache)
            built = True
        finally:
            if temporary.exists():
                _remove_target_tree(temporary)
    _readonly_target(cache)
    if not staged.exists():
        temporary = targets / f'.tmp-{commit}-{os.getpid()}'
        try:
            shutil.copytree(cache, temporary, symlinks=True, copy_function=os.link)
            _readonly_target(temporary)
            _strict_target(temporary, commit)
            temporary.rename(staged)
        finally:
            if temporary.exists():
                _remove_target_tree(temporary)
    _strict_target(staged, commit)
    files, size = _target_size(staged)
    if pack is not None and Path(pack) == targets / '.incoming' / f'{commit}.pack':
        Path(pack).unlink(missing_ok=True)
    return {'commit': commit, 'cache': 'built' if built else 'reused', 'staged': str(staged),
            'files': files, 'bytes': size}


def unstage_targets(label, runs=RUNS):
    _target_label(label)
    targets = _owned_target_directory(runs, ('_eden-test', 'stage', label, 'targets'))
    if targets is not None:
        _remove_target_tree(targets)


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


def default_card_lineup():
    stamps = sorted(path for path in CARD_LINEUPS.iterdir()
                    if path.is_dir() and re.fullmatch(r'\d{8}-\d{4}Z', path.name)) if CARD_LINEUPS.is_dir() else []
    if not stamps:
        raise SystemExit(f'no card lineup under {CARD_LINEUPS} (no <YYYYMMDD-HHMM>Z folder; on an Eden host '
                         'that folder does not exist: run go from the workspace machine or copy a lineup and pass '
                         '--card-lineup <dir>); or pass --no-card-lineup <reason>')
    folder = stamps[-1]
    if not (folder / 'plugins').is_dir():
        raise SystemExit(f'newest card lineup {stamps[-1]} has no plugins folder (older stamps are not used); '
                         'refresh a complete card lineup or pass --card-lineup <dir> / '
                         '--no-card-lineup <reason>')
    return folder


def read_card_lineup(folder):
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise SystemExit(f'card lineup folder missing or not a directory: {folder}')
    root = folder.parent if folder.name == 'plugins' and (folder.parent / 'ultimate').exists() else folder
    plugin_dir = root / 'plugins' if (root / 'plugins').is_dir() else root
    errors = []
    if plugin_dir.is_symlink():
        errors.append(f'card lineup plugins directory is a symlink: {plugin_dir}')
    sums = plugin_dir / 'SHA256SUMS.txt'
    lines = []
    sums_digest = None
    if sums.is_symlink():
        errors.append(f'SHA256SUMS.txt is a symlink: {sums}')
    elif not sums.is_file():
        errors.append(f'SHA256SUMS.txt missing or not a regular file: {sums}')
    else:
        try:
            data = sums.read_bytes()
            sums_digest = hashlib.sha256(data).hexdigest()
            lines = data.decode('utf-8').splitlines()
        except (OSError, UnicodeError) as error:
            errors.append(f'cannot read {sums}: {error}')
    listed = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        match = re.fullmatch(r'([0-9a-f]{64}) {1,2}\*?(.+)', line)
        if not match:
            errors.append(f'malformed SHA256SUMS.txt line {number}: {line!r}')
            continue
        digest, name = match.groups()
        if (name != Path(name).name or '/' in name or '\\' in name
                or not name.endswith('.nro') or name in ('.', '..')):
            errors.append(f'invalid plugin name on line {number}: {name!r}')
            continue
        if name in listed:
            errors.append(f'duplicate plugin name: {name}')
            continue
        listed[name] = digest
    if not listed:
        errors.append('SHA256SUMS.txt has no entries')
    for name, digest in listed.items():
        path = plugin_dir / name
        if path.is_symlink() or not path.is_file():
            errors.append(f'listed plugin missing, not a regular file, or a symlink: {path}')
            continue
        try:
            actual = sha256(path)
            if actual != digest:
                errors.append(f'hash mismatch for {name}: expected {digest[:12]}…, actual {actual[:12]}…')
        except OSError as error:
            errors.append(f'cannot read plugin {path}: {error}')
    try:
        for path in sorted(plugin_dir.iterdir()):
            if path.name != 'SHA256SUMS.txt' and path.name not in listed:
                errors.append(f'unlisted card lineup entry: {path.name}')
    except OSError as error:
        errors.append(f'cannot list card lineup plugins {plugin_dir}: {error}')
    assets = []
    manifest = root / 'CARD-SHA256SUMS.txt'
    full_layout = (root / 'plugins').is_dir() or manifest.exists()
    if full_layout:
        if manifest.is_symlink() or not manifest.is_file():
            errors.append(f'CARD-SHA256SUMS.txt missing, not a regular file, or a symlink: {manifest}')
        else:
            asset_list = {}
            try:
                for number, line in enumerate(manifest.read_text(encoding='utf-8').splitlines(), 1):
                    if not line.strip():
                        continue
                    match = re.fullmatch(r'([0-9a-f]{64}) {1,2}\*?(.+)', line)
                    if not match:
                        errors.append(f'malformed CARD-SHA256SUMS.txt line {number}: {line!r}')
                        continue
                    digest, name = match.groups()
                    path = Path(name)
                    if (path.is_absolute() or path.as_posix() != name or '..' in path.parts
                            or not (name.startswith('ultimate/mods/')
                                    or name.startswith('ultimate/arcropolis/') and name.endswith('.toml'))):
                        errors.append(f'invalid card asset path on line {number}: {name!r}')
                        continue
                    if name in asset_list:
                        errors.append(f'duplicate card asset path: {name}')
                        continue
                    asset_list[name] = digest
                    source = root / path
                    if source.is_symlink() or not source.is_file():
                        errors.append(f'card asset missing, not a regular file, or a symlink: {source}')
                        continue
                    actual = sha256(source)
                    if actual != digest:
                        errors.append(f'hash mismatch for card asset {name}: expected {digest[:12]}…, actual {actual[:12]}…')
                    assets.append({'path': name, 'sha256': digest, 'size': source.stat().st_size})
            except (OSError, UnicodeError) as error:
                errors.append(f'cannot read {manifest}: {error}')
            actual_assets = set()
            ultimate = root / 'ultimate'
            if ultimate.is_symlink() or not ultimate.is_dir():
                errors.append(f'full card lineup needs an ultimate directory: {ultimate}')
            else:
                unexpected = sorted(path.name for path in ultimate.iterdir()
                                    if path.name not in ('mods', 'arcropolis'))
                if unexpected:
                    errors.append(f'unexpected card lineup ultimate entries: {unexpected}')
            for base, suffix in ((root / 'ultimate/mods', None), (root / 'ultimate/arcropolis', '.toml')):
                if not base.exists():
                    continue
                if base.is_symlink() or not base.is_dir():
                    errors.append(f'card asset directory is not a regular directory: {base}')
                    continue
                for path in base.rglob('*'):
                    if path.is_symlink():
                        errors.append(f'symlink in card assets: {path}')
                    elif path.is_file():
                        if suffix is None or path.name.endswith(suffix):
                            actual_assets.add(path.relative_to(root).as_posix())
                        else:
                            errors.append(f'non-TOML ARCropolis file in card lineup: {path}')
            if actual_assets != set(asset_list):
                errors.append('CARD-SHA256SUMS.txt does not exactly cover the mods and ARCropolis TOML files '
                              f'(unlisted: {sorted(actual_assets - set(asset_list))}; missing: '
                              f'{sorted(set(asset_list) - actual_assets)})')
        allowed_root = {'plugins', 'ultimate', 'CARD-SHA256SUMS.txt'}
        for path in root.iterdir():
            if path.name not in allowed_root:
                errors.append(f'unexpected card lineup root entry: {path.name}')
    if errors:
        raise SystemExit('invalid card lineup:\n' + '\n'.join(errors))
    stamp = root.name if re.fullmatch(r'\d{8}-\d{4}Z', root.name) else None
    plugin_bytes = sum((plugin_dir / name).stat().st_size for name in listed)
    asset_bytes = sum(asset['size'] for asset in assets)
    return {'dir': str(root), 'stamp': stamp, 'sums_sha256': sums_digest,
            'plugins': [{'name': name, 'sha256': listed[name]} for name in sorted(listed)],
            'assets': sorted(assets, key=lambda item: item['path']),
            'mods_files': sum(asset['path'].startswith('ultimate/mods/') for asset in assets),
            'arcropolis_config_files': sum(asset['path'].startswith('ultimate/arcropolis/') for asset in assets),
            'plugin_bytes': plugin_bytes, 'asset_bytes': asset_bytes,
            'payload_bytes': plugin_bytes + asset_bytes}


def _go_card_lineup(args):
    reason = getattr(args, 'no_card_lineup', None)
    if reason is not None:
        if not reason.strip() or any(char in reason for char in "'\r\n"):
            raise SystemExit(f'--no-card-lineup needs a reason on one line without apostrophes; got: {reason!r}')
        return None
    folder = Path(args.card_lineup).resolve() if getattr(args, 'card_lineup', None) else default_card_lineup().resolve()
    if any(char in str(folder) for char in "'\r\n"):
        raise SystemExit(f'card lineup path {folder}: use a folder without apostrophes or line breaks')
    return read_card_lineup(folder)


def _link_card_input(source, target, digest, link_source=True):
    """Use a content-addressed, host-local hard link so identical lineups share storage across runs."""
    source, target = Path(source), Path(target)
    cache = RUNS / '_eden-test' / 'card-lineup-cache' / digest
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        if cache.is_symlink() or not cache.is_file() or sha256(cache) != digest:
            raise SystemExit(f'card lineup cache entry is not the pinned file: {cache}')
    else:
        writable_source = source.stat().st_mode & 0o222
        if link_source and not writable_source:
            try:
                os.link(source, cache)
            except FileExistsError:
                pass
            except OSError as error:
                raise SystemExit(f'cannot hard-link card lineup input into host cache {cache}: {error}') from error
        else:
            temporary = cache.parent / f'.{digest}.{uuid.uuid4().hex}.tmp'
            try:
                shutil.copyfile(source, temporary)
                temporary.chmod(temporary.stat().st_mode & ~0o222)
                try:
                    os.link(temporary, cache)
                except FileExistsError:
                    pass
            except OSError as error:
                raise SystemExit(f'cannot cache card lineup input {source} at {cache}: {error}') from error
            finally:
                temporary.unlink(missing_ok=True)
        if sha256(cache) != digest:
            raise SystemExit(f'card lineup changed while caching: {source}')
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(cache, target)
    except OSError as error:
        raise SystemExit(f'cannot hard-link cached card lineup input into run {target}: {error}') from error
    actual = sha256(target)
    if actual != digest:
        raise SystemExit(f'card lineup hard link changed: {target} is {actual[:12]}…, expected {digest[:12]}…')


def _discard_remote_lineup_stage(args):
    """Drop this run's transfer copy after go_new linked all files into the persistent host cache."""
    if not getattr(args, 'card_lineup_origin', None) or not getattr(args, 'card_lineup', None):
        return
    staged = RUNS / '_eden-test' / 'stage' / args.label / 'card-lineup'
    supplied = Path(args.card_lineup).resolve()
    if supplied == staged.resolve() and staged.is_dir() and not staged.is_symlink():
        for path in staged.rglob('*'):
            if path.is_file() and not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o200)
        shutil.rmtree(staged)


def host_interface_problem(name):
    if host_os.WINDOWS:
        if not isinstance(name, str) or not name or '\0' in name:
            return 'no such interface'
        interface = next((item for item in host_os.windows_interfaces() if item['name'] == name), None)
        if interface is None:
            return 'no such interface'
        if interface['loopback']:
            return 'is loopback'
        if not interface['up']:
            return 'is down'
        if not interface['ipv4']:
            return 'has no IPv4 address'
        return None
    if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\0' in name:
        return 'no such interface'
    encoded = os.fsencode(name)
    if len(encoded) > 15:
        return 'no such interface'
    interface = NET_SYSFS / name
    if not interface.exists():
        return 'no such interface'
    try:
        flags = int((interface / 'flags').read_text().strip(), 0)
    except (OSError, ValueError):
        return 'cannot read interface flags'
    if flags & 0x8:
        return 'is loopback'
    if not flags & 0x1:
        return 'is down'
    if fcntl is None:
        return 'IPv4 interface check unavailable on this host'
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            fcntl.ioctl(probe.fileno(), 0x8915, struct.pack('256s', encoded))
    except OSError:
        return 'has no IPv4 address'
    return None


def usable_interfaces():
    if host_os.WINDOWS:
        return sorted(item['name'] for item in host_os.windows_interfaces()
                      if item['up'] and not item['loopback'] and item['ipv4'])
    try:
        names = sorted(item.name for item in NET_SYSFS.iterdir())
    except OSError:
        return []
    return [name for name in names if host_interface_problem(name) is None]


def free_display(preferred, taken=()):
    if not isinstance(preferred, str) or not re.fullmatch(r':\d{1,3}', preferred):
        raise SystemExit(f'invalid base X display {preferred!r}; expected :<0..999>')
    for number in range(int(preferred[1:]), int(preferred[1:]) + 40):
        display = f':{number}'
        socket_path = X11_SOCKET_DIR / f'X{number}'
        if number <= 999 and number != 61 and display not in taken \
                and not socket_path.exists() and not socket_path.is_symlink():
            return display
    raise SystemExit(f'no free X display from {preferred} upward (40 tried)')


def network_row_overrides(rows, interface):
    path = Path(rows)
    plan = json.loads((path / 'rows.json' if path.is_dir() else path).read_text())
    conflicts = []

    def walk(steps, row, prefix=''):
        if not isinstance(steps, list):
            return
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                continue
            position = f'{prefix}{index}' if prefix else index
            run = step.get('run')
            args = step.get('args')
            if isinstance(args, dict):
                if run == 'routes:_setting' and args.get('key') == 'core.network_interface':
                    value = args.get('value')
                elif run == 'routes:host_network' and 'interface' in args:
                    value = args['interface']
                else:
                    value = interface
                if value != interface:
                    conflicts.append({'row': row, 'step': position, 'run': run, 'value': value})
            gif = step.get('gif')
            if isinstance(gif, dict):
                walk(gif.get('steps'), row, f'{position} gif step ')

    for row in plan.get('rows', []):
        if not isinstance(row, dict):
            continue
        row_id = row.get('id', '<unnamed>')
        walk(row.get('steps'), row_id)
        walk(row.get('recover'), row_id, 'recover ')
    walk(plan.get('recover'), '<plan recover>')
    return conflicts


def go_new(args):
    staged_targets = getattr(args, 'staged_target', None) or []
    try:
        for commit in staged_targets:
            _target_commit(commit)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    base = Path(args.from_config) if args.from_config else RUNS / '_eden-test' / 'base.json'
    config = copy.deepcopy(json.loads(base.read_text()))
    settings = getattr(args, 'eden_setting', None) or []
    if settings:
        config.setdefault('eden_settings', {}).update(dict(settings))
    config.pop('staged_targets', None)
    if staged_targets:
        config['staged_targets'] = sorted(set(staged_targets))
    if config.get('host') != args.host:
        raise SystemExit(f'base {base} is for host {config.get("host")!r}, not {args.host!r}')
    lineup = _go_card_lineup(args)
    _go_base_trace(config, args)
    module = _go_trace(args)
    interface = args.network_interface or config.get('network_interface')
    problem = host_interface_problem(interface) if interface else 'the base names none'
    if problem and not args.no_lobby:
        description = f'{json.dumps(interface)} {problem}' if interface else problem
        usable = ', '.join(usable_interfaces()) or 'none'
        raise SystemExit(f'lobby rows need a usable host network interface: {description}; usable here: {usable}. '
                         f'Set "network_interface" in {base} or pass --network-interface; '
                         'pass --no-lobby only for a run without lobby rows')
    config['network_interface'] = interface
    config['network_check'] = {'interface': interface, 'usable': problem is None,
                               'problem': problem, 'no_lobby': bool(args.no_lobby)}
    overrides = network_row_overrides(args.rows, interface)
    config['network_check']['row_overrides'] = overrides
    if overrides and not args.no_lobby:
        messages = [
            f'rows set core.network_interface to {json.dumps(item["value"])} in row {item["row"]} '
            f'step {item["step"]} ({item["run"]}), but this run\'s checked interface is {json.dumps(interface)}: '
            'remove that step and use {"run": "routes:host_network"} '
            '(or pass --network-interface <value> if that interface is meant; "lo" is never usable: '
            'Eden does not list loopback)'
            for item in overrides
        ]
        raise SystemExit('\n'.join(messages))
    choices = []
    for item in config['instances']:
        if 'display' in item:
            original = item['display']
            chosen = original
            reason = 'allocated under the host claim lock by eden-lifecycle'
            choices.append({'key': item['key'], 'base': original, 'chosen': chosen, 'reason': reason})
            item['display'] = chosen
    config['display_choice'] = choices
    run = RUNS / args.label
    if run.exists():
        raise SystemExit(f'{run} exists; pick a new label')
    config.update(label=args.label, owner=args.owner, ports_root=str(RUNS),
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
    cards = lineup['plugins'] if lineup else []
    if lineup:
        card_inputs = inputs / 'card-lineup'
        card_inputs.mkdir()
        plugin_dir = Path(lineup['dir']) / 'plugins'
        if not plugin_dir.is_dir():
            plugin_dir = Path(lineup['dir'])
        link_source = not getattr(args, 'card_lineup_origin', None)
        _link_card_input(plugin_dir / 'SHA256SUMS.txt', card_inputs / 'SHA256SUMS.txt',
                         lineup['sums_sha256'], link_source)
        for pin in cards:
            _link_card_input(plugin_dir / pin['name'], card_inputs / pin['name'], pin['sha256'], link_source)
        for asset in lineup['assets']:
            source = Path(lineup['dir']) / asset['path']
            _link_card_input(source, card_inputs / asset['path'], asset['sha256'], link_source)
        config['card_lineup'] = {'staged': True, 'dir': lineup['dir'], 'stamp': lineup['stamp'],
                                 'origin': getattr(args, 'card_lineup_origin', None) or lineup['dir'],
                                 'sums_sha256': lineup['sums_sha256'], 'files': len(cards),
                                 'mods_files': lineup['mods_files'],
                                 'arcropolis_config_files': lineup['arcropolis_config_files'],
                                 'asset_bytes': lineup['asset_bytes'], 'plugin_bytes': lineup['plugin_bytes'],
                                 'payload_bytes': lineup['payload_bytes']}
        config['superseded_base_pins'] = []
    else:
        config['card_lineup'] = {'staged': False, 'reason': args.no_card_lineup}
        config.pop('superseded_base_pins', None)
    config['staged_plugins'] = {}
    card_by_name = {pin['name']: pin for pin in cards}
    for item in config['instances']:
        pins = [{'name': pin['name'], 'path': str(inputs / 'card-lineup' / pin['name']), 'sha256': pin['sha256']}
                for pin in cards]
        staged = [{'name': pin['name'], 'sha256': pin['sha256'], 'source': 'card'} for pin in cards]
        for pin in item.get('plugins', []):
            name = pin.get('name', Path(pin['path']).name)
            if name in card_by_name:
                config['superseded_base_pins'].append({
                    'instance': item['key'], 'name': name, 'base_path': pin['path'],
                    'base_sha256': pin['sha256'], 'card_sha256': card_by_name[name]['sha256']})
            else:
                pins.append(pin)
                staged.append({'name': name, 'sha256': pin['sha256'], 'source': 'base'})
        item['plugins'] = pins
        for asset in lineup['assets'] if lineup else []:
            item.setdefault('files', []).append({'path': str(inputs / 'card-lineup' / asset['path']),
                                                 'to': 'user/sdmc/' + asset['path'],
                                                 'sha256': asset['sha256']})
        config['staged_plugins'][item['key']] = staged
    swapped = []
    for nro in args.nro:
        original = Path(nro).resolve()
        target = inputs / original.name
        shutil.copy2(original, target)
        digest = sha256(target)
        for item in config['instances']:
            pins = item.setdefault('plugins', [])
            staged = config['staged_plugins'][item['key']]
            hits = [index for index, pin in enumerate(pins)
                    if pin.get('name', Path(pin['path']).name) == original.name]
            for index in hits:
                pin = pins[index]
                swapped.append({'from': pin['path'], 'from_sha256': pin['sha256'], 'to': str(target)})
                replaces = {'source': staged[index]['source'], 'sha256': pin['sha256']}
                pin.update(path=str(target), sha256=digest)
                staged[index] = {'name': original.name, 'sha256': digest, 'source': 'nro', 'replaces': replaces}
            if not hits:
                pins.append({'name': original.name, 'path': str(target), 'sha256': digest})
                staged.append({'name': original.name, 'sha256': digest, 'source': 'nro'})
    if swapped:
        config['plugin_swaps'] = swapped
    if lineup:
        card_plugin_bytes = 0
        for item in config['instances']:
            plugin_pins = {pin.get('name', Path(pin['path']).name): pin for pin in item.get('plugins', [])}
            for entry in config['staged_plugins'][item['key']]:
                replaced = entry.get('replaces')
                if entry['source'] == 'card' or replaced and replaced.get('source') == 'card':
                    card_plugin_bytes += Path(plugin_pins[entry['name']]['path']).stat().st_size
        config['card_lineup']['profile_copy_bytes'] = (
            card_plugin_bytes + lineup['asset_bytes'] * len(config['instances']))
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
        port_path = inputs / 'port.txt'
        port_path.write_text('0\n')
        digest = sha256(port_path)
        for pin in port_pins:
            pin.update(path=str(port_path), sha256=digest)
        config['bridge_port'] = 'allocated atomically at lifecycle claim'
    return _finish_new(run, config, Path(args.rows))


def _python():
    return [sys.executable, '-B', '-X', 'utf8'] if host_os.WINDOWS else [sys.executable, '-B', '-I']


def _relay(request):
    if os.environ.get('EDEN_TEST_RELAY') != '1':
        raise SystemExit('on a Windows host the lifecycle and the ledger are served by the Linux controller: '
                         'start this through `eden_test.py go` or `eden_test.py remote` on the controller')
    request = {**request, 'id': uuid.uuid4().hex}
    print(RELAY_PREFIX + json.dumps(request), flush=True)
    try:
        reply = json.loads(sys.stdin.readline())
        if not isinstance(reply, dict) or reply.get('id') != request['id']:
            raise ValueError('invalid id')
    except (ValueError, TypeError):
        raise SystemExit('relay: invalid reply') from None
    return reply


def lifecycle(run, mode, *extra):
    config = json.loads((run / 'config.json').read_text())
    if config.get('host') not in ('surface', 'yoga', 'xps', 'ally'):
        raise SystemExit('config host must be surface, yoga, xps or ally')
    if host_os.WINDOWS:
        evidence = extra[extra.index('--evidence-dir') + 1] if '--evidence-dir' in extra else None
        reply = _relay({'kind': 'lifecycle', 'mode': mode, 'config': config, 'keep_state': '--keep-state' in extra,
                        'evidence_dir': evidence})
        text = json.dumps(reply, indent=2)
        with (run / f'lifecycle-{mode}.log').open('a', encoding='utf-8') as log:
            log.write(f'$ relay lifecycle {mode}\n{text}\n')
        print(text)
        ok = reply.get('ok') and (mode != 'preflight' or (reply.get('result') or {}).get('ok'))
        return _ExitCode(0 if ok else 1, reply.get('error') or '')
    argv = [*_python(), str(LIFECYCLE), mode, str(run / 'config.json'), '--execute', *extra]
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
    target = (spec.get('target') or {}).get('plugin') or {}
    sha256, commit = target.get('nro_sha256'), target.get('commit', '')
    if sha256:
        matches = [pin for instance in config.get('instances', []) for pin in instance.get('plugins', [])
                   if pin.get('sha256') == sha256]
        if not matches:
            problems.append(f'target NRO {sha256[:12]}… is not staged in this run')
        for pin in matches:
            path = Path(pin['path'])
            try:
                if commit[:8].encode('ascii') not in path.read_bytes():
                    problems.append(f'target NRO {path.name} does not carry commit {commit[:8]}')
            except (OSError, UnicodeError) as error:
                problems.append(f'target NRO {path.name} cannot be read: {error}')
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
    if sh([*_python(), str(RUNNER), 'up-check', str(rows), '--out', str(run / 'out')],
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
    code = sh([*_python(), str(RUNNER), 'up-check', str(run / 'rows' / 'rows.json'),
               '--out', str(run / 'out')], run / 'dry-run.log')
    if code:
        return code
    stop = run / 'out' / 'STOP-INPUT'
    if (retry or rows) and stop.exists():
        stop.rename(run / 'out' / f'STOP-INPUT.{int(time.time())}')
    if rows:
        set_aside(run, rows.split(','))
    stamp(run, 'run_start')
    code = sh([*_python(), str(RUNNER), 'run', str(run / 'rows' / 'rows.json'),
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
        if host_os.WINDOWS:
            key = item.get('key')
            try:
                record = json.loads((run / 'state' / f'run-{key}' / 'run.json').read_text()).get('owned_pids_stopped')
            except (OSError, ValueError):
                record = None
            if not isinstance(record, dict) or 'job_terminated' not in record:
                results.append(f'{key}: no stop record')
            elif record['job_terminated']:
                results.append(f'{key}: Windows Job terminated by Down')
            else:
                results.append(f'{key}: the Job was already gone at Down')
            continue
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
    reported = report_rows_with_stalls(run)
    stalled = [(row, result, stalls) for row, result, _, _, stalls in reported
               if result == 'PASS' and stalls]
    stall_suffix = _stall_totals(reported)
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
             f'- Rows: {len(passed)} PASS, {len(failed)} FAILED, {len(unrun)} unrun{stall_suffix}; {retained} earlier failed attempts retained '
             f'(receipts in `out/`); final rows.json {rows_note}; {len(manifests)} runner manifests in `out/manifests/`']
    for row, result, stalls in stalled[:6]:
        lines.append(f'  - {shown_result(result, stalls)} {row["id"]}')
    if len(stalled) > 6:
        lines.append(f'  - and {len(stalled) - 6} more rows passed with stalls (see REPORT.md)')
    for row, error in failed[:6]:
        lines.append(f'  - FAILED {row}: {one_line(error)}')
    if unrun:
        lines.append(f'  - unrun: {one_line(", ".join(unrun), 160)}')
    pending = ', release pending' if journal.get('release_pending') else ''
    lines += [one_line(f'- Down: cleanup_failures={journal.get("cleanup_failures")}, released={journal.get("released")}{pending}; '
                       + '; '.join(unit_results(run)), 240),
              one_line(f'- Prune: {prune_summary(run, journal)}', 240),
              *([_cleanup_line(run)] if (run / 'out' / CLEANUP_RECEIPT).is_file() else []),  # written by down
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
    (run / 'RESULTS.md').write_text('\n'.join(lines) + '\n')
    print('\n'.join(lines))


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
        sources = {pin['name']: pin for pin in config.get('staged_plugins', {}).get(item.get('key'), [])}

        def suffix(name):
            entry = sources.get(name)
            if not entry:
                return ''
            if entry['source'] == 'card':
                return '; from the card lineup'
            if entry['source'] == 'base':
                return ('; from the base (not on the card)' if config.get('card_lineup', {}).get('staged')
                        else '; from the base')
            replaced = entry.get('replaces')
            if replaced:
                kind = 'card file' if replaced['source'] == 'card' else 'base pin'
                return f'; --nro, replaces the {kind} {replaced["sha256"]}'
            return '; --nro, added (no card or base file of this name)'

        profile = (instances.get(item.get('key')) or {}).get('profile')
        folder = Path(profile) / PLUGINS if profile else None
        if folder is None or not folder.is_dir():
            lines += [f'  - {name} `{digest}` as pinned in config.json (the profile is gone or not on this machine, '
                      f'so the loaded file was not re-hashed){suffix(name)}' for name, digest in pins.items()]
            continue
        for path in sorted(folder.glob('*.nro')):
            digest = sha256(path)
            verdict = ('NOT in the config' if path.name not in pins else 'matches the config pin'
                       if pins[path.name] == digest else f'DIFFERS from the config pin {pins[path.name]}')
            lines.append(f'  - {path.name} `{digest}` in the profile; {verdict}{suffix(path.name)}')
        lines += [f'  - {name}: pinned `{digest}` but MISSING from the profile{suffix(name)}' for name, digest in pins.items()
                  if not (folder / name).exists()]
    return lines


def card_lineup_report(config):
    lineup = config.get('card_lineup')
    if lineup is None:
        return '- Card lineup: not recorded (run made before the card-lineup default)'
    if not lineup['staged']:
        return f'- Card lineup: NOT STAGED — reason: {lineup["reason"]}'
    replacements, additions = [], []
    for entries in config.get('staged_plugins', {}).values():
        for entry in entries:
            replaced = entry.get('replaces')
            if replaced:
                text = (f'{entry["name"]} ({replaced["source"]} {replaced["sha256"][:12]}…'
                        f' → {entry["sha256"][:12]}…)')
                if text not in replacements:
                    replacements.append(text)
            elif entry['source'] in ('base', 'nro'):
                kind = 'base' if entry['source'] == 'base' else '--nro'
                text = f'{entry["name"]} ({kind})'
                if text not in additions:
                    additions.append(text)
    stamp = f', stamp {lineup["stamp"]}' if lineup.get('stamp') else ''
    assets = (f', {lineup["mods_files"]} mod files and '
              f'{lineup["arcropolis_config_files"]} ARCropolis configs '
              f'({lineup["asset_bytes"]} bytes); hard-linked staging, '
              f'{lineup["profile_copy_bytes"]} card payload bytes copied to private SD profiles'
              if 'profile_copy_bytes' in lineup else '; payload size not recorded by this older run')
    return (f'- Card lineup: {lineup["origin"]}{stamp} (SHA256SUMS {lineup["sums_sha256"][:12]}…, '
            f'{lineup["files"]} plugins verified{assets}); replaced by --nro: {", ".join(replacements) or "none"}; '
            f'added: {", ".join(additions) or "none"}')


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
        files = sorted(path for path in folder.rglob('*')
                       if path.is_file() and not path.name.startswith('.gate-candidate-')) if folder.is_dir() else []
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
        files += [path for path in sorted((out / row_id).rglob('*'))
                  if path.suffix.lower() in MEDIA and not path.name.startswith('.gate-candidate-')]
    for path in dict.fromkeys(files):
        shown = f'out/{path.relative_to(out)}' if path.is_relative_to(out) else str(path)
        proof.append(f'{shown} ({path.stat().st_size} bytes)' if path.is_file() else f'{shown} (missing here)')
    return result, reason, proof + [f'out/{name}' for name in asides]


class _RowStalls(list):
    def __init__(self):
        super().__init__()
        self.unreadable = 0


def row_stalls(run, row_id, receipt) -> list[dict]:
    stalls = _RowStalls()
    seen = {}

    def add(event):
        if not isinstance(event, dict):
            return
        identity = json.dumps({key: value for key, value in event.items() if key != 'block'},
                              sort_keys=True, default=str)
        if identity not in seen:
            seen[identity] = len(stalls)
            stalls.append(dict(event))
        elif 'block' in event:
            stalls[seen[identity]].setdefault('block', event['block'])

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == 'stalls' and isinstance(child, list):
                    for event in child:
                        add(event)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for step in receipt.get('results', []):
        if isinstance(step, dict):
            walk(step.get('value'))
    path = Path(run) / 'out' / row_id / 'stall-events.jsonl'
    if path.exists():
        try:
            with path.open(encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        stalls.unreadable += 1
                        continue
                    if isinstance(event, dict):
                        add(event)
                    else:
                        stalls.unreadable += 1
        except OSError:
            stalls.unreadable += 1
    return stalls


def shown_result(result, stalls):
    if result != 'PASS' or not stalls:
        return result
    count = len(stalls)
    return f'PASS with {count} {"stall" if count == 1 else "stalls"}'


def report_rows_with_stalls(run):
    run = Path(run)
    rows = []
    for row, result, reason, proof in report_rows(run):
        path = run / 'out' / f'{row["id"]}.json'
        receipt = json.loads(path.read_text()) if path.exists() else {}
        rows.append((row, result, reason, proof, row_stalls(run, row['id'], receipt)))
    return rows


def _stall_totals(rows):
    passed_stalls = [stalls for _, result, _, _, stalls in rows if result == 'PASS' and stalls]
    if not passed_stalls:
        return ''
    passed = len(passed_stalls)
    total = sum(len(stalls) for stalls in passed_stalls)
    return (f' ({passed} {"row" if passed == 1 else "rows"} passed with stalls, '
            f'{total} {"stall" if total == 1 else "stalls"} in those rows)')


def _stall_text(event):
    fields = ('block', 'kind', 'step', 'screen', 'send')
    parts = [f'{key}={event[key]}' for key in fields if key in event]
    return ', '.join(parts) if parts else json.dumps(event, sort_keys=True, default=str)


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
    rows = report_rows_with_stalls(run)
    timing = json.loads((run / 'timing.json').read_text()) if (run / 'timing.json').exists() else {}

    def utc(key):
        return time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(timing[key])) if key in timing else '?'

    host, me = config.get('host', '?'), f'/usr/bin/python3 -I -B {Path(__file__).resolve()}'
    if host in WINDOWS_HOSTS:
        me = f'eden_test.py remote --host {host} --label {config.get("label", run.name)}'
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
    for row, result, reason, proof, stalls in rows:
        counts[result] += 1
        blocks = ', '.join(str(step.get('run', next(iter(step), '?'))).split(':')[-1] for step in row.get('steps', []))
        details = []
        if stalls:
            text = f'stalls: {len(stalls)}: ' + '; '.join(_stall_text(event) for event in stalls)
            details.append(cell(text, len(text)))
        if stalls.unreadable:
            details.append(f'unreadable stall-event lines: {stalls.unreadable}')
        details.append(cell(reason, 420))
        table.append(f'| {row["id"]} | {cell(blocks, 60)} | {shown_result(result, stalls)} | '
                     f'{"; ".join(details)} | {"<br>".join(proof)} |')
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
             card_lineup_report(config),
             '- NROs:', *nro_lines(run, config),
             f'- Rows: {counts["PASS"]} PASS, {counts["FAIL"]} FAIL, {counts["NOT RUN"]} NOT RUN of {len(rows)}'
             f'{_stall_totals(rows)} (proof paths are relative to `{run}`)',
             '', '| Row | Steps | Result | Reason | Proof |', '| --- | --- | --- | --- | --- |', *table, '',
             f'- Times: Up {utc("up_start")} to {utc("up_done")}; rows {utc("run_start")} to {utc("run_end")} over '
             f'{len(timing.get("runs", [])) or 1} runner call(s); setup rows {split["setup"]:.0f} s, test rows '
             f'{split["test"]:.0f} s']
    if 'down_done' not in timing:
        suffix = '' if host in WINDOWS_HOSTS else f' {run}'
        lines += [f'- Re-run rows in this boot: `{me} run{suffix} --rows <id>[,<id>]` then `{me} report{suffix}`',
                  f'- Down: `{me} down{suffix}`']
    return '\n'.join(lines) + '\n'


def cmd_report(run):
    text = report_md(run)
    (run / 'REPORT.md').write_text(text)
    print(text, end='')
    return 0


def _go_sh(argv):
    print('$ ' + shlex.join(str(arg) for arg in argv), flush=True)
    return sh(argv)


def _go_diagnosis(mux, kind, code, what, *, timed_out=False):
    diagnosis = _lifecycle_module.ssh_transport_failure(
        mux, kind, code, getattr(code, 'stderr', ''), timed_out=timed_out)
    if diagnosis:
        diagnosis = _lifecycle_module.ssh_record(diagnosis, kind, what, getattr(mux, 'receipt_dir', None))
    return diagnosis


def _go_suffix(diagnosis):
    return '; ' + _lifecycle_module.ssh_diagnosis_with_receipt(diagnosis) if diagnosis else ''


def _pwsh_arg(value):
    value = str(value)
    if re.fullmatch(r'[A-Za-z0-9_./:@!=+-]+', value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _remote_kind(host_name):
    if host_name in WINDOWS_HOSTS:
        return {'windows': True, 'root': 'C:/home/intpa/eden-runs',
                'python': [r'C:\Program Files\Python312\python.exe', '-u', '-X', 'utf8', '-B']}
    return {'windows': False, 'root': '/home/intpa/eden-runs', 'python': ['/usr/bin/python3', '-I', '-B']}


def _windows_ssh(host, mux, script):
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    word = f'"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}'
    if len(word) > 7800:
        raise SystemExit(f'remote command too long for the Windows host: {len(word)} characters')
    return ['ssh', '-T', *mux.rider_args, host, word]


def _remote_command(host, mux, remote, windows):
    if windows:
        script = "$env:EDEN_TEST_RELAY='1'; & " + ' '.join(_pwsh_arg(x) for x in remote) + '; exit $LASTEXITCODE'
        return _windows_ssh(host, mux, script)
    return ['ssh', *mux.rider_args, host, *(_pwsh_arg(x) for x in remote)]


def _stage_tar(args, lineup, path):
    with tarfile.open(path, 'w:gz') as archive:
        archive.add(PLAYBOOK / 'tools', arcname='tools')
        if lineup:
            archive.add(lineup['dir'], arcname='card-lineup')
        for nro in args.nro:
            archive.add(nro, arcname=f'nro/{Path(nro).name}')
        if args.elf:
            archive.add(args.elf, arcname=f'elf/{Path(args.elf).name}')
        rows = Path(args.rows)
        archive.add(rows, arcname='rows' if rows.is_dir() else 'rows/rows.json')


def _relay_claims(config):
    info = _lifecycle_module.Ledger(config['ledger'], config['host'], config['owner'], '0' * 32,
                                    config.get('ledger_ssh'), state_dir=None).inspect()
    lines = info.get('open') if isinstance(info, dict) else None
    if not isinstance(lines, list):
        raise ValueError('the ledger read returned no claim list')
    return [str(line) for line in lines]


def _relay_run(command, host, args, popen=subprocess.Popen, forward=None, claims=None):
    process = popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace')
    for line in process.stdout:
        if not line.startswith(RELAY_PREFIX):
            sys.stdout.write(line)
            sys.stdout.flush()
            continue
        request = {}
        try:
            request = json.loads(line[len(RELAY_PREFIX):])
            if not isinstance(request, dict):
                raise ValueError('relay: invalid request')
            config = request.get('config')
            if not isinstance(config, dict) or config.get('host') != args.host or config.get('label') != args.label:
                raise ValueError('relay: foreign run')
            kind = request.get('kind')
            if kind == 'lifecycle':
                mode = request.get('mode')
                if mode not in ('up', 'down', 'preflight', 'status'):
                    raise ValueError('relay: invalid lifecycle mode')
                result = (forward or _lifecycle_module.forward_windows)(
                    host, mode, config, mode not in ('status', 'preflight'),
                    keep_state=bool(request.get('keep_state')), evidence_dir=request.get('evidence_dir'))
            elif kind == 'claims':
                result = (claims or _relay_claims)(config)
            else:
                raise ValueError('relay: unknown request')
            reply = {'id': request.get('id'), 'ok': True, 'result': result}
        except Exception as exc:
            reply = {'id': request.get('id') if isinstance(request, dict) else None, 'ok': False, 'error': str(exc)}
        if not isinstance(request, dict):
            request = {}
        operation = ('lifecycle ' + str(request.get('mode')) if request.get('kind') == 'lifecycle'
                     else str(request.get('kind')))
        print(f'relay: {operation} -> {"ok" if reply["ok"] else reply["error"]}', flush=True)
        process.stdin.write(json.dumps(reply) + '\n')
        process.stdin.flush()
    return _ExitCode(process.wait(), '')


def _go_targets(rows):
    rows = Path(rows)
    path = rows / 'rows.json' if rows.is_dir() else rows
    try:
        plan = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise SystemExit(f'cannot read rows plan {path}: {error}') from error
    if not isinstance(plan, dict):
        raise SystemExit(f'rows plan {path} must be an object')
    if 'target' not in plan:
        return []
    target = plan['target']
    if not isinstance(target, dict):
        raise SystemExit('target must be an object')
    repos = target.get('repos', {})
    if not isinstance(repos, dict):
        raise SystemExit('target repos must be an object')
    specs = [('plugin', target.get('plugin'))]
    if 'bridge' in target:
        specs.append(('bridge', target['bridge']))
    specs.extend((f'repos.{name}', spec) for name, spec in repos.items())
    found = []
    for label, spec in specs:
        if not isinstance(spec, dict):
            raise SystemExit(f'target {label} must be an object')
        tree, commit = spec.get('tree'), spec.get('commit')
        try:
            _target_commit(commit)
            if not isinstance(tree, str) or not Path(tree).is_absolute() or not Path(tree).is_dir():
                raise ValueError(f'tree {tree!r} is not an existing absolute directory')
            _target_git(tree, 'cat-file', '-e', f'{commit}^{{commit}}', timeout=20)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            raise SystemExit(f'target {label}: {error}') from error
        found.append({'label': label, 'tree': tree, 'commit': commit})
    return found


def _go_target_ssh(host, mux, remote, timeout, windows=False):
    mux.ensure()
    command = _remote_command(host, mux, remote, windows)
    print('$ ' + shlex.join(command), flush=True)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        diagnosis = _go_diagnosis(mux, 'ssh', None, 'target staging', timed_out=True)
        raise SystemExit(f'target staging SSH failed: {error}{_go_suffix(diagnosis)}') from error
    except OSError as error:
        raise SystemExit(f'target staging SSH failed: {error}') from error
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    code = _ExitCode(result.returncode, result.stderr or '')
    diagnosis = _go_diagnosis(mux, 'ssh', code, 'target probe' if '--probe' in remote else 'target staging')
    return _ExitCode(code, code.stderr, diagnosis), result.stdout, result.stderr


def _go_target_rsync(command):
    print('$ ' + shlex.join(command), flush=True)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired as error:
        return _ExitCode(124, str(error), timed_out=True), None
    except OSError as error:
        raise SystemExit(f'target pack rsync failed: {error}') from error
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    match = re.search(r'(?m)^Total bytes sent:\s*([0-9,]+)', result.stdout)
    return _ExitCode(result.returncode, result.stderr or ''), int(match.group(1).replace(',', '')) if match else None


_GO_BASE_NAME = r'[A-Za-z0-9][A-Za-z0-9_.-]*\.json'


def _go_this_host():
    """The host this machine is, by its shared base, or None when it has no shared base. A local copy of another
    host's base never answers this. A shared base that cannot say is a stop, not a guess."""
    shared = RUNS / '_eden-test' / 'base.json'
    if not shared.exists():
        return None
    try:
        config = json.loads(shared.read_text())
        host = config.get('host') if isinstance(config, dict) else None
        if not isinstance(host, str) or not host:
            raise ValueError('it names no host')
    except (OSError, ValueError) as error:
        raise SystemExit(f'this machine\'s shared base {shared} cannot say which host this is: {error}; '
                         'nothing was started') from error
    return host


def _go_route(args):
    """The ssh name this machine reaches args.host by: --ssh, else its hosts.json entry, else None. A hosts.json
    that exists and cannot be read is a stop: it must not look like a machine with no route."""
    if args.ssh:
        return args.ssh
    hosts = RUNS / '_eden-test' / 'hosts.json'
    if not hosts.exists():
        return None
    try:
        mapping = json.loads(hosts.read_text())
        if not isinstance(mapping, dict):
            raise ValueError('it is not an object of host names')
        route = mapping.get(args.host)
        if route is not None and (not isinstance(route, str) or not route):
            raise ValueError(f'its entry for {args.host!r} is not an ssh name')
    except (OSError, ValueError) as error:
        raise SystemExit(f'cannot find the ssh name for host {args.host!r}: {hosts} cannot be used: {error}; '
                         'nothing was started') from error
    return route


def _go_remote_base(args):
    """The --from of a go that is sent to another host: a path on that host, in this run's own stage folder. It is
    forwarded as given; a local file is never copied there."""
    value = getattr(args, 'from_config', None)
    if not value:
        return None
    stage = f'{_remote_kind(args.host)["root"]}/_eden-test/stage/{args.label}'
    name = value[len(stage) + 1:] if value.startswith(stage + '/') else ''
    if not re.fullmatch(_GO_BASE_NAME, name):
        raise SystemExit(
            f'--from {value!r}: a go sent to another host takes a base that is on the host, a .json file directly in '
            f'this run\'s own stage folder {stage}/ (a plain name of letters, digits, "_", "-" and "."). '
            'A local file is not copied there, and it does not make this machine the host')
    return value


def _go_received_base(args):
    """Check the base of a go a controller sent to this host (it carries --token). Such a go runs here or not at
    all: a missing or foreign base is refused here and is never sent on. --from must be a file in this run's own
    stage folder, reached without a symlink, for this host, and it must pin the Eden AppImage this host's shared
    base pins (go_new then checks that file's hash). The shared base is only read."""
    shared = RUNS / '_eden-test' / 'base.json'

    def load(path, what):
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f'{what} {path} is missing on this host or is not a plain file; nothing was started')
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise SystemExit(f'{what} {path} cannot be read: {error}; nothing was started') from error
        host = config.get('host') if isinstance(config, dict) else None
        if host != args.host or not isinstance(config.get('instances'), list):
            raise SystemExit(f'{what} {path} is for host {host!r}, not {args.host!r}; nothing was started')
        return config

    if not args.from_config:
        load(shared, 'the base')
        return
    stage = RUNS / '_eden-test' / 'stage' / args.label
    try:
        _target_label(args.label)
        stage_text = stage.as_posix()
        name = args.from_config[len(stage_text) + 1:] if args.from_config.startswith(f'{stage_text}/') else ''
        if not re.fullmatch(_GO_BASE_NAME, name):
            raise ValueError('not a .json file directly in that folder')
        if _owned_target_directory(RUNS, ('_eden-test', 'stage', args.label)) is None:
            raise ValueError('that folder does not exist')
    except ValueError as error:
        raise SystemExit(f'--from {args.from_config!r}: {error}; a go sent to a host takes its base from the run\'s '
                         f'own stage folder {stage}/; nothing was started') from error
    config = load(stage / name, '--from')
    released = {(item.get('app', {}).get('path'), item.get('app', {}).get('sha256'))
                for item in load(shared, "this host's shared base")['instances'] if isinstance(item, dict)}
    for item in config['instances']:
        app = item.get('app', {}) if isinstance(item, dict) else {}
        if (app.get('path'), app.get('sha256')) not in released:
            pinned = ', '.join(sorted(f'{path} ({str(digest)[:12]}…)' for path, digest in released))
            raise SystemExit(f'--from {stage / name}: it pins Eden AppImage {app.get("path")} '
                             f'({str(app.get("sha256"))[:12]}…), not the released AppImage this host\'s shared base '
                             f'{shared} pins: {pinned}; nothing was started')


def _go_remote(args, lineup):
    hosts = RUNS / '_eden-test' / 'hosts.json'
    host = _go_route(args)
    if not host:
        raise SystemExit(f'no SSH host for {args.host!r}; use --ssh or add it to {hosts}')
    if host.startswith('-'):
        raise SystemExit(f'invalid SSH host {host!r}')
    try:
        _target_label(args.label)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    _go_remote_base(args)
    targets = _go_targets(args.rows)
    options = ('-i', '/home/intpa/.ssh/id_rsa', '-o', 'IdentitiesOnly=yes') if args.host in WINDOWS_HOSTS else ()
    receipt_dir = RUNS / '_eden-test' / 'ssh-diag' / args.label
    with SshMux(host, ssh_options=options, windows=args.host in WINDOWS_HOSTS, evidence_dir=receipt_dir,
                receipt_dir=receipt_dir) as mux:
        return _go_remote_with_mux(args, lineup, host, mux, targets)


def _go_remote_with_mux(args, lineup, host, mux, targets=None, pack_dir=None):
    base = _go_remote_base(args)
    if targets is None:
        try:
            _target_label(args.label)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        targets = _go_targets(args.rows)
    if pack_dir is None:
        with tempfile.TemporaryDirectory(prefix='eden-target-packs-') as directory:
            return _go_remote_with_mux(args, lineup, host, mux, targets, directory)
    mux.receipt_dir = RUNS / '_eden-test' / 'ssh-diag' / args.label
    kind = _remote_kind(args.host)
    windows = kind['windows']
    stage = f'{kind["root"]}/_eden-test/stage/{args.label}'
    archive = None
    if windows:
        archive = Path(pack_dir) / 'stage.tar.gz'
        _stage_tar(args, lineup, archive)
        mkdir = f'New-Item -ItemType Directory -Force -Path {_pwsh_arg(stage)} | Out-Null'
        unpack = (f'tar.exe -xzf {_pwsh_arg(stage + "/stage.tar.gz")} -C {_pwsh_arg(stage)}; '
                  f'if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}; '
                  f'Remove-Item -LiteralPath {_pwsh_arg(stage + "/stage.tar.gz")}')
        commands = [_windows_ssh(host, mux, mkdir),
                    ['scp', *mux.rider_args, str(archive), f'{host}:{stage}/stage.tar.gz'],
                    _windows_ssh(host, mux, unpack)]
    else:
        commands = [['rsync', '-a', '--delete', '--mkpath', str(PLAYBOOK / 'tools') + '/', f'{host}:{stage}/tools/']]
        if lineup:
            commands.append(['rsync', '-a', '--delete', '--mkpath', lineup['dir'] + '/', f'{host}:{stage}/card-lineup/'])
        commands.extend([['rsync', '-a', '--mkpath', str(nro), f'{host}:{stage}/nro/'] for nro in args.nro])
        if args.elf:
            commands.append(['rsync', '-a', '--mkpath', str(args.elf), f'{host}:{stage}/elf/'])
        rows = Path(args.rows)
        if rows.is_dir():
            commands.append(['rsync', '-a', '--mkpath', str(rows) + '/', f'{host}:{stage}/rows/'])
        else:
            commands.append(['rsync', '-a', '--mkpath', str(rows), f'{host}:{stage}/rows/rows.json'])
    script = f'{stage}/tools/eden-test/eden_test.py'
    staged_commits = {}
    for index, command in enumerate(commands):
        mux.ensure()
        if not windows:
            command[1:1] = ['-e', shlex.join(['ssh', *mux.rider_args])]
        if code := _go_sh(command):
            diagnosis = _go_diagnosis(mux, ('scp' if command[0] == 'scp' else 'ssh') if windows else 'rsync',
                                      code, 'go staging')
            raise SystemExit(f'go staging failed ({code}): {shlex.join(command)}{_go_suffix(diagnosis)}')
        if index == (2 if windows else 0):
            if archive is not None:
                archive.unlink()
            for target in targets:
                commit, label = target['commit'], target['label']
                if commit in staged_commits:
                    print(f'target {label} {commit[:12]}: cache reused, 0 bytes shipped in 0.0 s')
                    continue
                started = time.monotonic()
                helper = [*kind['python'], script, 'stage-target',
                          '--commit', commit, '--label', args.label]
                code, output, stderr = (_go_target_ssh(host, mux, [*helper, '--probe'], 180, windows=True)
                                        if windows else _go_target_ssh(host, mux, [*helper, '--probe'], 180))
                detail = stderr.strip()[-2000:] or 'no stderr'
                if code:
                    raise SystemExit(f'target {label} cache probe failed: {detail}'
                                     f'{_go_suffix(getattr(code, "diagnosis", None))}')
                try:
                    probe = json.loads(output.strip().splitlines()[-1])
                    if not isinstance(probe, dict) or probe.get('commit') != commit \
                            or type(probe.get('cached')) is not bool:
                        raise ValueError('invalid probe receipt')
                except (ValueError, IndexError, TypeError) as error:
                    raise SystemExit(f'target {label} cache probe failed: {detail}') from error
                sent = 0
                pack_file = None
                try:
                    if not probe['cached']:
                        try:
                            pack = pack_target(target['tree'], commit, pack_dir)
                        except (ValueError, OSError, subprocess.SubprocessError) as error:
                            raise SystemExit(f'target {label}: {error}') from error
                        pack_file = Path(pack['pack_file'])
                        incoming = f'{stage}/targets/.incoming/{commit}.pack'
                        if windows:
                            folder = f'{stage}/targets/.incoming'
                            mkdir = _windows_ssh(host, mux,
                                                 f'New-Item -ItemType Directory -Force -Path {_pwsh_arg(folder)} | Out-Null')
                            if failure := _go_sh(mkdir):
                                diagnosis = _go_diagnosis(mux, 'ssh', failure, f'target {label} pack')
                                raise SystemExit(f'target {label} pack mkdir failed ({failure}){_go_suffix(diagnosis)}')
                            transfer = ['scp', *mux.rider_args, str(pack_file), f'{host}:{incoming}']
                            mux.ensure()
                            failure, sent = _go_sh(transfer), pack_file.stat().st_size
                        else:
                            transfer = ['rsync', '-e', shlex.join(['ssh', *mux.rider_args]), '-a', '--partial', '--stats',
                                        '--mkpath', str(pack_file), f'{host}:{incoming}']
                            mux.ensure()
                            failure, sent = _go_target_rsync(transfer)
                        if failure:
                            diagnosis = _go_diagnosis(mux, 'scp' if windows else 'rsync', failure, f'target {label} pack',
                                                     timed_out=getattr(failure, 'timed_out', False))
                            if getattr(failure, 'timed_out', False):
                                raise SystemExit(f'target pack rsync failed: {failure.stderr}{_go_suffix(diagnosis)}')
                            raise SystemExit(f'target {label} pack rsync failed ({failure}): '
                                             f'{shlex.join(transfer)}{_go_suffix(diagnosis)}')
                        helper.extend(('--pack', incoming))
                    code, output, _ = (_go_target_ssh(host, mux, helper, 900, windows=True)
                                       if windows else _go_target_ssh(host, mux, helper, 900))
                finally:
                    if pack_file is not None:
                        pack_file.unlink(missing_ok=True)
                if code:
                    raise SystemExit(f'target {label} staging failed ({code})'
                                     f'{_go_suffix(getattr(code, "diagnosis", None))}')
                try:
                    result = json.loads(output.strip().splitlines()[-1])
                    if result['commit'] != commit or result['cache'] not in ('built', 'reused'):
                        raise ValueError('invalid target receipt')
                except (ValueError, KeyError, IndexError, TypeError) as error:
                    raise SystemExit(f'target {label} returned invalid staging receipt') from error
                staged_commits[commit] = result
                print(f'target {label} {commit[:12]}: cache {result["cache"]}, '
                      f'{sent if sent is not None else "unknown"} bytes shipped in {time.monotonic() - started:.1f} s')
    remote = [*kind['python'], script, 'go', '--host', args.host, '--label', args.label]
    if base:
        remote.extend(('--from', base))
    for nro in args.nro:
        remote.extend(('--nro', f'{stage}/nro/{Path(nro).name}'))
    remote.extend(('--rows', f'{stage}/rows'))
    if lineup:
        remote.extend(('--card-lineup', f'{stage}/card-lineup', '--card-lineup-origin', lineup['dir']))
    else:
        remote.extend(('--no-card-lineup', args.no_card_lineup))
    if args.elf:
        remote.extend(('--elf', f'{stage}/elf/{Path(args.elf).name}'))
    for point in args.trace_point:
        remote.extend(('--trace-point', point))
    if args.network_interface:
        remote.extend(('--network-interface', args.network_interface))
    if args.no_lobby:
        remote.append('--no-lobby')
    if getattr(args, 'prepare', False):
        remote.append('--prepare')
    if getattr(args, 'down', False):
        remote.append('--down')
    for key, value in getattr(args, 'eden_setting', None) or []:
        remote.extend(('--eden-setting', f'{key}={value}'))
    for commit in sorted(staged_commits):
        remote.extend(('--staged-target', commit))
    remote.extend(('--owner', args.owner))
    token = uuid.uuid4().hex
    remote.extend(('--token', token))
    mux.ensure()
    command = _remote_command(host, mux, remote, windows)
    ssh_code = _relay_run(command, host, args) if windows else _go_sh(command)
    if ssh_code == 255 or _lifecycle_module.ssh_master_dead(mux):
        diagnosis = _go_diagnosis(mux, 'ssh', _ExitCode(255, getattr(ssh_code, 'stderr', '')), 'remote go')
        raise SystemExit(f'remote go failed (ssh exit {ssh_code}); the remote go may be running; '
                         f'check the host before starting another{_go_suffix(diagnosis)}')
    if getattr(args, 'prepare', False):
        if not ssh_code:
            print(f'Continue: eden_test.py remote --host {args.host} --label {args.label} fly'
                  f'{" --down" if getattr(args, "down", False) else ""}')
        return ssh_code
    run = f'{kind["root"]}/{args.label}'
    prefix = (f'eden_test.py remote --host {args.host} --label {args.label}' if windows else
              f'on {host}: /usr/bin/python3 -I -B {script}')
    suffix = '' if windows else f' {run}'
    with tempfile.TemporaryDirectory(prefix='eden-go-') as directory:
        result_file = Path(directory) / 'go-result.json'
        mux.ensure()
        result_error = _go_sh(['scp', *mux.rider_args, f'{host}:{run}/go-result.json', str(result_file)])
        result_diagnosis = _go_diagnosis(mux, 'scp', result_error, 'go-result transfer') if result_error else None
        try:
            result = json.loads(result_file.read_text()) if not result_error else {}
            code = result['code']
            if (result.get('token') != token or type(code) is not int or code not in (0, 1, 3)
                    or not isinstance(result['counts'], dict)):
                raise ValueError('invalid or mismatched result')
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise SystemExit(f'remote go produced no result for this call (ssh exit {ssh_code}); '
                             f'nothing was copied; see the output above{_go_suffix(result_diagnosis)}') from error
    report = f'./REPORT-{args.label}.md'
    mux.ensure()
    if report_error := _go_sh(['scp', *mux.rider_args, f'{host}:{run}/REPORT.md', report]):
        diagnosis = _go_diagnosis(mux, 'scp', report_error, 'report transfer')
        print(f'Down: {prefix} down{suffix}')
        raise SystemExit(f'copying remote report failed ({report_error}){_go_suffix(diagnosis)}')
    print(f'Report: {Path(report).resolve()}')
    print(f'Re-run: {prefix} run{suffix} --rows <id>; then {prefix} report{suffix}')
    print(f'Down: {prefix} down{suffix}')
    return code


def cmd_remote(args):
    host = _go_route(args)
    if not host or host.startswith('-'):
        raise SystemExit(f'no valid SSH host for {args.host!r}; use --ssh or add it to hosts.json')
    try:
        _target_label(args.label)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    kind = _remote_kind(args.host)
    stage = f'{kind["root"]}/_eden-test/stage/{args.label}'
    run = f'{kind["root"]}/{args.label}'
    command = [*kind['python'], f'{stage}/tools/eden-test/eden_test.py', args.remote_mode, run, *args.remote_args]
    options = ('-i', '/home/intpa/.ssh/id_rsa', '-o', 'IdentitiesOnly=yes') if kind['windows'] else ()
    receipt_dir = RUNS / '_eden-test' / 'ssh-diag' / args.label
    with SshMux(host, ssh_options=options, windows=kind['windows'], evidence_dir=receipt_dir,
                receipt_dir=receipt_dir) as mux:
        mux.ensure()
        remote = _remote_command(host, mux, command, kind['windows'])
        return _relay_run(remote, host, args) if kind['windows'] else _go_sh(remote)


def _unit(label):
    try:
        _target_label(label)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    return f'eden-go-{label}'


def _detach(args, argv):
    if host_os.WINDOWS:
        raise SystemExit('--detach requires a Linux controller')
    unit = _unit(args.label)
    active = subprocess.run(['systemctl', '--user', 'is-active', unit], capture_output=True, text=True)
    if active.returncode == 0 and active.stdout.strip() == 'active':
        raise SystemExit(f'{unit} is already active')
    folder = RUNS / '_eden-test' / 'go' / args.label
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / 'go.log'
    forwarded = list(argv)
    forwarded.remove('--detach')
    command = ['systemd-run', '--user', '--unit', unit, '--quiet', '--collect', '--expand-environment=no',
               '-p', 'MemoryMax=2G', '-p', 'TasksMax=512', '-p', 'RuntimeMaxSec=14400', '-p', 'KillMode=mixed',
               '-p', 'TimeoutStopSec=600', f'--working-directory={Path.cwd()}', '/usr/bin/python3', '-I', '-B',
               str(Path(__file__).resolve()), *forwarded, '--go-log', str(log)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f'{unit} launch failed: {result.stderr or result.stdout}')
    print(f'Unit: {unit}\nLog: {log}')
    print(f'eden_test.py peek --label {args.label}')
    ssh = f' --ssh {shlex.quote(args.ssh)}' if getattr(args, 'ssh', None) else ''
    print(f'eden_test.py stop --host {args.host}{ssh} --label {args.label}')
    return 0


def _logged(args, argv):
    global _GO_LOG
    path = Path(args.go_log)
    path.parent.mkdir(parents=True, exist_ok=True)
    status_path = path.parent / 'status.json'
    status = {'label': args.label, 'host': args.host, 'argv': argv, 'started': time.time(), 'phase': 'running'}
    status_path.write_text(json.dumps(status, indent=2) + '\n')
    code = 1
    with path.open('a', encoding='utf-8', buffering=1) as stream:
        previous = _GO_LOG
        _GO_LOG = stream
        try:
            with redirect_stdout(stream), redirect_stderr(stream):
                try:
                    code = _dispatch(args) or 0
                except SystemExit as exc:
                    code = exc.code if type(exc.code) is int else 1
                    if exc.code is not None and type(exc.code) is not int:
                        print(exc.code, file=sys.stderr)
                    raise
                except Exception:
                    traceback.print_exc()
                    raise
        finally:
            _GO_LOG = previous
            status.update(phase='ended', ended=time.time(), exit=int(code))
            status_path.write_text(json.dumps(status, indent=2) + '\n')
    return code


def cmd_peek(args):
    unit = _unit(args.label)
    folder = RUNS / '_eden-test' / 'go' / args.label
    status = folder / 'status.json'
    log = folder / 'go.log'
    print(status.read_text() if status.is_file() else f'no status: {status}', end='\n')
    result = subprocess.run(['systemctl', '--user', 'show', unit, '-p', 'ActiveState', '-p', 'SubState', '-p',
                             'Result'], capture_output=True, text=True)
    print(result.stdout, end='')
    if result.stderr:
        print(result.stderr, end='', file=sys.stderr)
    if log.is_file():
        print('\n'.join(log.read_text(errors='replace').splitlines()[-40:]))
    return 0


def _remote_mux(args):
    _unit(args.label)
    host = _go_route(args)
    if not host or host.startswith('-'):
        raise SystemExit(f'no valid SSH host for {args.host!r}; use --ssh or add it to hosts.json')
    windows = args.host in WINDOWS_HOSTS
    options = ('-i', '/home/intpa/.ssh/id_rsa', '-o', 'IdentitiesOnly=yes') if windows else ()
    receipt_dir = RUNS / '_eden-test' / 'ssh-diag' / args.label
    return host, SshMux(host, ssh_options=options, windows=windows, evidence_dir=receipt_dir, receipt_dir=receipt_dir)


def cmd_stop(args):
    host, context = _remote_mux(args)
    kind = _remote_kind(args.host)
    stop = f'{kind["root"]}/{args.label}/out/STOP'
    with context as mux:
        mux.ensure()
        if kind['windows']:
            script = f'New-Item -ItemType File -Force -Path {_pwsh_arg(stop)} | Out-Null'
            command = _windows_ssh(host, mux, script)
        else:
            command = _remote_command(host, mux, ['/usr/bin/touch', stop], False)
        if code := _go_sh(command):
            raise SystemExit(f'stop failed ({code}){_go_suffix(_go_diagnosis(mux, "ssh", code, "stop"))}')
    print(f'Stop requested: {host}:{stop}')
    return 0


_COLLECT_NAMES = ('config.json', 'lifecycle-*.log', 'runner.log', 'dry-run.log', 'RESULTS.md', 'REPORT.md',
                  'timing.json', 'go-result.json', 'harvest.json', 'out/', 'state/')
_FILM_EXCLUDES = ('*.mkv', '*.capture.gif')


def _extract_collect(archive, dest):
    root = Path(dest).resolve()
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar.getmembers():
            name = member.name.replace('\\', '/')
            target = (root / name).resolve()
            if (name.startswith('/') or '..' in name.split('/') or re.match(r'^[A-Za-z]:', name)
                    or not target.is_relative_to(root)):
                raise ValueError(f'unsafe collect member: {member.name}')
            if member.issym() or member.islnk():
                link = member.linkname.replace('\\', '/')
                base = target.parent if member.issym() else root
                if link.startswith('/') or re.match(r'^[A-Za-z]:', link) or not (base / link).resolve().is_relative_to(root):
                    raise ValueError(f'unsafe collect link: {member.name}')
        tar.extractall(root, filter='data')


def cmd_collect(args):
    host, context = _remote_mux(args)
    kind = _remote_kind(args.host)
    run = f'{kind["root"]}/{args.label}'
    dest = Path(args.to) / args.label
    if dest.exists():
        raise SystemExit(f'collect destination already exists: {dest}')
    dest.mkdir(parents=True)
    with context as mux:
        mux.ensure()
        if not kind['windows']:
            excludes = [] if args.films else [f'--exclude={pattern}' for pattern in _FILM_EXCLUDES]
            for name in _COLLECT_NAMES:
                target = str(dest / name) if name.endswith('/') else str(dest) + '/'
                transfer = ['rsync', '-e', shlex.join(['ssh', *mux.rider_args]), '-a', '--mkpath',
                            '--ignore-missing-args', *excludes, f'{host}:{run}/{name}', target]
                if code := _go_sh(transfer):
                    raise SystemExit(f'collect failed ({code}): {name}')
        else:
            names = ','.join("'" + name.rstrip('/').replace("'", "''") + "'" for name in _COLLECT_NAMES)
            excludes = '' if args.films else ' --exclude=' + ' --exclude='.join(_pwsh_arg(p) for p in _FILM_EXCLUDES)
            archive = f'{run}/collect.tar.gz'
            run_literal = "'" + run.replace("'", "''") + "'"
            script = (f'$run={run_literal}; $names=@({names}); '
                      '$files=@(foreach($n in $names) { Get-ChildItem -LiteralPath $run -Filter $n '
                      '-ErrorAction SilentlyContinue | ForEach-Object { $_.Name } }); '
                      f'& tar.exe -czf {_pwsh_arg(archive)} -C $run{excludes} @files; exit $LASTEXITCODE')
            if code := _go_sh(_windows_ssh(host, mux, script)):
                raise SystemExit(f'collect archive failed ({code})')
            try:
                with tempfile.TemporaryDirectory(prefix='eden-collect-') as temp:
                    local = Path(temp) / 'collect.tar.gz'
                    if code := _go_sh(['scp', *mux.rider_args, f'{host}:{archive}', str(local)]):
                        raise SystemExit(f'collect transfer failed ({code})')
                    _extract_collect(local, dest)
            finally:
                remove = f'Remove-Item -LiteralPath {_pwsh_arg(archive)}'
                if code := _go_sh(_windows_ssh(host, mux, remove)):
                    print(f'collect: remote archive removal failed ({code})', file=sys.stderr)
    print(f'Collected: {dest}')
    return 0


def _eden_setting(value):
    key, separator, raw = value.partition('=')
    if not separator or '.' not in key or not key or not re.fullmatch(r'[+-]?\d+', raw):
        raise argparse.ArgumentTypeError('eden setting must be KEY=INTEGER with a dot in KEY')
    return key, int(raw)


def _prepare(run):
    problems = check_run(run, skip_setup_plan=True, allow_volatile_pins=True)
    for problem in problems:
        print(problem)
    dry = sh([*_python(), str(RUNNER), 'up-check', str(run / 'rows' / 'rows.json'),
              '--out', str(run / 'out')], run / 'dry-run.log')
    preflight = lifecycle(run, 'preflight')
    print(f'Prepared: {run}')
    return 0 if not problems and not dry and not preflight else 1


def cmd_fly(run, down=False, token=None):
    try:
        cmd_up(run, skip_setup_plan=True, allow_volatile_pins=True)
    except BaseException:
        timing = run / 'timing.json'
        if timing.is_file() and 'up_start' in json.loads(timing.read_text()):
            print(f'Down: /usr/bin/python3 -I -B {Path(__file__).resolve()} down {run}')
        raise
    code, error, down_error = 3, None, None
    try:
        try:
            code = cmd_run(run)
        except (Exception, SystemExit) as exc:
            print(f'run failed: {exc}')
        finally:
            cmd_report(run)
            print(f'Report: {run / "REPORT.md"}')
        counts = report_counts(run)
        code = 0 if code == 0 and not counts['FAIL'] and not counts['NOT RUN'] else 3
        (run / 'go-result.json').write_text(json.dumps({'token': token, 'code': code, 'counts': counts}, indent=2) + '\n')
    except BaseException as exc:
        error = exc
    finally:
        if down:
            try:
                cmd_down(run)
            except (Exception, SystemExit) as exc:
                down_error = exc
                print(f'down failed: {exc}')
    if down_error is not None:
        if (run / 'go-result.json').is_file():
            result = json.loads((run / 'go-result.json').read_text())
            result['code'] = 1
            (run / 'go-result.json').write_text(json.dumps(result, indent=2) + '\n')
        return 1
    if error is not None:
        raise error
    return code


def cmd_go(args):
    # Match the owner rule in eden-lifecycle/lifecycle.py:624.
    if not re.fullmatch(r'(OpenCode|Codex|Claude|Cursor)\s+\S+', args.owner):
        raise SystemExit('--owner must be "<Engine> <session>/<stream>" (OpenCode|Codex|Claude|Cursor), '
                         f'e.g. "Claude devin-<lane>/<label>"; got: {args.owner!r}')
    if args.token:  # a controller sent this go here: it runs on this machine or not at all
        _go_received_base(args)
    else:
        # Which machine this is comes from its own shared base, never from the base the go names: a local copy of
        # the host's base does not make this machine the host.
        shared = RUNS / '_eden-test' / 'base.json'
        this_host = _go_this_host()
        if this_host == args.host:
            remote = False  # this is the host: any local --from, as before; an ssh name is not needed
        elif _go_route(args):
            remote = True  # the host is elsewhere: --from is a path there, and a local file is not opened
        elif this_host is not None:
            raise SystemExit(
                f'this machine is host {this_host!r} by its shared base {shared}, not {args.host!r}, and it has no '
                f'ssh name for {args.host!r} (pass --ssh or add it to {RUNS / "_eden-test" / "hosts.json"}); '
                'nothing was started')
        else:
            # No shared base and no route: nothing says this machine is the host, and a file the go names is no
            # evidence of it. A host is set up by giving it its shared base.
            raise SystemExit(
                f'this machine has no shared base {shared}, so it is not known to be host {args.host!r}, and it has '
                f'no ssh name for {args.host!r} (pass --ssh or add it to {RUNS / "_eden-test" / "hosts.json"}); '
                'a base named with --from does not say which machine this is; nothing was started')
        if remote:
            _go_trace(args)
            lineup = _go_card_lineup(args)
            return _go_remote(args, lineup)
    run = go_new(args)
    _discard_remote_lineup_stage(args)
    config = json.loads((run / 'config.json').read_text())
    for key, entries in config['staged_plugins'].items():
        lineup = config['card_lineup']
        if lineup['staged']:
            replaced = ', '.join(entry['name'] for entry in entries if entry.get('replaces')) or 'none'
            added = ', '.join(f'{entry["name"]} ({entry["source"]})' for entry in entries
                              if entry['source'] in ('base', 'nro') and not entry.get('replaces')) or 'none'
            detail = (f'card lineup {lineup["origin"]} ({lineup["files"]} files verified); '
                      f'stamp {lineup.get("stamp") or "custom"}, '
                      f'{lineup.get("mods_files", 0)} mod files + {lineup.get("arcropolis_config_files", 0)} '
                      f'config files; {lineup.get("profile_copy_bytes", 0)} card payload bytes per private SD set; '
                      f'--nro replaced: {replaced}; added: {added}')
        else:
            detail = f'card lineup NOT STAGED, reason: {lineup["reason"]}'
        print(f'Staged plugins [{key}]: {len(entries)} — {detail}')
    if getattr(args, 'prepare', False):
        return _prepare(run)
    return cmd_fly(run, down=getattr(args, 'down', False), token=args.token)


def stop_captures(run):
    """End every detached bridge capture of this run: out/bridge-run (socket) and out/bridge-run-<instance> (the file
    transport, one per instance)."""
    results = (stop_capture(run, path.parent) for path in sorted((run / 'out').glob('bridge-run*/capture.json')))
    return [result for result in results if result]


def stop_capture(run, folder=None):
    """End the detached bridge capture `routes:bridge_on` started for this run (out/bridge-run/capture.json)."""
    folder = folder or run / 'out' / 'bridge-run'
    if not (folder / 'capture.json').is_file():
        return None
    pid = json.loads((folder / 'capture.json').read_text()).get('pid')
    match = host_os.argv_has(pid, 'capture-bridge', str(folder))
    if match is None:
        return {'pid': pid, 'stopped': False, 'why': 'not running'}
    if not match:
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


CLEANUP_RECEIPT = 'down-cleanup.json'
_PATH_NAME_CHAR = r'[A-Za-z0-9._+-]'
_HIDDEN_PROCESSES = ('sshd-session', 'ssh-agent', '(sd-pam)')
_USER_MANAGERS = ('systemd', '/usr/lib/systemd/systemd', '/lib/systemd/systemd')


def _names_path(text, path):
    """True when text names `path` itself or something under it (not a longer name that only starts the same)."""
    return re.search(re.escape(str(path)) + rf'(?!{_PATH_NAME_CHAR})', text) is not None


def _pin_matches(value, path, absolute=False):
    text = str(path)
    if host_os.WINDOWS:
        value, text = (s.replace('\\', '/').lower() for s in (value, text))
        rooted = PureWindowsPath(value).is_absolute()
    else:
        rooted = value.startswith('/')
    return ((not absolute or rooted) and _names_path(value, text)
            or rooted and (absolute or len(value) > 1) and text.startswith(value.rstrip('/') + '/'))


def _json_strings(node):
    """Yield run references, excluding ports_root: it bounds allocation scans, not an owned run's data."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if key == 'ports_root':
                continue
            yield key
            yield from _json_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _json_strings(value)


def _present(path):
    """True when the path is there, False when it is not; an entry that cannot be examined raises OSError."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _peer_directory(path):
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise OSError(errno.ELOOP, 'linked peer not inspected; its pins are unknown', path)
    return stat.S_ISDIR(mode)


def _pinning_files(run, runs):
    """The files through which another run can need a path: every other run's configs and Up-time journal, the host's
    shared base, and the bases other labels keep in their stage folders. A folder that cannot be listed raises."""
    for name in sorted(os.listdir(runs)):
        entry = runs / name
        if name == '_eden-test' or entry == run or not _peer_directory(entry):
            continue
        yield entry / 'config.json'
        yield from (entry / other for other in sorted(os.listdir(entry))
                    if other.startswith('config.after-up.') and other.endswith('.json'))
        yield entry / 'state' / 'lifecycle.json'
    yield runs / '_eden-test' / 'base.json'
    stage = runs / '_eden-test' / 'stage'
    if _present(stage):
        for label in sorted(os.listdir(stage)):
            other = stage / label
            if label != run.name and _peer_directory(other):
                yield from (other / name for name in sorted(os.listdir(other)) if name.endswith('.json'))


def _process_reason(paths, proc=Path('/proc')):
    """Why a live process of this user, other than this Down and its ancestors, keeps one of `paths`: it names one in
    its command line, sits in one, or has a file open in one; or it cannot be inspected and is not one of the
    processes the kernel hides (the rule of remote-gate.ps1 -Action Done). None when no process keeps them."""
    if host_os.WINDOWS:
        processes = host_os.windows_processes()
        parents = {p['pid']: p['ppid'] for p in processes}
        skip, pid = set(), os.getpid()
        while pid and pid not in skip:
            skip.add(pid)
            pid = parents.get(pid, 0)
        for process in processes:
            if process['pid'] in skip or (process['command'] is None and process['exe'] is None):
                continue
            texts = [(s or '').replace('\\', '/').lower() for s in (process['command'], process['exe'])]
            for path in paths:
                target = str(path).replace('\\', '/').lower().rstrip('/')
                if any(_names_path(text, target) for text in texts):
                    return f'process {process["pid"]} ({process["name"]}) uses {path}'
        for path in paths:
            if _present(path) and (busy := host_os.windows_busy_path(path)) is not None:
                return f'an open file or directory is in use: {busy}'
        return None
    skip, pid = set(), os.getpid()
    while pid > 1 and pid not in skip:
        skip.add(pid)
        try:
            status = (proc / str(pid) / 'status').read_text()
            pid = int(re.search(r'^PPid:\s+(\d+)', status, re.M).group(1))
        except (OSError, AttributeError, ValueError):
            break
    uid, foreign_children, managers = os.getuid(), set(), []
    for entry in sorted(proc.iterdir(), key=lambda item: item.name):
        if not entry.name.isdigit() or int(entry.name) in skip:
            continue
        try:
            mine = entry.stat().st_uid == uid
            status = (entry / 'status').read_text()
        except OSError:
            if entry.is_dir():
                return f'process {entry.name} cannot be read'
            continue
        name = (re.search(r'^Name:\s+(.*)$', status, re.M) or [None, ''])[1].strip()
        parent = (re.search(r'^PPid:\s+(\d+)', status, re.M) or [None, ''])[1]
        if not mine:
            foreign_children.add(parent)
            continue
        states, targets = {}, []
        for link in ('cwd', 'exe'):
            try:
                targets.append(os.readlink(entry / link))
                states[link] = 'read'
            except PermissionError:
                states[link] = 'hidden'
            except (FileNotFoundError, ProcessLookupError):
                states[link] = 'absent'
            except OSError:
                states[link] = 'unknown'
        fd_states = set()
        try:
            for fd in os.listdir(entry / 'fd'):
                try:
                    targets.append(os.readlink(entry / 'fd' / fd))
                    fd_states.add('read')
                except PermissionError:
                    fd_states.add('hidden')
                except (FileNotFoundError, ProcessLookupError):
                    fd_states.add('absent')
                except OSError:
                    fd_states.add('unknown')
        except OSError:
            fd_states.add('unlisted')
        try:
            raw = (entry / 'cmdline').read_bytes().decode('utf-8', errors='surrogateescape')
            argv = (raw[:-1] if raw.endswith('\0') else raw).split('\0')
        except OSError:
            argv = None
        if not entry.is_dir():
            continue  # it ended while it was read
        for path in paths:
            text = str(path)
            if any(target == text or target.startswith(text + '/') for target in targets) \
                    or (argv is not None and _names_path(' '.join(argv), path)):
                return f'process {entry.name} ({name}) uses {path}'
        if states['cwd'] == states['exe'] == 'read' and fd_states <= {'read', 'absent'} and argv is not None:
            continue
        zombie = re.search(r'^State:\s+Z', status, re.M) is not None
        try:
            fd_owner = (entry / 'fd').stat().st_uid
        except OSError:
            fd_owner = -1
        manager = (name == 'systemd' and argv is not None and len(argv) in (2, 3) and argv[0] in _USER_MANAGERS
                   and argv[1] == '--user' and (len(argv) == 2 or re.fullmatch(r'--deserialize=[0-9]+', argv[2]))
                   and parent == '1' and states['cwd'] == states['exe'] == 'hidden' and fd_states == {'hidden'})
        if manager:
            managers.append(entry.name)
        elif argv is None or not (zombie or (name in _HIDDEN_PROCESSES and fd_owner == 0)):
            return f'process {entry.name} ({name}) cannot be inspected'
    for manager in managers:
        if manager in foreign_children:
            return f'a child of the service manager {manager} belongs to another user and cannot be inspected'
    return None


class _DownGuard:
    """What must stay after a Down because another run may need it. Anything that cannot be read keeps everything."""

    def __init__(self, run, runs, claims, own_token=None, proc=Path('/proc')):
        self.run, self.runs, self.proc, self.blocked, self.files, self.keep = run, runs, proc, None, [], []
        self.claims, tokens, labels = [], set(), set()
        current = runs
        try:
            for current in _pinning_files(run, runs):
                if _present(current):
                    data = json.loads(current.read_text())
                    self.files.append((current, list(_json_strings(data))))
                    if current.name == 'lifecycle.json' and isinstance(data, dict) and isinstance(data.get('token'), str):
                        tokens.add(data['token'])
                    if current.name == 'config.json':
                        labels.add(current.parent.name)
            current = runs / '.keep'
            if _present(current):
                self.keep = [line.strip() for line in current.read_text().splitlines()
                             if line.strip() and not line.lstrip().startswith('#')]
        except (OSError, ValueError) as error:
            why = getattr(error, 'strerror', None) or type(error).__name__
            self.blocked = f'cannot read {getattr(error, "filename", None) or current}: {why}'
        if isinstance(claims, str):
            self.blocked = self.blocked or claims
            return
        # An open claim is a run that may be using anything here. It is covered when its run was read above (by its
        # lifecycle token, or by its label with a readable config); any other open claim keeps everything.
        for line in claims:
            token = re.search(r'\blifecycle=([A-Za-z0-9_-]+)', line)
            if token and token[1] == own_token:
                self.blocked = self.blocked or "the ledger still shows this run's claim open"
                continue
            named = [found[1] for found in (re.search(r'CLAIMED:\s*\S+\s+\S+/([A-Za-z0-9][\w.-]*)', line),
                                            re.search(r'\brun\s+([A-Za-z0-9][\w.-]*)', line)) if found]
            if not ((token and token[1] in tokens) or any(name in labels for name in named)):
                self.blocked = self.blocked or f'an open ledger claim is not matched to a run here: {one_line(line, 150)}'

    def reason(self, path):
        """Why `path` is kept, or None when nothing found needs it."""
        if self.blocked:
            return self.blocked
        for name in self.keep:
            if name == self.run.name or _pin_matches(name, path, absolute=True):
                return f'{self.runs / ".keep"} lists {name}'
        for source, strings in self.files:
            for value in strings:
                if _pin_matches(value, path):
                    return f'{source} names {value if len(value) <= 120 else value[:117] + "..."}'
        try:
            return _process_reason([path], self.proc)
        except OSError as error:
            return f'cannot read the processes: {type(error).__name__}'


def _gif_receipts(node):
    if isinstance(node, dict):
        if isinstance(node.get('gif'), dict):
            yield node['gif']
        for value in node.values():
            yield from _gif_receipts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _gif_receipts(value)


def raw_captures(run):
    """(raw capture, None) for each Eden recording out/<row>/*.capture.gif that the receipt of its own row binds to a
    published clip which is there with the recorded sha256; (raw capture, why it is kept) for every other one."""
    out, bound = run / 'out', {}
    for receipt in sorted(out.glob('*.json')):
        row = out / receipt.stem
        if receipt.is_symlink() or row.is_symlink() or not row.is_dir():
            continue
        try:
            data = json.loads(receipt.read_text())
        except (OSError, ValueError):
            continue
        for info in _gif_receipts(data):
            name = info.get('capture')
            if isinstance(name, str) and name and Path(name).name == name:
                bound.setdefault(row / name, []).append(info)
    for raw in sorted(out.glob('*/*.capture.gif')):
        if raw.is_symlink() or not raw.is_file() or os.path.realpath(raw) != str(raw):
            yield raw, 'not a plain file in a plain row folder'
            continue
        reason = 'no receipt of its own row binds it to a published clip'
        for info in bound.get(raw, []):
            clip = Path(str(info.get('path') or ''))
            try:
                verified = (clip.is_absolute() and not clip.is_symlink() and clip.is_file() and clip != raw
                            and os.path.realpath(clip) == str(clip) and sha256(clip) == info.get('sha256'))
            except OSError:
                verified = False
            if verified:
                reason = None
                break
            reason = f'its published clip {clip} is missing or differs from the recorded sha256'
        yield raw, reason


def _open_claims(run, config):
    """The ledger's open claim lines for this host, read (never written) through the lifecycle's own reader."""
    if host_os.WINDOWS:
        reply = _relay({'kind': 'claims', 'config': config})
        if not reply.get('ok'):
            raise ValueError(reply.get('error'))
        lines = reply.get('result')
        if not isinstance(lines, list):
            raise ValueError('the ledger read returned no claim list')
        return [str(line) for line in lines]
    info = _lifecycle_module.Ledger(config['ledger'], config['host'], config['owner'], '0' * 32,
                                    config.get('ledger_ssh'), state_dir=run / 'state').inspect()
    lines = info.get('open') if isinstance(info, dict) else None
    if not isinstance(lines, list):
        raise ValueError('the ledger read returned no claim list')
    return [str(line) for line in lines]


def _cleanup_claims(run, config):
    try:
        return _open_claims(run, config)
    except Exception as error:  # An unavailable ledger is not an empty ledger.
        return f'cannot read the ledger: {type(error).__name__}'


def _unclean_down(journal, claims=()):
    """Why this Down is not a clean one (nothing is then removed), or None."""
    if not journal or journal.get('phase') != 'down':
        return 'the lifecycle journal does not say down'
    if journal.get('release_pending'):
        return 'the ledger release is pending'
    if not journal.get('released') and journal.get('claimed') is not False:
        return 'the claim is not released'
    if journal.get('cleanup_failures'):
        return 'the lifecycle reported cleanup failures'
    token = journal.get('token')
    if isinstance(claims, (list, tuple)) and token and any(f'lifecycle={token}' in line for line in claims):
        return 'the ledger still shows this run\'s claim open'
    return None


def cleanup_summary(run):
    """RESULTS.md's Cleanup line, from out/down-cleanup.json."""
    path = run / 'out' / CLEANUP_RECEIPT
    if not path.is_file():
        return 'not run'
    record = json.loads(path.read_text())
    if record.get('skipped'):
        return f'not run: {record["skipped"]}'
    size = sum(item['bytes'] for item in record['removed'])
    removed = ', '.join(item['what'] for item in record['removed']) or 'nothing'
    kept = '; '.join(f'{item["what"]} ({item["reason"]})' for item in record['kept'])
    return f'removed {removed} ({size / 10 ** 9:.1f} GB)' + (f'; KEPT {kept}' if kept else '') \
        + f'; receipt out/{CLEANUP_RECEIPT}'


def _cleanup_line(run):
    return one_line(f'- Cleanup: {cleanup_summary(run)}', 240)


def _save_cleanup(run, record):
    (run / 'out').mkdir(parents=True, exist_ok=True)
    record['disk_free'] = shutil.disk_usage(run).free
    (run / 'out' / CLEANUP_RECEIPT).write_text(json.dumps(record, indent=2) + '\n')


def _path_bytes(path):
    return path.lstat().st_size if path.is_symlink() or not path.is_dir() else _target_size(path)[1]


def cmd_down(run, keep_state=False):
    restore_up_config(run)
    config = json.loads((run / 'config.json').read_text())
    for capture in stop_captures(run):  # before Eden stops: a capture would otherwise keep reconnecting to a dead port
        print('bridge capture: ' + json.dumps(capture))
    stamp(run, 'down_start')
    # A clean Down copies the files the run wrote to out/post-run-<key>/, then prunes work/ and the state snapshots.
    # The first call stops Eden and releases the claim with the lifecycle's own --keep-state, so nothing is pruned
    # before the check for what another run needs; the second call lets the lifecycle prune.
    evidence = ('--evidence-dir', str(run / 'out'))
    code = lifecycle(run, 'down', *evidence, '--keep-state')
    journal_path = run / 'state' / 'lifecycle.json'
    read_journal = lambda: json.loads(journal_path.read_text()) if journal_path.is_file() else {}
    record = {'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'label': config.get('label'), 'skipped': None,
              'removed': [], 'kept': []}
    guard, pruned = None, False
    keep = lambda what, path, reason: record['kept'].append({'what': what, 'path': str(path), 'reason': reason})
    if keep_state:
        record['skipped'] = '--keep-state'
    elif code != 0:
        record['skipped'] = f'lifecycle down failed ({code})'
    else:
        claims = _cleanup_claims(run, config)
        record['skipped'] = _unclean_down(read_journal(), claims)
    if not record['skipped']:
        token = read_journal().get('token')
        guard = _DownGuard(run, RUNS, claims, token)

        def current_reason(path):
            return _DownGuard(run, RUNS, _cleanup_claims(run, config), token).reason(path)

        copies = [path for path in (run / 'work', *sorted((run / 'state').glob('snapshot-*')),
                                    *sorted((run / 'state').glob('post-run-*'))) if path.is_symlink() or path.exists()]
        held = [(path, reason) for path in copies if (reason := guard.reason(path))]
        for path, reason in held:
            keep(f'{path.relative_to(run)}/', path, reason)
        # An unknown peer vetoes the prune itself, even when no copy is visible to this scan.
        stop = guard.blocked or ('a copy of this run is kept' if held else None)
        if not stop:
            sizes = [(path, _path_bytes(path)) for path in copies]
            prune_code = lifecycle(run, 'down', *evidence)
            done = (read_journal().get('prune') or {}).get('done')
            pruned = prune_code == 0 and bool(done)
            if not pruned:
                stop = f'the lifecycle did not prune (exit {prune_code})'
            for path, size in sizes:
                if not pruned:
                    keep(f'{path.relative_to(run)}/', path, f'the lifecycle did not prune (exit {prune_code}); see RESULTS')
                elif not path.exists():
                    record['removed'].append({'what': f'{path.relative_to(run)}/', 'path': str(path), 'bytes': size})
        # Nothing more goes unless the lifecycle's own prune ran and finished: a held copy or a failed prune means
        # Down has to run again, from the same folders.
        for raw, reason in raw_captures(run):
            reason = stop or reason or current_reason(raw)
            what = f'raw capture {raw.relative_to(run / "out")}'
            if reason:
                keep(what, raw, reason)
            else:
                size = raw.stat().st_size
                raw.unlink()
                record['removed'].append({'what': what, 'path': str(raw), 'bytes': size})
    stamp(run, 'down_done')
    done = subprocess.run([*_python(), str(HARVEST), str(run / 'rows' / 'rows.json'),
                           '--label', config['label'], '--out', str(run / 'out')], capture_output=True, text=True)
    (run / 'harvest.json').write_text(done.stdout.strip() + '\n')
    timing_path = run / 'timing.json'
    timing = json.loads(timing_path.read_text())
    timing['split'] = time_split(run)
    timing_path.write_text(json.dumps(timing, indent=2) + '\n')
    _save_cleanup(run, record)
    results_md(run, config)
    (run / 'REPORT.md').write_text(report_md(run))
    if code != 0:
        raise SystemExit(f'lifecycle down failed ({code}); the claim stays held; see {run / "lifecycle-down.log"}')
    if guard:
        # Last, because a remote Down runs from this folder: the stage copy, with whatever was put into it.
        try:
            stage = None
            if _present(RUNS / '_eden-test' / 'stage' / str(config['label'])):
                _target_label(config['label'])
                stage = _owned_target_directory(RUNS, ('_eden-test', 'stage', config['label']))
            if stage is not None:
                reason = None if pruned else f'Down has to run again from it: {stop}'
                reason = reason or current_reason(stage)
                if reason:
                    keep('stage copy', stage, reason)
                else:
                    size = _path_bytes(stage)
                    _remove_target_tree(stage)
                    record['removed'].append({'what': 'stage copy', 'path': str(stage), 'bytes': size})
                _save_cleanup(run, record)
                results = run / 'RESULTS.md'
                results.write_text(''.join(_cleanup_line(run) + '\n' if line.startswith('- Cleanup: ') else line
                                           for line in results.read_text().splitlines(keepends=True)))
        except (ValueError, OSError) as error:
            keep('stage copy', RUNS / '_eden-test' / 'stage' / str(config.get('label')), f'not removed: {error}')
            _save_cleanup(run, record)
        print(f'Cleanup: {cleanup_summary(run)}')
    if journal_path.is_file():
        journal = json.loads(journal_path.read_text())
        if journal.get('release_pending'):
            unit = journal.get('settle_unit') or 'not started'
            receipt = run / 'state' / 'RELEASE-PENDING.md'
            lines = receipt.read_text(encoding='utf-8').splitlines() if receipt.is_file() else []
            last = lines[-1] if lines else 'receipt unavailable'
            print(f'Down complete; the ledger release is pending: {last}; settle unit {unit}; '
                  'running down again completes it')


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
    lineup = p.add_mutually_exclusive_group()
    lineup.add_argument('--card-lineup', help='stamped card-lineup folder with plugins/, ultimate/, and manifests')
    lineup.add_argument('--no-card-lineup', metavar='REASON', help='do not stage the card lineup; record why')
    p.add_argument('--card-lineup-origin', help=argparse.SUPPRESS)
    p.add_argument('--rows', required=True, help='rows.json or a folder containing rows.json')
    p.add_argument('--elf')
    p.add_argument('--trace-point', action='append', default=[])
    p.add_argument('--network-interface', help='override the base host network interface for lobby rows')
    p.add_argument('--no-lobby', action='store_true', help='this run has no lobby rows')
    p.add_argument('--owner', required=True,
                   help='ledger owner, "<Engine> <session>/<stream>" with Engine one of OpenCode, Codex, '
                        'Claude, Cursor — e.g. "Claude devin-<lane>/<label>"')
    p.add_argument('--from', dest='from_config')
    p.add_argument('--ssh')
    p.add_argument('--token', help=argparse.SUPPRESS)
    p.add_argument('--staged-target', action='append', type=_staged_target_arg, default=[], help=argparse.SUPPRESS)
    p.add_argument('--prepare', action='store_true')
    p.add_argument('--down', action='store_true')
    p.add_argument('--eden-setting', action='append', type=_eden_setting, default=[])
    p.add_argument('--detach', action='store_true')
    p.add_argument('--go-log', help=argparse.SUPPRESS)
    p = sub.add_parser('remote', help='run a follow-up command on a remote host')
    p.add_argument('--host', required=True)
    p.add_argument('--ssh')
    p.add_argument('--label', required=True)
    p.add_argument('--detach', action='store_true')
    p.add_argument('--go-log', help=argparse.SUPPRESS)
    p.add_argument('remote_mode', choices=('check', 'up', 'run', 'report', 'pace', 'down', 'fly'))
    p.add_argument('remote_args', nargs=argparse.REMAINDER)
    p = sub.add_parser('fly', help='Up, run and report an existing staged run')
    p.add_argument('run', type=Path)
    p.add_argument('--down', action='store_true')
    p.add_argument('--token', help=argparse.SUPPRESS)
    p = sub.add_parser('peek')
    p.add_argument('--label', required=True)
    for name in ('stop', 'collect'):
        p = sub.add_parser(name)
        p.add_argument('--host', required=True)
        p.add_argument('--ssh')
        p.add_argument('--label', required=True)
        if name == 'collect':
            p.add_argument('--to', required=True)
            p.add_argument('--films', action='store_true')
    p = sub.add_parser('stage-target', help='build or reuse a target cache and stage it for a run')
    p.add_argument('--commit', required=True)
    p.add_argument('--label', required=True)
    p.add_argument('--pack')
    p.add_argument('--probe', action='store_true', help=argparse.SUPPRESS)
    p = sub.add_parser('unstage-targets', help='remove the run target trees, retaining the cache')
    p.add_argument('--label', required=True)
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
    raw = list(sys.argv[1:] if argv is None else argv)
    tail_log = raw[-1] if len(raw) >= 2 and raw[-2] == '--go-log' else None
    args = parser.parse_args(raw[:-2] if tail_log is not None else raw)
    if args.mode in ('go', 'remote'):
        if tail_log is not None:
            args.go_log = tail_log
        if args.mode == 'remote' and '--detach' in args.remote_args:
            args.remote_args.remove('--detach')
            args.detach = True
        if args.detach:
            return _detach(args, raw)
        if args.go_log:
            return _logged(args, raw)
    return _dispatch(args)


def _dispatch(args):
    if args.mode == 'go':
        return cmd_go(args)
    if args.mode == 'remote':
        return cmd_remote(args)
    if args.mode == 'fly':
        return cmd_fly(args.run.resolve(), down=args.down, token=args.token)
    if args.mode == 'peek':
        return cmd_peek(args)
    if args.mode == 'stop':
        return cmd_stop(args)
    if args.mode == 'collect':
        return cmd_collect(args)
    if args.mode == 'stage-target':
        try:
            _target_label(args.label)
            if args.probe:
                _target_commit(args.commit)
                _incoming_target(args.commit, args.label, RUNS, create=True)
                print(json.dumps({'commit': args.commit, 'cached': probe_target(args.commit, runs=RUNS)}))
                return 0
            print(json.dumps(stage_target(args.commit, args.label, args.pack, runs=RUNS)))
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            raise SystemExit(f'stage-target: {error}') from error
    elif args.mode == 'unstage-targets':
        try:
            unstage_targets(args.label, runs=RUNS)
        except (ValueError, OSError) as error:
            raise SystemExit(f'unstage-targets: {error}') from error
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
