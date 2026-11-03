#!/usr/bin/env python3
"""Shared, journalled Eden lifecycle. Import is inert; see README.md for config.

  lifecycle.py up     config.json --execute [--ssh memento-claw-yoga-ubuntu]
  lifecycle.py down   config.json --execute [--ssh memento-claw-yoga-ubuntu]
  lifecycle.py status config.json           [--ssh memento-claw-yoga-ubuntu]

No runtime operations are performed by tests: inject a fake Host and Ledger.
SSH executes the SAME source through the remote login shell's PowerShell 7,
with configuration on stdin. Paths and instances.json belong to the executor
host; run the shared eden-runner there (no implicit socket forwarding).
"""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
GIB = 1024 ** 3
MIN_MEMORY = 10 * GIB
MIN_DISK = 40 * 10 ** 9
READ_ONLY = ('user/nand/user/Contents', 'user/nand/system/Contents', 'user/firmware')
PLUGINS = 'user/sdmc/atmosphere/contents/01006A800016E000/romfs/skyline/plugins'
CONFIG = 'user/config/qt-config.ini'
RPC_METHODS = {'status', 'settings.set', 'settings.get', 'settings.save', 'game.load', 'game.stop', 'jobs.get'}


class Refused(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def mute_is_zero(value):
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
    return bool(found) and all(not isinstance(x, bool) and str(x) in ('0', '0.0') for x in found)


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
    require(values.get('nand_directory') == str(Path(profile) / 'user/nand')
            and values.get('sdmc_directory') == str(Path(profile) / 'user/sdmc'), 'Config escapes private NAND/SDMC')
    if require_mute:
        require(values.get('volume') in ('0', '0.0'), 'Persisted [Audio] volume not zero')
    return values


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
        require(not part.is_symlink(), 'Symlink ancestor refused: ' + str(part))
    return path


def relative(value):
    path = Path(value)
    require(bool(value) and not path.is_absolute() and '..' not in path.parts and str(path) != '.',
            'Non-traversing relative path required: ' + str(path))
    return path


def inventory(root):
    """Full byte/type/mode inventory, including empty directories. No symlinks."""
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), 'Profile/snapshot directory required')
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        name = str(path.relative_to(root))
        require(not stat.S_ISLNK(info.st_mode), 'Profile symlinks need an explicit policy: ' + str(path))
        if stat.S_ISDIR(info.st_mode):
            result[name] = {'type': 'directory', 'mode': stat.S_IMODE(info.st_mode)}
        else:
            require(stat.S_ISREG(info.st_mode), 'Special profile file refused: ' + str(path))
            result[name] = {'type': 'file', 'mode': stat.S_IMODE(info.st_mode),
                            'size': info.st_size, 'sha256': digest(path)}
    return result


def clone_profile(source, target):
    """Link only immutable game content; private-copy EVERY other file."""
    source, target = safe_absolute(source), safe_absolute(target)
    require(source.is_dir() and source.name != 'a' and target.name != 'a', 'Profile a/missing source refused')
    require(source != target and source not in target.parents and target not in source.parents, 'Overlapping profiles refused')
    require(not target.exists(), 'Refuse an existing profile/snapshot: ' + str(target))
    before = inventory(source)

    def copy(src, dst):
        rel = Path(src).relative_to(source)
        immutable = any(rel == Path(prefix) or Path(prefix) in rel.parents for prefix in READ_ONLY)
        return os.link(src, dst) if immutable else shutil.copy2(src, dst)

    shutil.copytree(source, target, copy_function=copy)
    require(inventory(target) == before, 'Clone differs from source')
    return before


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
        explicit = re.search(r'\bhost=(surface|yoga|ally)\b', body, re.I)
        named = re.search(r'\bon\s+(surface|yoga|ally)\b', body, re.I)
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


class Ledger:
    def __init__(self, path, host, owner, token, remote=None):
        self.path, self.host, self.owner, self.token = str(path), host, owner, token
        self.remote = remote

    def operate(self, operation):
        if self.remote:
            return forward(self.remote, 'ledger-' + operation,
                           {'ledger': self.path, 'host': self.host, 'owner': self.owner, 'token': self.token}, True)
        path = safe_absolute(self.path)
        require(path.is_file(), 'Authoritative ledger must already exist')
        # Never replace/truncate the shared ledger or normalize it on disk.
        with path.open('r+', encoding='utf-8') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            text = stream.read()
            if operation == 'read':
                opened = active_claims(text, self.host)
                exact = lambda line: (f'lifecycle={self.token} ' in line and f'{self.owner} ' in line
                                      and f'host={self.host} ' in line)
                return {'open': opened, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                        'owned_claim': any(exact(line) for line in opened),
                        'owned_release': any('RELEASED:' in line and exact(line) for line in text.splitlines())}
            require(operation in ('claim', 'release'), 'Unknown ledger operation')
            if operation == 'claim':
                require(not active_claims(text, self.host), 'Open host claim; no profile changes permitted')
            else:
                own = [line for line in active_claims(text, self.host)
                       if f'lifecycle={self.token} ' in line and f'{self.owner} ' in line]
                require(len(own) == 1, 'Exact owned lifecycle claim not found; release refused')
            kind = 'CLAIMED' if operation == 'claim' else 'RELEASED'
            line = f'{stamp()} {kind}: {self.owner} host={self.host} lifecycle={self.token} '
            line += 'Eden lifecycle admitted' if operation == 'claim' else 'Eden stopped; full profile restored and separately verified'
            stream.seek(0, os.SEEK_END)
            stream.write('\n' + line + '\n')
            stream.flush()
            os.fsync(stream.fileno())
            return {'line': line}

    def claim(self):
        return self.operate('claim')

    def release(self):
        return self.operate('release')

    def inspect(self):
        return self.operate('read')


def validate(config):
    require(config.get('host') in ('surface', 'yoga'), 'host must be surface or yoga')
    require(isinstance(config.get('hostname'), str) and re.fullmatch(r'[A-Za-z0-9_.-]+', config['hostname']),
            'Explicit executor kernel hostname required; SSH aliases are not host identity')
    require(isinstance(config.get('owner'), str) and re.fullmatch(r'(OpenCode|Codex|Claude|Cursor)\s+\S+', config['owner']),
            'owner must be Engine session/stream (no newline or free-form suffix)')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{1,63}', config.get('label', '')), 'Invalid label')
    root, state_dir = safe_absolute(config['work_root']), safe_absolute(config['state_dir'])
    require(len(root.parts) >= 4 and len(state_dir.parts) >= 4 and root != state_dir, 'Dedicated work_root and state_dir required')
    require(root not in state_dir.parents and state_dir not in root.parents, 'State and profile roots must be disjoint')
    safe_absolute(config['ledger'])
    for pinned in (config['runner'], config['rows']):
        safe_absolute(pinned['path'])
        require(re.fullmatch(r'[0-9a-f]{64}', pinned.get('sha256', '')), 'Runner/rows SHA256 required')
    if config.get('display_helper'):
        safe_absolute(config['display_helper']['path'])
        require(re.fullmatch(r'[0-9a-f]{64}', config['display_helper'].get('sha256', '')), 'Display helper SHA256 required')
    require(1 <= len(config['instances']) <= 2, 'One or an explicitly authorized pair only')
    require(len(config['instances']) == 1 or config.get('pair_authorized') is True, 'Pair exception needs pair_authorized=true')
    displays, keys = set(), set()
    for item in config['instances']:
        require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,20}', item['key']) and item['key'] != 'a-profile', 'Invalid instance key')
        require(item['key'] not in keys, 'Duplicate instance key')
        keys.add(item['key'])
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
        for name in item.get('sources', {}).values():
            relative(name)
        require(not set(item.get('sources', {})) & {'socket', 'profile', 'console_log', 'control_log'},
                'File source alias cannot override a transport/owned path')
    for key, default in (('socket_cap', 90), ('load_cap', 180), ('stop_cap', 30), ('force_cap', 30), ('runtime', 5400)):
        value = config.get(key, default)
        require(isinstance(value, (int, float)) and math.isfinite(value) and 0 < value <= (10800 if key == 'runtime' else 300),
                'Invalid bounded ' + key)
    return config


class Host:
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
                if comm.startswith(('eden', 'yuzu')) or comm in (
                        'cargo', 'rustc', 'cc', 'c++', 'gcc', 'g++', 'clang', 'clang++', 'cc1', 'cc1plus', 'ninja', 'cmake'):
                    groups = (entry / 'cgroup').read_text()
                    if not any(unit in groups for unit in owned_units):
                        processes.append({'pid': int(entry.name), 'name': comm})
            except (FileNotFoundError, ProcessLookupError):
                pass
        return {'mem_available': memory, 'disk_free': shutil.disk_usage(disk_path).free, 'foreign_processes': processes}

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
            '--property=LoadState,MainPID,ControlGroup,ActiveState,MemoryMax,MemorySwapMax,TasksMax,RuntimeMaxUSec,MemoryPeak'],
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
                'limits': {k: info.get(k) for k in ('MemoryMax', 'MemorySwapMax', 'TasksMax', 'RuntimeMaxUSec')}}

    def launch(self, item, config):
        require(not Path('/tmp/.X11-unix/X' + item['display'][1:]).is_symlink(), 'Display socket alias refused')
        require(Path('/tmp/.X11-unix/X' + item['display'][1:]).exists(),
                'Display must be provisioned explicitly before up: ' + item['display'])
        profile, run = Path(item['profile']), Path(item['run'])
        run.mkdir(parents=True, exist_ok=True)
        env = {'DISPLAY': item['display'], 'WAYLAND_DISPLAY': '', 'XAUTHORITY': '', 'QT_QPA_PLATFORM': 'xcb',
               'XDG_DATA_HOME': str(profile / 'xdg-data'), 'XDG_CONFIG_HOME': str(profile / 'xdg-config'),
               'XDG_CACHE_HOME': str(profile / 'xdg-cache')}
        # Eden needs a sanitized environment: no inherited LAN/runtime/seed overrides.
        command = ['/usr/bin/env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin',
                   'HOME=' + str(Path.home()), 'XDG_RUNTIME_DIR=' + os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')]
        command += [key + '=' + value for key, value in env.items()]
        command += [item['app']['path'], '--control-socket', item['socket'],
                    '--control-log', str(run / 'control.jsonl'), '--control-verbose', '--control-background']
        self.run(['systemd-run', '--user', '--quiet', '--service-type=exec', '--unit=' + item['unit'],
                  '--property=MemoryMax=8G', '--property=MemorySwapMax=0', '--property=TasksMax=256',
                  '--property=RuntimeMaxSec=' + str(config.get('runtime', 5400)), '--property=KillMode=control-group',
                  '--property=TimeoutStopSec=10', '--property=StandardOutput=append:' + item['console_log'],
                  '--property=StandardError=append:' + item['console_log'], '--working-directory=' + str(profile),
                  '--', *command])
        info = self.unit(item['unit'])
        require(info.get('ActiveState') == 'active' and info.get('MemoryMax') == str(8 * GIB)
                and info.get('MemorySwapMax') == '0' and info.get('TasksMax') == '256', 'Actual cgroup limits differ')
        identities = self.owned(item)
        require(identities, 'No owned Eden PID recorded')
        return identities

    def socket_ready(self, item):
        try:
            return stat.S_ISSOCK(Path(item['socket']).stat().st_mode)
        except FileNotFoundError:
            return False

    def call(self, item, method, params, timeout):
        require(method in RPC_METHODS, 'Lifecycle cannot send input/navigation/trace calls')
        tools = str(Path(item['app']['path']).parent / 'tools')
        # Same supported Eden CLI client as the shared runner. Lazy: import is inert.
        path = Path(tools) / 'edenctl.py'
        spec = importlib.util.spec_from_file_location('_lifecycle_edenctl', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Client(item['socket'], timeout=timeout).request(method, params)

    def force_stop(self, item, cap):
        # Unique journalled unit/cgroup only. Never process-name matching or pkill.
        before = list({(x['pid'], x['start'], x['boot']): x
                       for x in [*item.get('owned_pids', []), *self.owned(item)]}.values())
        try:
            self.run(['systemctl', '--user', 'stop', item['unit']], cap=cap)
        except (Refused, subprocess.TimeoutExpired):
            pass
        for identity in self.owned(item):
            if self.identity(identity['pid']) == identity:
                fd = os.pidfd_open(identity['pid'])
                try:
                    # Recheck after pidfd_open, which pins the process against reuse.
                    require(self.identity(identity['pid']) == identity, 'Owned PID changed before force stop')
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
        deadline = self.clock() + cap
        while self.clock() < deadline:
            if not self.owned(item) and all(self.identity(x['pid']) != x for x in before):
                return {'owned_pids_stopped': before}
            self.sleep(.1)
        raise Refused('Owned Eden processes remain; do not restore/release')


class Lifecycle:
    def __init__(self, config, host=None, ledger=None, plan_check=None):
        self.c = validate(config)
        self.host = host if host is not None else Host()
        self.directory = Path(config['state_dir'])
        self.journal = self.directory / 'lifecycle.json'
        self.injected_ledger = ledger
        self.plan_check = plan_check if plan_check is not None else self.check_rows
        self.state = None

    def check_rows(self):
        runner = self.c['runner']
        require(digest(runner['path']) == runner['sha256'], 'Shared runner changed')
        require(digest(self.c['rows']['path']) == self.c['rows']['sha256'], 'Row list changed')
        spec = importlib.util.spec_from_file_location('_lifecycle_shared_runner', runner['path'])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.dry_run(Path(self.c['rows']['path']))

    def record(self, kind, value):
        with (self.directory / 'actions.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'utc': stamp(), 'kind': kind, 'value': value}) + '\n')

    def write(self):
        save(self.journal, self.state)

    def check_state(self):
        """Never trust a journal's arbitrary unit/path as permission to stop it."""
        require(self.state['owner'] == self.c['owner'] and self.state['config'] == self.c,
                'Owner/config differs from journal; no cleanup action')
        token = self.state['token']
        require(re.fullmatch(r'[0-9a-f]{32}', token), 'Invalid owned lifecycle token')
        originals = {item['key']: item for item in self.c['instances']}
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
                    and item['socket'] == str(self.directory / ('run-' + key) / 'control.sock'),
                    'Foreign journal profile/run path refused')

    def ledger(self):
        return self.injected_ledger if self.injected_ledger is not None else Ledger(
            self.c['ledger'], self.c['host'], self.c['owner'], self.state['token'], self.c.get('ledger_ssh'))

    @contextmanager
    def lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        require(not self.directory.is_symlink(), 'State directory alias refused')
        with (self.directory / '.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def admission(self, owned=()):
        path = Path(self.c['work_root'])
        while not path.exists():
            path = path.parent
        value = self.host.resources(path, owned)
        self.record('admission', value)
        require(value['mem_available'] >= MIN_MEMORY, 'MemAvailable below 10 GiB')
        require(value['disk_free'] >= MIN_DISK, 'Free disk below 40 GB')
        require(not value['foreign_processes'], 'Eden/compiler already active on host')

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
        text = text.replace(item['source'], str(profile))
        section = None
        output = []
        volume = 0
        for line in text.splitlines():
            if line.startswith('['):
                section = line.strip()
            key = line.split('=', 1)[0].strip()
            if key in ('nand_directory', 'sdmc_directory'):
                line = key + '=' + str(profile / ('user/nand' if key == 'nand_directory' else 'user/sdmc'))
            if section == '[Audio]' and key == 'volume':
                line, volume = 'volume=0', volume + 1
            if section == '[Audio]' and key == 'volume\\default':
                line = 'volume\\default=false'
            output.append(line)
        require(volume == 1, 'Expected exactly one persisted [Audio] volume')
        require(str(profile / 'user/nand') in '\n'.join(output) and str(profile / 'user/sdmc') in '\n'.join(output),
                'Private NAND/SDMC config paths missing')
        config.write_text('\n'.join(output) + '\n')
        plugins = profile / PLUGINS
        plugins.mkdir(parents=True, exist_ok=True)
        aside = profile / 'lifecycle-plugin-originals'
        aside.mkdir()
        for path in plugins.glob('*.nro'):
            path.replace(aside / path.name)
        for pin in item['plugins']:
            source = Path(pin['path'])
            destination = plugins / pin.get('name', source.name)
            require(destination.parent == plugins, 'Plugin destination traversal')
            shutil.copyfile(source, destination)
            require(digest(destination) == pin['sha256'], 'Staged plugin changed during copy')
        for pin in item.get('files', []):
            target = profile / relative(pin['to'])
            safe_absolute(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()  # break any link before writing
            shutil.copyfile(pin['path'], target)
            require(digest(target) == pin['sha256'], 'Staged file changed during copy')
        private_config(profile)
        self.record('prepared', {'key': item['key'], 'snapshot_entries': len(expected), 'persisted_volume': 0})

    def up(self):
        require(self.host.name() == self.c['hostname'], 'Wrong executor hostname; no claim/profile action')
        with self.lock():
            require(not self.journal.exists(), 'Run already journalled; use down/status, never blind up retry')
            root = Path(self.c['work_root'])
            require(not root.exists(), 'Fresh isolated work_root required')
            report = self.plan_check()
            require(not report.get('errors') and report.get('rows', 0) > 0
                    and report.get('result') in ('READY', 'READY_TO_RECORD'), 'Shared runner dry-run failed')
            save(self.directory / 'dry-run.json', report)
            # Validate pinned inputs before claim/staging. Never choose a newest binary.
            for item in self.c['instances']:
                require(Path(item['source']).is_dir() and Path(item['game']).is_file(), 'Source/game missing')
                for pin in [item['app'], *item['plugins'], *item.get('files', [])]:
                    require(digest(pin['path']) == pin['sha256'], 'Pinned input mismatch: ' + pin['path'])
            token = uuid.uuid4().hex
            self.state = {'schema': 1, 'phase': 'claim-intent', 'owner': self.c['owner'], 'host': self.c['host'],
                          'label': self.c['label'], 'token': token, 'claimed': False, 'released': False,
                          'config': self.c, 'instances': [], 'started': stamp()}
            self.write()
            try:
                self.admission()
                claim = self.ledger().claim()
                self.state['claimed'] = True
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
                    require(len(os.fsencode(item['socket'])) < 108, 'Control socket path too long; choose shorter state_dir')
                    self.state['instances'].append(item)
                    self.write()
                    clone_profile(original['source'], profile)
                    self.prepare(item)
                    self.admission([x['unit'] for x in self.state['instances'] if x['launch_intent']])
                    item['display_intent'] = bool(self.c.get('display_helper'))
                    self.write()
                    item['display_pids'] = self.host.start_display(item, self.c)
                    self.write()
                    item['launch_intent'] = True
                    self.write()  # recovery can stop this exact unit even if launch raises
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
                    # The CLI save must really persist mute, before any game.load.
                    private_config(profile)
                    self.rpc(item, 'game.load', {'path': item['game']}, self.c.get('load_cap', 180))
                    self.poll(lambda: self.ready(item), self.c.get('load_cap', 180), 'game loading complete')
                    run_state = json.loads((Path(item['run']) / 'run.json').read_text())
                    run_state.update(state='running', game_loaded=stamp())
                    save(Path(item['run']) / 'run.json', run_state)
                instances = {}
                for item in self.state['instances']:
                    sources = {'settings': 'user/sdmc/ultimate/TrainingRooms/settings.json',
                               'diag_log': 'user/sdmc/ultimate/TrainingRooms/practice-diagnostics.log',
                               'detector_log': 'user/sdmc/ultimate/ssbu-desync-detector/events.log', **item.get('sources', {})}
                    instances[item['key']] = {'socket': item['socket'], 'console_log': item['console_log'],
                        'profile': item['profile'], 'control_log': str(Path(item['run']) / 'control.jsonl'),
                        **{name: str(Path(item['profile']) / rel) for name, rel in sources.items()}}
                save(self.directory / 'instances.json', instances)
                self.state['phase'] = 'up'
                self.write()
                return {'phase': 'up', 'instances': instances, 'instances_file': str(self.directory / 'instances.json')}
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
        save(self.directory / (item['key'] + '-verify.json'), {'equal': True, 'snapshot_equal': True, 'entries': len(expected)})

    def _down(self):
        failures, stop_errors = [], []
        self.state['phase'] = 'stopping'
        self.write()
        # Claim/release ACK loss must not become an imaginary unclaimed/down state.
        # The durable token also lets a fresh down recover a crash after append.
        try:
            ownership = self.ledger().inspect()
            self.record('claim-readback', ownership)
            if ownership['owned_claim']:
                self.state['claimed'] = True
            elif ownership['owned_release']:
                self.state['released'] = True
            elif self.state['claimed']:
                raise Refused('Owned claim disappeared without its exact release record')
        except Exception as error:
            failures.append({'stage': 'claim-readback', 'error': repr(error)})
        # Even a ledger outage must not skip owned process stop/restore attempts.
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
        self.state['cleanup_failures'] = failures
        if not failures and self.state['claimed'] and not self.state['released']:
            try:
                self.record('release', self.ledger().release())
                self.state['released'] = True
            except Exception as error:
                failures.append({'stage': 'release', 'error': repr(error)})
        self.state['phase'] = 'cleanup-failed' if failures else 'down'
        self.write()
        require(not failures, 'Cleanup incomplete; claim retained: ' + repr(failures))
        return {'phase': 'down', 'released': self.state['released'], 'game_stop_errors': stop_errors}

    def down(self):
        require(self.host.name() == self.c['hostname'], 'Wrong executor hostname; no cleanup action')
        with self.lock():
            require(self.journal.is_file(), 'No owned lifecycle journal')
            self.state = json.loads(self.journal.read_text())
            self.check_state()
            if self.state['phase'] == 'down':
                return {'phase': 'down', 'released': self.state['released'], 'already_down': True}
            return self._down()

    def status(self):
        require(self.host.name() == self.c['hostname'], 'Wrong executor hostname')
        if not self.journal.is_file():
            return {'phase': 'absent'}
        value = json.loads(self.journal.read_text())
        self.state = value
        self.check_state()
        return {'phase': value['phase'], 'claimed': value['claimed'], 'released': value['released'],
                'owned_pids': {x['key']: self.host.owned(x) for x in value['instances'] if x['launch_intent']},
                'cleanup_failures': value.get('cleanup_failures', [])}


def forward(host, mode, config, execute, run=subprocess.run):
    """One bounded SSH request; remote account login shell must be pwsh."""
    require(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host), 'Unsafe SSH host')
    bootstrap = 'import json,sys; p=json.load(sys.stdin); n={"__name__":"eden_lifecycle_remote"}; exec(compile(p["source"],"lifecycle.py","exec"),n); n["remote_main"](p)'
    script = ("& /usr/bin/systemd-run --user --scope -p MemoryMax=256M /usr/bin/timeout 900 "
              "/usr/bin/python3 -I -B -c '" + bootstrap.replace("'", "''") + "'; exit $LASTEXITCODE")
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host,
               '/usr/bin/pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
    source = globals().get('_FORWARDED_SOURCE') or Path(__file__).read_text()
    packet = {'mode': mode, 'config': config, 'execute': execute, 'source': source}
    done = run(command, input=json.dumps(packet), capture_output=True, text=True, timeout=900)
    require(done.returncode == 0, 'SSH lifecycle failed: ' + done.stderr[-2000:] + done.stdout[-2000:])
    return json.loads(done.stdout)


def dispatch(mode, config, execute=False):
    require(mode == 'status' or execute, 'Runtime mutation needs explicit --execute; tests inject fakes')
    if mode.startswith('ledger-'):
        return Ledger(config['ledger'], config['host'], config['owner'], config['token']).operate(mode[7:])
    return getattr(Lifecycle(config), mode)()


def remote_main(packet):
    # Allows a Surface executor to reach the authoritative Yoga ledger too.
    globals()['_FORWARDED_SOURCE'] = packet['source']
    print(json.dumps(dispatch(packet['mode'], packet['config'], packet['execute'])))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=('up', 'down', 'status'))
    parser.add_argument('config', type=Path)
    parser.add_argument('--execute', action='store_true', help='explicit runtime authorization; NEVER used by offline tests')
    parser.add_argument('--ssh', help='executor host (remote login shell PowerShell 7)')
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text())
        require(args.mode == 'status' or args.execute, 'up/down require explicit --execute')
        value = forward(args.ssh, args.mode, config, args.execute) if args.ssh else dispatch(args.mode, config, args.execute)
        print(json.dumps(value, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'phase': 'refused/failed', 'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
