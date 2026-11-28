#!/usr/bin/env python3
"""Shared, journalled Eden lifecycle. Import is inert; see README.md for config.

  lifecycle.py up        config.json --execute [--ssh tr-yoga]
  lifecycle.py down      config.json --execute [--ssh tr-yoga] [--evidence-dir DIR] [--keep-state]
  lifecycle.py settle    config.json --execute [--minutes N]
  lifecycle.py status    config.json           [--ssh tr-yoga]
  lifecycle.py reserve   config.json --approver NAME --execute
  lifecycle.py cancel    config.json --reservation-token TOKEN --execute
  lifecycle.py preflight config.json           [--ssh tr-yoga]
  lifecycle.py watchdog  config.json --execute [--ssh tr-yoga] [--rounds N] [--pause S]

preflight is read-only: pins, game base, declared update version, playbook commit,
memory floor, disk, compilers (each one's nice; niced compiles are admitted with headroom),
ledger claims and a declared display. It runs
again, still without writing, before every up. When the config declares watchdog
and does not set auto false, up starts that watchdog as a sidecar until down.
watchdog watches control activity and either parks with the declared input
sequence or (the default) Downs with a receipt. A missing control log counts
from the journal's Up time. Extra instance files are declared sources.
A clean down copies the files the run wrote to the evidence folder, then prunes
the run's copies (work_root profiles, state snapshot-*/post-run-*) unless
--keep-state; the journal's prune gives the bytes.

No runtime operations are performed by tests: inject a fake Host and Ledger.
SSH executes the SAME source through the remote login shell's PowerShell 7,
with configuration on stdin. Paths and instances.json belong to the executor
host; run the shared eden-runner there (no implicit socket forwarding).
"""
import argparse
import base64
from copy import deepcopy
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:  # Windows uses an exclusive byte-range run lock; ledger lives on Linux.
    fcntl = None
import fnmatch
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import queue
import threading

sys.dont_write_bytecode = True
GIB = 1024 ** 3
# Same filter as tools/eden-solo/gv.py launch. "log_filter" in the config overrides it.
DEFAULT_LOG_FILTER = '*:Warning Frontend:Info Service.Sockets:Info Network:Info'
MIN_MEMORY = 10 * GIB
MIN_MEMORY_SECOND = 9 * GIB  # for a second Eden on the host (each instance is capped at 8 GiB)
# Process names that are one running emulator instance (Linux comm, Windows image name).
EDEN_INSTANCE_NAMES = {'eden', 'yuzu', 'eden.exe'}
# Up refuses below this much free disk; "disk_floor_bytes" in the config overrides it. Raised from 40 GB on 2026-11-09:
# a run's copies are about 3 GB until Down prunes them, and several boots a day filled the XPS and the Yoga.
MIN_DISK = 60 * 10 ** 9
# Compiles beside an Up (operator 2026-11-10: "if there's headroom it should be allowed"). An Up is admitted while
# compilers run when every one of them runs at nice >= COMPILE_NICE_MIN, so the emulator keeps CPU priority, and
# MemAvailable covers the instance floor plus this reserve for the compile's own growth. "compile_reserve_bytes" in the
# config overrides it; "perf_sensitive": true keeps the old rule (no compiler at all). Measured on the Surface
# 2026-11-10 (TrainingRooms 889919e1, clean target dirs, nice 19, CARGO_BUILD_JOBS=2, the process tree summed every
# 0.2 s): `cargo skyline build --release --features diag` peaked at 1.72 GB (1.61 GiB) RSS, `cargo test` at 2.61 GB
# (2.43 GiB). 4 GiB keeps a margin of 1.57 GiB over the larger one.
COMPILE_RESERVE = 4 * GIB
COMPILE_NICE_MIN = 10
LEDGER_ATTEMPT_SECONDS = 10
SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
               '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2']
X_SOCKET_ROOT = Path('/tmp')  # X11's Unix sockets do not follow TMPDIR.
_PORTABLE_LOCK_SOURCE = globals().get('_FORWARDED_PORTABLE_LOCK_SOURCE')
if _PORTABLE_LOCK_SOURCE:
    _PORTABLE_LOCK = type(sys)('_eden_lifecycle_portable_lock')
    exec(compile(_PORTABLE_LOCK_SOURCE, 'portable_lock.py', 'exec'), _PORTABLE_LOCK.__dict__)
else:
    _PORTABLE_LOCK_SPEC = importlib.util.spec_from_file_location(
        '_eden_lifecycle_portable_lock', Path(__file__).resolve().parents[1] / 'eden-runner' / 'portable_lock.py')
    _PORTABLE_LOCK = importlib.util.module_from_spec(_PORTABLE_LOCK_SPEC)
    _PORTABLE_LOCK_SPEC.loader.exec_module(_PORTABLE_LOCK)


def portable_lock_source():
    return (_PORTABLE_LOCK_SOURCE or
            (Path(__file__).resolve().parents[1] / 'eden-runner' / 'portable_lock.py').read_text(encoding='utf-8'))


def standalone_source():
    """Return lifecycle source that can run from a copied standalone file."""
    source = globals().get('_FORWARDED_SOURCE') or Path(__file__).read_text(encoding='utf-8')
    return '_FORWARDED_PORTABLE_LOCK_SOURCE = ' + repr(portable_lock_source()) + '\n' + source


# Linux comm names that count as a compiler (the same set resources() lists).
COMPILER_NAMES = ('cargo', 'rustc', 'cc', 'c++', 'gcc', 'g++', 'clang', 'clang++', 'cc1', 'cc1plus', 'ninja', 'cmake')
# Down copies the files a run wrote to the evidence folder before it prunes the run's copies, except the caches Eden
# and the GPU driver rebuild: those are listed in the prune manifest. On qcf-sobs01 (Surface, 2026-11-09) they were
# 273 of the 285 MiB each instance wrote: user/cache/shader/<title>/vulkan.bin and xdg-cache/nvidia/GLCache.
REBUILT_CACHES = ('user/cache/shader', 'user/shader', 'xdg-cache')
READ_ONLY = ('user/nand/user/Contents', 'user/nand/system/Contents', 'user/firmware')
MODS = 'user/sdmc/ultimate/mods'
PLUGINS = 'user/sdmc/atmosphere/contents/01006A800016E000/romfs/skyline/plugins'
CONFIG = 'user/config/qt-config.ini'
RPC_METHODS = {'status', 'settings.set', 'settings.get', 'settings.save', 'game.load', 'game.stop', 'jobs.get'}


class Refused(RuntimeError):
    pass


class LedgerTransport(Refused):
    def __init__(self, diagnoses, message=None):
        if isinstance(diagnoses, str):
            diagnoses = [{'line': diagnoses, 'cause': 'unknown', 'transient': False, 'phase': 'unknown',
                          'evidence': ''}]
        if message:
            diagnoses = [*diagnoses[:-1], {**diagnoses[-1], 'stop': message}]
        else:
            message = diagnoses[-1].get('stop')
        self.diagnoses = diagnoses
        self.transient = bool(diagnoses[-1].get('transient', False))
        super().__init__((message + '; ' if message else '') +
                         '\n'.join(ssh_diagnosis_with_receipt(d) for d in diagnoses))


class SettleDeadline(Exception):
    pass


@contextmanager
def settle_attempt_deadline(seconds):
    require(hasattr(signal, 'setitimer') and threading.current_thread() is threading.main_thread(),
            'Settle deadline requires the main Linux process')
    previous = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signum, _frame):
        raise SettleDeadline()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(.001, seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if old_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(.001, old_timer[0] - (time.monotonic() - started)), old_timer[1])


def require(condition, message):
    if not condition:
        raise Refused(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def setting_is(value, expected):
    """Use actual CLI value/current/effective fields, never truthiness."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in ('value', 'current', 'effective') and not isinstance(child, (dict, list)):
                    found.append(child)
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        found.append(value)
    walk(value)
    acceptable = {str(expected)}
    if type(expected) is int:
        acceptable.add(str(expected) + '.0')
    return bool(found) and all(not isinstance(x, bool) and str(x) in acceptable for x in found)


def mute_is_zero(value):
    return setting_is(value, 0)


def private_config(profile, require_mute=True):
    values, section = {}, None
    for line in (Path(profile) / CONFIG).read_text().splitlines():
        if line.startswith('['):
            section = line.strip()
        if '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            if key in ('nand_directory', 'sdmc_directory') or section == '[Audio]' and key == 'volume':
                require(key not in values, 'Duplicate private config key: ' + key)
                values[key] = value.strip()
    require(Path(values.get('nand_directory', '.')) == Path(profile) / 'user/nand'
            and Path(values.get('sdmc_directory', '.')) == Path(profile) / 'user/sdmc', 'Config escapes private NAND/SDMC')
    if require_mute:
        require(values.get('volume') in ('0', '0.0'), 'Persisted [Audio] volume not zero')
    return values


def profile_directories(profile):
    """Qt persists forward slashes on Windows too; do not compare native str()."""
    return {'nand_directory': (profile / 'user/nand').as_posix(),
            'sdmc_directory': (profile / 'user/sdmc').as_posix()}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    """Atomic journal replacement; callers hold the run lock."""
    path = Path(path)
    temporary = path.with_name(path.name + '.next')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    if os.name == 'nt':
        return  # Windows has no directory fsync; file bytes were flushed before atomic replace.
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def safe_absolute(value):
    path = Path(value)
    require(path.is_absolute() and '..' not in path.parts, 'Absolute, non-traversing path required: ' + str(path))
    # Reject aliases before any recursive copy/removal, including dangling links.
    for part in (path, *path.parents):
        require(not part.is_symlink() and not getattr(part, 'is_junction', lambda: False)(),
                'Symlink ancestor refused: ' + str(part))
    return path


def pending_release_dir():
    home = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
    return safe_absolute(home / 'eden-lifecycle' / 'pending-release')


def relative(value):
    path = Path(value)
    require(bool(value) and not path.is_absolute() and '..' not in path.parts and str(path) != '.',
            'Non-traversing relative path required: ' + str(path))
    require(':' not in str(path) and not path.drive, 'Drive/alternate stream refused')
    return path


SOURCE_BUILTIN = {
    'settings': 'user/sdmc/ultimate/TrainingRooms/settings.json',
    'diag_log': 'user/sdmc/ultimate/TrainingRooms/practice-diagnostics.log',
    'detector_log': 'user/sdmc/ultimate/ssbu-desync-detector/events.log',
}
SOURCE_RESERVED = {'socket', 'profile', 'console_log', 'control_log', 'game', 'trace_jsonl', 'screenshot_target'}


def validate_sources(mapping):
    """A declared file the runner can read by key. Paths stay relative; no plugin name is implied."""
    require(isinstance(mapping, dict), 'sources must be an object')
    require(not set(mapping) & SOURCE_RESERVED, 'File source alias cannot override a transport/owned path')
    for name, spec in mapping.items():
        require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,40}', str(name)), 'Invalid source name')
        if isinstance(spec, str):
            relative(spec)
        elif isinstance(spec, dict) and set(spec) == {'beside', 'name'}:
            require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,40}', str(spec.get('beside') or '')),
                    'beside source needs a source name')
            filename = spec.get('name')
            require(isinstance(filename, str) and Path(filename).name == filename and filename not in ('.', '..'),
                    'beside source name must be a single file name')
        elif isinstance(spec, dict) and set(spec) == {'sdmc'}:
            relative(spec.get('sdmc'))
        else:
            raise Refused('source needs a relative path, beside+name, or sdmc')


def inventory(root):
    """Full byte/type/mode inventory, including empty directories. No symlinks."""
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), 'Profile/snapshot directory required')
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        name = str(path.relative_to(root))
        require(not stat.S_ISLNK(info.st_mode) and not getattr(path, 'is_junction', lambda: False)(),
                'Profile symlinks need an explicit policy: ' + str(path))
        if stat.S_ISDIR(info.st_mode):
            result[name] = {'type': 'directory', 'mode': stat.S_IMODE(info.st_mode)}
        else:
            require(stat.S_ISREG(info.st_mode), 'Special profile file refused: ' + str(path))
            result[name] = {'type': 'file', 'mode': stat.S_IMODE(info.st_mode),
                            'size': info.st_size, 'sha256': digest(path)}
    return result


def link_payload(source, target):
    """Never change a shared file's attributes. A private copy also works across volumes."""
    if os.name == 'nt' and not Path(source).stat().st_mode & stat.S_IWRITE:
        # A read-only NTFS link cannot be pruned with ordinary unlink; never clear the source attribute.
        shutil.copyfile(source, target)
        return False
    try:
        os.link(source, target)
        return True
    except FileExistsError:
        raise
    except OSError:
        shutil.copyfile(source, target)
        return False


def clone_profile(source, target, read_only=READ_ONLY + (MODS, PLUGINS)):
    """Link only immutable game content; private-copy EVERY other file."""
    source, target = safe_absolute(source), safe_absolute(target)
    require(source.is_dir() and source.name != 'a' and target.name != 'a', 'Profile a/missing source refused')
    require(source != target and source not in target.parents and target not in source.parents, 'Overlapping profiles refused')
    require(not target.exists(), 'Refuse an existing profile/snapshot: ' + str(target))
    before = inventory(source)

    def copy(src, dst):
        rel = Path(src).relative_to(source)
        immutable = any(Path(prefix) in rel.parents and
                        (not prefix.endswith('/plugins') or rel.suffix.lower() == '.nro') for prefix in read_only)
        if immutable:
            if not link_payload(src, dst):
                shutil.copystat(src, dst)
        else:
            shutil.copy2(src, dst)

    shutil.copytree(source, target, copy_function=copy)
    require(inventory(target) == before, 'Clone differs from source')
    return before


def tree_entries(root):
    """(path, lstat) for root and everything under it, each directory before its contents. Refuses links, so a
    prune never follows one out of its tree."""
    stack = [Path(root)]
    while stack:
        path = stack.pop()
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not getattr(path, 'is_junction', lambda: False)(),
                'Link inside a pruned tree: ' + str(path))
        yield path, info
        if stat.S_ISDIR(info.st_mode):
            stack.extend(path.iterdir())


def remove_tree(root, skipped, windows=os.name == 'nt'):
    """Delete root, files before their directories. A Windows read-only attribute is cleared only where no other path
    shares the data: directories (always private), and files with one link. A hard link into an installed source or
    app keeps its attributes and stays, listed in skipped with the directories above it."""
    for path, info in reversed(list(tree_entries(root))):
        directory = stat.S_ISDIR(info.st_mode)
        if directory and any(s == str(path) or s.startswith(str(path) + os.sep) for s in skipped):
            continue
        remove = path.rmdir if directory else path.unlink
        try:
            remove()
        except PermissionError:
            if not windows or not directory and info.st_nlink != 1:
                skipped.append(str(path))
                continue
            path.chmod(stat.S_IWRITE)
            remove()


def run_files(post_run, snapshot, expected):
    """The files a run created or changed in its post-run profile, against the pristine snapshot's inventory, and the
    snapshot files it removed. A file that still shares its snapshot twin's inode or modification time was not
    rewritten (clone_profile links game content and copy2 keeps file times), so only the rest are hashed."""
    post_run, snapshot = Path(post_run), Path(snapshot)
    changed, present = [], set()
    for path, info in tree_entries(post_run):
        if not stat.S_ISREG(info.st_mode):
            continue
        rel = path.relative_to(post_run)
        present.add(str(rel))
        before = expected.get(str(rel))
        if before and before['type'] == 'file' and before['size'] == info.st_size:
            twin = (snapshot / rel).lstat()
            if ((twin.st_dev, twin.st_ino) == (info.st_dev, info.st_ino) or twin.st_mtime_ns == info.st_mtime_ns
                    or digest(path) == before['sha256']):
                continue
        changed.append((rel, info))
    gone = sorted(Path(name).as_posix() for name, value in expected.items()
                  if value['type'] == 'file' and name not in present)
    return changed, gone


def active_claims(text, host):
    """Append order is authoritative; no timestamp expiry or foreign exclusions.

    Accept historical fractional ISO timestamps and owner spacing. Unknown host
    or unparseable claim records block admission rather than disappearing.
    """
    # Releases pair with their owner's open claims: same lifecycle token, else the claims on the host the release
    # names, else (no host named) all of the owner's legacy claims. Release lines often don't repeat "on <host>" ("stopped Eden and
    # Surface display :78"); requiring the same scope left six released 2026-10-30..11-02 claims looking open.
    open_claims = {}  # owner key -> [(scope, token, run, line)] in claim order
    for number, line in enumerate(text.splitlines()):
        event = re.search(r'\b(CLAIMED|RELEASED):\s*(.*)', line)
        if not event:
            continue
        body = event[2]
        owner = re.match(r'(OpenCode|Codex|Claude|Cursor)\s*([^\s]+)', body, re.I)
        owner_key = (owner[1].lower(), owner[2]) if owner else ('unknown', str(number))
        explicit = re.search(r'\bhost=(surface|yoga|ally|xps)\b', body, re.I)
        named = re.search(r'\bon\s+(surface|yoga|ally|xps)\b', body, re.I)
        scope = (explicit or named)
        scope = scope[1].lower() if scope else 'unknown'
        token = re.search(r'\blifecycle=([a-zA-Z0-9_-]+)\b', body)
        token = token[1] if token else 'legacy'
        run = re.search(r'\brun\s+([A-Za-z0-9][\w.-]*)', body)
        run = run[1] if run else None
        claims = open_claims.setdefault(owner_key, [])
        if event[1] == 'CLAIMED':
            claims.append((scope, token, run, line))
            continue
        # No run-label matching: one claim is often carried across runs (NOTE "continues with run e04") and
        # released once naming only the last run. A live run is still caught by admission's foreign-process check.
        tests = []
        if token != 'legacy':
            tests.append(lambda c: c[1] == token)
        else:
            if scope != 'unknown':
                tests.append(lambda c: c[0] in (scope, 'unknown') and c[1] == 'legacy')
            tests.append(lambda c: c[1] == 'legacy')
        # A release closes every open claim of that owner matching the first criterion that matches any: agents
        # re-post CLAIMED to extend or amend a claim and release once.
        for test in tests:
            if any(test(c) for c in claims):
                claims[:] = [c for c in claims if not test(c)]
                break
    return [line for claims in open_claims.values() for scope, _, _, line in claims
            if scope in (host.lower(), 'unknown')]


def claim_line_matches(line, suffix):
    """Match our exact claim suffix when an older claim includes its inline ports field."""
    if line.endswith(suffix):
        return True
    normalized = re.sub(r'\s+ports=\S+(?=\s)', '', line, count=1)
    return normalized.endswith(suffix)


def active_port_records(text, host, opened):
    tokens = {m[1] for line in opened if (m := re.search(r'\blifecycle=(\w+)\b', line))}
    prefix = f'PORTS: host={host} lifecycle='
    return [line for line in text.splitlines() if line.startswith(prefix)
            and line[len(prefix):].partition(' ')[0] in tokens]


def load_cap(config):
    """Return the configured game-load cap, defaulting to 300 seconds on every host."""
    return config.get('load_cap', 300)


def helper_display(display, reserved=()):
    number = int(display[1:])
    while (f':{number}' in reserved or (X_SOCKET_ROOT / f'.X{number}-lock').exists()
           or (X_SOCKET_ROOT / '.X11-unix' / f'X{number}').exists()):
        number += 1
    return f':{number}'


def packet_ports(path):
    if not path or not Path(path).is_file():
        return
    stack = [json.loads(Path(path).read_text())]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            pins = [('GDB', node.get('gdb_port'))]
            if node.get('run') == 'routes:bridge_on':
                pins.append(('BRIDGE', (node.get('args') or {}).get('port')))
            for kind, value in pins:
                if value:
                    try:
                        port = int(value)
                    except (TypeError, ValueError):
                        raise Refused(f'Invalid packet {kind} port pin: {value!r}') from None
                    require(0 < port <= 65535, f'Invalid packet {kind} port pin: {value!r}')
                    yield kind, port
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def validate_stale_claim_hours(value):
    require(type(value) in (int, float) and 0 < value < math.inf,
            'stale_claim_hours must be a finite positive number')


RESERVATION_TTL_MINUTES = 20
EXCLUSIVE_HOST_MARKER = ' exclusive_host=true'
RESERVATION = re.compile(
    r'(?P<at>\S+) CLAIMED: (?P<owner>(?:OpenCode|Codex|Claude|Cursor)[ \t]+\S+) '
    r'host=(?P<host>surface|yoga|xps) lifecycle=(?P<token>[0-9a-f]{32}) '
    r'Eden reservation run (?P<label>[A-Za-z0-9][A-Za-z0-9_-]{1,63}) '
    r'approver=(?P<approver>[A-Za-z0-9][A-Za-z0-9_./@-]*)'
    r'(?P<exclusive>' + re.escape(EXCLUSIVE_HOST_MARKER) + r')?')


def reservation_expired(record, now):
    try:
        created = datetime.fromisoformat(record['at'])
        return (now - created).total_seconds() >= RESERVATION_TTL_MINUTES * 60
    except (ValueError, TypeError, OverflowError):
        return None


def claim_admission(text, host, owner, stale_claim_hours=None, admission=None, now=None, *,
                    label=None, exclusive_host=False):
    """Opt-in admission policy only; never reconcile or release foreign claims.

    Keep active_claims' release pairing authoritative. Ambiguous ownership,
    scope or time stays blocking, even when the opt-in policy is enabled.
    """
    opened = []
    reservations, matching = [], []
    now = now if now is not None else datetime.now(timezone.utc)
    for line in active_claims(text, host):
        record = RESERVATION.fullmatch(line)
        if not record:
            (reservations if line.endswith(EXCLUSIVE_HOST_MARKER) else opened).append(line)
            continue
        expired = reservation_expired(record, now)
        if expired:
            continue
        if (expired is False and bool(record['exclusive']) == exclusive_host
                and (record['host'], record['owner'], record['label']) == (host, owner, label)):
            matching.append(record)
        else:
            reservations.append(line)
    # One matching reservation can be converted; an ambiguous duplicate stays blocking.
    if len(matching) > 1:
        reservations.extend(record[0] for record in matching)
    if exclusive_host:
        return {'blocking': reservations + opened, 'stale_claim_skipped': [],
                **({'reservation': matching[0]['token']} if len(matching) == 1 else {'reservation_required': True})}
    # Operator 2026-11-04: the Surface and the Yoga run two emulators. A claim this tool wrote for another run on the
    # same host (`host=<host> lifecycle=<token> Eden lifecycle admitted`; each run has its own profile copy) is a
    # concurrent instance, not a block, while fewer than the host's `max_eden` are open. Admission's process, memory
    # and compiler checks still run first. Manual and foreign claims block as before; the Ally's limit stays 1.
    limit = admission.get('max_eden', 1) if isinstance(admission, dict) else 1
    lifecycle_claim = re.compile(r'CLAIMED:\s*\S+(?:\s+\S+)*?\s+host=' + re.escape(host) +
                                 r'\s+lifecycle=[A-Za-z0-9_-]+\s+Eden lifecycle admitted\s*$', re.I)
    concurrent = [line for line in opened if lifecycle_claim.search(line)]
    if 0 < len(concurrent) < limit:
        opened = [line for line in opened if line not in concurrent]
    else:
        concurrent = []
    extra = {'concurrent_claims': concurrent} if concurrent else {}
    if len(matching) == 1:
        extra['reservation'] = matching[0]['token']
    if stale_claim_hours is None:
        return {'blocking': reservations + opened, 'stale_claim_skipped': [], **extra}
    validate_stale_claim_hours(stale_claim_hours)
    clean = isinstance(admission, dict) and admission.get('foreign_processes') == []
    current_owner = re.fullmatch(r'(OpenCode|Codex|Claude|Cursor)\s*(\S+)', owner, re.I)
    current_owner = (current_owner[1].lower(), current_owner[2]) if current_owner else None
    blocking, skipped = reservations, []
    for line in opened:
        # Require an entire, timezone-aware timestamp before the CLAIMED event.
        event = re.fullmatch(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?'
                             r'(?:Z|[+-]\d{2}:\d{2}))\s+CLAIMED:\s*(.*)', line)
        if not clean or not event:
            blocking.append(line)
            continue
        body = event[2]
        claimed_owner = re.match(r'(OpenCode|Codex|Claude|Cursor)\s*'
                                 r'([A-Za-z0-9][A-Za-z0-9_./:-]*)(?=\s|$)', body, re.I)
        scopes = {a.lower() or b.lower() for a, b in re.findall(
            r'\bhost=([A-Za-z0-9_.-]+)\b|\bon\s+(surface|yoga|ally|xps)\b', body, re.I)}
        if (current_owner is None or not claimed_owner
                or claimed_owner[2].lower() in ('on', 'host', 'lifecycle', 'run', 'display', 'profile')
                or (claimed_owner[1].lower(), claimed_owner[2]) == current_owner
                or scopes != {host.lower()}):
            blocking.append(line)
            continue
        try:
            claimed_at = datetime.fromisoformat(event[1].replace('Z', '+00:00'))
            age_hours = (now - claimed_at).total_seconds() / 3600
        except (ValueError, OverflowError):
            blocking.append(line)
            continue
        if age_hours > stale_claim_hours:
            skipped.append({'line': line, 'age_hours': age_hours, 'checked_at': now.isoformat()})
        else:
            blocking.append(line)
    return {'blocking': blocking, 'stale_claim_skipped': skipped, **extra}


def append_ledger(stream, lines):
    stream.seek(0, os.SEEK_END)
    stream.write('\n' + '\n\n'.join(lines) + '\n')
    stream.flush()
    os.fsync(stream.fileno())


def reservation_release(record, reason):
    return (f"{stamp()} RELEASED: {record['owner']} host={record['host']} lifecycle={record['token']} "
            f"Eden reservation {reason}")


def foreign_edens(admission):
    return [p for p in admission['foreign_processes'] if str(p.get('name', '')).lower() in EDEN_INSTANCE_NAMES]


class Ledger:
    def __init__(self, path, host, owner, token, remote=None, *, label=None, exclusive_host=False, state_dir=None):
        self.path, self.host, self.owner, self.token = str(path), host, owner, token
        self.remote, self.label = remote, label
        self.exclusive_host = exclusive_host
        self.state_dir = state_dir

    def operate(self, operation, stale_claim_hours=None, admission=None, *, approver=None):
        if globals().get('_LEDGER_BRIDGE'):
            return _LEDGER_BRIDGE(operation, self.token, stale_claim_hours, admission)
        if self.remote:
            config = {'ledger': self.path, 'host': self.host, 'owner': self.owner, 'token': self.token,
                      'stale_claim_hours': stale_claim_hours, 'admission': admission,
                      'label': self.label, 'approver': approver, 'exclusive_host': self.exclusive_host,
                      'state_dir': str(self.state_dir) if self.state_dir is not None else None}
            return ledger_forward(self.remote, operation, config)
        path = safe_absolute(self.path)
        require(path.is_file(), 'Authoritative ledger must already exist')
        # Never replace/truncate the shared ledger or normalize it on disk.
        with path.open('r+', encoding='utf-8') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            text = stream.read()
            claim_suffix = (f' CLAIMED: {self.owner} host={self.host} lifecycle={self.token} '
                            'Eden lifecycle admitted')
            release_suffix = (f' RELEASED: {self.owner} host={self.host} lifecycle={self.token} '
                              'Eden stopped; full profile restored and separately verified')
            if operation == 'read':
                opened = active_claims(text, self.host)
                return {'open': opened, 'port_records': active_port_records(text, self.host, opened),
                        'sha256': hashlib.sha256(text.encode()).hexdigest(),
                        'owned_claim': any(claim_line_matches(line, claim_suffix) or
                                           claim_line_matches(line, claim_suffix + EXCLUSIVE_HOST_MARKER)
                                           for line in opened),
                        'owned_release': any(line.endswith(release_suffix) for line in text.splitlines())}
            require(operation in ('claim', 'release', 'reserve', 'cancel'), 'Unknown ledger operation')
            now = datetime.now(timezone.utc)
            reservations = []
            if operation != 'release':
                opened = active_claims(text, self.host)
                reservations = [match for line in opened if (match := RESERVATION.fullmatch(line))]
            expired = [record for record in reservations if reservation_expired(record, now)]
            if operation == 'cancel':
                require(self.exclusive_host, 'Only exclusive_host reservations can be cancelled')
                own_reservations = [record for record in reservations
                                    if (record['token'], record['owner'], record['host'], record['label'])
                                    == (self.token, self.owner, self.host, self.label)
                                    and record['exclusive'] and reservation_expired(record, now) is False]
                require(len(own_reservations) == 1, 'Exact owned reservation not found; cancellation refused')
                line = reservation_release(own_reservations[0], 'cancelled by owner')
                append_ledger(stream, [line])
                return {'line': line, 'cancelled': True}
            if operation == 'reserve':
                converted_suffix = (f' RELEASED: {self.owner} host={self.host} lifecycle={self.token} '
                                    'Eden reservation converted to lifecycle claim')
                require(not any(line.endswith(converted_suffix) for line in text.splitlines()),
                        'Reservation already converted to a lifecycle claim')
                own_reservations = [record for record in reservations
                                    if (record['token'], record['owner'], record['host']) ==
                                    (self.token, self.owner, self.host)]
                if own_reservations:
                    require(len(own_reservations) == 1, 'Ambiguous owned reservation')
                    record = own_reservations[0]
                    if reservation_expired(record, now) is False:
                        age = (now - datetime.fromisoformat(record['at'])).total_seconds()
                        remaining = max(1, int(RESERVATION_TTL_MINUTES * 60 - age))
                        return {'line': record[0], 'reservation_token': self.token,
                                'expires_in_seconds': remaining, 'already_reserved': True}
                line = (f'{now.isoformat()} CLAIMED: {self.owner} host={self.host} lifecycle={self.token} '
                        f'Eden reservation run {self.label} approver={approver}')
                if self.exclusive_host:
                    line += EXCLUSIVE_HOST_MARKER
                require(approver is not None and self.label is not None and RESERVATION.fullmatch(line),
                        'Reservation requires a valid Linux host, owner, run label and approver')
                require(len(reservations) == len(expired), 'Host already has an open Eden reservation')
                claims = [entry for entry in opened if not RESERVATION.fullmatch(entry)]
                require(not any(entry.endswith(EXCLUSIVE_HOST_MARKER) for entry in claims),
                        'Host already has an exclusive Eden claim')
                if self.exclusive_host:
                    require(not claims, 'Exclusive host reservation requires no open Eden claim')
                    require(isinstance(admission, dict) and isinstance(admission.get('foreign_processes'), list)
                            and not foreign_edens(admission), 'Exclusive host reservation requires no running Eden')
                append_ledger(stream, [*(reservation_release(old, 'expired') for old in expired), line])
                return {'line': line, 'reservation_token': self.token, 'expires_in_seconds': RESERVATION_TTL_MINUTES * 60}
            skipped = []
            reservation = None
            if operation == 'claim':
                own = [line for line in opened
                       if claim_line_matches(line, claim_suffix)
                       or claim_line_matches(line, claim_suffix + EXCLUSIVE_HOST_MARKER)]
                if own:
                    # A resent claim must not append twice: release requires exactly one open claim.
                    require(len(own) == 1, 'Ambiguous owned lifecycle claim')
                    return {'line': own[0], 'stale_claim_skipped': [], 'already_claimed': True}
                require(not any(line.endswith(release_suffix) for line in text.splitlines()),
                        'Lifecycle token already released; a new run needs a new token')
                policy = claim_admission(text, self.host, self.owner, stale_claim_hours, admission, now,
                                         label=self.label, exclusive_host=self.exclusive_host)
                require(not policy['blocking'], 'Open host claim; no profile changes permitted')
                require(not policy.get('reservation_required'), 'Exclusive host requires a matching approved reservation')
                skipped = policy['stale_claim_skipped']
                reservation = policy.get('reservation')
            else:
                own = [line for line in active_claims(text, self.host)
                       if f'lifecycle={self.token} ' in line and f'{self.owner} ' in line]
                if not own:
                    previous = [line for line in text.splitlines() if line.endswith(release_suffix)]
                    if previous:
                        return {'line': previous[-1], 'already_released': True}
                require(len(own) == 1, 'Exact owned lifecycle claim not found; release refused')
            kind = 'CLAIMED' if operation == 'claim' else 'RELEASED'
            line = f'{stamp()} {kind}: {self.owner} host={self.host} lifecycle={self.token} '
            line += 'Eden lifecycle admitted' if operation == 'claim' else 'Eden stopped; full profile restored and separately verified'
            if operation == 'claim' and self.exclusive_host:
                line += EXCLUSIVE_HOST_MARKER
            lines = [*(reservation_release(old, 'expired') for old in expired), line]
            if operation == 'claim' and isinstance(admission, dict) and admission.get('port_allocation'):
                allocation = json.dumps(admission['port_allocation'], sort_keys=True, separators=(',', ':'))
                lines.append(f'PORTS: host={self.host} lifecycle={self.token} allocation={allocation}')
            if reservation:
                record = next(item for item in reservations
                              if (item['token'], item['owner'], item['label']) ==
                              (reservation, self.owner, self.label)
                              and not reservation_expired(item, now))
                converted = reservation_release(record, 'converted to lifecycle claim')
                if reservation == self.token:
                    lines.insert(-1, converted)
                else:
                    lines.append(converted)
            append_ledger(stream, lines)
            receipt = {'line': line, 'stale_claim_skipped': skipped}
            if operation == 'claim' and isinstance(admission, dict) and admission.get('port_allocation'):
                receipt['port_allocation'] = admission['port_allocation']
            if reservation:
                receipt['consumed_reservation'] = reservation
            return receipt

    def claim(self, stale_claim_hours=None, admission=None):
        return self.operate('claim', stale_claim_hours, admission)

    def release(self):
        return self.operate('release')

    def inspect(self):
        return self.operate('read')


# The XPS (ubuntu-xps, 2026-11-06) is a Linux executor like the Surface and Yoga, using the Yoga ledger
# through ledger_ssh. It runs two Eden instances, like them (operator 2026-11-06). Timed rows stay on the
# Surface and Yoga. Every XPS Eden boots at 0.5x unless eden_settings overrides core.resolution_setup.
HOSTS = ('surface', 'yoga', 'xps', 'ally')
LINUX_MAX_EDEN = {'surface': 2, 'yoga': 2, 'xps': 2}
# Res1_2X (settings_enums.h ResolutionSetup). Applied before game.load.
HOST_EDEN_SETTINGS = {'xps': {'core.resolution_setup': 1}}


def validate_eden_settings(settings, where):
    require(isinstance(settings, dict), where + ' eden_settings must be an object')
    for key, value in settings.items():
        require(key == 'core.resolution_setup', where + ' eden_settings only overrides core.resolution_setup')
        require(type(value) is int and value >= 0, where + ' core.resolution_setup must be a non-negative integer')


def qt_value(path, name):
    """The last plain `name=` value in a Qt ini (`name\\default=` lines are separate keys), or None."""
    found = None
    for line in Path(path).read_text(errors='replace').splitlines():
        key, equal, value = line.partition('=')
        if equal and key.strip() == name:
            found = value.strip()
    return found


def effective_qt_config(item):
    """The Qt config an instance boots with: its pinned fixture (applied after prepare), else the source's."""
    pins = [pin for pin in item.get('files', []) if Path(pin.get('to', '')) == Path(CONFIG)]
    return Path(pins[-1]['path']) if pins else Path(item['source']) / CONFIG


def eden_boot_settings(config, item):
    """Host defaults, then the run, then the instance. An explicit key wins."""
    values = dict(HOST_EDEN_SETTINGS.get(config.get('host'), {}))
    for source in (config.get('eden_settings'), item.get('eden_settings')):
        if source:
            values.update(source)
    return values


def validate(config):
    require(config.get('host') in HOSTS, 'host must be surface, yoga, xps or ally')
    windows = config['host'] == 'ally'
    if 'stale_claim_hours' in config:
        validate_stale_claim_hours(config['stale_claim_hours'])
    require(isinstance(config.get('hostname'), str) and re.fullmatch(r'[A-Za-z0-9_.-]+', config['hostname']),
            'Explicit executor kernel hostname required; SSH aliases are not host identity')
    require(isinstance(config.get('owner'), str) and re.fullmatch(r'(OpenCode|Codex|Claude|Cursor)\s+\S+', config['owner']),
            'owner must be Engine session/stream (no newline or free-form suffix)')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{1,63}', config.get('label', '')), 'Invalid label')
    if 'notify' in config:
        notify = config['notify']
        require(isinstance(notify, dict) and set(notify) <= {'file', 'ssh', 'from', 'to'},
                'notify must be an object with file, ssh, from and to only')
        path = notify.get('file')
        if windows:
            valid_path = (isinstance(path, str) and re.fullmatch(r'[A-Za-z]:\\[A-Za-z0-9_.@+ \\-]+', path)
                          and PureWindowsPath(path).is_absolute() and '..' not in PureWindowsPath(path).parts)
        else:
            valid_path = (isinstance(path, str) and re.fullmatch(r'/[A-Za-z0-9_./@+ -]+', path)
                          and '..' not in Path(path).parts)
        require(valid_path, 'notify file must be an absolute safe path')
        if 'ssh' in notify:
            require(isinstance(notify['ssh'], str)
                    and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', notify['ssh']), 'Unsafe notify SSH host')
        for key in ('from', 'to'):
            if key in notify:
                require(isinstance(notify[key], str) and notify[key]
                        and not re.search(r'[\r\n]', notify[key]), 'Invalid notify ' + key)
    root, state_dir = safe_absolute(config['work_root']), safe_absolute(config['state_dir'])
    require(len(root.parts) >= 4 and len(state_dir.parts) >= 4 and root != state_dir, 'Dedicated work_root and state_dir required')
    require(root not in state_dir.parents and state_dir not in root.parents, 'State and profile roots must be disjoint')
    if not (windows and config.get('ledger_ssh')):
        safe_absolute(config['ledger'])
    require(not config.get('lifecycle_only') or windows and config['lifecycle_only'] is True,
            'lifecycle_only is the explicit Windows no-row proof mode')
    for pinned in (() if config.get('lifecycle_only') else (config['runner'], config['rows'])):
        safe_absolute(pinned['path'])
        require(re.fullmatch(r'[0-9a-f]{64}', pinned.get('sha256', '')), 'Runner/rows SHA256 required')
    if config.get('display_helper'):
        require(not windows, 'Windows has no Linux display helper')
        safe_absolute(config['display_helper']['path'])
        require(re.fullmatch(r'[0-9a-f]{64}', config['display_helper'].get('sha256', '')), 'Display helper SHA256 required')
    require(1 <= len(config['instances']) <= 2, 'One or an explicitly authorized pair only')
    require(len(config['instances']) == 1 or config.get('pair_authorized') is True, 'Pair exception needs pair_authorized=true')
    if 'exclusive_host' in config:
        require(type(config['exclusive_host']) is bool, 'exclusive_host must be true or false')
        require(not config['exclusive_host'] or (not windows and len(config['instances']) == 1),
                'exclusive_host requires a single Eden on a Linux host')
    if 'eden_settings' in config:
        validate_eden_settings(config['eden_settings'], 'run')
    displays, keys = set(), set()
    for item in config['instances']:
        require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,20}', item['key']) and item['key'] != 'a-profile', 'Invalid instance key')
        require(item['key'] not in keys, 'Duplicate instance key')
        keys.add(item['key'])
        if not windows:
            require(re.fullmatch(r':\d{1,3}', item['display']) and item['display'] != ':61', 'Forbidden display')
            require(item['display'] not in displays, 'Duplicate display')
            displays.add(item['display'])
        source = safe_absolute(item['source'])
        require(source.name != 'a' and root != source and root not in source.parents and source not in root.parents,
                'Source profile is protected/overlaps work root')
        safe_absolute(item['game'])
        for pinned in [item['app'], *item['plugins'], *item.get('files', [])]:
            safe_absolute(pinned['path'])
            require(re.fullmatch(r'[0-9a-f]{64}', pinned.get('sha256', '')), 'Input SHA256 required')
        for pinned in item.get('files', []):
            relative(pinned['to'])
            require(not any(Path(pinned['to']) == Path(p) or Path(p) in Path(pinned['to']).parents for p in READ_ONLY),
                    'No writes into shared immutable content')
        names = [pin.get('name', Path(pin['path']).name) for pin in item['plugins']]
        require(len(names) == len(set(names)) and all(Path(name).name == name and name.endswith('.nro') for name in names),
                'Unique basename .nro plugin names required')
        if 'trace' in item:
            # Persistent guest tracing from boot (Eden's --trace-config; docs/GUEST_TRACEPOINT.md).
            trace = item['trace']
            require(not windows, 'trace: launch-time tracing is wired for Linux hosts only')
            require(isinstance(trace, dict) and isinstance(trace.get('points'), list) and 0 < len(trace['points']) <= 256,
                    'trace needs 1..256 points')
            for symbols in trace.get('symbols', []):
                require(isinstance(symbols.get('module'), str) and symbols['module'], 'trace symbols need a module')
                safe_absolute(symbols['elf'])
            require(trace.get('unmapped_access', 'log') in ('log', 'off'), 'trace unmapped_access must be log or off')
        if 'sources' in item:
            validate_sources(item['sources'])
        if 'eden_settings' in item:
            validate_eden_settings(item['eden_settings'], 'instance ' + item['key'])
    for key, default in (('socket_cap', 90), ('load_cap', load_cap(config)), ('stop_cap', 30),
                         ('force_cap', 30), ('runtime', 5400)):
        value = config.get(key, default)
        require(isinstance(value, (int, float)) and math.isfinite(value) and 0 < value <= (10800 if key == 'runtime' else 300),
                'Invalid bounded ' + key)
    if 'expected_game_version' in config:
        require(isinstance(config['expected_game_version'], str) and config['expected_game_version'],
                'expected_game_version must be a non-empty string')
    if 'playbook' in config:
        book = config['playbook']
        require(isinstance(book, dict) and isinstance(book.get('root'), str) and isinstance(book.get('commit'), str)
                and book['root'] and book['commit'], 'playbook needs root and commit strings')
    for key in ('memory_floor_bytes', 'disk_floor_bytes', 'compile_reserve_bytes'):
        if key in config:
            require(type(config[key]) is int and config[key] > 0, key + ' must be a positive integer')
    if 'perf_sensitive' in config:
        require(type(config['perf_sensitive']) is bool, 'perf_sensitive must be true or false')
    if 'lan_online_pair' in config:
        require(type(config['lan_online_pair']) is bool, 'lan_online_pair must be true or false')
    if 'watchdog' in config:
        spec = config['watchdog']
        require(isinstance(spec, dict), 'watchdog must be an object')
        IdleWatchdog(spec)
        for detector in spec.get('detectors') or []:
            require(isinstance(detector, dict) and detector.get('kind') in DEMO_DETECTORS,
                    'unknown watchdog detector: ' + str(detector.get('kind') if isinstance(detector, dict) else detector))
            if detector['kind'] == 'new-files':
                require(isinstance(detector.get('directory'), str) and detector['directory'],
                        'new-files detector needs a directory')
            elif detector['kind'] == 'log-line':
                require(isinstance(detector.get('pattern'), str) and detector['pattern'],
                        'log-line detector needs a pattern')
            elif detector['kind'] == 'screen-class':
                require(isinstance(detector.get('demo_states'), list) and detector['demo_states'],
                        'screen-class detector needs demo_states')
            elif detector['kind'] == 'hook':
                require(isinstance(detector.get('module'), str) and isinstance(detector.get('function'), str),
                        'hook detector needs module and function')
        if 'auto' in spec:
            require(type(spec['auto']) is bool, 'watchdog.auto must be true or false')
        if 'poll_s' in spec:
            pause = spec['poll_s']
            require(isinstance(pause, (int, float)) and not isinstance(pause, bool) and 0 <= pause <= 300,
                    'watchdog.poll_s must be a number from 0 to 300')
    if 'sources' in config:
        validate_sources(config['sources'])
    if windows:
        require(type(config.get('interactive_session')) is int and config['interactive_session'] > 0,
                'Windows interactive_session required')
        for pin in (config['windows_job'], config['windows_control'], config['windows_python']):
            safe_absolute(pin['path'])
            require(re.fullmatch(r'[0-9a-f]{64}', pin.get('sha256', '')), 'Windows helper pins required')
    return config


@contextmanager
def run_lock(stream):
    if fcntl is not None:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    else:
        import msvcrt
        stream.seek(0)
        if not stream.read(1):
            stream.write('0')
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def stat_nice(text):
    """The nice value from a /proc/<pid>/stat line: proc(5) field 19. The comm in parentheses may hold spaces or a ")",
    so the fields are counted after the last ")": field 3 (state) is the first of them."""
    return int(text.rsplit(')', 1)[1].split()[16])


class Host:
    # Linux rigs (Surface, Yoga, XPS: 32 GB): two Eden instances at once, each under its own 8 GiB unit (operator
    # 2026-11-04; the XPS 2026-11-06).
    max_eden = 2
    """Real Linux executor. Tests replace this class; no shell-string commands."""
    clock = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)
    name = staticmethod(socket.gethostname)

    def run(self, argv, cap=30):
        done = subprocess.run([str(x) for x in argv], capture_output=True, text=True, timeout=cap)
        require(done.returncode == 0, f'Command failed {argv[0]}: {done.returncode}: {done.stderr[-1000:]}')
        return done.stdout

    def resources(self, disk_path, owned_units=()):
        memory = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                      if line.startswith('MemAvailable:'))
        processes = []
        for entry in Path('/proc').glob('[0-9]*'):
            try:
                comm = (entry / 'comm').read_text().strip().lower()
                if comm.startswith(('eden', 'yuzu')) or comm in COMPILER_NAMES:
                    groups = (entry / 'cgroup').read_text()
                    if not any(unit in groups for unit in owned_units):
                        process = {'pid': int(entry.name), 'name': comm}
                        if comm in COMPILER_NAMES:
                            process['nice'] = stat_nice((entry / 'stat').read_text())
                        processes.append(process)
            except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
                pass
        return {'mem_available': memory, 'disk_free': shutil.disk_usage(disk_path).free, 'foreign_processes': processes}

    def display_ready(self, item, config):
        """A declared display is ready when its socket exists, or is free for a helper to create."""
        display = item.get('display')
        if not display:
            return {'declared': False, 'ready': True, 'detail': 'no display declared'}
        socket_path = Path('/tmp/.X11-unix/X' + display[1:])
        helper = bool(config.get('display_helper'))
        try:
            exists = socket_path.exists() and not socket_path.is_symlink()
        except OSError as error:
            return {'declared': True, 'ready': False, 'helper': helper, 'socket': str(socket_path),
                    'detail': f'display socket unreadable: {error}'}
        if helper:
            ready = not exists
            detail = 'display socket free for the helper' if ready else 'display socket already in use: ' + str(socket_path)
        else:
            ready = exists
            detail = 'display socket present: ' + str(socket_path) if ready else 'display socket missing: ' + str(socket_path)
        return {'declared': True, 'ready': ready, 'helper': helper, 'socket': str(socket_path), 'detail': detail}

    def identity(self, pid):
        try:
            raw = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            if raw[0] == 'Z':
                return None
            return {'pid': int(pid), 'start': raw[19],
                    'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
        except (FileNotFoundError, ProcessLookupError):
            return None

    def unit(self, name):
        done = subprocess.run(['systemctl', '--user', 'show', name,
            '--property=LoadState,MainPID,ControlGroup,ActiveState,MemoryMax,MemorySwapMax,TasksMax,RuntimeMaxUSec,MemoryPeak,KillMode,KillSignal,FinalKillSignal'],
            capture_output=True, text=True, timeout=15)
        info = dict(line.split('=', 1) for line in done.stdout.splitlines() if '=' in line)
        require(done.returncode == 0 or info.get('LoadState') == 'not-found', 'Cannot inspect exact owned unit')
        return info

    def owned(self, item):
        info = self.unit(item['unit'])
        group = info.get('ControlGroup', '')
        require(not group or item['unit'] in Path(group).parts, 'Unexpected unit cgroup identity')
        pids = []
        if group:
            directory = Path('/sys/fs/cgroup') / group.lstrip('/')
            for path in [directory / 'cgroup.procs', *directory.rglob('cgroup.procs')]:
                if path.is_file():
                    pids.extend(int(p) for p in path.read_text().split())
        if int(info.get('MainPID') or 0):
            pids.append(int(info['MainPID']))
        return [identity for pid in sorted(set(pids)) if (identity := self.identity(pid)) is not None]

    def start_display(self, item, config):
        helper = config.get('display_helper')
        if not helper:
            return {}  # explicitly supplied existing display; never stopped by us
        require(digest(helper['path']) == helper['sha256'], 'Display helper changed')
        display_socket = Path('/tmp/.X11-unix/X' + item['display'][1:])
        require(not display_socket.exists(), 'Display already in use; do not adopt/stop it')
        stem = item['display_units'][0].removesuffix('.service')
        self.run(['/usr/bin/pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-File', helper['path'],
                  '-Action', 'Start', '-Unit', stem, '-Socket', stem, '-XDisplay', item['display'],
                  '-MaxRuntimeSeconds', str(int(config.get('runtime', 5400)))], cap=60)
        return {unit: self.owned({'unit': unit}) for unit in item['display_units']}

    def metrics(self, item):
        info = self.unit(item['unit'])
        value = info.get('MemoryPeak', '')
        return {'memory_peak': int(value) if value.isdecimal() else None,
                'limits': {k: info.get(k) for k in ('MemoryMax', 'MemorySwapMax', 'TasksMax', 'RuntimeMaxUSec',
                                                  'KillMode', 'KillSignal', 'FinalKillSignal')}}

    def launch(self, item, config):
        require(not Path('/tmp/.X11-unix/X' + item['display'][1:]).is_symlink(), 'Display socket alias refused')
        require(Path('/tmp/.X11-unix/X' + item['display'][1:]).exists(),
                'Display must be provisioned explicitly before up: ' + item['display'])
        profile, run = Path(item['profile']), Path(item['run'])
        run.mkdir(parents=True, exist_ok=True)
        env = {'DISPLAY': item['display'], 'WAYLAND_DISPLAY': '', 'XAUTHORITY': '', 'QT_QPA_PLATFORM': 'xcb',
               'XDG_DATA_HOME': str(profile / 'xdg-data'), 'XDG_CONFIG_HOME': str(profile / 'xdg-config'),
               'XDG_CACHE_HOME': str(profile / 'xdg-cache'),
               # Without a filter Eden logs at Debug: lr-04 wrote 1 GB of console in 6 minutes (2026-11-03).
               'EDEN_LOG_FILTER': config.get('log_filter', DEFAULT_LOG_FILTER)}
        # Eden needs a sanitized environment: no inherited LAN/runtime/seed overrides.
        command = ['/usr/bin/env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin',
                   'HOME=' + str(Path.home()), 'XDG_RUNTIME_DIR=' + os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')]
        command += [key + '=' + value for key, value in env.items()]
        command += [item['app']['path'], '--control-socket', item['socket'],
                    '--control-log', str(run / 'control.jsonl'), '--control-verbose', '--control-background']
        if item.get('trace'):
            # Points armed before boot resolve when their module loads, so plugin start-up is observed.
            trace = item['trace']
            trace_config = {'run_directory': str(run), 'label': item['key'],
                            'unmapped_access': trace.get('unmapped_access', 'log'),
                            'symbols': trace.get('symbols', []), 'points': trace['points']}
            save(run / 'trace.json', trace_config)
            command += ['--trace-config', str(run / 'trace.json')]
        self.run(['systemd-run', '--user', '--quiet', '--service-type=exec', '--unit=' + item['unit'],
                  '--property=MemoryMax=8G', '--property=MemorySwapMax=0', '--property=TasksMax=256',
                  '--property=RuntimeMaxSec=' + str(config.get('runtime', 5400)), '--property=KillMode=mixed',
                  '--property=KillSignal=SIGTERM', '--property=FinalKillSignal=SIGKILL',
                  '--property=TimeoutStopSec=10', '--property=StandardOutput=append:' + item['console_log'],
                  '--property=StandardError=append:' + item['console_log'], '--working-directory=' + str(profile),
                  '--', *command])
        info = self.unit(item['unit'])
        require(info.get('ActiveState') == 'active' and info.get('MemoryMax') == str(8 * GIB)
                and info.get('MemorySwapMax') == '0' and info.get('TasksMax') == '256'
                and info.get('KillMode') == 'mixed' and info.get('KillSignal') == '15'
                and info.get('FinalKillSignal') == '9', 'Actual cgroup limits/stop policy differ')
        identities = self.owned(item)
        require(identities, 'No owned Eden PID recorded')
        return identities

    def socket_ready(self, item):
        """A leftover socket file is not readiness. The deadline retries until status answers."""
        try:
            reply = self.call(item, 'status', {}, 2)
        except (Refused, OSError, subprocess.TimeoutExpired):
            return False
        return isinstance(reply, dict) and reply.get('ok') is True

    def call(self, item, method, params, timeout):
        require(method in RPC_METHODS, 'Lifecycle cannot send input/navigation/trace calls')
        tools = str(Path(item['app']['path']).parent / 'tools')
        # Same supported Eden CLI client as the shared runner. Lazy: import is inert.
        path = Path(tools) / 'edenctl.py'
        spec = importlib.util.spec_from_file_location('_lifecycle_edenctl', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Client(item['socket'], timeout=timeout).request(method, params)

    def signal_identity(self, identity, sig):
        fd = os.pidfd_open(identity['pid'])
        try:
            # Recheck after pidfd_open, which pins the process against reuse.
            require(self.identity(identity['pid']) == identity, 'Owned PID changed before force stop')
            signal.pidfd_send_signal(fd, sig)
        finally:
            os.close(fd)

    def stop_main(self, item, cap):
        """Keep AppImage FUSE/helper processes alive until the actual main PID is gone."""
        info = self.unit(item['unit'])
        pid = int(info.get('MainPID') or 0)
        if not pid:
            require(info.get('ActiveState') not in ('activating', 'reloading'),
                    'Main PID not settled; no cgroup stop')
            return {'main_pid_stopped': None, 'main_stop_signals': []}
        identity = self.identity(pid)
        if identity is None:
            return {'main_pid_stopped': None, 'main_stop_signals': []}
        require(identity in self.owned(item), 'Main PID not in owned cgroup; no cgroup stop')
        require(not item.get('owned_pids') or identity in item['owned_pids'],
                'Main PID differs from journal; no cgroup stop')
        deadline = self.clock() + cap
        sent = []
        for sig, end in ((signal.SIGTERM, self.clock() + cap / 2), (signal.SIGKILL, deadline)):
            if self.identity(pid) != identity:
                break
            self.signal_identity(identity, sig)
            sent.append(sig.name)
            while self.clock() < end and self.identity(pid) == identity:
                self.sleep(max(0, min(.1, end - self.clock())))
        require(self.identity(pid) != identity, 'Owned main PID remains; no cgroup stop/restore/release')
        return {'main_pid_stopped': identity, 'main_stop_signals': sent}

    def force_stop(self, item, cap):
        # Unique journalled unit/cgroup only. Never process-name matching or pkill.
        deadline = self.clock() + cap
        before = list({(x['pid'], x['start'], x['boot']): x
                        for x in [*item.get('owned_pids', []), *self.owned(item)]}.values())
        # A group-wide SIGTERM kills the AppImage FUSE daemon while Eden still maps
        # its files (lr-09: main SIGBUS plus Go companion fault). Even on timeout,
        # escalate ONLY the main PID and confirm its death before touching the group.
        main_stop = self.stop_main(item, cap / 2) if item.get('app') else {}
        try:
            self.run(['systemctl', '--user', 'stop', item['unit']], cap=max(.001, deadline - self.clock()))
        except (Refused, subprocess.TimeoutExpired):
            pass
        for identity in self.owned(item):
            if self.identity(identity['pid']) == identity:
                self.signal_identity(identity, signal.SIGKILL)
        while True:
            if not self.owned(item) and all(self.identity(x['pid']) != x for x in before):
                return {'owned_pids_stopped': before, **main_stop}
            if self.clock() >= deadline:
                break
            self.sleep(max(0, min(.1, deadline - self.clock())))
        raise Refused('Owned Eden processes remain; do not restore/release')

    def start_watchdog(self, unit, command, directory, runtime):
        require(re.fullmatch(r'eden-life-[0-9a-f]{32}-watchdog\.service', unit), 'Foreign watchdog unit refused')
        self.run(['systemd-run', '--user', '--quiet', '--collect', '--service-type=exec', '--unit=' + unit,
                  '--property=MemoryMax=256M', '--property=MemorySwapMax=0',
                  '--property=RuntimeMaxSec=' + str(int(runtime) + 60),
                  '--setenv=EDEN_WATCHDOG_SIDECAR=1', '--working-directory=' + str(directory), '--', *command])

    def start_settle(self, unit, command, directory, runtime):
        require(re.fullmatch(r'eden-settle-[0-9a-f]{8}\.service', unit), 'Foreign settle unit refused')
        argv = ['systemd-run', '--user', '--quiet', '--collect', '--service-type=exec', '--unit=' + unit,
                '--property=MemoryMax=256M', '--property=MemorySwapMax=0', '--property=Nice=19',
                '--property=RuntimeMaxSec=' + str(int(runtime) + 60), '--working-directory=' + str(directory)]
        if os.environ.get('XDG_STATE_HOME'):
            argv.append('--setenv=XDG_STATE_HOME=' + os.environ['XDG_STATE_HOME'])
        self.run([*argv, '--', *command])

    def stop_watchdog(self, unit):
        require(re.fullmatch(r'eden-life-[0-9a-f]{32}-watchdog\.service', unit), 'Foreign watchdog unit refused')
        done = subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True, text=True, timeout=15)
        detail = (done.stderr or '') + (done.stdout or '')
        require(done.returncode == 0 or 'not loaded' in detail.lower() or 'not-found' in detail.lower(),
                'Watchdog stop failed: ' + detail[-300:])

    def stop_settle(self, unit):
        require(re.fullmatch(r'eden-settle-[0-9a-f]{8}\.service', unit), 'Foreign settle unit refused')
        done = subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True, text=True, timeout=15)
        detail = (done.stderr or '') + (done.stdout or '')
        require(done.returncode == 0 or 'not loaded' in detail.lower() or 'not-found' in detail.lower(),
                'Settle stop failed: ' + detail[-300:])

    def settle_running(self, unit):
        require(re.fullmatch(r'eden-settle-[0-9a-f]{8}\.service', unit), 'Foreign settle unit refused')
        return self.unit(unit).get('ActiveState') in ('active', 'activating', 'reloading', 'deactivating')

    def watchdog_running(self, unit):
        return self.unit(unit).get('ActiveState') == 'active'


class WindowsHost(Host):
    """Native Ally executor: interactive task + session-scoped, kill-on-close Job.

    Reuses the pinned, reviewed winjob API, not its installed-profile controllers.
    All RPCs use the pinned asynchronous PowerShell named-pipe client.
    """
    max_eden = 1  # the Ally (18.7 GB, 23 GB commit limit) runs one Eden at a time

    def __init__(self, config, job_api=None):
        self.config, self._job, self.peaks = config, job_api, {}

    def ps(self, script, cap=30):
        encoded = base64.b64encode(script.encode('utf-16-le')).decode()
        return self.run([r'C:\Program Files\PowerShell\7\pwsh.exe', '-NoLogo', '-NoProfile',
                         '-NonInteractive', '-EncodedCommand', encoded], cap)

    @staticmethod
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    @property
    def job(self):
        if self._job is None:
            pin = self.config['windows_job']
            require(digest(pin['path']) == pin['sha256'], 'Windows Job helper changed')
            spec = importlib.util.spec_from_file_location('_eden_lifecycle_winjob', pin['path'])
            api = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(api)
            # The old helper omitted HANDLE prototypes. Pin full-width handles on x64.
            import ctypes
            from ctypes import wintypes as w
            original = api._k32
            def typed():
                k = original()
                definitions = {
                    'CreateJobObjectW': (w.HANDLE, [ctypes.c_void_p, w.LPCWSTR]),
                    'OpenJobObjectW': (w.HANDLE, [w.DWORD, w.BOOL, w.LPCWSTR]),
                    'OpenProcess': (w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
                    'CloseHandle': (w.BOOL, [w.HANDLE]),
                    'SetInformationJobObject': (w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]),
                    'QueryInformationJobObject': (w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]),
                    'AssignProcessToJobObject': (w.BOOL, [w.HANDLE, w.HANDLE]),
                    'TerminateJobObject': (w.BOOL, [w.HANDLE, w.UINT]),
                    'GetExitCodeProcess': (w.BOOL, [w.HANDLE, ctypes.c_void_p]),
                }
                for name, (result, args) in definitions.items():
                    fn = getattr(k, name)
                    fn.restype, fn.argtypes = result, args
                return k
            api._k32 = typed
            original_limits = api.limits
            def limits(handle):
                import ctypes
                record = original_limits(handle)
                info = api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                returned = ctypes.c_ulong()
                api._check(api._k32().QueryInformationJobObject(handle, 9, ctypes.byref(info),
                           ctypes.sizeof(info), ctypes.byref(returned)), 'Query Job process cap')
                record['ActiveProcessLimit'] = int(info.BasicLimitInformation.ActiveProcessLimit)
                return record
            api.limits = limits
            self._job = api
        return self._job

    def identity(self, pid):
        import ctypes
        from ctypes import wintypes as w
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.GetProcessTimes.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.OpenProcess(0x1000, False, int(pid))
        if not handle:
            require(ctypes.get_last_error() in (87, 1168), 'Cannot inspect owned Windows PID')
            return None
        try:
            code = w.DWORD()
            require(k.GetExitCodeProcess(handle, ctypes.byref(code)), 'Cannot inspect Windows exit state')
            if code.value != 259:
                return None
            times = [ctypes.c_ulonglong() for _ in range(4)]
            require(k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)), 'Cannot inspect PID creation time')
            path, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            require(k.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)), 'Cannot inspect PID image')
            return {'pid': int(pid), 'start': str(times[0].value), 'image': path.value,
                    'session': self.job.session_of(pid)}
        finally:
            k.CloseHandle(handle)

    def open_job(self, unit, access=4):
        require(re.fullmatch(r'eden-life-[0-9a-f]{32}-[A-Za-z][A-Za-z0-9_-]{0,20}\.service', unit),
                'Foreign Windows Job name refused')
        return self.job.open_job(f'Session\\{self.config["interactive_session"]}\\{unit}', access)

    def owned(self, item):
        handle = self.open_job(item['unit'])
        if handle is None:
            return []
        try:
            return [identity for pid in self.job.pids(handle) if (identity := self.identity(pid)) is not None]
        finally:
            self.job.close(handle)

    def resources(self, disk_path, owned_units=()):
        owned = {p['pid'] for unit in owned_units for p in self.owned({'unit': unit})}
        raw = self.ps("$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process | "
                      "Select-Object ProcessId,Name) | ConvertTo-Json -Compress", cap=20)
        processes = json.loads(raw)
        if isinstance(processes, dict):
            processes = [processes]
        names = {'eden.exe', 'eden-cli.exe', 'cargo.exe', 'rustc.exe', 'cl.exe', 'link.exe',
                 'ninja.exe', 'cmake.exe', 'msbuild.exe', 'clang.exe', 'clang++.exe',
                 'gcc.exe', 'g++.exe', 'wsl.exe', 'wslhost.exe', 'vmmemwsl'}
        conflicts = [{'pid': p['ProcessId'], 'name': p['Name']} for p in processes
                     if p['Name'].lower() in names and p['ProcessId'] not in owned]
        return {'mem_available': self.job.system_memory()['available_bytes'],
                'disk_free': shutil.disk_usage(disk_path).free, 'foreign_processes': conflicts}

    def start_display(self, item, config):
        return {}  # Task principal explicitly binds the interactive desktop; no display adoption.

    def clone_source(self, original, profile):
        source = safe_absolute(original['source'])
        profile.mkdir()
        clone_profile(source, profile / 'user', tuple(p.removeprefix('user/') for p in (*READ_ONLY, MODS, PLUGINS)))
        app = safe_absolute(original['app']['path']).parent
        inventory(app)  # Reject reparse points/special files before linking read-only runtime files.
        shutil.copytree(app, profile, dirs_exist_ok=True, copy_function=os.link,
                        ignore=lambda directory, names: ['user'] if Path(directory) == app and 'user' in names else [])
        require(digest(profile / 'eden.exe') == original['app']['sha256'], 'Private Eden binary differs')
        save(profile.parent.parent / ('source-' + original['key'] + '.json'), inventory(source))

    def metrics(self, item):
        handle = self.open_job(item['unit'])
        try:
            limits = self.job.limits(handle) if handle else {}
            peak = limits.get('PeakJobMemoryUsed', self.peaks.get(item['unit']))
            self.peaks[item['unit']] = peak
            return {'memory_peak': peak, 'memory_limit_kind': 'job_commit', 'limits': limits}
        finally:
            if handle:
                self.job.close(handle)

    def task_name(self, item):
        self.open_job_name_check(item['unit'])
        return 'EdenLife-' + item['unit'].removeprefix('eden-life-').removesuffix('.service')

    @staticmethod
    def open_job_name_check(unit):
        require(re.fullmatch(r'eden-life-[0-9a-f]{32}-[A-Za-z][A-Za-z0-9_-]{0,20}\.service', unit),
                'Foreign Windows task refused')

    def launch(self, item, config):
        run = Path(item['run'])
        run.mkdir(parents=True, exist_ok=True)
        for pin in (config['windows_control'], config['windows_python']):
            require(digest(pin['path']) == pin['sha256'], 'Windows helper changed')
        exe = Path(item['profile']) / 'eden.exe'
        # No prompt interaction. Only an explicitly authorized inbound Block may be added.
        rule = self.task_name(item)
        script = ("$ErrorActionPreference='Stop'; $exe=" + self.quote(exe) + '; '
                  "$rules=@(Get-NetFirewallApplicationFilter | Where-Object {$_.Program -ieq $exe} | "
                  "Get-NetFirewallRule | Where-Object {$_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block'}); "
                  "$created=$false; "
                  "if($rules.Count -eq 0){" +
                  ("New-NetFirewallRule -Name " + self.quote(rule) + ' -DisplayName ' + self.quote(rule) +
                   " -Direction Inbound -Action Block -Program $exe | Out-Null; $created=$true; "
                   "$rules=@(Get-NetFirewallRule -Name " + self.quote(rule) + '); '
                   if config.get('allow_inbound_block') is True else "throw 'Explicit inbound Block policy required'; ") +
                  "}; @{program=$exe;rules=@($rules.Name);created=$created;utc=[DateTimeOffset]::UtcNow.ToString('o')}|ConvertTo-Json -Compress")
        save(run / 'firewall.json', json.loads(self.ps(script)))
        backend = run / 'lifecycle-backend.py'
        backend.write_text(standalone_source(), encoding='utf-8')
        save(run / 'launch.json', {'config': config, 'item': item})
        from xml.sax.saxutils import escape
        sid = self.ps('[Security.Principal.WindowsIdentity]::GetCurrent().User.Value').strip()
        require(re.fullmatch(r'S-\d-(?:\d+-)*\d+', sid), 'Cannot bind task to current Windows user SID')
        action = subprocess.list2cmdline([str(backend), '_windows-launch', str(run / 'launch.json')])
        xml = ('<?xml version="1.0" encoding="UTF-16"?><Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
               '<Triggers><TimeTrigger><StartBoundary>2099-01-01T00:00:00</StartBoundary><Enabled>false</Enabled></TimeTrigger></Triggers>'
               '<Principals><Principal id="Author"><UserId>' + escape(sid) +
               '</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>'
               '<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
               '<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT' + str(int(config.get('runtime', 5400)) + 30) +
               'S</ExecutionTimeLimit></Settings><Actions Context="Author"><Exec><Command>' + escape(config['windows_python']['path']) +
               '</Command><Arguments>' + escape(action) + '</Arguments><WorkingDirectory>' + escape(str(run)) + '</WorkingDirectory></Exec></Actions></Task>')
        task_xml = run / 'task.xml'
        task_xml.write_text(xml, encoding='utf-16')
        task = self.task_name(item)
        self.run(['schtasks.exe', '/create', '/f', '/tn', task, '/xml', task_xml])
        self.run(['schtasks.exe', '/run', '/tn', task])
        end = self.clock() + config.get('socket_cap', 90)
        while self.clock() < end:
            if (run / 'launcher.json').is_file():
                launch = json.loads((run / 'launcher.json').read_text())
                require(not launch.get('error'), 'Windows launcher failed: ' + str(launch))
                if launch.get('owned_pids'):
                    require(launch['session'] == config['interactive_session'], 'Wrong interactive session')
                    limits = launch['limits']
                    require(limits['JobMemoryLimit'] == 8 * GIB and limits['KillOnJobClose']
                            and limits['ActiveProcessLimit'] == 256, 'Job limits differ')
                    return launch['owned_pids']
            self.sleep(.2)
        raise Refused('Windows task did not publish a bounded launcher receipt')

    def socket_ready(self, item):
        try:
            return self.call(item, 'status', {}, 2).get('ok') is True
        except (Refused, OSError, subprocess.TimeoutExpired):
            return False

    def call(self, item, method, params, timeout):
        require(method in RPC_METHODS, 'Lifecycle cannot send input/navigation/trace calls')
        pin = self.config['windows_control']
        require(digest(pin['path']) == pin['sha256'], 'Windows control client changed')
        script = ('$ErrorActionPreference=\'Stop\'; & ' + self.quote(pin['path']) +
                  ' -PipeName ' + self.quote(item['socket'].removeprefix('\\\\.\\pipe\\')) +
                  ' -Method ' + self.quote(method) + ' -ParamsJson ' + self.quote(json.dumps(params)) +
                  ' -TimeoutMs ' + str(max(1, int(timeout * 1000))))
        return json.loads(self.ps(script, cap=timeout + 8))

    def force_stop(self, item, cap):
        before = [*item.get('owned_pids', []), *self.owned(item)]
        handle = self.open_job(item['unit'], 4 | 8)
        try:
            if handle:
                self.job.terminate(handle, 137)  # Exact named Job only, including its owning launcher.
            task = self.task_name(item)
            # End/delete only our token-derived task; prevents late-start races after a partial launch.
            subprocess.run(['schtasks.exe', '/end', '/tn', task], capture_output=True, timeout=10)
            subprocess.run(['schtasks.exe', '/delete', '/f', '/tn', task], capture_output=True, timeout=10)
            end = self.clock() + cap
            while self.clock() < end:
                if not self.owned(item) and all(self.identity(p['pid']) != p for p in before):
                    return {'owned_pids_stopped': before, 'job_terminated': bool(handle), 'task': task}
                self.sleep(.2)
            raise Refused('Owned Windows Job/PIDs remain; no restore/release')
        finally:
            if handle:
                self.job.close(handle)

    def start_watchdog(self, unit, command, directory, runtime):
        """The Ally equivalent of the Linux user unit: one scheduled task named for the run."""
        self.open_job_name_check(unit)
        directory = Path(directory)
        sid = self.ps('[Security.Principal.WindowsIdentity]::GetCurrent().User.Value').strip()
        require(re.fullmatch(r'S-\d-(?:\d+-)*\d+', sid), 'Cannot bind task to current Windows user SID')
        from xml.sax.saxutils import escape
        task = 'EdenLife-' + unit.removeprefix('eden-life-').removesuffix('.service')
        executable, *arguments = command
        xml = ('<?xml version="1.0" encoding="UTF-16"?><Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
               '<Triggers><TimeTrigger><StartBoundary>2099-01-01T00:00:00</StartBoundary><Enabled>false</Enabled></TimeTrigger></Triggers>'
               '<Principals><Principal id="Author"><UserId>' + escape(sid) +
               '</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>'
               '<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
               '<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT' + str(int(runtime) + 60) +
               'S</ExecutionTimeLimit></Settings><Actions Context="Author"><Exec><Command>' + escape(str(executable)) +
               '</Command><Arguments>' + escape(subprocess.list2cmdline(arguments)) + '</Arguments><WorkingDirectory>' +
               escape(str(directory)) + '</WorkingDirectory></Exec></Actions></Task>')
        task_xml = directory / 'watchdog-task.xml'
        task_xml.write_text(xml, encoding='utf-16')
        self.run(['schtasks.exe', '/create', '/f', '/tn', task, '/xml', str(task_xml)])
        self.run(['schtasks.exe', '/run', '/tn', task])

    def stop_watchdog(self, unit):
        self.open_job_name_check(unit)
        task = 'EdenLife-' + unit.removeprefix('eden-life-').removesuffix('.service')
        subprocess.run(['schtasks.exe', '/end', '/tn', task], capture_output=True, timeout=10)
        subprocess.run(['schtasks.exe', '/delete', '/f', '/tn', task], capture_output=True, timeout=10)

    def watchdog_running(self, unit):
        self.open_job_name_check(unit)
        task = 'EdenLife-' + unit.removeprefix('eden-life-').removesuffix('.service')
        done = subprocess.run(['schtasks.exe', '/query', '/tn', task, '/fo', 'LIST'],
                              capture_output=True, text=True, timeout=10)
        return done.returncode == 0 and 'Running' in done.stdout


def windows_launch(packet_path):
    """Task-only entry: join the Job BEFORE creating Eden, so children inherit it."""
    packet = json.loads(Path(packet_path).read_text())
    config, item = validate(packet['config']), packet['item']
    host = WindowsHost(config)
    require(os.name == 'nt' and host.name().casefold() == config['hostname'].casefold(), 'Wrong Windows launch host')
    host.open_job_name_check(item['unit'])
    run, profile = safe_absolute(item['run']), safe_absolute(item['profile'])
    require(run.parent == Path(config['state_dir']) and profile.parent == Path(config['work_root']) / 'profiles',
            'Foreign task profile/run')
    with (run / 'launch-once').open('x'):
        pass
    job = host.job.create(item['unit'], 8 * GIB)
    try:
        import ctypes
        info = host.job.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x200 | 0x2000 | 0x400 | 0x8
        info.BasicLimitInformation.ActiveProcessLimit = 256
        info.JobMemoryLimit = 8 * GIB
        host.job._check(host.job._k32().SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)),
                        'Set Job memory/process caps')
        host.job.assign(job, os.getpid())  # Descendants cannot run outside this Job, even before their first instruction.
        session = host.job.session_of(os.getpid())
        require(session == config['interactive_session'], 'Task not in the authorized interactive session')
        limits = host.job.limits(job)
        require(limits['JobMemoryLimit'] == 8 * GIB and limits['KillOnJobClose']
                and limits['ActiveProcessLimit'] == 256, 'Unbounded Windows Job')
        exe = profile / 'eden.exe'
        require(digest(exe) == item['app']['sha256'], 'Windows executable changed')
        private_config(profile)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('EDEN_', 'YUZU_'))}
        env.update(APPDATA=str(profile / 'isolated-appdata'), LOCALAPPDATA=str(profile / 'isolated-localappdata'))
        env['EDEN_LOG_FILTER'] = config.get('log_filter', '*:Warning Frontend:Info Debug.Emulated:Info Service.FS:Info')
        with Path(item['console_log']).open('ab', buffering=0) as console:
            child = subprocess.Popen([str(exe), '--control-socket', item['socket'], '--control-log',
                str(run / 'control.jsonl'), '--control-verbose', '--control-background'], cwd=profile,
                env=env, stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT)
            save(run / 'launcher.json', {'session': session, 'limits': limits,
                 'owned_pids': host.owned(item), 'started': stamp(), 'runtime': config.get('runtime', 5400)})
            try:
                child.wait(timeout=config.get('runtime', 5400))
            except subprocess.TimeoutExpired:
                save(run / 'runtime-expired.json', {'utc': stamp(), 'limits': host.job.limits(job)})
    except BaseException as error:
        save(run / 'launcher.json', {'error': repr(error), 'utc': stamp()})
        raise
    finally:
        host.job.close(job)  # KILL_ON_JOB_CLOSE includes this launcher and every descendant.


def iter_pin_nodes(node):
    """Every object that pins a file with path and sha256. The walk is the config, not a plugin."""
    if isinstance(node, dict):
        path, digest_value = node.get('path'), node.get('sha256')
        if isinstance(path, str) and isinstance(digest_value, str) and re.fullmatch(r'[0-9a-f]{64}', digest_value):
            yield node
        for key, child in node.items():
            if key in ('path', 'sha256'):
                continue
            yield from iter_pin_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from iter_pin_nodes(child)


def update_pins(config):
    pins = [pin for pin in config.get('game_updates') or [] if isinstance(pin, dict)]
    for item in config.get('instances') or []:
        pins.extend(pin for pin in (item.get('updates') or []) if isinstance(pin, dict))
    return pins


def detect_new_files(spec, sample):
    pattern = spec.get('pattern') or '*'
    return any(fnmatch.fnmatch(str(name), pattern) for name in sample.get('new_files') or [])


def detect_log_line(spec, sample):
    return re.search(spec.get('pattern') or '', sample.get('log_delta') or '') is not None


def detect_screen_class(spec, sample):
    return sample.get('screen_class') in set(spec.get('demo_states') or [])


def detect_hook(spec, sample):
    module_path = Path(spec['module'])
    loaded = importlib.util.spec_from_file_location('watchdog_detector_' + module_path.stem, module_path)
    module = importlib.util.module_from_spec(loaded)
    loaded.loader.exec_module(module)
    return bool(getattr(module, spec['function'])(spec, sample))


DEMO_DETECTORS = {'new-files': detect_new_files, 'log-line': detect_log_line,
                  'screen-class': detect_screen_class, 'hook': detect_hook}


class IdleWatchdog:
    """Decide when an instance has been idle, or has drifted into a demo, and which action to take.

    The sample is plain data. Screen names, file patterns and log lines come from the config.
    The default action is Down. Park is only the declared button or stick sequence.
    """

    def __init__(self, spec):
        spec = spec or {}
        self.idle_s = spec.get('idle_s', 300)
        require(isinstance(self.idle_s, (int, float)) and not isinstance(self.idle_s, bool) and self.idle_s > 0,
                'watchdog.idle_s must be a positive number')
        self.action = spec.get('action', 'down')
        require(self.action in ('down', 'park'), 'watchdog.action must be down or park')
        self.safe = set(spec.get('safe_states') or [])
        self.park = spec.get('park')
        self.detectors = spec.get('detectors') or []
        if self.action == 'park':
            require(isinstance(self.park, list) and self.park, 'watchdog park needs a declared input sequence')
        require(isinstance(self.detectors, list), 'watchdog.detectors must be a list')
        require(isinstance(spec.get('safe_states', []), list), 'watchdog.safe_states must be a list')

    def decide(self, now, samples):
        for key, sample in (samples or {}).items():
            hit = self.demo(sample)
            idle_for = now - sample.get('activity', now)
            if hit is not None:
                return self.decision(key, 'demo-drift', hit, idle_for)
            if sample.get('state') in self.safe:
                continue
            if idle_for > self.idle_s:
                return self.decision(key, 'idle', None, idle_for)
        return None

    def demo(self, sample):
        for spec in self.detectors:
            kind = spec.get('kind') if isinstance(spec, dict) else None
            fn = DEMO_DETECTORS.get(kind)
            if fn is None:
                raise Refused('unknown watchdog detector: ' + str(kind))
            if fn(spec, sample):
                return spec
        return None

    def decision(self, key, reason, detector, idle_for):
        value = {'action': self.action, 'reason': reason, 'instance': key, 'idle_s': idle_for}
        if detector is not None:
            value['detector'] = detector.get('kind')
        if self.action == 'park':
            value['park'] = self.park
        return value


def record_action(directory, kind, value):
    entry = {'utc': stamp(), 'kind': kind, 'value': value}
    if isinstance(value, dict) and value.get('ssh_retry'):
        entry['ssh_retry'] = value['ssh_retry']
    with (Path(directory) / 'actions.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry) + '\n')


class Lifecycle:
    def __init__(self, config, host=None, ledger=None, plan_check=None):
        self.c = validate(config)
        self.host = host if host is not None else WindowsHost(config) if config['host'] == 'ally' else Host()
        if host is None and config['host'] != 'ally':
            self.host.max_eden = LINUX_MAX_EDEN[config['host']]
        self.directory = Path(config['state_dir'])
        self.journal = self.directory / 'lifecycle.json'
        self.injected_ledger = ledger
        self.plan_check = plan_check if plan_check is not None else self.check_rows
        self.state = None
        self._watchdog_seen = {}

    def check_rows(self):
        if self.c.get('lifecycle_only'):
            return {'result': 'LIFECYCLE_ONLY', 'rows': 0, 'errors': [], 'scope': 'no runner/input authorized'}
        runner = self.c['runner']
        require(digest(runner['path']) == runner['sha256'], 'Shared runner changed')
        require(digest(self.c['rows']['path']) == self.c['rows']['sha256'], 'Row list changed')
        spec = importlib.util.spec_from_file_location('_lifecycle_shared_runner', runner['path'])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        require(hasattr(module, 'up_check'), 'Pinned runner has no up_check: a feature Up needs the packet gate')
        return module.up_check(Path(self.c['rows']['path']), self.c)

    def record(self, kind, value):
        record_action(self.directory, kind, value)

    def pending_receipt(self, text):
        with (self.directory / 'RELEASE-PENDING.md').open('a', encoding='utf-8') as stream:
            stream.write(text.replace('\r', ' ').replace('\n', ' ') + '\n')

    def notify(self, text):
        try:
            spec = self.c.get('notify')
            if not spec:
                self.pending_receipt('notify not configured')
                return
            line = (f"\n## {datetime.now(timezone.utc):%H:%M:%S}Z {spec.get('from', 'eden-lifecycle')} "
                    f"{self.c['label']} → {spec.get('to', self.c['owner'])} | {text}\n")
            if spec.get('ssh'):
                with SshMux(spec['ssh'], evidence_dir=self.directory, receipt_dir=self.directory) as mux:
                    for diagnosis in getattr(mux, 'diagnoses', ()):
                        self.record('ssh-diagnosis', {'line': diagnosis.get('reported_line',
                                                                           ssh_diagnosis_with_receipt(diagnosis))})
                    try:
                        done = subprocess.run(['ssh', '-T', *mux.rider_args, spec['ssh'],
                                               'cat >> ' + shlex.quote(spec['file'])],
                                              input=line, capture_output=True, text=True, timeout=10)
                    except subprocess.TimeoutExpired as error:
                        diagnosis = ssh_transport_failure(mux, 'ssh', None, '', timed_out=True)
                        diagnosis = ssh_record(diagnosis, 'ssh', 'notify', self.directory)
                        raise Refused(f'{error!r}; {ssh_diagnosis_with_receipt(diagnosis)}') from error
                    diagnosis = ssh_transport_failure(mux, 'ssh', done.returncode, done.stderr)
                    if diagnosis:
                        diagnosis = ssh_record(diagnosis, 'ssh', 'notify', self.directory)
                    require(done.returncode == 0, 'notify SSH failed: ' + (done.stderr or '')[-500:] +
                            (f'; {ssh_diagnosis_with_receipt(diagnosis)}' if diagnosis else ''))
            else:
                path = safe_absolute(spec['file'])
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(line)
        except Exception as error:
            try:
                failure = {'error': repr(error), 'utc': stamp()}
                self.state.setdefault('notify_errors', []).append(failure)
                self.record('notify-error', failure)
                self.write()
            except Exception:
                pass

    def write(self):
        save(self.journal, self.state)

    @contextmanager
    def host_port_lock(self):
        root = Path(self.c.get('ports_root') or Path(self.c['state_dir']).parent)
        root.mkdir(parents=True, exist_ok=True)
        lock_root = Path(tempfile.gettempdir()) / 'eden-lifecycle' / self.c['hostname'].casefold()
        lock_root.mkdir(parents=True, exist_ok=True)
        path = lock_root / '.eden-port-claims.lock'
        with path.open('a+b') as stream:
            _PORTABLE_LOCK.lock_exclusive(stream, blocking=True)
            yield root

    @staticmethod
    def _port_bound(port):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(('0.0.0.0', int(port)))
            return False
        except OSError:
            return True
        finally:
            probe.close()

    def _allocate_ports(self, root):
        """Allocate and persist this run's port pins while holding the host admission lock."""
        tcp = set()
        reserved = {'gdb': tcp, 'bridge': tcp, 'display': set()}

        def reserve(instances):
            for item in instances:
                for kind in reserved:
                    if item.get(kind) is not None:
                        reserved[kind].add(str(item[kind]) if kind == 'display' else int(item[kind]))

        ledger = self.ledger_text()
        opened = active_claims(ledger, self.c['host'])
        for line in opened:
            field = re.search(r'\sports=(\S+)', line)
            if field:
                for entry in field[1].split(','):
                    match = re.fullmatch(r'[^,:]+:(gdb|bridge|display)=(.+)', entry)
                    require(match, 'Invalid port pin in open lifecycle claim')
                    reserve([{match[1]: match[2]}])
        for line in active_port_records(ledger, self.c['host'], opened):
            reserve(json.loads(line.partition(' allocation=')[2])['instances'].values())
        for folder in root.iterdir():
            if not folder.is_dir() or folder == self.directory or folder == self.directory.parent:
                continue
            record = folder / 'state' / 'port-allocation.json'
            if record.is_file():
                try:
                    value = json.loads(record.read_text())
                    if value.get('released') is True:
                        continue
                    reserve(value['instances'].values())
                except (OSError, ValueError, TypeError):
                    raise Refused(f'Unreadable live port reservation: {record}')
            for pin in folder.rglob('port.txt'):
                if not pin.is_file():
                    continue
                try:
                    reserved['bridge'].add(int(pin.read_text().strip()))
                except (OSError, ValueError):
                    raise Refused(f'Invalid bridge port pin: {pin}')
            config_path = folder / 'config.json'
            if config_path.is_file():
                try:
                    prior = json.loads(config_path.read_text())
                    for instance in prior.get('instances', []):
                        if instance.get('gdb_port'):
                            reserved['gdb'].add(int(instance['gdb_port']))
                        if instance.get('display') is not None:
                            reserved['display'].add(str(instance['display']))
                    for choice in prior.get('display_choice', []):
                        if choice.get('chosen') is not None:
                            reserved['display'].add(str(choice['chosen']))
                    rows_path = (prior.get('rows') or {}).get('path')
                    tcp.update(port for _, port in packet_ports(rows_path))
                except (OSError, ValueError, TypeError):
                    raise Refused(f'Unreadable pinned port configuration: {config_path}')

        def choose(kind, requested, first, last):
            if requested and self._port_bound(requested):
                raise Refused(f'{kind.upper()} port {requested} is already bound on this host')
            for candidate in ([requested] if requested else []) + list(range(first, last + 1)):
                if candidate in reserved[kind] or self._port_bound(candidate):
                    continue
                reserved[kind].add(candidate)
                return candidate
            raise Refused(f'No free {kind.upper()} port in {first}-{last}')

        rows_path = (self.c.get('rows') or {}).get('path')
        for kind, port in packet_ports(rows_path):
            if self._port_bound(port):
                raise Refused(f'{kind} packet port {port} is already bound on this host')

        allocated = {}
        for original in self.c['instances']:
            key = original['key']
            gdb = choose('gdb', original.get('gdb_port'), 6793, 6893)
            bridge_file = next((pin for pin in original.get('files', [])
                                if pin.get('to', '').endswith('ultimate/ssbu-hw-bridge/port.txt')), None)
            bridge_requested = None
            if bridge_file:
                try:
                    bridge_requested = int(Path(bridge_file['path']).read_text().strip())
                except (OSError, ValueError):
                    pass
                if bridge_requested is not None and bridge_requested <= 0:
                    bridge_requested = None
            bridge = choose('bridge', bridge_requested, 6980, 7079) if bridge_file else None
            display = original.get('display')
            if display is not None and self.c.get('display_helper'):
                display = helper_display(display, reserved['display'])
            if display is not None:
                require(display not in reserved['display'], 'Existing display already claimed: ' + display)
                reserved['display'].add(display)
            item = {'gdb': gdb, 'bridge': bridge, 'display': display}
            allocated[key] = item
            original['gdb_port'] = gdb
            if display is not None:
                original['display'] = display
            if bridge_file:
                target = self.directory / ('bridge-port-' + key + '.txt')
                target.write_text(f'{bridge}\n')
                bridge_file.update(path=str(target), sha256=digest(target))
        allocation = {'schema': 1, 'host': self.c['hostname'], 'token': self.state['token'],
                      'instances': allocated, 'released': False, 'claimed': stamp()}
        save(self.directory / 'port-allocation.json', allocation)
        self.state['port_allocation'] = allocation
        self.state['config'] = self.c
        config_path = Path(self.c['state_dir']).parent / 'config.json'
        if config_path.is_file():
            save(config_path, self.c)
        self.write()
        return allocation

    def claim_with_ports(self, admission):
        with self.host_port_lock() as root:
            admission['port_allocation'] = self._allocate_ports(root)
            # A lost reply can follow a committed claim. Down reconciles ownership before releasing the pins.
            return self.ledger().claim(stale_claim_hours=self.c.get('stale_claim_hours'), admission=admission)

    def check_state(self):
        """Never trust a journal's arbitrary unit/path as permission to stop it."""
        original_config = self.state.get('config_original', self.state['config'])
        require(self.state['owner'] == self.c['owner']
                and (original_config == self.c or self.state['config'] == self.c),
                'Owner/config differs from journal; no cleanup action')
        token = self.state['token']
        require(re.fullmatch(r'[0-9a-f]{32}', token), 'Invalid owned lifecycle token')
        originals = {item['key']: item for item in self.state['config']['instances']}
        seen = set()
        for item in self.state['instances']:
            key = item['key']
            require(key in originals and key not in seen, 'Foreign/duplicate journal instance')
            seen.add(key)
            require(all(item.get(name) == value for name, value in originals[key].items()), 'Journal instance input changed')
            unit = f'eden-life-{token}-{key}'
            require(item['unit'] == unit + '.service'
                    and item['display_units'] == [unit + '-display.service', unit + '-display-x.service'],
                    'Foreign journal unit refused')
            require(item['profile'] == str(Path(self.c['work_root']) / 'profiles' / ('profile-' + key))
                    and item['snapshot'] == str(self.directory / ('snapshot-' + key))
                    and item['run'] == str(self.directory / ('run-' + key))
                    and item['socket'] == (f'\\\\.\\pipe\\eden-life-{token}-{key}' if self.c['host'] == 'ally'
                                           else str(self.directory / ('run-' + key) / 'control.sock')),
                    'Foreign journal profile/run path refused')
        watched = self.state.get('watchdog_unit')
        if watched:
            require(watched == f'eden-life-{token}-watchdog.service', 'Foreign watchdog unit refused')
        settle = self.state.get('settle_unit')
        if settle:
            require(self.c['host'] != 'ally' and settle == f'eden-settle-{token[:8]}.service',
                    'Foreign settle unit refused')

    def ledger(self, token=None):
        return self.injected_ledger if self.injected_ledger is not None else Ledger(
            self.c['ledger'], self.c['host'], self.c['owner'], token or self.state['token'], self.c.get('ledger_ssh'),
            label=self.c['label'], exclusive_host=self.c.get('exclusive_host', False), state_dir=self.directory)

    def reserve_exclusive(self, approver):
        require(self.c.get('exclusive_host'), 'Exclusive reservation requires exclusive_host=true')
        require(self.host.name().casefold() == self.c['hostname'].casefold(), 'Wrong executor hostname')
        # Process scan is on the executor. The authoritative ledger checks open claims under the append lock.
        admission = self.host.resources(Path(Path(self.c['work_root']).anchor))
        result = self.ledger(uuid.uuid4().hex).operate('reserve', admission=admission, approver=approver)
        if result.get('ssh_retry'):
            self.record('reserve', result)
        return result

    def cancel_reservation(self, token):
        require(isinstance(token, str) and re.fullmatch(r'[0-9a-f]{32}', token),
                'cancel requires --reservation-token from the reserve receipt')
        result = self.ledger(token).operate('cancel')
        if result.get('ssh_retry'):
            self.record('cancel-reservation', result)
        return result

    @contextmanager
    def lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        require(not self.directory.is_symlink(), 'State directory alias refused')
        with (self.directory / '.lock').open('a+') as stream:
            with run_lock(stream):
                yield

    def admission(self, owned=()):
        path = Path(self.c['work_root'])
        while not path.exists():
            path = path.parent
        value = self.host.resources(path, owned)
        # The ledger admits up to this many of this tool's own claims on the host (claim_admission).
        value['max_eden'] = getattr(self.host, 'max_eden', 1)
        # resources() removes these units from foreign_processes. Count them here or a pair's
        # second launch looks like the only Eden on the host.
        value['owned_running'] = len(tuple(owned))
        self.record('admission', value)
        # Compilers block an Up unless every one runs at nice >= COMPILE_NICE_MIN and MemAvailable covers the floor plus
        # the compile reserve (operator 2026-11-10); a perf_sensitive config still refuses any compiler. Other Eden
        # instances are allowed up to the host's limit (operator 2026-11-04:
        # the Surface and the Yoga can each run two emulators; the Ally stays at one). Only the emulator binary counts
        # as an instance; its AppImage launcher and LAN helper processes belong to the same instance.
        # The first instance needs 10 GiB available. A second one, foreign or already launched by this run,
        # needs 9 GiB: its unit caps it at 8 GiB, plus 1 GiB.
        problems, _lines, _checks = self.resource_report(value)
        if problems:
            require(False, problems[0])
        return value

    def resource_report(self, value):
        """Memory, disk, compilers and Eden slots. Same refusals admission uses, without writing."""
        problems, lines, checks = [], [], []
        floor = self.c.get('disk_floor_bytes', MIN_DISK)
        if value['disk_free'] < floor:
            problems.append(f'Free disk below {floor / 10 ** 9:g} GB')
            lines.append(f'FAIL disk: Free disk below {floor / 10 ** 9:g} GB')
            checks.append({'name': 'disk', 'ok': False, 'free': value['disk_free'], 'floor': floor})
        else:
            lines.append(f'OK disk: {value["disk_free"]} bytes free')
            checks.append({'name': 'disk', 'ok': True, 'free': value['disk_free']})
        foreign = value['foreign_processes']
        edens = foreign_edens(value)
        others = [p for p in foreign if not str(p.get('name', '')).lower().startswith(('eden', 'yuzu'))]
        reserve = 0
        if others:
            # A process with no readable nice (the Windows host reports none) counts as un-niced.
            niced = [p for p in others if type(p.get('nice')) is int and p['nice'] >= COMPILE_NICE_MIN]
            unniced = [p for p in others if p not in niced]
            seen = ', '.join(f'{p.get("name")} {p.get("pid")} nice {p.get("nice", "?")}' for p in others[:6])
            seen += f' (+{len(others) - 6} more)' if len(others) > 6 else ''
            check = {'name': 'compiler', 'running': len(others), 'unniced': len(unniced), 'processes': others}
            if self.c.get('perf_sensitive'):
                detail = 'Compiler already active on host (perf_sensitive: no compiler may run)'
                problems.append(detail)
                lines.append(f'FAIL compiler: {detail}; seen: {seen}')
                checks.append(dict(check, ok=False, perf_sensitive=True))
            elif unniced:
                detail = (f'Compiler already active on host: {len(unniced)} of {len(others)} below nice '
                          f'{COMPILE_NICE_MIN} (build under nice -n 19)')
                problems.append(detail)
                lines.append(f'FAIL compiler: {detail}; seen: {seen}')
                checks.append(dict(check, ok=False))
            else:
                reserve = self.c.get('compile_reserve_bytes', COMPILE_RESERVE)
                lowest = min(p['nice'] for p in others)
                lines.append(f'OK compiler: {len(others)} running, all at nice >= {COMPILE_NICE_MIN} (lowest {lowest}); '
                             f'compile reserve {reserve} bytes added to the memory floor; seen: {seen}')
                checks.append(dict(check, ok=True, lowest_nice=lowest, reserve=reserve))
        else:
            lines.append('OK compiler: none running')
            checks.append({'name': 'compiler', 'ok': True})
        limit = value.get('max_eden', getattr(self.host, 'max_eden', 1))
        # Preflight has no owned_running, so it still counts foreign Edens only.
        owned_running = value.get('owned_running', 0)
        if type(owned_running) is not int or owned_running < 0:
            owned_running = 0
        running = len(edens) + owned_running
        if self.c.get('exclusive_host') and edens:
            detail = 'Exclusive host requires no other running Eden'
            problems.append(detail)
            lines.append('FAIL eden slots: ' + detail)
            slot = {'name': 'eden-slots', 'ok': False, 'running': running, 'limit': limit}
        elif running >= limit:
            detail = f'{running} Eden instance(s) already active on host (limit {limit})'
            problems.append(detail)
            lines.append('FAIL eden slots: ' + detail)
            slot = {'name': 'eden-slots', 'ok': False, 'running': running, 'limit': limit}
        else:
            lines.append(f'OK eden slots: {running} running, limit {limit}, requesting {len(self.c["instances"])}')
            slot = {'name': 'eden-slots', 'ok': True, 'running': running, 'limit': limit,
                    'requesting': len(self.c['instances'])}
        if 'owned_running' in value:
            slot['owned_running'] = owned_running
        checks.append(slot)
        need = MIN_MEMORY_SECOND if running else MIN_MEMORY
        floor = self.c.get('memory_floor_bytes', need)
        available = value['mem_available']
        memory = {'name': 'memory', 'available': available, 'floor': floor}
        if reserve:
            memory.update(reserve=reserve, headroom=available - floor - reserve)
        if available < floor:
            if 'memory_floor_bytes' in self.c:
                detail = f'MemAvailable {available} is below the per-instance floor {floor}'
            else:
                detail = f'MemAvailable below {need // GIB} GiB'
            problems.append(detail)
            lines.append('FAIL memory: ' + detail)
            checks.append(dict(memory, ok=False))
        elif available < floor + reserve:
            detail = (f'MemAvailable {available} is below the floor {floor} plus the compile reserve {reserve} '
                      f'({floor + reserve}) while niced compilers run')
            problems.append(detail)
            lines.append('FAIL memory: ' + detail)
            checks.append(dict(memory, ok=False))
        elif reserve:
            lines.append(f'OK memory: {available} bytes available, floor {floor} + compile reserve {reserve}; '
                         f'headroom {available - floor - reserve}')
            checks.append(dict(memory, ok=True))
        else:
            lines.append(f'OK memory: {available} bytes available, floor {floor}')
            checks.append(dict(memory, ok=True))
        return problems, lines, checks

    def ledger_text(self):
        """Read the ledger. Injected ledgers and a remote read do not append."""
        if self.injected_ledger is not None:
            return getattr(self.injected_ledger, 'open_text', '')
        if self.c.get('ledger_ssh'):
            info = Ledger(self.c['ledger'], self.c['host'], self.c['owner'], '0' * 32,
                          self.c.get('ledger_ssh'), state_dir=self.directory).inspect()
            if info.get('ssh_retry'):
                self.record('claim-readback', info)
            return '\n'.join([*(info.get('open') or []), *(info.get('port_records') or [])])
        path = safe_absolute(self.c['ledger'])
        require(path.is_file(), 'Authoritative ledger must already exist')
        with path.open('r', encoding='utf-8') as stream:
            if fcntl is not None:
                fcntl.flock(stream, fcntl.LOCK_SH)
            return stream.read()

    def playbook_head(self, root):
        probe = getattr(self.host, 'playbook_head', None)
        if probe is not None:
            return probe(root)
        safe_absolute(root)
        done = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=15)
        require(done.returncode == 0, 'Playbook commit unreadable: ' + (done.stderr or done.stdout)[-300:])
        return done.stdout.strip()

    def static_checks(self):
        """Pins, game base, declared update version, playbook commit and display. No writes."""
        problems, lines, checks = [], [], []
        windows = set()
        if self.c.get('host') == 'ally':
            for key in ('windows_job', 'windows_control', 'windows_python'):
                helper = self.c.get(key) or {}
                if isinstance(helper, dict) and isinstance(helper.get('path'), str):
                    windows.add(helper['path'])
        seen = set()
        for pin in iter_pin_nodes(self.c):
            identity = (pin.get('path'), pin.get('sha256'))
            if identity in seen:
                continue
            seen.add(identity)
            path = Path(pin['path'])
            if not path.is_file():
                detail = f'Pinned input missing: {path}'
            else:
                got = digest(path)
                detail = None if got == pin['sha256'] else f'Pinned input mismatch: {path}'
            if detail and str(path) in windows:
                detail = 'Windows helper mismatch before claim'
            if detail:
                problems.append(detail)
                lines.append('FAIL pin: ' + detail)
                checks.append({'name': 'pin', 'ok': False, 'path': str(path), 'detail': detail})
            else:
                lines.append(f'OK pin: {path}')
                checks.append({'name': 'pin', 'ok': True, 'path': str(path)})
        for item in self.c['instances']:
            game = Path(item['game'])
            if game.is_file():
                lines.append(f'OK game base: {game}')
                checks.append({'name': 'game-base', 'ok': True, 'instance': item['key'], 'path': str(game)})
            else:
                detail = f'Game base missing: {game}'
                problems.append(detail)
                lines.append('FAIL game base: ' + detail)
                checks.append({'name': 'game-base', 'ok': False, 'instance': item['key'], 'detail': detail})
            source = Path(item['source'])
            if source.is_dir():
                lines.append(f'OK source profile: {source}')
                checks.append({'name': 'source-profile', 'ok': True, 'instance': item['key'], 'path': str(source)})
            else:
                detail = f'Source profile missing: {source}'
                problems.append(detail)
                lines.append('FAIL source profile: ' + detail)
                checks.append({'name': 'source-profile', 'ok': False, 'instance': item['key'], 'detail': detail})
            for symbols in (item.get('trace') or {}).get('symbols') or []:
                elf = Path(str(symbols.get('elf') or ''))
                if elf.is_file():
                    lines.append(f'OK trace elf: {elf}')
                    checks.append({'name': 'trace-elf', 'ok': True, 'instance': item['key'], 'path': str(elf)})
                else:
                    detail = f'Trace ELF missing: {elf}'
                    problems.append(detail)
                    lines.append('FAIL trace elf: ' + detail)
                    checks.append({'name': 'trace-elf', 'ok': False, 'instance': item['key'], 'detail': detail})
            if len(self.c['instances']) == 2:
                # Eden<->Eden local wireless: with LAN Online on, the room LDN layer connects and the guest game then
                # gives up ("Unable to join"; ldn2c-vanilla-yoga-1 vs ldn2c-eblan-yoga-1, 2026-11-18). A pair that
                # really tests LAN Online declares lan_online_pair=true.
                # Fail closed: an unreadable config or a value other than false/absent (Eden's default is false)
                # refuses the pair too.
                qt = effective_qt_config(item)
                try:
                    value = qt_value(qt, 'lan_online')
                except OSError as error:
                    value = 'unreadable: ' + error.__class__.__name__
                check = {'name': 'pair-lan-online', 'instance': item['key'], 'path': str(qt), 'value': value}
                if value not in (None, 'false') and self.c.get('lan_online_pair') is not True:
                    detail = (f'Pair instance {item["key"]} boots with lan_online={value} ({qt}): pin a qt-config with '
                              'lan_online=false, or set lan_online_pair=true for a LAN Online pair test')
                    problems.append(detail)
                    lines.append('FAIL pair lan_online: ' + detail)
                    checks.append(dict(check, ok=False, detail=detail))
                else:
                    lines.append(f'OK pair lan_online: {item["key"]} {value} ({qt})')
                    checks.append(dict(check, ok=True))
        expected = self.c.get('expected_game_version')
        if expected:
            matched = [pin for pin in update_pins(self.c) if pin.get('version') == expected]
            if matched:
                lines.append(f'OK game update: version {expected} pinned {len(matched)} file(s)')
                checks.append({'name': 'game-update', 'ok': True, 'version': expected, 'count': len(matched)})
            else:
                detail = f'No game update pin for version {expected}'
                problems.append(detail)
                lines.append('FAIL game update: ' + detail)
                checks.append({'name': 'game-update', 'ok': False, 'detail': detail})
        else:
            lines.append('OK game update: no expected version declared')
            checks.append({'name': 'game-update', 'ok': True, 'detail': 'not declared'})
        book = self.c.get('playbook')
        if isinstance(book, dict):
            try:
                head = self.playbook_head(book.get('root'))
            except Exception as error:  # noqa: BLE001 - the receipt names the commit failure
                detail = f'Playbook commit unreadable: {error}'
                head = None
            else:
                detail = None
            want = str(book.get('commit') or '')
            if detail is None and want and (head == want or head.startswith(want)):
                lines.append(f'OK playbook commit: {head}')
                checks.append({'name': 'playbook', 'ok': True, 'commit': head})
            else:
                detail = detail or f'Playbook commit {head} != declared {want}'
                problems.append(detail)
                lines.append('FAIL playbook: ' + detail)
                checks.append({'name': 'playbook', 'ok': False, 'detail': detail})
        else:
            lines.append('OK playbook commit: not declared')
            checks.append({'name': 'playbook', 'ok': True, 'detail': 'not declared'})
        probe = getattr(self.host, 'display_ready', None)
        if probe is None:
            lines.append('OK display: no display probe on this host')
            checks.append({'name': 'display', 'ok': True, 'detail': 'no display probe on this host'})
        else:
            for item in self.c['instances']:
                if self.c.get('display_helper') and self.c['host'] != 'ally' and item.get('display'):
                    item = {**item, 'display': helper_display(item['display'])}
                result = probe(item, self.c)
                detail = result.get('detail') or ''
                if result.get('ready'):
                    lines.append(f'OK display {item.get("key")}: {detail}')
                    checks.append({'name': 'display', 'ok': True, 'instance': item.get('key'), **result})
                else:
                    problems.append(detail or f'Display not ready: {item.get("display")}')
                    lines.append(f'FAIL display {item.get("key")}: {detail}')
                    checks.append({'name': 'display', 'ok': False, 'instance': item.get('key'), **result})
        state_root = Path(self.c['state_dir'])
        if state_root.is_dir() and self.c['host'] != 'ally':
            for item in self.c['instances']:
                run = state_root / ('run-' + item['key'])
                socket = run / 'control.sock'
                if socket.exists():
                    lines.append(f'REPORT stale control socket: {socket} (removed before launch)')
                    checks.append({'name': 'stale-socket', 'ok': True, 'path': str(socket), 'action': 'remove-before-launch'})
                trace = run / (item['key'] + '.trace.jsonl')
                if trace.exists():
                    lines.append(f'REPORT stale trace log: {trace} (moved aside before launch)')
                    checks.append({'name': 'stale-trace', 'ok': True, 'path': str(trace), 'action': 'move-aside-before-launch'})
        return problems, lines, checks

    def runtime_checks(self, value):
        """Resources plus ledger claims. value is an admission snapshot; this does not record one."""
        problems, lines, checks = self.resource_report(value)
        try:
            text = self.ledger_text()
            policy = claim_admission(text, self.c['host'], self.c['owner'], self.c.get('stale_claim_hours'), value,
                                     label=self.c['label'], exclusive_host=self.c.get('exclusive_host', False))
        except Exception as error:  # noqa: BLE001
            detail = f'Ledger unreadable: {error}'
            problems.append(detail)
            lines.append('FAIL ledger: ' + detail)
            checks.append({'name': 'ledger', 'ok': False, 'detail': detail})
            return problems, lines, checks
        blocking = policy.get('blocking') or []
        if blocking or policy.get('reservation_required'):
            detail = ('Open host claim; no profile changes permitted' if blocking else
                      'Exclusive host requires a matching approved reservation')
            problems.append(detail)
            lines.append('FAIL ledger: ' + detail)
            checks.append({'name': 'ledger', 'ok': False, 'blocking': len(blocking)})
        else:
            lines.append('OK ledger: no blocking claim')
            checks.append({'name': 'ledger', 'ok': True, 'stale_claim_skipped': len(policy.get('stale_claim_skipped') or [])})
        return problems, lines, checks

    def preflight(self, runtime=True):
        """One read-only receipt. runtime=False is the half that runs before up creates any state."""
        require(self.host.name().casefold() == self.c['hostname'].casefold(), 'Wrong executor hostname')
        problems, lines, checks = self.static_checks()
        if runtime:
            path = Path(self.c['work_root'])
            while not path.exists():
                path = path.parent
            value = self.host.resources(path)
            value['max_eden'] = getattr(self.host, 'max_eden', 1)
            extra_problems, extra_lines, extra_checks = self.runtime_checks(value)
            problems.extend(extra_problems)
            lines.extend(extra_lines)
            checks.extend(extra_checks)
        return {'phase': 'preflight', 'ok': not problems, 'mutation': False, 'host': self.c['host'],
                'lines': lines, 'checks': checks, 'message': '; '.join(problems) if problems else 'preflight ok'}

    def _watchdog_text(self, item, source_name):
        """The last 40 lines of a declared source. This is a window, not a consumed delta."""
        path = self.directory / 'instances.json'
        if not source_name or not path.is_file():
            return ''
        instances = json.loads(path.read_text())
        source = (instances.get(item['key']) or {}).get(source_name)
        if not source or not Path(source).is_file():
            return ''
        return '\n'.join(Path(source).read_text(errors='replace').splitlines()[-40:])

    def observe_watchdog(self, seen):
        """Control-log mtime, a declared state probe, and files that appeared after the first look."""
        require(self.journal.is_file(), 'No owned lifecycle journal')
        self.state = json.loads(self.journal.read_text())
        self.check_state()
        spec = self.c.get('watchdog') or {}
        samples = {}
        for item in self.state['instances']:
            activity = self.control_activity(item)
            text = self._watchdog_text(item, (spec.get('state_probe') or {}).get('source'))
            state = None
            for rule in (spec.get('state_probe') or {}).get('rules') or []:
                if rule.get('pattern') and re.search(rule['pattern'], text):
                    state = rule.get('state')
            new_files = []
            for detector in spec.get('detectors') or []:
                if not isinstance(detector, dict) or detector.get('kind') != 'new-files':
                    continue
                directory = Path(detector['directory'])
                pattern = detector.get('pattern') or '*'
                current = set()
                if directory.is_dir():
                    current = {path.name for path in directory.iterdir()
                               if path.is_file() and fnmatch.fnmatch(path.name, pattern)}
                known = seen.get(item['key'])
                if known is None:
                    seen[item['key']] = current
                else:
                    new_files.extend(sorted(current - known))
                    seen[item['key']] = current
            samples[item['key']] = {'activity': activity, 'state': state, 'new_files': new_files,
                                    'log_delta': text, 'screen_class': state}
        return samples

    def send_park(self, decision):
        """Send only the declared park sequence. Lifecycle up/down/status still send no input."""
        require(self.journal.is_file(), 'No owned lifecycle journal')
        self.state = json.loads(self.journal.read_text())
        self.check_state()
        sent = []
        for item in self.state['instances']:
            if item['key'] != decision['instance']:
                continue
            tools = str(Path(item['app']['path']).parent / 'tools')
            if tools not in sys.path:
                sys.path.insert(0, tools)
            from edenctl import Client
            client = Client(item['socket'], 10)
            for step in decision.get('park') or []:
                player = int(step.get('player', 0))
                if 'buttons' in step:
                    client.request('input.button', {'player': player, 'buttons': step['buttons'], 'hold': True,
                                                    'lease_ms': int(step.get('hold', 150))})
                elif 'stick' in step:
                    stick_x, stick_y, stick_ms = step['stick']
                    client.request('input.stick', {'player': player, 'x': stick_x, 'y': stick_y, 'hold': True,
                                                   'lease_ms': int(stick_ms)})
                else:
                    raise Refused('watchdog park step needs buttons or stick')
                sent.append(step)
            client.request('input.reset', {})
        require(sent, 'watchdog park found no matching instance')
        return sent

    def watchdog(self, now=None, samples=None, rounds=None, pause=None):
        """Watch until a demo drift or an idle outside a declared safe state, then park or Down."""
        spec = self.c.get('watchdog') or {}
        dog = IdleWatchdog(spec)
        rounds = spec.get('rounds', 1) if rounds is None else rounds
        pause = spec.get('poll_s', 0) if pause is None else pause
        # Sidecar mode calls this once per poll. A fresh dict each call would treat every
        # new-files look as a baseline, so a file that appears between polls is never new.
        seen, decision, current = self._watchdog_seen, None, samples
        for _ in range(max(1, int(rounds))):
            if samples is None:
                current = self.observe_watchdog(seen)
            moment = now if now is not None else time.time()
            decision = dog.decide(moment, current)
            if decision is not None or samples is not None:
                break
            if pause:
                self.host.sleep(pause)
        if decision is None:
            return {'phase': 'watchdog', 'action': None, 'ok': True, 'mutation': False,
                    'lines': ['OK watchdog: no idle or demo drift']}
        receipt = {**decision, 'utc': stamp(), 'phase': 'watchdog', 'ok': True,
                   'mutation': decision['action'] == 'down',
                   'lines': [f"ACTION watchdog: {decision['reason']} on {decision['instance']}; {decision['action']}"]}
        if self.directory.exists():
            save(self.directory / 'watchdog-receipt.json', receipt)
        actor = getattr(self, 'watchdog_actor', None)
        if actor is not None:
            actor(receipt)
            return receipt
        if decision['action'] == 'down':
            receipt['down'] = self.down()
        elif decision['action'] == 'park':
            receipt['park_sent'] = self.send_park(decision)
        if self.directory.exists():
            save(self.directory / 'watchdog-receipt.json', receipt)
        return receipt

    def control_activity(self, item):
        """Later of the control log and the journal Up time. A missing log is not decades of idle."""
        started = datetime.fromisoformat(self.state['started']).timestamp()
        control = Path(item['run']) / 'control.jsonl'
        try:
            observed = control.stat().st_mtime
        except OSError:
            return started
        return max(observed, started)

    def watchdog_serve(self):
        """Sidecar loop. It leaves when the journal is gone, the instance is Down, or it takes an action."""
        self._in_sidecar = True
        pause = (self.c.get('watchdog') or {}).get('poll_s', 5)
        while True:
            if not self.journal.is_file():
                return {'phase': 'watchdog', 'action': None, 'ok': True, 'mutation': False,
                        'lines': ['OK watchdog: no journal']}
            state = json.loads(self.journal.read_text())
            if state.get('phase') == 'down':
                return {'phase': 'watchdog', 'action': None, 'ok': True, 'mutation': False,
                        'lines': ['OK watchdog: instance is down']}
            receipt = self.watchdog(rounds=1, pause=0)
            if receipt.get('action'):
                return receipt
            self.host.sleep(pause if pause else 5)

    def resolved_sources(self, item):
        """Builtin paths, then the run's sources, then the instance's. beside names a path already resolved."""
        declared = dict(SOURCE_BUILTIN)
        if isinstance(self.c.get('sources'), dict):
            declared.update(self.c['sources'])
        declared.update(item.get('sources') or {})
        resolved, pending = {}, []
        profile = Path(item['profile'])
        for name, spec in declared.items():
            if isinstance(spec, str):
                resolved[name] = str(profile / spec)
            elif 'sdmc' in spec:
                resolved[name] = str(profile / 'user' / 'sdmc' / spec['sdmc'])
            else:
                pending.append((name, spec))
        for name, spec in pending:
            base = resolved.get(spec['beside'])
            require(base, 'beside source unknown: ' + spec['beside'])
            resolved[name] = str(Path(base).with_name(spec['name']))
        return resolved

    def start_owned_watchdog(self):
        spec = self.c.get('watchdog')
        if not isinstance(spec, dict) or spec.get('auto', True) is not True:
            return None
        source = standalone_source()
        script = self.directory / 'lifecycle-watchdog.py'
        script.write_text(source, encoding='utf-8')
        config_path = self.directory / 'lifecycle-config.json'
        save(config_path, self.c)
        unit = f"eden-life-{self.state['token']}-watchdog.service"
        self.state['watchdog_unit'] = unit
        self.write()
        program = self.c['windows_python']['path'] if self.c['host'] == 'ally' else '/usr/bin/python3'
        command = [program, '-I', '-B', str(script), 'watchdog-serve', str(config_path), '--execute']
        self.host.start_watchdog(unit, command, self.directory, self.c.get('runtime', 5400))
        self.state['watchdog_started'] = True
        self.write()
        return unit

    def retire_stale_runtime(self, item):
        """Drop a dead control socket and set aside a leftover trace log before Eden creates them."""
        run = Path(item['run'])
        run.mkdir(parents=True, exist_ok=True)
        retired = []
        socket = Path(item['socket'])
        if self.c['host'] != 'ally':
            require(socket.parent == run, 'Control socket is outside the run directory')
            if socket.exists() or socket.is_socket():
                socket.unlink()
                retired.append({'path': str(socket), 'action': 'removed'})
        trace = run / (item['key'] + '.trace.jsonl')
        if trace.exists() and not trace.is_symlink():
            aside = run / (item['key'] + '.trace-aside-' + datetime.now(timezone.utc).strftime('%H%M%S%f') + '.jsonl')
            trace.replace(aside)
            retired.append({'path': str(trace), 'aside': str(aside), 'action': 'moved'})
        if retired:
            self.record('stale-runtime', {'key': item['key'], 'files': retired})
        return retired

    def _stop_watchdog_unit(self):
        # The sidecar is this process. Stopping its unit here would kill the Down it is running.
        if getattr(self, '_in_sidecar', False):
            return
        unit = (self.state or {}).get('watchdog_unit')
        if not unit:
            return
        require(unit == f"eden-life-{self.state['token']}-watchdog.service", 'Foreign watchdog unit refused')
        self.host.stop_watchdog(unit)

    def poll(self, predicate, cap, what):
        end = self.host.clock() + cap
        while self.host.clock() < end:
            value = predicate()
            if value:
                return value
            self.host.sleep(.1)
        raise Refused('Deadline: ' + what)

    def rpc(self, item, method, params=None, cap=15):
        deadline = self.host.clock() + cap
        reply = self.host.call(item, method, params or {}, min(cap, 5))
        self.record('rpc', {'instance': item['key'], 'method': method, 'reply': reply})
        require(reply.get('ok') is True, 'RPC rejected: ' + method)
        result = reply.get('result')
        if isinstance(result, dict) and result.get('accepted'):
            require(result.get('job'), 'Accepted asynchronous operation has no job')
            job_id = result['job']

            def terminal():
                remaining = deadline - self.host.clock()
                require(remaining > 0, 'Deadline: terminal ' + method)
                progress = self.host.call(item, 'jobs.get', {'job': job_id}, min(remaining, 5))
                self.record('rpc-job', {'instance': item['key'], 'method': method, 'reply': progress})
                require(progress.get('ok') is True, 'Cannot inspect job')
                job = progress.get('result') or {}
                require(job.get('state') not in ('error', 'failed', 'cancelled') and not job.get('error'),
                        'Asynchronous job failed: ' + method)
                if job.get('state') in ('done', 'completed', 'finished'):
                    require(isinstance(job.get('response'), dict) and job['response'].get('ok') is True,
                            'Terminal job lacks successful response')
                    return job['response']
                return None

            return self.poll(terminal, max(0, deadline - self.host.clock()), 'terminal ' + method)
        return result

    def prepare(self, item):
        profile = Path(item['profile'])
        # Snapshot is the full pristine PRIVATE clone, before all staging/muting.
        expected = clone_profile(profile, item['snapshot'])
        save(self.directory / (item['key'] + '-snapshot.json'), expected)
        item['snapshot_ready'] = True
        self.write()
        config = profile / CONFIG
        text = config.read_text()
        replacement = profile / 'user' if self.c['host'] == 'ally' else profile
        text = text.replace(item['source'], str(replacement))
        if self.c['host'] == 'ally':
            text = text.replace(Path(item['source']).as_posix(), replacement.as_posix())
        section = None
        output = []
        volume = 0
        directories = profile_directories(profile)
        for line in text.splitlines():
            if line.startswith('['):
                section = line.strip()
            key = line.split('=', 1)[0].strip()
            if key in ('nand_directory', 'sdmc_directory'):
                line = key + '=' + directories[key]
            if section == '[Audio]' and key == 'volume':
                line, volume = 'volume=0', volume + 1
            if section == '[Audio]' and key == 'volume\\default':
                line = 'volume\\default=false'
            output.append(line)
        require(volume == 1, 'Expected exactly one persisted [Audio] volume')
        require(all(value in '\n'.join(output) for value in directories.values()),
                'Private NAND/SDMC config paths missing')
        config.write_text('\n'.join(output) + '\n')
        plugins = profile / PLUGINS
        plugins.mkdir(parents=True, exist_ok=True)
        aside = profile / 'lifecycle-plugin-originals'
        aside.mkdir()
        if self.c.get('card_lineup', {}).get('asset_sums_sha256'):
            # The installed profile can contain mods/presets absent from this card stamp.
            ultimate = profile / 'user/sdmc/ultimate'
            for name in ('mods', 'arcropolis'):
                folder = ultimate / name
                if folder.is_dir():
                    folder.rename(aside / name)
                folder.mkdir(parents=True, exist_ok=True)
        storage = {'linked_bytes': 0, 'copied_bytes': 0}

        def stage(pin, target, immutable):
            source = Path(pin['path'])
            linked = False
            if immutable:
                linked = link_payload(source, target)
            else:
                shutil.copyfile(source, target)
            require(digest(target) == pin['sha256'], 'Staged input differs from pin: ' + str(target))
            storage['linked_bytes' if linked else 'copied_bytes'] += target.stat().st_size

        for path in plugins.glob('*.nro'):
            path.replace(aside / path.name)
        for pin in item['plugins']:
            source = Path(pin['path'])
            destination = plugins / pin.get('name', source.name)
            require(destination.parent == plugins, 'Plugin destination traversal')
            stage(pin, destination, True)
        for pin in item.get('files', []):
            target = profile / relative(pin['to'])
            safe_absolute(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()  # break any link before writing
            stage(pin, target, Path(MODS) in relative(pin['to']).parents)
        private_config(profile)
        item['profile_pin_storage'] = storage
        self.record('prepared', {'key': item['key'], 'snapshot_entries': len(expected), 'persisted_volume': 0,
                                 'profile_pin_storage': storage})

    def up(self):
        report = self.preflight(runtime=False)
        require(report['ok'], report['message'])
        self._preflight_static = report
        with self.lock():
            require(not self.journal.exists(), 'Run already journalled; use down/status, never blind up retry')
            root = Path(self.c['work_root'])
            require(not root.exists(), 'Fresh isolated work_root required')
            report = self.plan_check()
            if self.c.get('lifecycle_only'):
                require(report.get('result') == 'LIFECYCLE_ONLY' and report.get('rows') == 0,
                        'Explicit lifecycle-only proof required')
            else:
                require(not report.get('errors') and report.get('rows', 0) > 0
                        and report.get('result') in ('READY', 'READY_TO_RECORD'), 'Shared runner dry-run failed')
            save(self.directory / 'dry-run.json', report)
            # Validate pinned inputs before claim/staging. Never choose a newest binary.
            if self.c['host'] == 'ally':
                for pin in (self.c['windows_job'], self.c['windows_control'], self.c['windows_python']):
                    require(digest(pin['path']) == pin['sha256'], 'Windows helper mismatch before claim')
            for item in self.c['instances']:
                require(Path(item['source']).is_dir() and Path(item['game']).is_file(), 'Source/game missing')
                for pin in [item['app'], *item['plugins'], *item.get('files', [])]:
                    require(digest(pin['path']) == pin['sha256'], 'Pinned input mismatch: ' + pin['path'])
            token = uuid.uuid4().hex
            self.state = {'schema': 1, 'phase': 'claim-intent', 'owner': self.c['owner'], 'host': self.c['host'],
                          'label': self.c['label'], 'token': token, 'claimed': False, 'released': False,
                          'config': self.c, 'config_original': deepcopy(self.c), 'instances': [], 'started': stamp()}
            self.write()
            try:
                if self.c['host'] != 'ally':
                    try:
                        self.settle_pending()
                    except Exception as error:
                        self.record('pending-settle-error', {'error': repr(error)})
                admission = self.admission()
                problems, lines, checks = self.runtime_checks(admission)
                require(not problems, '; '.join(problems))
                self.state['preflight'] = {'ok': True, 'mutation': False,
                                           'lines': self._preflight_static['lines'] + lines,
                                           'checks': self._preflight_static['checks'] + checks}
                self.write()
                claim = self.claim_with_ports(admission)
                self.state['claimed'] = True
                if claim.get('already_claimed'):
                    self.state['claim_recovered_after_transport_failure'] = True
                self.state['stale_claim_skipped'] = claim.get('stale_claim_skipped', [])
                for skipped in self.state['stale_claim_skipped']:
                    self.record('stale_claim_skipped', skipped)
                self.record('claim', claim)
                self.write()
                root.mkdir(parents=True)
                for original in self.c['instances']:
                    key = original['key']
                    profile, run = root / 'profiles' / ('profile-' + key), self.directory / ('run-' + key)
                    profile.parent.mkdir(parents=True, exist_ok=True)
                    item = {**original, 'profile': str(profile), 'snapshot': str(self.directory / ('snapshot-' + key)),
                            'run': str(run), 'socket': str(run / 'control.sock'), 'console_log': str(run / 'console.log'),
                            'unit': f'eden-life-{token}-{key}.service', 'snapshot_ready': False, 'launch_intent': False,
                            'display_units': [f'eden-life-{token}-{key}-display.service', f'eden-life-{token}-{key}-display-x.service'],
                            'display_intent': False}
                    if self.c['host'] == 'ally':
                        item['socket'] = f'\\\\.\\pipe\\eden-life-{token}-{key}'
                    else:
                        require(len(os.fsencode(item['socket'])) < 108, 'Control socket path too long; choose shorter state_dir')
                    self.state['instances'].append(item)
                    self.write()
                    if self.c['host'] == 'ally':
                        self.host.clone_source(original, profile)
                    else:
                        clone_profile(original['source'], profile)
                    self.prepare(item)
                    self.admission([x['unit'] for x in self.state['instances'] if x['launch_intent']])
                    item['display_intent'] = bool(self.c.get('display_helper'))
                    self.write()
                    item['display_pids'] = self.host.start_display(item, self.c)
                    self.write()
                    item['launch_intent'] = True
                    self.write()  # recovery can stop this exact unit even if launch raises
                    self.retire_stale_runtime(item)
                    item['owned_pids'] = self.host.launch(item, self.c)
                    self.write()
                    save(Path(item['run']) / 'run.json', {'unit': item['unit'], 'state': 'frontend',
                         'owner': self.c['owner'], 'token': token, 'profile': item['profile'],
                         'app_sha256': item['app']['sha256'], 'owned_pids': item['owned_pids'],
                         'runtime_cap_s': self.c.get('runtime', 5400), 'started': stamp()})
                    self.poll(lambda: self.host.socket_ready(item), self.c.get('socket_cap', 90), 'control socket')
                    self.rpc(item, 'settings.set', {'key': 'core.volume', 'value': 0})
                    self.rpc(item, 'settings.save')
                    muted = self.rpc(item, 'settings.get', {'key': 'core.volume'})
                    require(mute_is_zero(muted), 'CLI mute readback failed')
                    for key, value in eden_boot_settings(self.c, item).items():
                        self.rpc(item, 'settings.set', {'key': key, 'value': value})
                        self.rpc(item, 'settings.save')
                        readback = self.rpc(item, 'settings.get', {'key': key})
                        require(setting_is(readback, value), 'CLI setting readback failed: ' + key)
                    # The CLI save must really persist mute, before any game.load.
                    private_config(profile)
                    run_state = json.loads((Path(item['run']) / 'run.json').read_text())
                    if self.c.get('defer_load'):
                        # Online pairs: Eden refuses to create or join a multiplayer room while a game runs
                        # ("Game already running", pkg4 2026-11-03). The rows set up the room through the frontend,
                        # then load the game with a runner {"call": "game.load"} step.
                        run_state.update(state='frontend-ready', game_load='deferred to the rows')
                    else:
                        self.rpc(item, 'game.load', {'path': item['game']}, load_cap(self.c))
                        self.poll(lambda: self.ready(item), load_cap(self.c), 'game loading complete')
                        run_state.update(state='running', game_loaded=stamp())
                    if self.c['host'] == 'ally':
                        run_state['loaded_status'] = self.rpc(item, 'status')
                        run_state['screenshot_target'] = 'screen'
                    save(Path(item['run']) / 'run.json', run_state)
                instances = {}
                for item in self.state['instances']:
                    instances[item['key']] = {**self.resolved_sources(item),
                        'socket': item['socket'], 'console_log': item['console_log'],
                        'gdb_port': item.get('gdb_port'), 'bridge_port': self.state['port_allocation']['instances'][item['key']].get('bridge'),
                        'display': item.get('display'),
                        'profile': item['profile'], 'control_log': str(Path(item['run']) / 'control.jsonl'),
                        'game': item['game'],
                        **({'trace_jsonl': str(Path(item['run']) / (item['key'] + '.trace.jsonl'))}
                           if item.get('trace') else {}),
                        **({'screenshot_target': 'screen'} if self.c['host'] == 'ally' else {})}
                save(self.directory / 'instances.json', instances)
                self.state['phase'] = 'up'
                self.write()
                self.start_owned_watchdog()
                return {'phase': 'up', 'instances': instances, 'instances_file': str(self.directory / 'instances.json'),
                        'stale_claim_skipped': self.state['stale_claim_skipped'],
                        'preflight': self.state.get('preflight'),
                        'watchdog_unit': self.state.get('watchdog_unit')}
            except BaseException as error:
                self.state['up_error'] = repr(error)
                self.write()
                self._down()
                raise

    def ready(self, item):
        value = self.rpc(item, 'status')
        return (isinstance(value, dict) and value.get('running') is True and value.get('loading_complete') is True
                and not value.get('modal') and isinstance(value.get('last_frame_age_ms'), (int, float))
                and value['last_frame_age_ms'] < 1500)

    def restore(self, item):
        backup, profile = Path(item['snapshot']), Path(item['profile'])
        expected = json.loads((self.directory / (item['key'] + '-snapshot.json')).read_text())
        require(inventory(backup) == expected, 'Immutable snapshot changed; retain claim')
        require(profile.parent == Path(self.c['work_root']) / 'profiles' and profile.name == 'profile-' + item['key'],
                'Foreign restore target refused')
        safe_absolute(profile)
        # Preserve all run-generated profile data before restoring the owned clone.
        preserved = self.directory / ('post-run-' + item['key'])
        if profile.exists():
            require(not preserved.exists(), 'Prior preserved profile exists; explicit recovery required')
            profile.rename(preserved)
        clone_profile(backup, profile)
        save(self.directory / (item['key'] + '-restore.json'), {'restored': True, 'preserved': str(preserved)})

    def verify(self, item):
        # Deliberately a separate full read, not an alias of the restore receipt.
        expected = json.loads((self.directory / (item['key'] + '-snapshot.json')).read_text())
        require(inventory(item['snapshot']) == expected and inventory(item['profile']) == expected,
                'Separate full-profile verification failed; retain claim')
        if self.c['host'] == 'ally':
            original = json.loads((Path(self.c['work_root']) / ('source-' + item['key'] + '.json')).read_text())
            require(inventory(item['source']) == original, 'Installed source profile changed; retain claim')
            save(self.directory / (item['key'] + '-source-verify.json'), {'unchanged': True, 'entries': len(original)})
        save(self.directory / (item['key'] + '-verify.json'), {'equal': True, 'snapshot_equal': True, 'entries': len(expected)})

    def _pending_path(self):
        token = self.state['token']
        require(re.fullmatch(r'[0-9a-f]{32}', token), 'Invalid owned lifecycle token')
        return safe_absolute(pending_release_dir() / (token + '.json'))

    def _write_pending(self):
        path = self._pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        config_path = self.directory / 'lifecycle-settle-config.json'
        save(config_path, self.c)
        save(path, {'ledger': self.c['ledger'], 'host': self.c['host'], 'owner': self.c['owner'],
                    'token': self.state['token'], 'ledger_ssh': self.c.get('ledger_ssh'),
                    'config_path': str(config_path), 'journal_path': str(self.journal), 'time': stamp()})

    def _settle_ledger(self, stop_unit=True):
        ownership = self.ledger().inspect()
        self.record('settle-readback', ownership)
        if ownership['owned_claim']:
            was_released = self.state.get('released')
            self.state['claimed'] = True
            self.state['released'] = False
            if ownership['owned_release']:
                self.record('settle-contradiction', ownership)
            if was_released or ownership['owned_release']:
                self.write()
            self.record('release', self.ledger().release())
            if ownership['owned_release']:
                readback = self.ledger().inspect()
                self.record('settle-readback', readback)
                require(not readback['owned_claim'], 'Open lifecycle claim remains after release')
            self.state['released'] = True
        elif ownership['owned_release']:
            self.state['released'] = True
        elif self.state['claimed']:
            raise Refused('Owned claim disappeared without its exact release record')
        unit = self.state.get('settle_unit')
        if stop_unit and unit:
            self.host.stop_settle(unit)
            self.state['settle_unit'] = None
        self.state['release_pending'] = False
        allocation = self.state.get('port_allocation')
        if allocation and not allocation.get('released'):
            allocation.update(released=True, released_at=stamp())
            save(self.directory / 'port-allocation.json', allocation)
        self.write()
        path = self._pending_path()
        if path.exists():
            path.unlink()
        return True

    def _note_settle_cap(self):
        unit = self.state.get('settle_unit')
        if not self.state.get('release_pending') or not unit or self.host.settle_running(unit):
            return
        receipt = self.directory / 'RELEASE-PENDING.md'
        lines = receipt.read_text(encoding='utf-8').splitlines() if receipt.is_file() else []
        starts = [i for i, line in enumerate(lines) if line.startswith('settle unit started: ')]
        if starts:
            lines = lines[starts[-1] + 1:]
        final = ('released at ', 'release still pending after ', 'release still pending: stopped on ',
                 'settle unit stopped by its time cap')
        if not any(line.startswith(final) for line in lines):
            self.pending_receipt('settle unit stopped by its time cap')

    def _start_settle_unit(self):
        if self.c['host'] == 'ally':
            return
        unit = f"eden-settle-{self.state['token'][:8]}.service"
        try:
            previous = self.state.get('settle_unit')
            if previous:
                if self.host.settle_running(previous):
                    return
                self.host.stop_settle(previous)
                self.state['settle_unit'] = None
            script = self.directory / 'lifecycle-settle.py'
            script.write_text(standalone_source(), encoding='utf-8')
            config_path = self.directory / 'lifecycle-settle-config.json'
            command = ['/usr/bin/python3', '-I', '-B', str(script), 'settle', str(config_path), '--execute']
            self.host.start_settle(unit, command, self.directory, 30 * 60)
            self.state['settle_unit'] = unit
            self.write()
            self.record('settle-start', {'unit': unit})
            self.pending_receipt(f'settle unit started: {unit}')
        except Exception as error:
            self.record('settle-start-error', {'unit': unit, 'error': repr(error)})

    def _finish_down(self, failures, stop_errors):
        diagnosis = None
        if not failures:
            try:
                self._settle_ledger()
            except LedgerTransport as error:
                self.state['release_pending'] = True
                diagnosis = error.diagnoses[-1]
                self.record('release-pending', {'error': str(error)})
                try:
                    self._write_pending()
                    line = f"release pending since {datetime.now(timezone.utc):%H:%M:%S}Z: " + \
                           ssh_diagnosis_with_receipt(diagnosis)
                    self.pending_receipt(line)
                except Exception as pending_error:
                    failures.append({'stage': 'pending-record', 'error': repr(pending_error)})
            except Exception as error:
                failures.append({'stage': 'release', 'error': repr(error)})
        self.state['cleanup_failures'] = failures
        self.state['phase'] = 'cleanup-failed' if failures else 'down'
        self.write()
        if not failures and self.state.get('release_pending') and diagnosis:
            if diagnosis.get('transient', False):
                self._start_settle_unit()
                self.notify(line)
            else:
                unit = self.state.get('settle_unit')
                if unit:
                    try:
                        self.host.stop_settle(unit)
                        self.state['settle_unit'] = None
                        self.write()
                    except Exception as error:
                        self.record('settle-stop-error', {'unit': unit, 'error': repr(error)})
                line = f"release pending: not retried (cause: {diagnosis.get('cause', 'unknown')}); manual Down needed"
                self.pending_receipt(line)
                self.notify(line)
        require(not failures, 'Cleanup incomplete; claim retained: ' + repr(failures))
        return {'phase': 'down', 'released': self.state['released'],
                'release_pending': self.state.get('release_pending', False),
                'settle_unit': self.state.get('settle_unit'), 'game_stop_errors': stop_errors}

    def settle_pending(self):
        folder = pending_release_dir()
        if not folder.is_dir():
            return
        for path in sorted(folder.glob('*.json')):
            try:
                safe_absolute(path)
                pending = json.loads(path.read_text())
                if (pending.get('host') != self.c['host'] or pending.get('ledger') != self.c['ledger']
                        or pending.get('ledger_ssh') != self.c.get('ledger_ssh')):
                    continue
                token = pending['token']
                require(re.fullmatch(r'[0-9a-f]{32}', token) and path.name == token + '.json',
                        'Invalid pending release token')
                journal = safe_absolute(pending['journal_path'])
                value = json.loads(journal.read_text())
                config = value['config']
                require(journal.name == 'lifecycle.json' and journal.parent == Path(config['state_dir'])
                        and value['token'] == token and value['owner'] == pending['owner']
                        and config['owner'] == pending['owner'] and config['host'] == pending['host']
                        and config['ledger'] == pending['ledger'] and config.get('ledger_ssh') == pending.get('ledger_ssh'),
                        'Pending release does not match its journal')
                other = Lifecycle(config, self.host, self.injected_ledger)
                with other.lock():
                    other.state = json.loads(other.journal.read_text())
                    other.check_state()
                    require(other.state['token'] == token and other.state['phase'] == 'down'
                            and (other.state.get('release_pending') or other.state.get('released')),
                            'Pending release journal is not a clean Down')
                    other._settle_ledger()
                self.record('pending-settled', {'token': token})
            except Exception as error:
                self.record('pending-settle-error', {'path': str(path), 'error': repr(error)})

    def settle(self, minutes=30):
        require(self.host.name().casefold() == self.c['hostname'].casefold(), 'Wrong executor hostname')
        require(isinstance(minutes, (int, float)) and not isinstance(minutes, bool)
                and math.isfinite(minutes) and 0 < minutes <= 30, 'settle minutes must be between 0 and 30')
        deadline = self.host.clock() + minutes * 60
        last_start = deadline - 60
        next_attempt = self.host.clock() + 60
        attempts = 0
        last = 'no attempt completed'
        while self.host.clock() < deadline:
            if next_attempt > last_start or self.host.clock() > last_start:
                self.host.sleep(max(0, deadline - self.host.clock() - 1))
                break
            wait = min(next_attempt, deadline) - self.host.clock()
            if wait > 0:
                self.host.sleep(wait)
            if self.host.clock() > last_start:
                break
            acquired = False
            try:
                with self.lock():
                    acquired = True
                    require(self.journal.is_file(), 'No owned lifecycle journal')
                    self.state = json.loads(self.journal.read_text())
                    self.check_state()
                    require(self.state['phase'] == 'down', 'Settle requires a clean Down')
                    if not self.state.get('release_pending'):
                        require(self.state['released'] or not self.state['claimed'], 'Settle requires a pending release')
                        return {'phase': 'down', 'released': self.state['released'], 'release_pending': False}
                    if self.host.clock() > last_start:
                        next_attempt = deadline
                        continue
                    attempts += 1
                    next_attempt = self.host.clock() + 60
                    try:
                        with settle_attempt_deadline(deadline - self.host.clock() - 5):
                            self._settle_ledger(stop_unit=False)
                        self.record('release-settled', {'released': self.state['released']})
                        line = f"released at {datetime.now(timezone.utc):%H:%M}Z after {attempts} attempts"
                        self.pending_receipt(line)
                        self.notify(line)
                        return {'phase': 'down', 'released': self.state['released'], 'release_pending': False}
                    except LedgerTransport as error:
                        diagnosis = error.diagnoses[-1]
                        last = ssh_diagnosis_with_receipt(diagnosis)
                        self.pending_receipt(f"release pending since {datetime.now(timezone.utc):%H:%M:%S}Z: {last}")
                        self.record('settle-retry', {'error': str(error)})
                        if not diagnosis.get('transient', False):
                            line = f'release still pending: stopped on a non-transient cause: {last}; manual Down needed'
                            self.pending_receipt(line)
                            self.notify(line)
                            return {'phase': 'down', 'released': False, 'release_pending': True}
                    except SettleDeadline:
                        last = 'attempt exceeded the settle deadline'
                        break
            except BlockingIOError:
                if acquired:
                    raise
                self.host.sleep(min(1, max(0, deadline - self.host.clock())))
        line = f'release still pending after {minutes:g} minutes: manual Down needed; last: {last}'
        self.pending_receipt(line)
        self.notify(line)
        return {'phase': 'down', 'released': False, 'release_pending': True}

    def _down(self):
        failures, stop_errors = [], []
        self._stop_watchdog_unit()
        self.state['phase'] = 'stopping'
        self.write()
        for item in reversed(self.state['instances']):
            if item['launch_intent']:
                try:
                    self.rpc(item, 'game.stop', cap=self.c.get('stop_cap', 30))
                except Exception as error:
                    stop_errors.append({'key': item['key'], 'error': repr(error)})
                    self.record('game-stop-error', stop_errors[-1])
                try:
                    try:
                        self.record('resource-before-stop', self.host.metrics(item))
                    except Exception as error:
                        self.record('resource-read-error', {'error': repr(error)})
                    result = self.host.force_stop(item, self.c.get('force_cap', 30))
                    self.record('forced-stop', result)
                    require(not self.host.owned(item), 'Owned leftover process; no restore/release')
                    run = Path(item['run'])
                    if run.is_dir():
                        try:
                            metrics = self.host.metrics(item)
                        except Exception as error:
                            metrics = {'memory_peak': None, 'error': repr(error)}
                        (run / 'memory.peak').write_text(str(metrics['memory_peak']) + '\n')
                        metadata = json.loads((run / 'run.json').read_text()) if (run / 'run.json').exists() else {}
                        metadata.update(unit=item['unit'], state='stopped', stopped=stamp(),
                                        owned_pids_stopped=result, resources=metrics)
                        save(run / 'run.json', metadata)
                except Exception as error:
                    failures.append({'key': item['key'], 'stage': 'stop', 'error': repr(error)})
            if item.get('display_intent'):
                for unit in reversed(item['display_units']):
                    try:
                        self.host.force_stop({'unit': unit, 'owned_pids': item.get('display_pids', {}).get(unit, [])},
                                             self.c.get('force_cap', 30))
                        require(not self.host.owned({'unit': unit}), 'Owned display PID remains')
                    except Exception as error:
                        failures.append({'key': item['key'], 'stage': 'display-stop', 'error': repr(error)})
        # Do not restore ANY profile while even one owned guest remains alive.
        if not any(f.get('stage') in ('stop', 'display-stop') for f in failures):
            for item in reversed(self.state['instances']):
                if item['snapshot_ready'] and not item.get('verified'):
                    try:
                        if not item.get('restored'):
                            self.restore(item)
                            item['restored'] = True
                            self.write()
                        self.verify(item)
                        item['verified'] = True
                        self.write()
                    except Exception as error:
                        failures.append({'key': item['key'], 'stage': 'restore/verify', 'error': repr(error)})
        self.state['stop_errors'] = stop_errors
        if failures:
            return self._finish_down(failures, stop_errors)
        return self._finish_down(failures, stop_errors)

    def prune(self, evidence_dir=None):
        """After a clean Down, copy the files each instance's run wrote (its post-run profile against the snapshot)
        to <evidence>/post-run-<key>/ with a manifest <evidence>/post-run-<key>.json, then delete the run's copies:
        the work_root profiles, state/snapshot-<key> and state/post-run-<key>. The journal, actions.jsonl, the
        receipts and the run-<key> logs stay. Evidence is saved once: a later Down finishes an interrupted delete."""
        record = self.state.get('prune') or {}
        record.pop('kept', None)
        root = safe_absolute(self.c['work_root'])
        items = self.state['instances']
        post_runs = {item['key']: self.directory / ('post-run-' + item['key']) for item in items}
        trees = [*(Path(item['profile']) for item in items), *(Path(item['snapshot']) for item in items),
                 *post_runs.values()]
        if not record.get('evidence_saved'):
            evidence = safe_absolute(evidence_dir or self.directory / 'evidence')
            require(evidence != self.directory and not any(evidence == tree or tree in evidence.parents
                                                           for tree in (root, *trees)),
                    'Evidence folder inside a pruned tree or the state folder itself')
            evidence.mkdir(parents=True, exist_ok=True)
            record = {'evidence': str(evidence), 'copied_files': 0, 'copied_bytes': 0, 'listed_files': 0}
            for item in items:
                key, post_run = item['key'], post_runs[item['key']]
                if not post_run.is_dir():
                    continue
                expected = json.loads((self.directory / (key + '-snapshot.json')).read_text())
                changed, gone = run_files(post_run, item['snapshot'], expected)
                # Bytes already known before the run (pinned inputs, snapshot files moved aside) are not its output.
                known = {value['sha256'] for value in expected.values() if value['type'] == 'file'}
                known.update(pin['sha256'] for pin in [item['app'], *item['plugins'], *item.get('files', [])])
                manifest = {'post_run': str(post_run), 'copied': [], 'listed': [], 'known_content': 0, 'gone': gone}
                for rel, info in changed:
                    name = rel.as_posix()
                    if any(name.startswith(p + '/') for p in REBUILT_CACHES):
                        manifest['listed'].append({'path': name, 'size': info.st_size})
                        continue
                    value = digest(post_run / rel)
                    if value in known:
                        manifest['known_content'] += 1
                        continue
                    target = evidence / ('post-run-' + key) / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(post_run / rel, target)
                    require(digest(target) == value, 'Evidence copy differs: ' + str(target))
                    manifest['copied'].append({'path': name, 'size': info.st_size, 'sha256': value})
                save(evidence / ('post-run-' + key + '.json'), manifest)
                record['copied_files'] += len(manifest['copied'])
                record['copied_bytes'] += sum(entry['size'] for entry in manifest['copied'])
                record['listed_files'] += len(manifest['listed'])
            record['evidence_saved'] = stamp()
            self.state['prune'] = record
            self.write()
        targets = [tree for tree in trees if tree.exists()]
        inodes, size, skipped = {}, 0, []
        for tree in targets:
            for path, info in tree_entries(tree):
                if stat.S_ISREG(info.st_mode):
                    size += info.st_size
                    seen = inodes.setdefault((info.st_dev, info.st_ino) if info.st_ino else str(path),
                                             [0, info.st_nlink, info.st_size])
                    seen[0] += 1
        free = shutil.disk_usage(self.directory).free
        for tree in targets:
            remove_tree(tree, skipped)
        for item in items:
            source = root / ('source-' + item['key'] + '.json')  # the Ally's installed-source inventory
            if source.is_file():
                source.unlink()
        for path in (root / 'profiles', root):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        # unique_bytes: files whose every link was in the pruned trees, so deleting them freed their blocks.
        record.update(done=stamp(), bytes=size, unique_bytes=sum(s for n, links, s in inodes.values() if n >= links),
                      disk_free_before=free, disk_free_after=shutil.disk_usage(self.directory).free,
                      removed=[str(tree) for tree in targets], skipped=skipped,
                      left=sorted(str(path) for path in root.iterdir()) if root.exists() else [])
        record.pop('error', None)
        self.state['prune'] = record
        self.write()
        self.record('prune', record)
        return record

    def down(self, keep_state=False, evidence_dir=None):
        require(self.host.name().casefold() == self.c['hostname'].casefold(), 'Wrong executor hostname; no cleanup action')
        with self.lock():
            require(self.journal.is_file(), 'No owned lifecycle journal')
            self.state = json.loads(self.journal.read_text())
            self.check_state()
            if self.state['phase'] == 'down':
                self._stop_watchdog_unit()
                if self.state.get('release_pending'):
                    self._note_settle_cap()
                    result = self._finish_down([], self.state.get('stop_errors', []))
                    result['already_down'] = True
                else:
                    result = {'phase': 'down', 'released': self.state['released'], 'already_down': True}
            else:
                result = self._down()
            prune = self.state.get('prune') or {}
            may_prune = not self.state.get('release_pending') and (
                self.state.get('released') or not self.state.get('claimed'))
            if may_prune and keep_state and not prune.get('done'):
                self.state['prune'] = {**prune, 'kept': stamp()}
                self.write()
            elif may_prune and not prune.get('done'):
                try:
                    self.prune(evidence_dir)
                except Exception as error:
                    self.state['prune'] = {**(self.state.get('prune') or {}), 'error': repr(error)}
                    self.write()
                    self.record('prune-error', {'error': repr(error)})
            result['prune'] = self.state.get('prune')
            return result

    def status(self):
        require(self.host.name().casefold() == self.c['hostname'].casefold(), 'Wrong executor hostname')
        if not self.journal.is_file():
            return {'phase': 'absent'}
        value = json.loads(self.journal.read_text())
        self.state = value
        self.check_state()
        unit = value.get('watchdog_unit')
        settle = value.get('settle_unit')
        self._note_settle_cap()
        receipt = self.directory / 'RELEASE-PENDING.md'
        lines = receipt.read_text(encoding='utf-8').splitlines() if receipt.is_file() else []
        return {'phase': value['phase'], 'claimed': value['claimed'], 'released': value['released'],
                'release_pending': value.get('release_pending', False), 'settle_unit': settle,
                'settle_running': bool(settle) and self.host.settle_running(settle),
                'release_pending_last_line': lines[-1] if lines else None,
                'owned_pids': {x['key']: self.host.owned(x) for x in value['instances'] if x['launch_intent']},
                'watchdog_unit': unit, 'watchdog_running': bool(unit) and self.host.watchdog_running(unit),
                'cleanup_failures': value.get('cleanup_failures', [])}


def ssh_phase(text):
    for pattern, phase in ((r'Authenticated to|Entering interactive session', 'session'),
                           (r'SSH2_MSG_NEWKEYS received', 'auth'),
                           (r'Remote protocol version', 'kex'),
                           (r'Connection established', 'banner'),
                           (r'Connecting to', 'tcp')):
        if re.search(pattern, text or '', re.I):
            return phase
    return 'unknown'


def target_healthy(diag_login, target):
    if not diag_login.startswith('ok in ') or not isinstance(target, dict):
        return False
    if 'cpuLoadPct' in target or target.get('journal') == 'unavailable on Windows':
        return False
    keys = ('load1', 'cores', 'memAvailMiB', 'oomKills5m', 'maxStartupsLines', 'sshdUnauth')
    try:
        if any(type(target.get(key)) not in (int, float) or not math.isfinite(target[key]) or target[key] < 0
               for key in keys):
            return False
    except OverflowError:
        return False
    maximum = re.match(r'^(?:default\s+)?(\d+)', str(target.get('maxStartups', '')))
    return bool(target['cores'] > 0 and maximum and target['maxStartupsLines'] == 0
                and target['sshdUnauth'] < int(maximum.group(1)) and target['load1'] / target['cores'] < 4
                and target['memAvailMiB'] >= 300 and target['oomKills5m'] == 0)


def ssh_cause(text, phase, diag_login, tcp, loss, target=None, address='', rtt_min=None, rtt_max=None,
              attempt_timed_out=False, attempt_killed=False):
    matches = re.findall(r'^.*(?:timed out|Connection (?:closed|reset|refused)|Permission denied|'
                         r'Host key verification failed|Could not resolve|No route to host).*$', text, re.I | re.M)
    message = matches[-1].strip()[:160] if matches else ''
    if not message and attempt_killed:
        message = 'none (attempt killed at the 10 s bound)'
    if re.search('Could not resolve hostname', text, re.I):
        return 'name does not resolve', False, message
    if re.search('Host key verification failed', text, re.I):
        return 'host key', False, message
    if re.search('Permission denied', text, re.I):
        return 'authentication refused', False, message
    if re.search('Connection refused|No route to host', text, re.I) or tcp == 'fail':
        return 'host down or sshd not listening', False, message
    if target:
        maximum = re.match(r'^(?:default\s+)?(\d+)', str(target.get('maxStartups', '')))
        unauth = target.get('sshdUnauth')
        drops = target.get('maxStartupsLines')
        if ((type(drops) in (int, float) and drops > 0) or
                (type(unauth) in (int, float) and math.isfinite(unauth) and unauth >= 0 and
                 maximum and unauth >= int(maximum.group(1)))):
            return 'sshd connection throttling on the target (MaxStartups)', False, message
        load, cores = target.get('load1'), target.get('cores')
        memory, oom = target.get('memAvailMiB'), target.get('oomKills5m')
        if ((isinstance(load, (int, float)) and isinstance(cores, (int, float)) and cores > 0 and load / cores >= 4)
                or (isinstance(memory, (int, float)) and memory < 300)
                or (isinstance(oom, (int, float)) and oom > 0)):
            values = [value if value is not None else 'n/a' for value in (load, cores, memory, oom)]
            return (f'target overloaded (load {values[0]} on {values[1]} cores, '
                    f'{values[2]} MiB available, {values[3]} OOM kills)'), False, message
    match_loss = re.fullmatch(r'(\d+(?:\.\d+)?)%', loss)
    if phase in ('tcp', 'banner', 'kex', 'auth', 'unknown') and match_loss and float(match_loss.group(1)) > 0:
        healthy = target_healthy(diag_login, target)
        suffix = '' if healthy else '; target health not confirmed, no retry'
        return f'packet loss {loss} on the path to {address}{suffix}', healthy, message
    if (phase in ('tcp', 'banner', 'kex', 'auth', 'unknown') and rtt_min is not None and rtt_max is not None
            and float(rtt_max) >= 100 and float(rtt_max) >= 10 * float(rtt_min)):
        healthy = target_healthy(diag_login, target)
        suffix = '' if healthy else '; target health not confirmed, no retry'
        return f'RTT spikes on the path to {address} (max {rtt_max} ms){suffix}', healthy, message
    if ((attempt_timed_out or re.search('timed out', text, re.I)) and phase in ('tcp', 'banner', 'kex')
            and tcp == 'ok' and target_healthy(diag_login, target)
            and type(target.get('load1')) in (int, float) and type(target.get('cores')) in (int, float)
            and target['cores'] > 0 and type(target.get('memAvailMiB')) in (int, float)):
        return (f"single {phase} timeout with TCP 22 answering and a healthy target "
                f"(load {target['load1']} on {target['cores']} cores, {target['memAvailMiB']} MiB available)"), True, message
    cause = f'not established (attempt stalled in {phase}; {diag_login})'
    if ((attempt_timed_out or re.search('timed out', text, re.I)) and phase in ('tcp', 'banner', 'kex')
            and tcp == 'ok' and isinstance(target, dict)
            and ('cpuLoadPct' in target or target.get('journal') == 'unavailable on Windows')):
        cause += '; target health not confirmed, no retry'
    return cause, False, message


def _tcp_probe(hostname, port):
    result = queue.Queue(maxsize=1)
    def connect():
        try:
            with socket.create_connection((hostname, port), timeout=5):
                result.put(True)
        except (OSError, ValueError, OverflowError):
            result.put(False)
    thread = threading.Thread(target=connect, daemon=True)
    thread.start()
    thread.join(5)
    return not thread.is_alive() and result.get_nowait()


def _ssh_process_count(host, hostname, run=subprocess.run):
    count = 0
    proc = Path('/proc')
    if not proc.is_dir():
        if os.name != 'nt':
            return 'n/a'
        safe_host = host.replace("'", "''")
        safe_hostname = hostname.replace("'", "''")
        script = (f"$p = @(Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | "
                  f"Where-Object {{ $_.CommandLine.Contains('{safe_host}') -or "
                  f"$_.CommandLine.Contains('{safe_hostname}') }}); $p.Count")
        encoded = base64.b64encode(script.encode('utf-16-le')).decode()
        try:
            done = run([r'C:\Program Files\PowerShell\7\pwsh.exe', '-NoLogo', '-NoProfile',
                        '-NonInteractive', '-EncodedCommand', encoded], capture_output=True, text=True, timeout=5)
            return int(done.stdout.strip()) if done.returncode == 0 else 'n/a'
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 'n/a'
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = (entry / 'cmdline').read_bytes().split(b'\0')
            if parts and Path(os.fsdecode(parts[0])).name in ('ssh', 'ssh.exe'):
                command = b'\0'.join(parts).decode(errors='replace')
                count += host in command or hostname in command
        except (OSError, ValueError):
            pass
    return count


def _control_socket_count(directory):
    if not directory:
        return 0
    count = 0
    try:
        for entry in Path(directory).iterdir():
            try:
                count += stat.S_ISSOCK(entry.lstat().st_mode)
            except OSError:
                pass
    except OSError:
        pass
    return count


SSH_TARGET_LINUX = r"""
$client = ($env:SSH_CONNECTION -split '\s+')[0]
$load1 = [double]::Parse((Get-Content /proc/loadavg -Raw).Split(' ')[0], [Globalization.CultureInfo]::InvariantCulture)
$memory = @{}
foreach ($line in (Get-Content /proc/meminfo)) {
    if ($line -match '^(MemAvailable|SwapFree):\s+(\d+)') { $memory[$Matches[1]] = [math]::Round([double]$Matches[2] / 1024, 1) }
}
$kernel = @(& journalctl -k --since '-5 min' --no-pager -o cat 2>$null)
$oom = if ($LASTEXITCODE -eq 0) { @($kernel | Where-Object { $_ -match 'Out of memory|oom-kill' }).Count } else { $null }
$journal = @(& journalctl -u ssh -u sshd --since '-5 min' --no-pager -o cat 2>$null)
$preauth = $null; $drops = $null
if ($LASTEXITCODE -eq 0) {
    $lines = @($journal | Where-Object { $_ -match 'Timeout before authentication|Connection (reset|closed) by .* \[preauth\]|kex_exchange_identification' })
    $ours = if ($client) {
        $address = '(?<![0-9A-Fa-f:.])' + [regex]::Escape($client) + '(?![0-9A-Fa-f:.])'
        @($lines | Where-Object { $_ -match $address }).Count
    } else { 0 }
    $preauth = [ordered]@{ total = $lines.Count; client = $ours }
    $drops = @($journal | Where-Object { $_ -match 'MaxStartups|drop connection' }).Count
}
$sshd = if (Test-Path /usr/sbin/sshd) { '/usr/sbin/sshd' } else { (Get-Command sshd -ErrorAction SilentlyContinue | Select-Object -First 1).Source }
$max = $null
if ($sshd) {
    $effective = @(& $sshd -T 2>$null)
    if ($LASTEXITCODE -eq 0) { $max = ($effective | Where-Object { $_ -match '^maxstartups\s+' } | Select-Object -First 1) -replace '^maxstartups\s+', '' }
}
if (-not $max) {
    $files = @('/etc/ssh/sshd_config') + @(Get-ChildItem /etc/ssh/sshd_config.d/*.conf -ErrorAction SilentlyContinue | Sort-Object Name | ForEach-Object FullName)
    foreach ($file in $files) {
        $line = Get-Content -LiteralPath $file -ErrorAction SilentlyContinue | Where-Object { $_ -match '^\s*MaxStartups\s+(\S+)' } | Select-Object -First 1
        if ($line -match '^\s*MaxStartups\s+(\S+)') { $max = $Matches[1]; break }
    }
}
if (-not $max) { $max = 'default 10:30:100' }
$processRows = @(& ps -eo 'pid=,ppid=,comm=,args=' 2>$null)
$sshdProcesses = $null; $sshdUnauth = $null
if ($LASTEXITCODE -eq 0) {
    $sshdProcesses = 0
    $pending = @{}
    foreach ($row in $processRows) {
        if ($row -cmatch '^\s*(\d+)\s+(\d+)\s+(sshd\S*)\s*(.*)$') {
            $processId = [int]$Matches[1]; $parentId = [int]$Matches[2]
            $comm = $Matches[3]; $arguments = $Matches[4]
            $sshdProcesses++
            if ($comm -ceq 'sshd-auth' -or
                $arguments -cmatch '\[preauth\]' -or
                $arguments -cmatch '^sshd(?:-session)?: .*\[net\]' -or
                $arguments -cmatch '^sshd: (?:\[accepted\]|unknown \[priv\])') {
                $pending[$processId] = $parentId
            }
        }
    }
    $sshdUnauth = 0
    foreach ($processId in $pending.Keys) {
        if (-not $pending.ContainsKey($pending[$processId])) { $sshdUnauth++ }
    }
}
[ordered]@{ client = $client; load1 = $load1; cores = [Environment]::ProcessorCount
    memAvailMiB = $memory['MemAvailable']; swapFreeMiB = $memory['SwapFree']; oomKills5m = $oom
    sshdPreauth5m = $preauth; maxStartupsLines = $drops
    sshdProcesses = $sshdProcesses; sshdUnauth = $sshdUnauth; maxStartups = $max } | ConvertTo-Json -Depth 4 -Compress
"""

SSH_TARGET_WINDOWS = r"""
$cpu = @(Get-CimInstance Win32_Processor | ForEach-Object { $_.LoadPercentage })
$os = Get-CimInstance Win32_OperatingSystem
[ordered]@{ client = ($env:SSH_CONNECTION -split '\s+')[0]
    cpuLoadPct = if ($cpu.Count) { [math]::Round(($cpu | Measure-Object -Average).Average, 1) } else { $null }
    memAvailMiB = if ($os) { [math]::Round($os.FreePhysicalMemory / 1024, 1) } else { $null }
    sshdProcesses = @(Get-Process -Name 'sshd*' -ErrorAction SilentlyContinue).Count
    sshdUnauth = $null
    journal = 'unavailable on Windows' } | ConvertTo-Json -Compress
"""


def ssh_diagnosis(host, run=subprocess.run, evidence_dir=None, tcp=None, ssh_options=(),
                  attempt_log=None, mux_dir=None, ssh_bin='ssh', windows=False, allow_retry=True,
                  attempt_timed_out=False, attempt_killed=False, rider=False):
    require(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host), 'Unsafe SSH host')
    directory = Path(evidence_dir) if evidence_dir is not None else (
        Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state') / 'eden-lifecycle' / 'ssh-diag')
    try:
        text = Path(attempt_log).read_text(encoding='utf-8', errors='replace') if attempt_log else ''
    except OSError:
        text = ''
    try:
        directory.mkdir(parents=True, exist_ok=True)
        evidence = directory / ('ssh-' + uuid.uuid4().hex + '.log')
        evidence.write_text(text, encoding='utf-8')
    except OSError:
        directory = Path(tempfile.mkdtemp(prefix='eden-ssh-diag-'))
        evidence = directory / ('ssh-' + uuid.uuid4().hex + '.log')
        evidence.write_text(text, encoding='utf-8')
    phase = ssh_phase(text)
    probe_log = evidence.with_suffix('.log.diag')
    direct = ['-T', *SSH_OPTIONS, *ssh_options, '-o', 'ControlPath=none']
    started = time.monotonic()
    try:
        probe = run([ssh_bin, '-vvv', '-E', str(probe_log), *direct, host, 'exit'],
                    capture_output=True, text=True, timeout=10)
        code, probe_text = probe.returncode, probe.stderr or ''
    except subprocess.TimeoutExpired as error:
        code, probe_text = 124, error.stderr or ''
    except OSError as error:
        code, probe_text = 255, str(error)
    if isinstance(probe_text, bytes):
        probe_text = probe_text.decode(errors='replace')
    probe_text = (probe_log.read_text(encoding='utf-8', errors='replace') if probe_log.exists() else '') + probe_text
    probe_log.write_text(probe_text, encoding='utf-8')
    diag_login = f'ok in {time.monotonic() - started:.1f} s' if code == 0 else f'failed, phase {ssh_phase(probe_text)}'
    target, target_display = None, 'unavailable (diagnostic login failed)'
    if code == 0:
        script = SSH_TARGET_WINDOWS if windows else SSH_TARGET_LINUX
        encoded = base64.b64encode(script.encode('utf-16-le')).decode()
        exe = r'"C:\Program Files\PowerShell\7\pwsh.exe"' if windows else '/usr/bin/pwsh'
        command = f'{exe} -NoLogo -NoProfile -NonInteractive -EncodedCommand {encoded}'
        try:
            result = run([ssh_bin, *direct, host, command], capture_output=True, text=True, timeout=10)
            if result.returncode:
                target_display = f'unavailable (target command exit {result.returncode})'
            else:
                target = json.loads(result.stdout)
                if not isinstance(target, dict) or not {'sshdProcesses', 'sshdUnauth', 'memAvailMiB'} <= target.keys():
                    raise ValueError('missing target fields')
                threshold = re.match(r'^(?:default\s+)?(\d+)', str(target.get('maxStartups', '')))
                unauth = target.get('sshdUnauth')
                target_display = ('unauth ' + (str(unauth) if unauth is not None else 'n/a') + '/' +
                                  (threshold.group(1) if threshold else 'n/a') + ' ' +
                                  json.dumps(target, separators=(',', ':'), ensure_ascii=False).replace(';', ' '))
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            target, target_display = None, 'unavailable (invalid target JSON or target command failed)'
    try:
        config = run([ssh_bin, '-G', *direct, host], capture_output=True, text=True, timeout=5)
        hostname_match = re.search(r'(?m)^hostname\s+(\S+)\s*$', config.stdout or '')
        port_match = re.search(r'(?m)^port\s+(\d+)\s*$', config.stdout or '')
        hostname = hostname_match.group(1) if hostname_match else host
        port = int(port_match.group(1)) if port_match else 22
    except (OSError, subprocess.TimeoutExpired):
        hostname, port = host, 22
    try:
        reachable = (tcp or _tcp_probe)(hostname, port)
    except OSError:
        reachable = False
    tcp_status = 'ok' if reachable else 'fail'
    try:
        ping = run(['ping', '-c', '10', '-i', '0.2', '-W', '1', hostname],
                   capture_output=True, text=True, timeout=8)
        ping_text = ping.stdout or ''
    except (OSError, subprocess.TimeoutExpired):
        ping_text = ''
    loss_match = re.search(r'(\d+(?:\.\d+)?)%\s+packet loss', ping_text)
    rtt_match = re.search(r'(?:rtt|round-trip)[^=]*=\s*([\d.]+)/([\d.]+)/([\d.]+)/', ping_text)
    loss = loss_match.group(1) + '%' if loss_match else 'n/a'
    avg = rtt_match.group(2) + ' ms' if rtt_match else 'n/a'
    try:
        qos = run([ssh_bin, *direct, '-o', 'IPQoS=none', host, 'exit'],
                  capture_output=True, text=True, timeout=10)
        qos_status = 'ok' if qos.returncode == 0 else 'fail'
    except (OSError, subprocess.TimeoutExpired):
        qos_status = 'fail'
    cause, transient, message = ssh_cause(text, phase, diag_login, tcp_status, loss, target, hostname,
                                          rtt_match.group(1) if rtt_match else None,
                                          rtt_match.group(3) if rtt_match else None,
                                          attempt_timed_out, attempt_killed)
    sockets = _control_socket_count(mux_dir)
    attempt_phase = f'{phase} (rider on an open master)' if rider and phase == 'session' else phase
    line = (f'ssh diag {socket.gethostname()} -> {host} ({hostname}): attempt phase {attempt_phase}; '
            f'message {message or "none"}; diag login {diag_login}; tcp22 {tcp_status}; ping loss {loss} avg {avg}; '
            f'IPQoS=none probe {qos_status}; target {target_display}; '
            f'local ssh processes {_ssh_process_count(host, hostname, run)}; control sockets {sockets}; '
            f'cause: {cause}; retry: {"yes, once" if transient and allow_retry else "no"}; evidence {evidence}')
    print(line, file=sys.stderr, flush=True)
    return {'line': line, 'transient': transient, 'phase': phase, 'cause': cause, 'evidence': str(evidence)}


def ssh_receipt(diagnosis, where=None):
    default = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state') / 'eden-lifecycle' / 'ssh-diag'
    intended = (Path(where) if where is not None else default) / 'ssh-diag.md'
    uid = os.getuid() if hasattr(os, 'getuid') else 'unknown'
    targets = (intended, default / 'ssh-diag-fallback.md', Path(tempfile.gettempdir()) / f'eden-ssh-diag-{uid}.md')
    line = str(diagnosis.get('line', 'ssh diagnosis unavailable')).replace('\r', ' ').replace('\n', ' ')
    text = f"{stamp()} {diagnosis.get('kind', 'ssh')} {diagnosis.get('what', 'connection')} : {line}\n"
    for index, path in enumerate(targets):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if index == 2:
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0) | \
                        getattr(os, 'O_NONBLOCK', 0)
                descriptor = os.open(path, flags, 0o600)
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (
                            hasattr(os, 'getuid') and info.st_uid != os.getuid()):
                        raise OSError('unsafe temporary receipt')
                    stream = os.fdopen(descriptor, 'a', encoding='utf-8')
                except Exception:
                    os.close(descriptor)
                    raise
            else:
                stream = path.open('a', encoding='utf-8')
            with stream:
                stream.write((f'intended {intended}: ' if index else '') + text)
                stream.flush()
                os.fsync(stream.fileno())
            return str(path)
        except OSError:
            continue
    return None


def ssh_record(diagnosis, kind, what, where=None):
    diagnosis = dict(diagnosis)
    diagnosis['receipt'] = ssh_receipt({**diagnosis, 'kind': kind, 'what': what}, where)
    return diagnosis


def ssh_diagnosis_with_receipt(diagnosis):
    line = str(diagnosis.get('line', 'ssh diagnosis unavailable'))
    if 'receipt' not in diagnosis:
        return line
    path = diagnosis.get('receipt')
    return line + (f'; ssh-diag receipt: {path}' if path else '; ssh-diag receipt could not be written anywhere')


def ssh_safe_diagnosis(host, **kwargs):
    try:
        diagnosis = ssh_diagnosis(host, **kwargs)
        return {'line': diagnosis.get('line', 'ssh diagnosis unavailable'),
                'transient': diagnosis.get('transient', False), 'phase': diagnosis.get('phase', 'unknown'),
                'cause': diagnosis.get('cause', 'unknown'), 'evidence': diagnosis.get('evidence', 'unavailable')}
    except OSError as error:
        phase = 'unknown'
        try:
            log = kwargs.get('attempt_log')
            if log:
                phase = ssh_phase(Path(log).read_text(encoding='utf-8', errors='replace'))
        except OSError:
            pass
        cause = f'diagnosis failed: {error}'
        return {'line': f'ssh diag {host}: attempt phase {phase}; cause: {cause}; evidence unavailable; retry: no',
                'transient': False, 'phase': phase, 'cause': cause, 'evidence': 'unavailable'}


def ssh_master_dead(mux):
    try:
        return mux._control('check').returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return True


def ssh_transport_failure(mux, kind, returncode, stderr, timed_out=False):
    if kind == 'ssh':
        failed = timed_out or returncode == 255
    elif kind == 'rsync':
        failed = timed_out or returncode in (255, 10, 12, 30, 35)
    elif kind == 'scp':
        failed = timed_out or returncode == 255 or (returncode != 0 and (
            re.search(r'Connection (?:closed|reset|refused|timed out)|lost connection|Broken pipe|ssh: ',
                      stderr or '', re.I) or ssh_master_dead(mux)))
    else:
        raise ValueError(f'Unknown SSH rider kind {kind!r}')
    if not failed:
        return None
    log = getattr(mux, 'master_log', None)
    try:
        if log is not None and not Path(log).is_file():
            log = None
    except OSError:
        log = None
    diagnosis = ssh_safe_diagnosis(mux.host, run=getattr(mux, 'run', subprocess.run),
                                  evidence_dir=getattr(mux, 'evidence_dir', None),
                                  ssh_options=getattr(mux, 'options', ()), attempt_log=log,
                                  mux_dir=getattr(mux, 'root', None), ssh_bin=getattr(mux, 'ssh_bin', 'ssh'),
                                  windows=getattr(mux, 'windows', False), allow_retry=False,
                                  attempt_timed_out=timed_out, rider=True)
    return {**diagnosis, 'transient': False}


class SshMux:
    _lock = threading.RLock()
    _directories = {}
    _references = {}
    _ready = set()
    _master_logs = {}
    _sequence = 0

    def __init__(self, host, run=subprocess.run, ssh_options=(), ssh_bin='ssh', windows=False,
                 evidence_dir=None, popen=None, allow_master_retry=True, receipt_dir=None):
        require(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host), 'Unsafe SSH host')
        self.host, self.run, self.options, self.ssh_bin = host, run, tuple(ssh_options), ssh_bin
        self.windows, self.evidence_dir, self.popen = windows, evidence_dir, popen or subprocess.Popen
        self.receipt_dir = receipt_dir if receipt_dir is not None else evidence_dir
        self.allow_master_retry = allow_master_retry
        self.root = Path(os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir()) / f'eden-ssh-{os.getpid()}'
        control_path = self.root / '%C'
        # 104 - 17 = 87; OpenSSH adds a period and 16 random characters.
        # Observed: unix_listener: path "...aaaaaaaa.RjJd3QMxzb3tMlbu" too long for Unix domain socket
        require(len(os.fsencode(self.root)) + 1 + 40 <= 87,
                f'SSH control path too long: {control_path}; set XDG_RUNTIME_DIR to a short directory')
        self.key = None
        self.entered = False
        self.attempt_deadline = None
        self.open_generation = 0
        self.master_log = None
        self.diagnoses = []

    @property
    def rider_args(self):
        return [*SSH_OPTIONS, *self.options, '-o', 'ControlMaster=no',
                '-o', f'ControlPath={self.root}/%C', '-o', 'ProxyCommand=/bin/false']

    def _control(self, operation, timeout=3):
        return self.run([self.ssh_bin, *self.rider_args, '-O', operation, self.host],
                        capture_output=True, text=True, timeout=timeout)

    def _connection_key(self):
        try:
            result = self.run([self.ssh_bin, '-G', *SSH_OPTIONS, *self.options,
                               '-o', f'ControlPath={self.root}/%C', self.host],
                              capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        match = re.search(r'(?m)^controlpath\s+(.+)\s*$', result.stdout or '') if result else None
        if result is None or result.returncode != 0 or not match:
            diagnosis = ssh_safe_diagnosis(self.host, run=self.run, evidence_dir=self.evidence_dir,
                                      ssh_options=self.options, ssh_bin=self.ssh_bin, windows=self.windows,
                                      allow_retry=False)
            diagnosis = ssh_record(diagnosis, 'ssh', 'master configuration', self.receipt_dir)
            diagnosis['reported_line'] = ssh_diagnosis_with_receipt(diagnosis)
            self.diagnoses.append(diagnosis)
            raise LedgerTransport([diagnosis], 'SSH master configuration failed')
        path = Path(match.group(1).strip())
        require(path.parent == self.root and path.name != '%C', 'Unsafe SSH control path')
        return str(path)

    @staticmethod
    def _same_user(directory):
        return not hasattr(os, 'getuid') or directory.stat().st_uid == os.getuid()

    @staticmethod
    def _pid_alive(pid):
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if not handle:
                return ctypes.get_last_error() != 87
            try:
                return kernel.WaitForSingleObject(handle, 0) != 0
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except ProcessLookupError:
            return False

    def _retire_directory(self, directory):
        for entry in directory.iterdir():
            try:
                if stat.S_ISSOCK(entry.lstat().st_mode):
                    self.run([self.ssh_bin, *SSH_OPTIONS, '-o', 'ControlMaster=no',
                              '-o', f'ControlPath={entry}', '-O', 'exit', 'localhost'],
                             capture_output=True, text=True, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        shutil.rmtree(directory)

    def _sweep(self):
        for directory in self.root.parent.glob('eden-ssh-*'):
            match = re.fullmatch(r'eden-ssh-([1-9][0-9]*)', directory.name)
            if not match or int(match.group(1)) == os.getpid() or directory.is_symlink():
                continue
            try:
                if not self._same_user(directory) or not directory.is_dir():
                    continue
                if self._pid_alive(int(match.group(1))):
                    continue
                self._retire_directory(directory)
            except (OSError, ValueError, OverflowError):
                continue

    def __enter__(self):
        self.key = self._connection_key()
        with self._lock:
            if not self._directories.get(str(self.root)):
                self._sweep()
                require(not self.root.is_symlink(), 'Unsafe SSH mux directory')
                if self.root.exists():
                    require(self.root.is_dir() and self._same_user(self.root), 'Foreign SSH mux directory')
                    self._retire_directory(self.root)
                self.root.mkdir(mode=0o700, exist_ok=True)
                require(self._same_user(self.root), 'Foreign SSH mux directory')
                os.chmod(self.root, 0o700)
            self._directories[str(self.root)] = self._directories.get(str(self.root), 0) + 1
            self._references[self.key] = self._references.get(self.key, 0) + 1
            self.entered = True
        try:
            self.ensure()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def ensure(self):
        with self._lock:
            if self.key in self._ready:
                try:
                    if self._control('check').returncode == 0:
                        self.master_log = self._master_logs.get(self.key)
                        return
                except (OSError, subprocess.TimeoutExpired):
                    pass
                self._ready.discard(self.key)
                try:
                    socket_path = Path(self.key)
                    if stat.S_ISSOCK(socket_path.lstat().st_mode):
                        socket_path.unlink()
                except FileNotFoundError:
                    pass
            diagnoses = []
            for attempt in range(2):
                type(self)._sequence += 1
                log = self.root / f'master-{type(self)._sequence}.log'
                deadline = time.monotonic() + 10
                command = [self.ssh_bin, '-o', 'ControlMaster=yes', '-o', 'ControlPersist=120',
                           *SSH_OPTIONS, *self.options, '-o', f'ControlPath={self.root}/%C',
                           '-vvv', '-E', str(log), '-f', '-N', self.host]
                process = None
                attempt_timed_out = attempt_killed = False
                try:
                    process = self.popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, start_new_session=True)
                    result = process.wait(timeout=max(.001, deadline - time.monotonic()))
                    remaining = deadline - time.monotonic()
                    if result == 0 and remaining > 0 and self._control('check', min(3, remaining)).returncode == 0:
                        self._ready.add(self.key)
                        self.master_log = self._master_logs[self.key] = log
                        self.attempt_deadline = deadline
                        self.open_generation += 1
                        return
                    attempt_timed_out = remaining <= 0
                except subprocess.TimeoutExpired:
                    attempt_timed_out = True
                except OSError:
                    pass
                finally:
                    if process is not None and process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                            attempt_killed = attempt_timed_out
                        except ProcessLookupError:
                            pass
                        except (OSError, AttributeError):
                            try:
                                process.kill()
                                attempt_killed = attempt_timed_out
                            except ProcessLookupError:
                                pass
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            pass
                diagnosis = ssh_safe_diagnosis(self.host, run=self.run, evidence_dir=self.evidence_dir,
                                          ssh_options=self.options, attempt_log=log, mux_dir=self.root,
                                          ssh_bin=self.ssh_bin, windows=self.windows,
                                          allow_retry=self.allow_master_retry and attempt == 0,
                                          attempt_timed_out=attempt_timed_out, attempt_killed=attempt_killed)
                diagnosis = ssh_record(diagnosis, 'ssh', 'master', self.receipt_dir)
                if diagnosis.get('receipt') is None:
                    diagnosis['transient'] = False
                    line = diagnosis.get('line', 'ssh diagnosis unavailable')
                    if re.search(r'\bretry: [^;]+', line):
                        line = re.sub(r'\bretry: [^;]+', 'retry: no (no durable receipt)', line, count=1)
                    else:
                        line += '; retry: no (no durable receipt)'
                    diagnosis['line'] = line
                diagnosis['reported_line'] = ssh_diagnosis_with_receipt(diagnosis)
                self.diagnoses.append(diagnosis)
                diagnoses.append(diagnosis)
                if attempt or not self.allow_master_retry or not diagnosis.get('transient', False):
                    raise LedgerTransport(diagnoses, 'SSH master failed')
                diagnosis['retried'] = True
                print(diagnosis['reported_line'], file=sys.stderr, flush=True)
                print(f"ssh retried once: {diagnosis.get('cause', 'unknown')}; "
                      f"ssh-diag receipt: {diagnosis['receipt']}", file=sys.stderr, flush=True)

    def __exit__(self, exc_type, exc, traceback):
        with self._lock:
            if not self.entered:
                return
            self.entered = False
            remaining = self._references[self.key] - 1
            if remaining:
                self._references[self.key] = remaining
            else:
                self._references.pop(self.key)
                try:
                    self._control('exit')
                except (OSError, subprocess.TimeoutExpired):
                    pass
                self._ready.discard(self.key)
                self._master_logs.pop(self.key, None)
            root = str(self.root)
            self._directories[root] -= 1
            if not self._directories[root]:
                self._directories.pop(root)
                shutil.rmtree(self.root, ignore_errors=True)


def ssh_retry_info(mux):
    for diagnosis in reversed(getattr(mux, 'diagnoses', ())):
        if diagnosis.get('retried'):
            return {'phase': diagnosis.get('phase', 'unknown'), 'cause': diagnosis.get('cause', 'unknown'),
                    'receipt': diagnosis.get('receipt')}
    return None


def forward(host, mode, config, execute, run=subprocess.run, watchdog_rounds=None, watchdog_pause=None,
            keep_state=False, evidence_dir=None):
    """One bounded SSH request; remote account login shell must be pwsh."""
    require(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host), 'Unsafe SSH host')
    if config.get('host') == 'ally' and not mode.startswith('ledger-'):
        return forward_windows(host, mode, config, execute, watchdog_rounds=watchdog_rounds, watchdog_pause=watchdog_pause,
                               keep_state=keep_state, evidence_dir=evidence_dir)
    bootstrap = ('import json,sys; p=json.load(sys.stdin); '
                 'n={"__name__":"eden_lifecycle_remote","__file__":"lifecycle.py",'
                 '"_FORWARDED_PORTABLE_LOCK_SOURCE":p["portable_lock_source"]}; '
                 'exec(compile(p["source"],"lifecycle.py","exec"),n); n["remote_main"](p)')
    script = ("& /usr/bin/systemd-run --user --scope -p MemoryMax=256M /usr/bin/timeout 900 "
              "/usr/bin/python3 -I -B -c '" + bootstrap.replace("'", "''") + "'; exit $LASTEXITCODE")
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    source = globals().get('_FORWARDED_SOURCE') or Path(__file__).read_text()
    packet = {'mode': mode, 'config': config, 'execute': execute, 'source': source,
              'portable_lock_source': portable_lock_source(),
              'watchdog_rounds': watchdog_rounds, 'watchdog_pause': watchdog_pause,
              'keep_state': keep_state, 'evidence_dir': evidence_dir}
    with SshMux(host, run=run, evidence_dir=config.get('state_dir'), receipt_dir=config.get('state_dir')) as mux:
        mux.ensure()
        log = mux.root / ('rider-' + uuid.uuid4().hex + '.log')
        command = ['ssh', '-T', *mux.rider_args, '-vvv', '-E', str(log), host,
                   '/usr/bin/pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
        try:
            done = run(command, input=json.dumps(packet), capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired as error:
            diagnosis = ssh_transport_failure(mux, 'ssh', None, '', timed_out=True)
            diagnosis = ssh_record(diagnosis, 'ssh', f'lifecycle {mode}', config.get('state_dir'))
            raise Refused(f'SSH lifecycle failed: {error}; {ssh_diagnosis_with_receipt(diagnosis)}') from error
        except OSError as error:
            raise Refused(f'SSH lifecycle failed: {error}') from error
        diagnosis = ssh_transport_failure(mux, 'ssh', done.returncode, done.stderr)
        if diagnosis:
            diagnosis = ssh_record(diagnosis, 'ssh', f'lifecycle {mode}', config.get('state_dir'))
            raise Refused('SSH lifecycle failed: ' + (done.stderr or '')[-2000:] +
                          (done.stdout or '')[-2000:] + '; ' + ssh_diagnosis_with_receipt(diagnosis))
        require(done.returncode == 0, 'SSH lifecycle failed: ' + done.stderr[-2000:] + done.stdout[-2000:])
        return json.loads(done.stdout)


def ledger_forward(host, operation, config, run=subprocess.run):
    require(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host), 'Unsafe SSH host')
    require(operation in ('read', 'claim', 'release', 'reserve', 'cancel'), 'Unknown ledger operation')
    bootstrap = ('import json,sys; p=json.load(sys.stdin); '
                 'n={"__name__":"eden_lifecycle_remote","__file__":"lifecycle.py",'
                 '"_FORWARDED_PORTABLE_LOCK_SOURCE":p["portable_lock_source"]}; '
                 'exec(compile(p["source"],"lifecycle.py","exec"),n); n["remote_main"](p)')
    script = ("& /usr/bin/systemd-run --user --scope -p MemoryMax=256M /usr/bin/timeout 6 "
              "/usr/bin/python3 -I -B -c '" + bootstrap.replace("'", "''") + "'; exit $LASTEXITCODE")
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    source = globals().get('_FORWARDED_SOURCE') or Path(__file__).read_text()
    packet = {'mode': 'ledger-' + operation, 'config': config, 'execute': True, 'source': source,
              'portable_lock_source': portable_lock_source()}
    started = time.monotonic()
    with SshMux(host, run=run, evidence_dir=config.get('state_dir'), receipt_dir=config.get('state_dir')) as mux:
        deadline = mux.attempt_deadline or started + LEDGER_ATTEMPT_SECONDS
        generation = mux.open_generation
        mux.ensure()
        if mux.open_generation != generation:
            deadline = mux.attempt_deadline
        log = mux.root / ('rider-' + uuid.uuid4().hex + '.log')
        command = ['ssh', '-T', *mux.rider_args, '-vvv', '-E', str(log), host,
                   '/usr/bin/pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, LEDGER_ATTEMPT_SECONDS)
            done = run(command, input=json.dumps(packet), capture_output=True, text=True, timeout=remaining)
        except subprocess.TimeoutExpired as error:
            diagnosis = ssh_transport_failure(mux, 'ssh', None, '', timed_out=True)
            diagnosis = ssh_record(diagnosis, 'ssh', f'ledger {operation}', config.get('state_dir'))
            raise LedgerTransport([diagnosis], f'SSH lifecycle failed: {error}') from error
        except OSError as error:
            raise Refused(f'SSH lifecycle failed: {error}') from error
        diagnosis = ssh_transport_failure(mux, 'ssh', done.returncode, done.stderr)
        if diagnosis:
            diagnosis = ssh_record(diagnosis, 'ssh', f'ledger {operation}', config.get('state_dir'))
            raise LedgerTransport([diagnosis], 'SSH lifecycle failed: ' +
                                  (done.stderr or '')[-2000:] + (done.stdout or '')[-2000:])
        require(done.returncode == 0, 'SSH lifecycle failed: ' + (done.stderr or '')[-2000:] + (done.stdout or '')[-2000:])
        try:
            result = json.loads(done.stdout)
            if not isinstance(result, dict):
                raise ValueError('ledger reply is not an object')
            retry = ssh_retry_info(mux)
            if retry:
                result['ssh_retry'] = retry
            return result
        except (ValueError, TypeError) as error:
            raise Refused(f'SSH lifecycle failed: invalid ledger reply: {error}') from error


def dispatch(mode, config, execute=False, watchdog_rounds=None, watchdog_pause=None, keep_state=False, evidence_dir=None):
    require(mode in ('status', 'preflight') or execute, 'Runtime mutation needs explicit --execute; tests inject fakes')
    if mode.startswith('ledger-'):
        return Ledger(config['ledger'], config['host'], config['owner'], config['token'],
                      label=config.get('label'), exclusive_host=config.get('exclusive_host', False)).operate(
            mode[7:], config.get('stale_claim_hours'), config.get('admission'), approver=config.get('approver'))
    if mode == 'reserve-exclusive':
        config = dict(config)
        approver = config.pop('reservation_approver')
        return Lifecycle(config).reserve_exclusive(approver)
    if mode == 'cancel-reservation':
        return Lifecycle(config).cancel_reservation(config.get('reservation_token'))
    tool = Lifecycle(config)
    if mode == 'watchdog':
        return tool.watchdog(rounds=watchdog_rounds, pause=watchdog_pause)
    if mode == 'watchdog-serve':
        return tool.watchdog_serve()
    if mode == 'settle':
        return tool.settle(minutes=30 if watchdog_rounds is None else watchdog_rounds)
    if mode == 'down':
        return tool.down(keep_state=keep_state, evidence_dir=evidence_dir)
    return getattr(tool, mode)()


def remote_main(packet):
    # Allows a Surface executor to reach the authoritative Yoga ledger too.
    globals()['_FORWARDED_SOURCE'] = packet['source']
    if packet.get('ledger_bridge'):
        def bridge(operation, token, stale_claim_hours, admission):
            print(json.dumps({'ledger_request': {'operation': operation, 'token': token,
                              'stale_claim_hours': stale_claim_hours, 'admission': admission}}), flush=True)
            reply = json.loads(sys.stdin.readline())
            if str(reply.get('error', '')).startswith('Ledger transport: '):
                detail = reply['error'][len('Ledger transport: '):]
                try:
                    diagnoses = json.loads(detail)
                    if not isinstance(diagnoses, list) or not diagnoses:
                        raise ValueError('missing diagnoses')
                except (ValueError, TypeError):
                    diagnoses = detail
                raise LedgerTransport(diagnoses)
            require('error' not in reply, reply.get('error'))
            return reply['result']
        globals()['_LEDGER_BRIDGE'] = bridge
        try:
            value = dispatch(packet['mode'], packet['config'], packet['execute'],
                             packet.get('watchdog_rounds'), packet.get('watchdog_pause'),
                             packet.get('keep_state', False), packet.get('evidence_dir'))
            print(json.dumps({'lifecycle_result': value}), flush=True)
        except BaseException as error:
            print(json.dumps({'lifecycle_error': str(error)}), flush=True)
    else:
        print(json.dumps(dispatch(packet['mode'], packet['config'], packet['execute'],
                                  packet.get('watchdog_rounds'), packet.get('watchdog_pause'),
                                  packet.get('keep_state', False), packet.get('evidence_dir'))))


def forward_windows(host, mode, config, execute, popen=subprocess.Popen, ledger_factory=Ledger,
                    watchdog_rounds=None, watchdog_pause=None, keep_state=False, evidence_dir=None):
    # SSH exec is always non-login/noninteractive (-T, explicit MSI pwsh -NoProfile).
    options = ('-i', '/home/intpa/.ssh/id_rsa', '-o', 'IdentitiesOnly=yes')
    with SshMux(host, ssh_options=options, windows=True, evidence_dir=config.get('state_dir'),
                receipt_dir=config.get('state_dir')) as mux:
        mux.ensure()
        return _forward_windows_once(host, mode, config, execute, popen, ledger_factory,
                                     watchdog_rounds, watchdog_pause, keep_state, evidence_dir, mux=mux)


def _forward_windows_once(host, mode, config, execute, popen=subprocess.Popen, ledger_factory=Ledger,
                          watchdog_rounds=None, watchdog_pause=None, keep_state=False, evidence_dir=None, mux=None):
    """Windows cmd-default SSH transport, with Linux-authoritative ledger RPC.

    No Windows SSH keys or copied ledger. Both sides retain the same durable token.
    The controller only honors its configured owner/host/path and bounded operations.
    """
    require(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host), 'Unsafe SSH host')
    require(mode in ('status', 'preflight') or execute, 'Windows up/down require --execute')
    bootstrap = ('import json,sys; p=json.loads(sys.stdin.readline()); '
                 'n={"__name__":"eden_lifecycle_remote","__file__":"lifecycle.py",'
                 '"_FORWARDED_PORTABLE_LOCK_SOURCE":p["portable_lock_source"]}; '
                 'exec(compile(p["source"],"lifecycle.py","exec"),n); n["remote_main"](p)')
    script = ('& "C:\\Program Files\\Python312\\python.exe" -I -B -X utf8 -c ' +
              "'" + bootstrap.replace("'", "''") + "'; exit $LASTEXITCODE")
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    command = ['ssh', '-T', '-x', *mux.rider_args, host,
               '"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + encoded]
    source = globals().get('_FORWARDED_SOURCE') or Path(__file__).read_text()
    process = popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8')
    messages, errors = queue.Queue(), []
    def drain():
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)
    def stderr():
        for line in process.stderr:
            errors.append(line)
            if len(errors) > 100:
                del errors[0]
    threading.Thread(target=drain, daemon=True).start()
    error_thread = threading.Thread(target=stderr, daemon=True)
    error_thread.start()
    end = time.monotonic() + 900
    token_seen = bound = None
    def failed(message, code, timed_out=False):
        diagnosis = ssh_transport_failure(mux, 'ssh', code, ''.join(errors), timed_out=timed_out)
        if diagnosis:
            diagnosis = ssh_record(diagnosis, 'ssh', f'Windows lifecycle {mode}', config.get('state_dir'))
            message += '; ' + ssh_diagnosis_with_receipt(diagnosis)
        error = Refused(message)
        error.transport_not_started = token_seen is None
        return error
    try:
        process.stdin.write(json.dumps({'mode': mode, 'config': config, 'execute': execute,
                                       'source': source,
                                       'portable_lock_source': portable_lock_source(),
                                       'ledger_bridge': True,
                                       'watchdog_rounds': watchdog_rounds, 'watchdog_pause': watchdog_pause,
                                       'keep_state': keep_state, 'evidence_dir': evidence_dir}) + '\n')
        process.stdin.flush()
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise failed('Windows SSH lifecycle deadline; use down with the same journal', None, True)
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty:
                raise failed('Windows SSH lifecycle deadline; use down with the same journal', None, True)
            if line is None:
                try:
                    code = process.wait(timeout=3)
                except subprocess.TimeoutExpired as cause:
                    raise failed(str(cause), None, True) from cause
                error_thread.join(timeout=2)
                raise failed('Windows SSH disconnected: ' + ''.join(errors)[-2000:], code)
            message = json.loads(line)
            if 'ledger_request' in message:
                request = message['ledger_request']
                token = request['token']
                require(re.fullmatch(r'[0-9a-f]{32}', token), 'Foreign bridge token')
                require(request['operation'] in ('read', 'claim', 'release'), 'Unknown bridge operation')
                # A read mutates nothing: Up's admission reads with the inspect token ('0' * 32, ledger_text)
                # before it claims. Only claim and release bind the run's one token.
                if request['operation'] != 'read':
                    require(bound in (None, token), 'Foreign bridge token')
                    bound = token
                token_seen = token
                require(mode not in ('status', 'preflight') or request['operation'] == 'read',
                        'Preflight/status cannot mutate a ledger')
                ledger = ledger_factory(config['ledger'], 'ally', config['owner'], token, config.get('ledger_ssh'))
                if hasattr(ledger, 'state_dir'):
                    ledger.state_dir = config.get('state_dir')
                try:
                    result = ledger.operate(request['operation'], request.get('stale_claim_hours'), request.get('admission'))
                    reply = {'result': result}
                except LedgerTransport as error:
                    reply = {'error': 'Ledger transport: ' + json.dumps(error.diagnoses)}
                except Exception as error:
                    reply = {'error': str(error)}
                process.stdin.write(json.dumps(reply) + '\n')
                process.stdin.flush()
            elif 'lifecycle_result' in message:
                try:
                    code = process.wait(timeout=15)
                except subprocess.TimeoutExpired as cause:
                    raise failed(str(cause), None, True) from cause
                if code != 0:
                    error_thread.join(timeout=2)
                    raise failed('Windows SSH failed after reply', code)
                return message['lifecycle_result']
            else:
                raise Refused(message.get('lifecycle_error', 'Unknown Windows protocol response'))
    except BrokenPipeError as cause:
        error_thread.join(timeout=2)
        try:
            code = process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            raise failed('Windows SSH disconnected: ' + ''.join(errors)[-2000:] + str(cause), None, True) from cause
        raise failed('Windows SSH disconnected: ' + ''.join(errors)[-2000:] + str(cause), code) from cause
    finally:
        if process.poll() is None:
            process.kill()  # Only this SSH transport; the task Job survives for explicit down/hard cap.
            process.wait(timeout=10)
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # Do not mask the classified pre-launch failure (or a completed reply).


def main(argv=None):
    actual = list(sys.argv[1:] if argv is None else argv)
    if actual and actual[0] == '_windows-launch':
        windows_launch(actual[1])
        return 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=('up', 'down', 'status', 'preflight', 'reserve', 'cancel', 'watchdog', 'watchdog-serve',
                                         'settle'))
    parser.add_argument('config', type=Path)
    parser.add_argument('--execute', action='store_true', help='explicit runtime authorization; NEVER used by offline tests')
    parser.add_argument('--ssh', help='executor host (remote login shell PowerShell 7)')
    parser.add_argument('--rounds', type=int, default=None, help='watchdog polls (default: the config, else 1)')
    parser.add_argument('--pause', type=float, default=None, help='seconds between watchdog polls')
    parser.add_argument('--minutes', type=float, default=None, help='settle deadline in minutes (default 30)')
    parser.add_argument('--keep-state', action='store_true',
                        help='down: keep the run\'s copies (work_root, state snapshots) for debugging; only when asked')
    parser.add_argument('--evidence-dir', help='down: where the files the run wrote are copied before the prune '
                        '(default <state_dir>/evidence; eden_test.py passes the run\'s out/)')
    parser.add_argument('--approver', help='reserve: name of the person who approved this reservation')
    parser.add_argument('--reservation-token', help='cancel: reservation_token from the reserve receipt')
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text())
        require(args.mode in ('status', 'preflight') or args.execute, 'up/down require explicit --execute')
        require(args.mode == 'down' or not (args.keep_state or args.evidence_dir),
                '--keep-state and --evidence-dir are down options')
        require(args.mode == 'reserve' or args.approver is None, '--approver is a reserve option')
        require(args.mode == 'cancel' or args.reservation_token is None, '--reservation-token is a cancel option')
        require(args.mode == 'settle' or args.minutes is None, '--minutes is a settle option')
        if args.mode == 'reserve':
            require(args.approver, 'reserve requires --approver')
            validate(config)
            if config.get('exclusive_host'):
                packet = dict(config, reservation_approver=args.approver)
                receipt = (forward(args.ssh, 'reserve-exclusive', packet, True) if args.ssh else
                           dispatch('reserve-exclusive', packet, True))
            else:
                ledger = Ledger(config['ledger'], config['host'], config['owner'], uuid.uuid4().hex,
                                config.get('ledger_ssh') or args.ssh, label=config['label'],
                                state_dir=config.get('state_dir'))
                receipt = ledger.operate('reserve', approver=args.approver)
                if receipt.get('ssh_retry'):
                    record_action(config['state_dir'], 'reserve', receipt)
            print(json.dumps(receipt, indent=2))
            return 0
        if args.mode == 'cancel':
            validate(config)
            require(args.reservation_token, 'cancel requires --reservation-token from the reserve receipt')
            config = dict(config, reservation_token=args.reservation_token)
            receipt = (forward(args.ssh, 'cancel-reservation', config, True) if args.ssh else
                       dispatch('cancel-reservation', config, True))
            if receipt.get('ssh_retry'):
                record_action(config['state_dir'], 'cancel-reservation', receipt)
            print(json.dumps(receipt, indent=2))
            return 0
        options = {'keep_state': args.keep_state, 'evidence_dir': args.evidence_dir}
        rounds = args.minutes if args.mode == 'settle' else args.rounds
        value = (forward(args.ssh, args.mode, config, args.execute, watchdog_rounds=rounds, watchdog_pause=args.pause,
                         **options)
                 if args.ssh else dispatch(args.mode, config, args.execute, rounds, args.pause, **options))
        if isinstance(value, dict) and args.mode in ('preflight', 'watchdog', 'watchdog-serve'):
            for line in value.get('lines') or []:
                print(line, file=sys.stderr)
        print(json.dumps(value, indent=2))
        if isinstance(value, dict) and args.mode == 'preflight' and not value.get('ok', False):
            return 1
        if isinstance(value, dict) and args.mode == 'settle' and value.get('release_pending'):
            return 1
        return 0
    except Exception as error:
        print(json.dumps({'phase': 'refused/failed', 'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
