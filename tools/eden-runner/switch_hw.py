#!/usr/bin/env python3
"""Up and Down for a REAL Switch run: the hardware counterpart of eden-lifecycle for tools/eden-runner.

    switch_hw.py up   <run dir> [--device /dev/ttyACM0] [--log-host 192.168.0.224:6969] [--owner TEXT]
    switch_hw.py down <run dir>
    switch_hw.py capture-log <file> --log-host HOST:PORT      (started by `up`; you don't run it by hand)

`up` writes <run dir>/instances.json with one instance, "sw":
  socket          usb://<device>; the runner's client factory hands it to switch-remote's client (macro.play is
                  uploaded and played by the pad at 60 Hz of its own clock; ui.screenshot is an OBS still)
  console_log     the live Skyline TCP log, captured into <run dir>/skyline.log one COMPLETE line at a time (no
  skyline_log     headers between chunks), so the runner's log gates and routes.LogCursor read it like an Eden log
  screenshot_target "switch"
There is no diag_log, tile_log or settings: on hardware the release NRO prints no diag lines, the tile cursor isn't
on the card, and SD files are read over FTP after the session, not live.

Before writing anything, `up` checks:
  - no other process holds the pad's tty (one driver per console);
  - the claim file is free (/home/intpa/.cache/switch-pad.claim, O_EXCL, released by `down`);
  - the pad answers VERSION;
  - OBS gives a still.
`up` then starts the log capture (pid in journal.json). `down` sends RESET, stops the capture and removes the claim.
It never powers, sleeps or reboots the console; HOME → X → A (`switch.terminate`) closes the game when a row needs it.
"""
import argparse
import json
import os
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


def _client(device):
    sys.path.insert(0, str(HERE))
    import runner
    return runner.default_client_factory()(f'usb://{device}')


def up(run, device, log_host, owner):
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
        client.close()
    except Exception as error:  # noqa: BLE001 - Down always continues to the release
        out['reset_error'] = repr(error)
    pid = journal.get('capture_pid')
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            out['capture_stopped'] = pid
        except ProcessLookupError:
            out['capture_stopped'] = 'gone'
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
    p = sub.add_parser('down')
    p.add_argument('run')
    p = sub.add_parser('capture-log')
    p.add_argument('file')
    p.add_argument('--log-host', default=DEFAULT_LOG)
    args = parser.parse_args(argv)
    if args.cmd == 'up':
        up(args.run, args.device, args.log_host, args.owner)
    elif args.cmd == 'down':
        down(args.run)
    else:
        host, _, port = args.log_host.rpartition(':')
        stopping = []
        signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
        capture_log(args.file, host, int(port), stop=lambda: bool(stopping))


if __name__ == '__main__':
    main()
