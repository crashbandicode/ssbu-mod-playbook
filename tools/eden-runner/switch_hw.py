#!/usr/bin/env python3
"""Up and Down for a REAL Switch run: the hardware counterpart of eden-lifecycle for tools/eden-runner.

    switch_hw.py up   <run dir> [--device /dev/ttyACM0] [--log-host 192.168.0.224:6969] [--owner TEXT]
                      [--bridge-host 192.168.0.224:6970]
    switch_hw.py down <run dir>
    switch_hw.py capture-log <file> --log-host HOST:PORT      (started by `up`; you don't run it by hand)
    switch_hw.py capture-bridge <run dir> --bridge-host HOST:PORT --console FILE   (started by `up --bridge-host`)
    switch_hw.py state <run dir> [--fresh pos,speed,roster,status] [--cap S]   (a bridge run: the console's state as JSON)

`up` writes <run dir>/instances.json with one instance, "sw":
  socket          usb://<device>; the runner's client factory hands it to switch-remote's client (macro.play is
                  uploaded and played by the pad at 60 Hz of its own clock; ui.screenshot is an OBS still)
  console_log     the live Skyline TCP log, captured into <run dir>/skyline.log one COMPLETE line at a time (no
  skyline_log     headers between chunks), so the runner's log gates and routes.LogCursor read it like an Eden log
  screenshot_target "switch"
The accepted Tile Cursor frame-repeat NRO is on the card. Its SD event log is read after the session.
A bridge-enabled Tile Cursor publishes live tile selection state through the bridge's plugin-state interface.

With `--bridge-host` (the test-only ssbu-hw-bridge NRO installed beside TrainingRooms, which serves TR's oracle lines and
native reads on its own port) the instance also gets, as on Eden:
  diag_log        <run dir>/bridge.log: the bridge's lines (TR's own oracle text plus ` frame= wall=`), also appended to
                  console_log, so log gates, LogCursor and routes.read_state read them unchanged
  settings        <run dir>/bridge/settings.json: TR's settings.json, fetched over the bridge at connect and after every
                  `settings.json written` line, so json gates read it unchanged
  bridge_files    <run dir>/bridge/files: other TR files (`bridge-file.req` beside settings.json names them, one per line)
The folder beside settings.json is a request spool: `diag-pos.req`, `diag-save.req`, `diag-load.req`, `diag-item.req`
and `diag-settings-next.json` (what Eden rows write beside TR's settings.json) become the bridge's `pos`, `save`, `load`,
`item N` and `set <base64>` requests (`bridge-speed.req`, `bridge-roster.req` and `bridge-status.req` ask for a fresh
speed, roster or status-id line). `bridge.requests.jsonl` records each one sent.

`state` (and `bridge_state()` for a row) reads the console without Eden: the last B-FRAME (` frame= wall=` on every
line), B-POS (`state pos`, both bodies), B-STAGE, B-ROSTER, B-ITEM, B-SPEED and B-STATUS-NAMES (`cpu_ids` ids) lines in
bridge.log. `--fresh` first writes the named requests to the spool and waits for their new lines.

Before writing anything, `up` checks:
  - no other process holds the pad's tty (one driver per console);
  - the claim file is free (/home/intpa/.cache/switch-pad.claim, O_EXCL, released by `down`);
  - the pad answers VERSION;
  - OBS gives a still;
  - OBS can split the recording per row (recording.split_check; refused when off unless --allow-no-split; the
    result is journal.json "obs_split").
`up` then starts the log capture (pid in journal.json). `down` sends RESET, stops the capture and removes the claim.
It never powers, sleeps or reboots the console; HOME → X → A (`switch.terminate`) closes the game when a row needs it.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLAIM = Path.home() / '.cache' / 'switch-pad.claim'
DEFAULT_DEVICE = '/dev/ttyACM0'
DEFAULT_LOG = '192.168.0.224:6969'
# The bridge's port (ssbu-hw-bridge; Skyline's own log stays on 6969).
BRIDGE_PORT = 6970
REQUEST_FILES = {'diag-pos.req': 'pos', 'diag-save.req': 'save', 'diag-load.req': 'load',
                 'bridge-backup.req': 'backup', 'bridge-restore.req': 'restore', 'bridge-verify.req': 'verify',
                 'bridge-speed.req': 'speed', 'bridge-roster.req': 'roster', 'bridge-status.req': 'status_names'}
SETTINGS_WRITTEN = b'[TrainingRooms] settings.json written by the writer thread'
# Protocol v2 (bridge slice 3): written as `bridge-v2*.req` spool files, one request per line (`bridge_v2`).
V2_VERBS = {'caps', 'mem', 'scene', 'fighters', 'status_names', 'hid', 'input_watch', 'inject', 'inject_start', 'inject_stop', 'peek', 'chain', 'watch',
            # plugin state and same-boot reconnect (ssbu-hw-bridge docs/PLUGIN-STATE.md)
            'state', 'state_watch', 'providers', 'since', 'events',
            # Runtime toggle requests: bridge status and bridge off.
            'bridge'}


def bridge_v2(run, *requests):
    """Queue protocol v2 request lines for the run's bridge capture (one spool file per call, sent in order)."""
    spool = Path(run) / 'bridge'
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / f'bridge-v2-{time.time_ns()}.req'
    tmp = path.with_suffix('.tmp')
    tmp.write_bytes(b''.join(r.encode() + b'\n' for r in requests))
    os.replace(tmp, path)
    return path


def holders(device):
    """PIDs other than ours with the device open (a scan of /proc/*/fd)."""
    target = os.path.realpath(device)
    found = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            for fd in (proc / 'fd').iterdir():
                if os.path.realpath(fd) == target:
                    found.append(int(proc.name))
                    break
        except OSError:
            continue
    return found


class LineWriter:
    """Bytes in, complete lines out: a partial line waits for its newline (and is flushed at the end)."""

    def __init__(self, handle):
        self.handle, self.pending = handle, b''

    def feed(self, data):
        data = self.pending + data
        lines = data.split(b'\n')
        self.pending = lines.pop()
        for line in lines:
            self.handle.write(line.rstrip(b'\r') + b'\n')
        self.handle.flush()

    def close(self):
        if self.pending:
            self.handle.write(self.pending.rstrip(b'\r') + b'\n')
            self.pending = b''
        self.handle.flush()


CONNECTED = '[capture] connected'
# None: a quiet connection is kept. Skyline's TCP logger takes ONE client per game boot, so dropping a quiet but live
# connection (switch-hw-session-2, 17:53Z: "idle 120 s: reconnecting") lost the log for the rest of the boot; a dead
# peer is found by the TCP keepalive below instead. A number re-enables the old drop-and-reconnect (tests, Eden).
IDLE_S = None


def _keepalive(sock):
    """TCP keepalive (idle 10 s, 5 s apart, 3 probes): a peer that vanished without a FIN (console asleep, Wi-Fi drop)
    errors the recv instead of leaving a half-open socket that times out forever. switch-hw-session-1's capture
    stopped writing at 15:25:58Z while its process lived on: the loop below only reconnected on EOF or an error."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (('TCP_KEEPIDLE', 10), ('TCP_KEEPINTVL', 5), ('TCP_KEEPCNT', 3)):
            if hasattr(socket, name):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except (OSError, AttributeError):
        pass


def capture_log(path, host, port, stop=None, connect=socket.create_connection, retry_s=0.5, idle_s=IDLE_S,
                clock=time.monotonic):
    """Read Skyline's TCP log forever (reconnecting) and append complete lines to path. Read-only. Each connection
    writes a `[capture] connected host:port <UTC>` line (up's liveness gate reads it). A quiet connection is kept
    (idle_s None, the default: TCP keepalive ends a half-open one); with idle_s set, a connection with no data for
    idle_s seconds is dropped and made again."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as f:
        writer = LineWriter(f)
        while not (stop and stop()):
            try:
                sock = connect((host, port), timeout=3)
            except OSError:
                time.sleep(retry_s)
                continue
            _keepalive(sock)
            sock.settimeout(1.0)
            writer.feed(f'{CONNECTED} {host}:{port} {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}\n'.encode())
            last = clock()
            try:
                while not (stop and stop()):
                    try:
                        data = sock.recv(65536)
                    except socket.timeout:
                        if idle_s is not None and clock() - last > idle_s:
                            writer.feed(f'[capture] idle {idle_s:.0f} s: reconnecting\n'.encode())
                            break
                        continue
                    if not data:
                        break
                    last = clock()
                    writer.feed(data)
            except OSError:
                pass
            finally:
                sock.close()
                writer.close()
            time.sleep(retry_s)


def spool_requests(spool):
    """The request files Eden rows write beside TR's settings.json, oldest first, as bridge request lines; each file
    is removed once read (what TR does with its diag-*.req files)."""
    spool = Path(spool)
    found = []
    for path in spool.iterdir() if spool.is_dir() else []:
        name = path.name
        v2 = name.startswith('bridge-v2') and name.endswith('.req')
        if path.is_file() and (v2 or name in REQUEST_FILES or name in ('diag-item.req', 'diag-settings-next.json', 'bridge-file.req')):
            found.append((path.stat().st_mtime_ns, name, path))
    lines = []
    for _, name, path in sorted(found):
        data = path.read_bytes()
        path.unlink()
        if name.startswith('bridge-v2'):
            # One protocol v2 request per line; only v2 verbs pass (the fixture verbs keep their own files).
            lines.extend(line.strip() for line in data.splitlines() if line.strip().split(b' ')[0].decode('ascii', 'replace') in V2_VERBS)
        elif name in REQUEST_FILES:
            lines.append(REQUEST_FILES[name].encode())
        elif name == 'diag-item.req':
            lines.append(b'item ' + data.strip())
        elif name == 'diag-settings-next.json':
            lines.append(b'set ' + base64.b64encode(data))
        else:
            lines.extend(b'file ' + n.strip() for n in data.splitlines() if n.strip())
    return lines


class BridgeMirror:
    """Lines from the bridge in: file transfers are reassembled into files (settings.json also to the instance's
    `settings`), every other line goes to the bridge log and the console log, whole."""
    FILE_DATA = re.compile(rb'^\[Bridge\] file_data name=(\S+) seq=(\d+) b64=(\S*)')
    FILE_END = re.compile(rb'^\[Bridge\] file_end name=(\S+) bytes=(\d+) sha256=([0-9a-f]{64}) result=ok')

    def __init__(self, run, logs):
        self.run, self.logs, self.parts, self.wanted = Path(run), logs, {}, []
        self.recovered = False

    def line(self, raw):
        raw = raw.rstrip(b'\r')
        data = self.FILE_DATA.match(raw)
        if data:
            try:
                self.parts.setdefault(data.group(1), []).append(base64.b64decode(data.group(3), validate=True))
            except ValueError:  # a cut or damaged chunk: the transfer's file_end then reports mirrored=false
                self.parts.setdefault(data.group(1), []).append(b'\0damaged')
            return
        end = self.FILE_END.match(raw)
        if end:
            name, body = end.group(1), b''.join(self.parts.pop(end.group(1), []))
            ok = len(body) == int(end.group(2)) and hashlib.sha256(body).hexdigest().encode() == end.group(3)
            if ok:
                target = self.run / 'bridge' / 'files' / name.decode()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
                if name == b'settings.json':
                    (self.run / 'bridge' / 'settings.json.tmp').write_bytes(body)
                    os.replace(self.run / 'bridge' / 'settings.json.tmp', self.run / 'bridge' / 'settings.json')
            raw += b' mirrored=' + (b'true' if ok else b'false')
        if raw.startswith(SETTINGS_WRITTEN):
            self.wanted.append(b'file settings.json')
        if raw.startswith(b'[Bridge] req backup result=refused reason=backup_open') and not self.recovered:
            # A session that never restored: put the player's files back first, then open a fresh backup. Once per
            # connection: a restore that can't complete yet must not loop.
            self.recovered = True
            self.wanted.extend([b'restore', b'backup'])
        for log in self.logs:
            log.write(raw + b'\n')
            log.flush()


def capture_bridge(run, host, port, console, stop=None, connect=socket.create_connection, retry_s=0.5,
                   heartbeat_s=2.0, idle_s=6.0, max_backoff_s=5.0):
    """Hold the bridge's socket (reconnecting): its lines to bridge.log and the console log, the spool's requests out."""
    run = Path(run)
    spool = run / 'bridge'
    spool.mkdir(parents=True, exist_ok=True)
    # One capture per run: the bridge serves one client, so a second capture would wait unaccepted and take the spool's
    # requests (seen in Eden bridge-proof-yoga-7).
    lock = (run / 'bridge.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit(f'refused: another capture-bridge holds {run / "bridge.lock"}') from None
    with lock, (run / 'bridge.log').open('ab') as blog, Path(console).open('ab') as clog, \
            (run / 'bridge.requests.jsonl').open('a') as sent:
        mirror = BridgeMirror(run, [blog, clog])
        # Same-boot reconnect: the bridge's hello names its boot; ring lines carry ` ev=<seq>` once `events on` is
        # sent, and after a reconnect `since <cursor>` replays what was missed (or answers `result=gap`). A request
        # whose answer was lost is never sent again by the capture: the row's gate re-reads state instead.
        link = {'boot': None, 'cursor': 0, 'connects': 0}
        backoff = retry_s  # capped doubling 0.5 -> 5 s between attempts, reset by a hello
        while not (stop and stop()):
            try:
                sock = connect((host, port), timeout=3)
            except OSError:
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff_s)
                continue
            sock.settimeout(0.1)
            pending = b''
            link['connects'] += 1
            # A connection that delivers nothing is dead even without an EOF (a dropped network or a peer that closed
            # without a FIN reaching us: Eden after the bridge replaced this client): a heartbeat request after
            # `heartbeat_s` of silence, a reconnect after `idle_s`.
            last_rx, last_beat = time.monotonic(), 0.0
            try:
                # The fixture backup (D-Q12) comes first: the bridge refuses fixture requests until it is open.
                sock.sendall(b'backup\nfile settings.json\n')  # the bridge greets each new client by itself
                link['resumed'] = False  # `events on` + `since` wait for this connection's hello (link_line)
                while not (stop and stop()):
                    for request in mirror.wanted + spool_requests(spool):
                        sock.sendall(request + b'\n')
                        sent.write(json.dumps({'wall': time.time(), 'request': request[:80].decode('ascii', 'replace')}) + '\n')
                        sent.flush()
                    mirror.wanted.clear()
                    try:
                        data = sock.recv(65536)
                    except socket.timeout:
                        quiet = time.monotonic() - last_rx
                        if quiet > idle_s:
                            blog.write(b'[Adapter] connection idle %.1f s: reconnecting\n' % quiet)
                            blog.flush()
                            break
                        if quiet > heartbeat_s and time.monotonic() - last_beat > heartbeat_s:
                            sock.sendall(b'providers\n')
                            last_beat = time.monotonic()
                        continue
                    if not data:
                        break
                    last_rx = time.monotonic()
                    lines = (pending + data).split(b'\n')
                    pending = lines.pop()
                    for raw in lines:
                        note = link_line(link, raw)
                        if note:
                            blog.write(note + b'\n')
                            blog.flush()
                        if link.get('resume') is not None:
                            sock.sendall(link.pop('resume'))
                            backoff = retry_s
                        try:
                            mirror.line(raw)
                        except (OSError, ValueError) as error:  # one bad line never stops the capture
                            blog.write(b'[Adapter] line not handled: ' + repr(error).encode() + b'\n')
                            blog.flush()
            except OSError:
                pass
            finally:
                sock.close()
            # A connection that ended before a valid hello (accept then EOF) backs off like a refused connect.
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_s)


HELLO_BOOT = re.compile(rb'\[Bridge\] hello .*?boot=(0x[0-9a-f]+) .*?reconnects=(\d+)')
EV = re.compile(rb' ev=(\d+) frame=')


def link_line(link, raw):
    """Track the boot and the confirmed event cursor. On a hello it queues `link['resume']` for this connection:
    `events on`, then for the same boot `since <last confirmed cursor>` (zero included); a new boot resets the cursor.
    Returns an `[Adapter]` note for the hello, else None."""
    m = EV.search(raw)
    if m:
        link['cursor'] = max(link['cursor'], int(m[1]))
        return None
    m = HELLO_BOOT.search(raw)
    if not m:
        return None
    boot = m[1].decode()
    same = link['boot'] == boot
    if link['boot'] is None:
        note = f'[Adapter] bridge connected boot={boot}'
    elif same:
        note = f'[Adapter] reconnect same_boot=1 boot={boot} connects={link["connects"]} cursor={link["cursor"]}'
    else:
        note = f'[Adapter] reconnect same_boot=0 boot={boot} was={link["boot"]} (the console restarted)'
        link['cursor'] = 0
    link['resume'] = b'events on\n' + (b'since %d\n' % link['cursor'] if same else b'')
    link['boot'] = boot
    return note.encode()


def capture_bridge_file(run, sd_dir, console, stop=None, poll_s=0.05):
    """The bridge's file transport (Eden's room backend: every guest socket made after the room join is a room proxy
    socket, so the host can't reach the bridge's TCP port). `sd_dir` is the instance's sdmc `ultimate/ssbu-hw-bridge`
    folder: its `stream.log` (truncated by the bridge at boot) is mirrored like the socket, and requests are handed over
    as one whole `requests.txt` (written aside, renamed in), the next batch only after the bridge took the last."""
    run, sd_dir = Path(run), Path(sd_dir)
    spool = run / 'bridge'
    spool.mkdir(parents=True, exist_ok=True)
    lock = (run / 'bridge.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit(f'refused: another capture-bridge holds {run / "bridge.lock"}') from None
    stream, requests = sd_dir / 'stream.log', sd_dir / 'requests.txt'
    with lock, (run / 'bridge.log').open('ab') as blog, Path(console).open('ab') as clog, \
            (run / 'bridge.requests.jsonl').open('a') as sent:
        mirror = BridgeMirror(run, [blog, clog])
        queue, offset, pending = [b'backup', b'file settings.json'], 0, b''
        while not (stop and stop()):
            queue += mirror.wanted + spool_requests(spool)
            mirror.wanted.clear()
            if queue and not requests.exists():
                tmp = requests.with_name('requests.txt.tmp')
                tmp.write_bytes(b''.join(r + b'\n' for r in queue))
                os.replace(tmp, requests)
                for r in queue:
                    sent.write(json.dumps({'wall': time.time(), 'request': r[:80].decode('ascii', 'replace')}) + '\n')
                sent.flush()
                queue = []
            if stream.exists():
                size = stream.stat().st_size
                if size < offset:  # a new boot truncated it
                    offset, pending = 0, b''
                if size > offset:
                    with stream.open('rb') as f:
                        f.seek(offset)
                        data = f.read(size - offset)
                    offset += len(data)
                    lines = (pending + data).split(b'\n')
                    pending = lines.pop()
                    for raw in lines:
                        try:
                            mirror.line(raw)
                        except (OSError, ValueError) as error:
                            blog.write(b'[Adapter] line not handled: ' + repr(error).encode() + b'\n')
                            blog.flush()
            time.sleep(poll_s)


SUFFIX = re.compile(r' frame=(\d+) wall=(\d+)$')
STATE_POS = re.compile(r'\[TrainingRooms\] state pos entry=(\d+) kind=(-?\d+) x=(\S+) y=(\S+) lr=(\S+) dmg=(\S+) '
                       r'status=(-?\d+) situation=(-?\d+)')
STAGE = re.compile(r'\[Bridge\] stage id=(-?\d+)')
ROSTER = re.compile(r'\[Bridge\] roster entry=(\d+) kind=(-?\d+) nana=(true|false)')
ROSTER_REPLY = re.compile(r'\[Bridge\] req roster result=ok(?: entries=0x([0-9a-f]+))? frame=(\d+) wall=')
ITEM = re.compile(r'\[Bridge\] item entry=(\d+) held=(true|false) kind=(-?\d+)')
SPEED = re.compile(r'\[Bridge\] speed fps=(\S+) ratio=(\S+) frames=(\d+) ms=(\d+)')
STATUS_ID = re.compile(r'(FIGHTER_STATUS_KIND_[A-Z0-9_]+)=(-?\d+)')
# Protocol v2 lines (TR-independent: the bridge's own hooks).
CAPS = re.compile(r'\[Bridge\] caps proto=(\d+) (.*?) ?tr=(\S+)')
SCENE = re.compile(r'\[Bridge\] scene mode=(0x[0-9a-f]+) melee=(0x[0-9a-f]+) init=([01]) ldn=(\w+) class=(\w+)'
                   r'(?: battle=([01]) netplay=([01]))?')
FIGHTER = re.compile(r'\[Bridge\] fighter entry=(\d+) kind=(-?\d+) x=(\S+) y=(\S+) lr=(\S+) dmg=(\S+) status=(-?\d+) '
                     r'situation=(-?\d+) stock=(-?\d+) dead=(-?\d+) item=(-?\d+)')
GONE = re.compile(r'\[Bridge\] fighter entry=(\d+) gone')
INPUT = re.compile(r'\[Bridge\] input slot=(\d+) src=(\w+) buttons=(0x[0-9a-f]+) lx=(-?\d+) ly=(-?\d+) rx=(-?\d+) ry=(-?\d+)')
WATCH = re.compile(r'\[Bridge\] watch addr=(\S+) len=(\d+) val=(0x[0-9a-f]+) left=(\d+)')
# Each fresh read: its spool file, the line that answers it, and the file's bytes (speed answers with its own line;
# roster re-sends the roster lines on the next frames after its reply; status_names sends the id lines, then its reply;
# pos also needs both bodies' `state pos` lines, checked below; the v2 reads are one-line v2 spool files).
FRESH = {'pos': ('diag-pos.req', r'\[Bridge\] req pos result=', b''),
         'speed': ('bridge-speed.req', r'\[Bridge\] speed fps=', b''),
         'roster': ('bridge-roster.req', r'\[Bridge\] roster entry=', b''),
         'status': ('bridge-status.req', r'\[Bridge\] req status_names result=', b''),
         'scene': ('bridge-v2-scene.req', r'\[Bridge\] scene mode=', b'scene'),
         'fighters': ('bridge-v2-fighters.req', r'\[Bridge\] fighters entries=', b'fighters'),
         'caps': ('bridge-v2-caps.req', r'\[Bridge\] caps proto=', b'caps'),
         'status_names': ('bridge-v2-status.req', r'\[Bridge\] req status_names result=', b'status_names')}


def parse_bridge_state(lines):
    """The newest value of each native read in bridge.log lines (oldest first): B-FRAME, B-POS, B-STAGE, B-ROSTER,
    B-ITEM, B-SPEED and B-STATUS-NAMES. A key is absent until its line has been seen."""
    out = {'pos': {}, 'roster': {}, 'items': {}, 'status_ids': {}, 'fighters': {}, 'inputs': {}}
    for line in lines:
        tail = SUFFIX.search(line)
        if tail:
            out['frame'], out['wall_ms'] = int(tail.group(1)), int(tail.group(2))
        frame = tail and int(tail.group(1))
        if (m := FIGHTER.search(line)):
            g = m.groups()
            out['fighters'][int(g[0])] = {'kind': int(g[1]), 'x': float(g[2]), 'y': float(g[3]), 'lr': float(g[4]),
                                          'dmg': float(g[5]), 'status': int(g[6]), 'situation': int(g[7]),
                                          'stock': int(g[8]), 'dead': int(g[9]), 'item': int(g[10]), 'frame': frame}
        elif (m := GONE.search(line)):
            out['fighters'].pop(int(m.group(1)), None)
        elif (m := INPUT.search(line)):
            g = m.groups()
            out['inputs'].setdefault(g[1], {})[int(g[0])] = {'buttons': int(g[2], 16), 'lx': int(g[3]), 'ly': int(g[4]),
                                                              'rx': int(g[5]), 'ry': int(g[6]), 'frame': frame}
        elif (m := SCENE.search(line)):
            out['scene'] = {'mode': int(m.group(1), 16), 'melee': int(m.group(2), 16), 'init': m.group(3) == '1',
                            'ldn': m.group(4), 'class': m.group(5), 'frame': frame}
            if m.group(6) is not None:  # the game's live state (bridge since 2026-11-18 slice 3 s3-6)
                out['scene'].update(battle=m.group(6) == '1', netplay=m.group(7) == '1')
        elif (m := CAPS.search(line)):
            out['caps'] = {'proto': int(m.group(1)), 'tr': m.group(3), 'features': m.group(2).split()}
        elif (m := WATCH.search(line)):
            out['watch'] = {'addr': m.group(1), 'len': int(m.group(2)), 'val': int(m.group(3), 16), 'left': int(m.group(4)),
                            'frame': frame}
        elif (m := STATE_POS.search(line)):
            out['pos'][int(m.group(1))] = {'kind': int(m.group(2)), 'x': float(m.group(3)), 'y': float(m.group(4)),
                                           'lr': float(m.group(5)), 'dmg': float(m.group(6)), 'status': int(m.group(7)),
                                           'situation': int(m.group(8)), 'frame': tail and int(tail.group(1))}
        elif (m := STAGE.search(line)):
            out['stage'] = int(m.group(1))
        elif (m := ROSTER.search(line)):
            out['roster'][int(m.group(1))] = {'kind': int(m.group(2)), 'nana': m.group(3) == 'true'}
        elif (m := ITEM.search(line)):
            out['items'][int(m.group(1))] = {'held': m.group(2) == 'true', 'kind': int(m.group(3))}
        elif (m := SPEED.search(line)):
            out['speed'] = {'fps': float(m.group(1)), 'ratio': float(m.group(2)), 'frames': int(m.group(3)),
                            'ms': int(m.group(4))}
        elif '[TrainingRooms] cpu_ids ' in line or line.startswith('[Bridge] status_names '):
            out['status_ids'].update({name: int(v) for name, v in STATUS_ID.findall(line)})
    return out


def roster_response(lines, entries=()):
    """The roster that answers the newest request reply in this request's log lines."""
    reply = None
    start = 0
    for index, line in enumerate(lines):
        match = ROSTER_REPLY.search(line)
        if match:
            reply, start = match, index + 1
    if reply is None:
        return None
    mask, frame = reply.group(1), int(reply.group(2))
    required = {bit for bit in range(int(mask, 16).bit_length()) if int(mask, 16) & (1 << bit)} \
        if mask is not None else set(entries)
    roster = {}
    for line in lines[start:]:
        match, tail = ROSTER.search(line), SUFFIX.search(line)
        if match and tail and int(tail.group(1)) >= frame:
            roster[int(match.group(1))] = {'kind': int(match.group(2)), 'nana': match.group(3) == 'true'}
    if not required.issubset(roster) or (mask is None and not required and not roster):
        return None
    return {entry: value for entry, value in roster.items() if mask is None or entry in required}


def fighters_response(lines):
    """Return bodies bound to the newest complete fighters reply, or None."""
    reply = None
    start = 0
    for index, line in enumerate(lines):
        match = re.search(r'\[Bridge\] req fighters result=ok frame=(\d+)(?: |$)', line)
        if match:
            reply, start = int(match.group(1)), index + 1
    if reply is None:
        return None
    bodies = {}
    for line in lines[start:]:
        tail = SUFFIX.search(line)
        frame = int(tail.group(1)) if tail else None
        if (summary := re.search(r'\[Bridge\] fighters entries=0x([0-9a-f]+)(?: |$)', line)):
            if frame is None or frame < reply:
                return None
            mask = int(summary.group(1), 16)
            required = {bit for bit in range(mask.bit_length()) if mask & (1 << bit)}
            if not required.issubset(bodies):
                return None
            return {entry: bodies[entry] for entry in sorted(required)}
        match = FIGHTER.search(line)
        if match and frame is not None and frame >= reply:
            values = match.groups()
            bodies[int(values[0])] = {'kind': int(values[1]), 'x': float(values[2]), 'y': float(values[3]),
                                      'lr': float(values[4]), 'dmg': float(values[5]), 'status': int(values[6])}
    return None


def fighters_now(run, cap=6.0):
    """Request and return fresh fighters bound to their reply."""
    run = Path(run)
    log = run / 'bridge.log'
    start = log.stat().st_size if log.exists() else 0
    filename, _, payload = FRESH['fighters']
    (run / 'bridge' / filename).write_bytes(payload)
    until = time.monotonic() + cap
    while True:
        if log.exists():
            with log.open('rb') as stream:
                stream.seek(start)
                lines = stream.read().decode(errors='replace').splitlines()
            result = fighters_response(lines)
            if result is not None:
                return result
        if time.monotonic() >= until:
            raise RuntimeError(f'bridge fighters: no complete answer within {cap} s')
        time.sleep(min(.1, max(0, until - time.monotonic())))


def bridge_state(run, fresh=(), cap=6.0, *, roster_entries=()):
    """The console's state from a bridge run (`up --bridge-host`): `fresh` names (pos, speed, roster, status) are asked
    for first through the spool and must answer within `cap` s; everything else is the newest line already seen."""
    run = Path(run)
    log = run / 'bridge.log'
    start = log.stat().st_size if log.exists() else 0
    for name in fresh:
        (run / 'bridge' / FRESH[name][0]).write_bytes(FRESH[name][2])
    want = {name: re.compile(FRESH[name][1]) for name in fresh}
    seen, until = set(), time.monotonic() + cap
    while len(seen) < len(want):
        with log.open('rb') as f:
            f.seek(start)
            new = f.read().decode(errors='replace').splitlines()
        seen = {name for name, regex in want.items() if any(regex.search(line) for line in new)}
        if 'pos' in want and len({int(m.group(1)) for line in new if (m := STATE_POS.search(line))}) < 2:
            seen.discard('pos')
        if 'roster' in want:
            response = roster_response(new, roster_entries)
            if response is None:
                seen.discard('roster')
            else:
                seen.add('roster')
        if len(seen) < len(want):
            if time.monotonic() > until:
                raise RuntimeError(f'bridge state: no answer for {sorted(set(want) - seen)} within {cap} s')
            time.sleep(0.1)
    state = parse_bridge_state(log.read_text(errors='replace').splitlines())
    if 'roster' in want:
        state['roster'] = response
    return state


def bridge_bodies(run, cap=1.5):
    """Each live body's numbers for a still, in the judge's `pos` shape: the bridge's own `fighters` (protocol v2: every
    battle scene, any mod, TR or not) when its caps line serves them, else TR's `state pos` (Training only). The
    returned state's `pos` holds whichever answered and `bodies_from` says which. Without a caps line yet it asks for
    one; a slice 2 bridge never answers it, and the TR read follows."""
    log = Path(run) / 'bridge.log'
    known = parse_bridge_state(log.read_text(errors='replace').splitlines() if log.exists() else [])
    if 'caps' not in known:
        try:
            known = bridge_state(run, ('caps',), cap)
        except RuntimeError:
            known = {}
    v2 = 'fighters' in known.get('caps', {}).get('features', [])
    # A slice 3 bridge names the statuses itself (the game's own binds) once per run; TR's cpu_ids still count.
    names = ('status_names',) if v2 and 'status_names' in known.get('caps', {}).get('features', []) \
        and not known.get('status_ids') else ()
    state = bridge_state(run, (('fighters',) + names) if v2 else ('pos',), cap)
    state['bodies_from'] = 'fighters' if v2 else 'pos'
    state['pos'] = state['fighters'] if v2 else state['pos']
    return state


def wait_scene(run, cls, cap=30.0, every=0.25):
    """Wait until the bridge's scene class is `cls` (training, local_vs, menu, local_wireless, internet_online, unknown):
    a fresh `scene` read each poll, so a class already reached answers at once. Returns the scene."""
    until = time.monotonic() + cap
    scene = None
    while True:
        try:
            scene = bridge_state(run, ('scene',), cap=max(0.5, min(6.0, until - time.monotonic()))).get('scene')
        except RuntimeError:
            scene = None
        if scene and scene['class'] == cls:
            return scene
        if time.monotonic() > until:
            raise RuntimeError(f'scene class {cls} not reached within {cap} s: last {scene}')
        time.sleep(every)


def bridge_request(host, port, request, cap=60.0, connect=socket.create_connection):
    """One request on a fresh connection to the bridge (the capture is stopped first): its `[Bridge] req <verb>` reply."""
    verb = request.split()[0].encode()
    deadline = time.monotonic() + cap
    while time.monotonic() < deadline:
        try:
            sock = connect((host, port), timeout=3)
        except OSError:
            time.sleep(0.5)
            continue
        with sock:
            sock.settimeout(1.0)
            sock.sendall(request.encode() + b'\n')
            pending = b''
            while time.monotonic() < deadline:
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                lines = (pending + data).split(b'\n')
                pending = lines.pop()
                for line in lines:
                    if line.startswith(b'[Bridge] req ' + verb + b' '):
                        return line.decode(errors='replace')
    return None


def restore_fixture(run, journal, connect=socket.create_connection):
    """D-Q12 at Down: the bridge writes back the files it backed up at Up and reads each one back; the receipt is kept."""
    host, _, port = journal['bridge_host'].rpartition(':')
    reply = bridge_request(host, int(port), 'restore', connect=connect)
    # `refused reason=no_backup` means no fixture backup is open: an earlier explicit restore already put the files back
    # and read them back (it deletes the backup only then), or none was ever opened, in which case the bridge refused
    # every fixture request. Either way nothing is outstanding (switch-hw-sol-3, 14:11Z).
    closed = bool(reply and ' result=ok ' in reply + ' ')
    absent = bool(reply and ' result=refused reason=no_backup' in reply)
    receipt = {'wall': time.time(), 'restore': reply, 'ok': closed or absent,
               'state': 'restored' if closed else 'no_open_backup' if absent else 'incomplete'}
    (Path(run) / 'bridge-restore.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def _client(device):
    sys.path.insert(0, str(HERE))
    import runner
    return runner.default_client_factory()(f'usb://{device}')


def wait_log_line(log, cap_s, start=None, poll=0.25, clock=time.monotonic):
    """The first complete line written to `log` past byte `start` (default: its size now), or None within cap_s."""
    log = Path(log)
    if start is None:
        start = log.stat().st_size if log.is_file() else 0
    until = clock() + cap_s
    while clock() < until:
        if log.is_file() and log.stat().st_size > start:
            with log.open('rb') as f:
                f.seek(start)
                line = f.readline()
            if line.endswith(b'\n'):
                return line.decode(errors='replace').rstrip()[:300]
        time.sleep(poll)
    return None


def external_log_connected(path):
    """The last connection line of another process's Skyline capture, or None. This module's capture writes
    `[capture] connected …`; switch-remote's skyline_tcplog.py writes `### <UTC> connected (#n)`."""
    path = Path(path)
    if not path.is_file():
        return None
    lines = [line for line in path.read_bytes().decode(errors='replace').splitlines()
             if line.startswith(CONNECTED) or re.match(r'### \S+ connected \(#\d+\)', line)]
    return lines[-1] if lines else None


def up(run, device, log_host, owner, allow_no_split=False, bridge_host=None, log_cap_s=30.0,
       log_file=None):
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True)
    others = holders(device)
    if others:
        raise SystemExit(f'refused: {device} is open in pid(s) {others}; one driver per console')
    CLAIM.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(CLAIM, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit(f'refused: claim held: {CLAIM.read_text().strip()}') from None
    os.write(fd, json.dumps({'owner': owner, 'run': str(run), 'pid': os.getpid(),
                             'since': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}).encode())
    os.close(fd)
    journal = {'claim': str(CLAIM), 'device': device, 'owner': owner, 'up': time.time()}
    try:
        client = _client(device)
        version = client.request('switch.version')
        if not version.get('ok'):
            raise SystemExit(f'pad did not answer VERSION: {version}')
        still = run / 'up-still.png'
        shot = client.request('ui.screenshot', {'path': str(still)})
        if not shot.get('ok') or not still.is_file():
            raise SystemExit(f'no OBS still: {shot}')
        # Per-row files (plan session_recording.per_row_files) need OBS's manual file splitting on. Checked here,
        # read-only: neither `up` nor the runner changes OBS settings (obsws.py split-enable does, by hand).
        split = client.request('recording.split_check', {})
        journal['obs_split'] = split.get('result') if split.get('ok') else {'ok': False, 'why': split.get('error')}
        if not journal['obs_split'].get('ok') and not allow_no_split:
            raise SystemExit(f'refused: OBS cannot split the recording per row: {journal["obs_split"].get("why")}. '
                             'Fix: /usr/bin/python3 -I <switch-remote>/tools/host/obsws.py split-enable (then up '
                             'again), or up --allow-no-split for a plan without "per_row_files"')
        client.request('input.reset')
        client.close()
        journal.update(version=version['result'], still=str(still))
        if log_file:
            # Skyline's logger takes ONE client per game boot. When another process already holds it (the card
            # installer's capture), a second capture would never connect: use that capture's file instead, and
            # prove it is the live one by its connection line, never by opening our own.
            log = Path(log_file)
            connected = external_log_connected(log)
            if not connected and not bridge_host:
                raise SystemExit(f'refused: {log} has no capture connection line: not a live capture')
            # With the bridge, no gate depends on the one-client Skyline logger: a missing one is only a warning.
            journal.update(log=str(log), log_external=True, log_live=(connected or '')[:200],
                           **({} if connected else {'log_warning': 'no live external logger capture (bridge gates only)'}))
        else:
            host, _, port = log_host.rpartition(':')
            log = run / 'skyline.log'
            log_start = log.stat().st_size if log.is_file() else 0  # lines from this capture on count as live
            proc = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), 'capture-log', str(log),
                                     '--log-host', log_host], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=open(run / 'capture-log.err', 'ab'), start_new_session=True)
            journal.update(capture_pid=proc.pid, log=str(log), log_host=log_host)
            live = wait_log_line(log, log_cap_s, start=log_start)
            if live is None and not bridge_host:
                proc.terminate()
                raise SystemExit(f'refused: no Skyline log line in {log} within {log_cap_s:.0f} s (log gates would '
                                 f'be blind); is the console on the network and the plugin logging to {log_host}?')
            journal['log_live'] = live
            if live is None:
                # The bridge carries every gate: the logger capture keeps trying, but is not needed.
                journal['log_warning'] = f'no Skyline log line within {log_cap_s:.0f} s (bridge gates only)'
        instances = {'sw': {'socket': f'usb://{device}', 'console_log': str(log), 'skyline_log': str(log),
                            'screenshot_target': 'switch', 'hardware': True}}
        if bridge_host:
            (run / 'bridge' / 'files').mkdir(parents=True, exist_ok=True)
            bridge = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), 'capture-bridge', str(run),
                                       '--bridge-host', bridge_host, '--console', str(log)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=open(run / 'capture-bridge.err', 'ab'), start_new_session=True)
            journal.update(bridge_pid=bridge.pid, bridge_host=bridge_host)
            instances['sw'].update(diag_log=str(run / 'bridge.log'), bridge_log=str(run / 'bridge.log'),
                                   settings=str(run / 'bridge' / 'settings.json'), bridge_files=str(run / 'bridge' / 'files'))
        (run / 'instances.json').write_text(json.dumps(instances, indent=2) + '\n')
    except BaseException:
        CLAIM.unlink(missing_ok=True)
        raise
    (run / 'journal.json').write_text(json.dumps(journal, indent=2) + '\n')
    print(json.dumps({'up': True, **journal}))


def down(run):
    run = Path(run)
    journal = json.loads((run / 'journal.json').read_text())
    out = {'down': True}
    try:
        client = _client(journal['device'])
        out['reset'] = client.request('input.reset').get('ok')
        # A session recording this run's runner started and didn't stop (it crashed or was killed): stop it. Only
        # that one: OBS is shared, so a recording without our open start in recording.jsonl is never touched.
        log = run / 'out' / 'recording.jsonl'
        if log.is_file():
            events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
            # per_row_files logs a "clip" event after the stop: look at the last start/stop, not the last line
            bounds = [e.get('event') for e in events if e.get('event') in ('start', 'stop')]
            if bounds and bounds[-1] == 'start':
                stopped = client.request('recording.session_stop', {})
                out['session_stop'] = stopped.get('result') if stopped.get('ok') else stopped.get('error')
                journal['session_recording'] = out['session_stop']
        client.close()
    except Exception as error:  # noqa: BLE001 - Down always continues to the release
        out['reset_error'] = repr(error)
    for key, label in (('capture_pid', 'capture_stopped'), ('bridge_pid', 'bridge_stopped')):
        pid = journal.get(key)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                out[label] = pid
            except ProcessLookupError:
                out[label] = 'gone'
    if journal.get('bridge_host'):
        time.sleep(1.0)  # the bridge accepts the next client once the capture's connection has closed
        out['bridge_restore'] = restore_fixture(run, journal)
    claim = Path(journal['claim'])
    ours = claim.is_file() and json.loads(claim.read_text()).get('run') == str(run)
    restored = not journal.get('bridge_host') or out['bridge_restore']['ok']
    if ours and restored:
        claim.unlink()
        out['released'] = True
    else:
        out['released'] = False
    if ours and not restored:
        # The player's TR files may still hold fixture data: the console stays claimed until `restore` succeeds.
        held = json.loads(claim.read_text())
        held['dirty'] = f"fixture restore incomplete: {out['bridge_restore'].get('restore')}"
        claim.write_text(json.dumps(held, indent=2) + '\n')
        out['dirty'] = held['dirty']
    journal['down'] = time.time()
    journal['down_result'] = out
    (run / 'journal.json').write_text(json.dumps(journal, indent=2) + '\n')
    print(json.dumps(out))
    if out.get('dirty'):
        raise SystemExit(f"down: {out['dirty']}; the claim stays until `switch_hw.py restore {run}` succeeds")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('up')
    p.add_argument('run')
    p.add_argument('--device', default=DEFAULT_DEVICE)
    p.add_argument('--log-host', default=DEFAULT_LOG)
    p.add_argument('--owner', default=os.environ.get('USER', 'runner'))
    p.add_argument('--allow-no-split', action='store_true',
                   help='go up even when OBS cannot split the recording per row (plans without per_row_files)')
    p.add_argument('--bridge-host', default=None,
                   help=f'HOST[:PORT] of the ssbu-hw-bridge NRO (port {BRIDGE_PORT} when omitted)')
    p.add_argument('--log-file', help="another process's live Skyline capture (the logger takes one client per "
                                      'boot): use it instead of opening a connection')
    p = sub.add_parser('down')
    p.add_argument('run')
    p = sub.add_parser('capture-log')
    p.add_argument('file')
    p.add_argument('--log-host', default=DEFAULT_LOG)
    p = sub.add_parser('restore', help='put back the TR files the bridge backed up (also done by down)')
    p.add_argument('run')
    p = sub.add_parser('state', help="a bridge run's console state (B-POS, FRAME, STAGE, ROSTER, ITEM, SPEED, STATUS) as JSON")
    p.add_argument('run')
    p.add_argument('--fresh', default='', help='comma list of pos,speed,roster,status,scene,fighters,caps to ask for first')
    p.add_argument('--cap', type=float, default=6.0)
    p = sub.add_parser('capture-bridge')
    p.add_argument('run')
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--bridge-host')
    source.add_argument('--bridge-files', help="the instance's sdmc ultimate/ssbu-hw-bridge folder (file transport)")
    p.add_argument('--console', required=True)
    args = parser.parse_args(argv)
    if getattr(args, 'bridge_host', None) and ':' not in args.bridge_host:
        args.bridge_host = f'{args.bridge_host}:{BRIDGE_PORT}'
    if args.cmd == 'up':
        up(args.run, args.device, args.log_host, args.owner, args.allow_no_split, args.bridge_host,
           log_file=args.log_file)
    elif args.cmd == 'down':
        down(args.run)
    elif args.cmd == 'restore':
        journal = json.loads((Path(args.run) / 'journal.json').read_text())
        receipt = restore_fixture(args.run, journal)
        print(json.dumps(receipt))
        claim = Path(journal['claim'])
        if not receipt['ok']:
            raise SystemExit('restore failed; the claim stays')
        if claim.is_file() and json.loads(claim.read_text()).get('dirty') and json.loads(claim.read_text()).get('run') == str(Path(args.run)):
            claim.unlink()
    elif args.cmd == 'state':
        print(json.dumps(bridge_state(args.run, [f for f in args.fresh.split(',') if f], args.cap), indent=2))
    elif args.cmd == 'capture-bridge':
        stopping = []
        signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
        if args.bridge_files:
            capture_bridge_file(args.run, args.bridge_files, args.console, stop=lambda: bool(stopping))
        else:
            host, _, port = args.bridge_host.rpartition(':')
            capture_bridge(args.run, host, int(port), args.console, stop=lambda: bool(stopping))
    else:
        host, _, port = args.log_host.rpartition(':')
        stopping = []
        signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
        capture_log(args.file, host, int(port), stop=lambda: bool(stopping))


if __name__ == '__main__':
    main()
