#!/usr/bin/env python3
"""Up and Down for a REAL Switch run: the hardware counterpart of eden-lifecycle for tools/eden-runner.

    switch_hw.py up   <run dir> [--device /dev/ttyACM0] [--log-host 192.168.0.224:6969] [--owner TEXT]
                      [--bridge-host 192.168.0.224:6970]
    switch_hw.py down <run dir>
    switch_hw.py capture-log <file> --log-host HOST:PORT      (started by `up`; you don't run it by hand)
    switch_hw.py capture-bridge <run dir> --bridge-host HOST:PORT --console FILE   (started by `up --bridge-host`)

`up` writes <run dir>/instances.json with one instance, "sw":
  socket          usb://<device>; the runner's client factory hands it to switch-remote's client (macro.play is
                  uploaded and played by the pad at 60 Hz of its own clock; ui.screenshot is an OBS still)
  console_log     the live Skyline TCP log, captured into <run dir>/skyline.log one COMPLETE line at a time (no
  skyline_log     headers between chunks), so the runner's log gates and routes.LogCursor read it like an Eden log
  screenshot_target "switch"
There is no diag_log, tile_log or settings: on hardware the release NRO prints no diag lines, the tile cursor isn't
on the card, and SD files are read over FTP after the session, not live.

With `--bridge-host` (the test-only ssbu-hw-bridge NRO installed beside TrainingRooms, which serves TR's oracle lines and
native reads on its own port) the instance also gets, as on Eden:
  diag_log        <run dir>/bridge.log: the bridge's lines (TR's own oracle text plus ` frame= wall=`), also appended to
                  console_log, so log gates, LogCursor and routes.read_state read them unchanged
  settings        <run dir>/bridge/settings.json: TR's settings.json, fetched over the bridge at connect and after every
                  `settings.json written` line, so json gates read it unchanged
  bridge_files    <run dir>/bridge/files: other TR files (`bridge-file.req` beside settings.json names them, one per line)
The folder beside settings.json is a request spool: `diag-pos.req`, `diag-save.req`, `diag-load.req`, `diag-item.req`
and `diag-settings-next.json` (what Eden rows write beside TR's settings.json) become the bridge's `pos`, `save`, `load`,
`item N` and `set <base64>` requests. `bridge.requests.jsonl` records each one sent.

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
                 'bridge-backup.req': 'backup', 'bridge-restore.req': 'restore', 'bridge-verify.req': 'verify'}
SETTINGS_WRITTEN = b'[TrainingRooms] settings.json written by the writer thread'


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


def capture_log(path, host, port, stop=None, connect=socket.create_connection, retry_s=0.5):
    """Read Skyline's TCP log forever (reconnecting) and append complete lines to path. Read-only."""
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
            sock.settimeout(1.0)
            try:
                while not (stop and stop()):
                    try:
                        data = sock.recv(65536)
                    except socket.timeout:
                        continue
                    if not data:
                        break
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
        if path.is_file() and (name in REQUEST_FILES or name in ('diag-item.req', 'diag-settings-next.json', 'bridge-file.req')):
            found.append((path.stat().st_mtime_ns, name, path))
    lines = []
    for _, name, path in sorted(found):
        data = path.read_bytes()
        path.unlink()
        if name in REQUEST_FILES:
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
        if raw.startswith(b'[Bridge] req backup result=refused reason=backup_open'):
            # A session that never restored: put the player's files back first, then open a fresh backup.
            self.wanted.extend([b'restore', b'backup'])
        for log in self.logs:
            log.write(raw + b'\n')
            log.flush()


def capture_bridge(run, host, port, console, stop=None, connect=socket.create_connection, retry_s=0.5):
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
        while not (stop and stop()):
            try:
                sock = connect((host, port), timeout=3)
            except OSError:
                time.sleep(retry_s)
                continue
            sock.settimeout(0.1)
            pending = b''
            try:
                # The fixture backup (D-Q12) comes first: the bridge refuses fixture requests until it is open.
                sock.sendall(b'backup\nfile settings.json\n')  # the bridge greets each new client by itself
                while not (stop and stop()):
                    for request in mirror.wanted + spool_requests(spool):
                        sock.sendall(request + b'\n')
                        sent.write(json.dumps({'wall': time.time(), 'request': request[:80].decode('ascii', 'replace')}) + '\n')
                        sent.flush()
                    mirror.wanted.clear()
                    try:
                        data = sock.recv(65536)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    lines = (pending + data).split(b'\n')
                    pending = lines.pop()
                    for raw in lines:
                        try:
                            mirror.line(raw)
                        except (OSError, ValueError) as error:  # one bad line never stops the capture
                            blog.write(b'[Adapter] line not handled: ' + repr(error).encode() + b'\n')
                            blog.flush()
            except OSError:
                pass
            finally:
                sock.close()
            time.sleep(retry_s)


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
    receipt = {'wall': time.time(), 'restore': reply, 'ok': bool(reply and ' result=ok ' in reply + ' ')}
    (Path(run) / 'bridge-restore.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def _client(device):
    sys.path.insert(0, str(HERE))
    import runner
    return runner.default_client_factory()(f'usb://{device}')


def up(run, device, log_host, owner, allow_no_split=False, bridge_host=None):
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
        host, _, port = log_host.rpartition(':')
        log = run / 'skyline.log'
        proc = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), 'capture-log', str(log),
                                 '--log-host', log_host], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=open(run / 'capture-log.err', 'ab'), start_new_session=True)
        journal.update(capture_pid=proc.pid, log=str(log), log_host=log_host)
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
    if claim.is_file() and json.loads(claim.read_text()).get('run') == str(run):
        claim.unlink()
        out['released'] = True
    else:
        out['released'] = False
    journal['down'] = time.time()
    journal['down_result'] = out
    (run / 'journal.json').write_text(json.dumps(journal, indent=2) + '\n')
    print(json.dumps(out))


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
    p = sub.add_parser('down')
    p.add_argument('run')
    p = sub.add_parser('capture-log')
    p.add_argument('file')
    p.add_argument('--log-host', default=DEFAULT_LOG)
    p = sub.add_parser('restore', help='put back the TR files the bridge backed up (also done by down)')
    p.add_argument('run')
    p = sub.add_parser('capture-bridge')
    p.add_argument('run')
    p.add_argument('--bridge-host', required=True)
    p.add_argument('--console', required=True)
    args = parser.parse_args(argv)
    if getattr(args, 'bridge_host', None) and ':' not in args.bridge_host:
        args.bridge_host = f'{args.bridge_host}:{BRIDGE_PORT}'
    if args.cmd == 'up':
        up(args.run, args.device, args.log_host, args.owner, args.allow_no_split, args.bridge_host)
    elif args.cmd == 'down':
        down(args.run)
    elif args.cmd == 'restore':
        print(json.dumps(restore_fixture(args.run, json.loads((Path(args.run) / 'journal.json').read_text()))))
    elif args.cmd == 'capture-bridge':
        host, _, port = args.bridge_host.rpartition(':')
        stopping = []
        signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
        capture_bridge(args.run, host, int(port), args.console, stop=lambda: bool(stopping))
    else:
        host, _, port = args.log_host.rpartition(':')
        stopping = []
        signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
        capture_log(args.file, host, int(port), stop=lambda: bool(stopping))


if __name__ == '__main__':
    main()
