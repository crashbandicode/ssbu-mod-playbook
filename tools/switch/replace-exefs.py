"""Single-writer, finite card transaction: replace one Skyline loader file on the
Switch SD card (not a plugin NRO) with a new build, keeping a rollback copy on the
card. The phases, receipts and readbacks mirror upgrade2-mtp.py and upgrade2-ums.py;
this tool exists for the two files they refuse:

    atmosphere/contents/01006A800016E000/exefs/subsdk9      (Skyline's loader, an NSO)
    atmosphere/contents/01006A800016E000/exefs/main.npdm    (its permissions)

One transaction replaces one file; run it twice for both. It never mounts, unmounts
or ejects anything and never starts or stops DBI or hekate UMS: the user connects
and disconnects the Switch (playbook WORKFLOW, Switch) and exposes the card, over
DBI MTP (default), DBI FTP ("transport": "ftp"), or a mounted USB disk
("transport": "ums"). For FTP add --root ftp://<host>:<port> to every phase.

  python3 -I replace-exefs.py preflight <dir>   # read-only: list, keep local copies, check `old`
  python3 -I replace-exefs.py install   <dir>   # rollback copy, stage, replace, read everything back
  python3 -I replace-exefs.py verify    <dir>   # read every file the install touched once more

<dir> holds the new file under its own name (`subsdk9` or `main.npdm`) and
operator-approval.json:

  operator_order, operator_install_authorized (install only), target (one of the two
  paths above), data (the folder for the rollback copy and the stage, `ultimate/<name>`;
  not exefs/ and not the plugin folder), old and new (sha256), and optionally
  transport ("mtp" | "ums" | "ftp"), mount (ums: the card's mount point) and extra_protected
  (card files `ultimate/<folder>/<file>` that are read before and after like the
  protected configs, for example the Training Modpack's save_states.json).

Preflight lists exefs/ and the plugin folder, keeps a local copy of every file in
both, of the protected configs (hekate_ipl.ini, the Deluxe and fx-control config.ini)
and of the extras, and checks that the installed file's sha256 is `old`. Install
writes `<data>/backups/pre-<new16>.<name>` (the rollback copy, read back), stages
the new file at `<data>/install/<name>-<new16>.stage` (read back), replaces the
target, then reads back the target and every file preflight copied, and checks the
two folders list the same names. A mismatch after the replace puts the old file
back and reads it back (`rolled_back_verified`, or `recovery_required` when even that
fails). Every phase writes its receipt in <dir> (preflight.json, deployment.json,
verify.json); a phase whose receipt exists is not run again: inspect it first.

Tests drive main() in a process with URI and GIO pointed at a local `file://` tree
(tools/switch/tests/); the script itself reads no environment switch for that."""
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import ftplib
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from urllib.parse import quote, urlsplit

URI = 'mtp://-_DBI_XAW10006803734/1%3A%20SD%20Card'
GIO = '/usr/bin/gio'
TITLE = 'atmosphere/contents/01006A800016E000'
EXEFS = TITLE + '/exefs'
PLUGINS = TITLE + '/romfs/skyline/plugins'
LOADER_MAGIC = {EXEFS + '/subsdk9': b'NSO0', EXEFS + '/main.npdm': b'META'}
PROTECTED = ['bootloader/hekate_ipl.ini', 'ultimate/ssbu_online_deluxe_rewrite/config.ini',
             'ultimate/ssbu_fx_control/config.ini']
EXTRA = re.compile(r'ultimate/[A-Za-z0-9_-]+/[A-Za-z0-9_-][A-Za-z0-9_.-]*')
DATA_FOLDER = re.compile(r'ultimate/[A-Za-z0-9_-]+')
SHA256 = re.compile(r'[0-9a-f]{64}')
RECEIPTS = {'preflight': 'preflight.json', 'install': 'deployment.json', 'verify': 'verify.json'}
MAX_BYTES = 30_000_000
ALIGN = 4096  # O_DIRECT buffer, offset and length alignment (covers 512 B and 4 KiB sectors)
REGULAR = '(regular)'

sha = lambda data: hashlib.sha256(data).hexdigest()
now = lambda: datetime.now(timezone.utc).isoformat()


class Refused(Exception):
    """A check failed."""


def require(condition, message):
    if not condition:
        raise Refused(message)


def info(path):
    data = Path(path).read_bytes()
    return {'sha256': sha(data), 'bytes': len(data), 'local': str(path)}


class Journal:
    """One phase's receipt, rewritten atomically after every step."""

    def __init__(self, path, receipt):
        self.path, self.receipt = path, receipt

    def save(self):
        self.receipt['updated'] = now()
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.receipt, indent=2) + '\n')
        temporary.replace(self.path)

    def state(self, value):
        self.receipt['state'] = value
        self.save()
        print(value, flush=True)


class Card:
    """What a transport does. Every operation is logged in the receipt's `commands`."""
    description = ''
    closing = ''

    def __init__(self, journal, commands):
        self.journal, self.commands = journal, commands
        self.deadline = time.monotonic() + 600

    def left(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('card phase total deadline reached')
        return left

    def allow_rollback_time(self):
        self.deadline = time.monotonic() + 240

    def identity(self):
        return {}

    def start(self):
        """Checks that must hold before the first card operation."""

    def listing(self, relative):
        raise NotImplementedError

    def exists(self, relative):
        raise NotImplementedError

    def mkdir_p(self, relative):
        raise NotImplementedError

    def fetch(self, relative, destination):
        """Copy a card file to a local path, as the card holds it."""
        raise NotImplementedError

    def store(self, local, relative):
        """Write a local file to a card path."""
        raise NotImplementedError

    def replace(self, staged_local, stage_relative, target_relative):
        """Put the staged file over the target."""
        raise NotImplementedError

    def finish(self, receipt):
        """Fields for the receipt when the phase ends."""


class MtpCard(Card):
    """DBI's MTP responder through gio, the calls upgrade2-mtp.py proved."""
    description = 'DBI USB MTP'
    closing = 'Done. The Switch is still connected: disconnect it (close DBI) when you are ready.'

    def identity(self):
        return {'device_sd_uri': URI}

    def uri(self, relative):
        return URI + '/' + quote(relative, safe='/')

    def gio(self, *args, check=True):
        left = self.left()
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')
        entry = {'argv': [GIO, *args], 'started': now()}
        process = subprocess.Popen([GIO, *args], env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=min(60, left))
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=2)
        except BaseException:
            # An interrupt must not leave a transfer running while the rollback starts.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            entry.update(finished=now(), exit_code=process.returncode, interrupted=True)
            self.commands.append(entry)
            self.journal.save()
            raise
        entry.update(finished=now(), exit_code=process.returncode, timed_out=timed_out,
                     stdout=stdout, stderr=stderr)
        self.commands.append(entry)
        self.journal.save()
        if check and (timed_out or process.returncode):
            raise RuntimeError(f'GIO failed/timeout: {entry!r}')
        return stdout

    def listing(self, relative):
        result = {}
        for line in self.gio('list', '-l', self.uri(relative)).splitlines():
            name, size, kind = line.split('\t')
            require('/' not in name and name not in ('.', '..'), f'odd name in {relative}: {name!r}')
            result[name] = {'size': int(size), 'kind': kind}
        return result

    def exists(self, relative):
        self.gio('info', self.uri(relative), check=False)
        entry = self.commands[-1]
        if entry['exit_code'] == 0 and not entry['timed_out']:
            return True
        message = entry['stderr'].lower()
        if entry['exit_code'] == 2 and not entry['timed_out'] and ('not found' in message or 'no such file' in message):
            return False
        raise RuntimeError(f'GIO could not tell whether {relative} exists: {entry!r}')

    def mkdir_p(self, relative):
        self.gio('mkdir', '-p', self.uri(relative))

    def fetch(self, relative, destination):
        self.gio('copy', '-T', '--', self.uri(relative), str(destination))

    def store(self, local, relative):
        self.gio('copy', '-T', '--', str(local), self.uri(relative))

    def replace(self, staged_local, stage_relative, target_relative):
        # Proven DBI copy-replace; MTP rename is not assumed atomic or supported.
        self.store(staged_local, target_relative)

    def finish(self, receipt):
        receipt['all_command_processes_exited'] = all('exit_code' in entry for entry in self.commands)


class UmsCard(Card):
    """The card mounted as a USB disk (hekate Nyx UMS, a card reader). The card is never
    unmounted or remounted: every read is O_DIRECT (or drops the file's cached pages first),
    so a readback shows what the card holds."""
    description = 'mounted card (USB disk)'
    closing = 'Done. The card is still mounted: unmount or eject it yourself when you are ready.'

    def __init__(self, journal, commands, root):
        super().__init__(journal, commands)
        self.root = Path(root)

    def identity(self):
        return {'mount': str(self.root)}

    def start(self):
        self.check_mount()
        self.journal.receipt['mount_source'] = self.mount_source()

    def check_mount(self):
        require(self.root.is_absolute() and self.root.is_mount(), f'{self.root} is not a mount point')

    def mount_source(self):
        entry = self.op('exec', '/usr/bin/findmnt', '-n', '-o', 'SOURCE', '--target', str(self.root))
        process = subprocess.run(['/usr/bin/findmnt', '-n', '-o', 'SOURCE', '--target', str(self.root)],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.done(entry, exit_code=process.returncode, stdout=process.stdout, stderr=process.stderr)
        require(process.returncode == 0, f'findmnt failed: {entry!r}')
        return process.stdout.strip()

    def mount_id(self):
        """The kernel's id of the mount at the root: unchanged across verify shows the card
        stayed mounted the whole time."""
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields = line.split()
            if fields[4].replace('\\040', ' ') == str(self.root):
                return int(fields[0])
        raise Refused(f'{self.root} is not in mountinfo')

    def op(self, name, *args):
        self.left()
        entry = {'op': name, 'args': [str(a) for a in args], 'started': now()}
        self.commands.append(entry)
        return entry

    def done(self, entry, **more):
        entry.update(finished=now(), **more)
        self.journal.save()

    def path(self, relative):
        require(not relative.startswith('/') and '..' not in relative.split('/'), f'bad card path {relative}')
        return self.root / relative

    def listing(self, relative):
        entry = self.op('list', relative)
        result = {}
        for item in sorted(self.path(relative).iterdir()):
            kind = REGULAR if item.is_file() and not item.is_symlink() else '(other)'
            result[item.name] = {'size': item.stat().st_size if kind == REGULAR else -1, 'kind': kind}
        self.done(entry, names=sorted(result))
        return result

    def exists(self, relative):
        entry = self.op('exists', relative)
        result = self.path(relative).exists()
        self.done(entry, exists=result)
        return result

    def mkdir_p(self, relative):
        entry = self.op('mkdir', relative)
        self.path(relative).mkdir(parents=True, exist_ok=True)
        self.done(entry)

    def read_from_card(self, relative):
        """`(bytes, method)` of a card file as the card holds it. O_DIRECT reads into a
        page-aligned buffer, skipping the page cache; if the open refuses O_DIRECT, the
        file's clean cached pages are dropped (every write here is fsynced) and it is read
        normally."""
        path = self.path(relative)
        size = path.stat().st_size
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
        except OSError as error:
            if error.errno != errno.EINVAL:
                raise
            with open(path, 'rb') as handle:
                os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                return handle.read(), 'fadvise-dontneed'
        try:
            buffer = mmap.mmap(-1, max(ALIGN, -(-size // ALIGN) * ALIGN))
            view = memoryview(buffer)
            got = 0
            while got < size:
                require(got % ALIGN == 0, f'{relative}: short O_DIRECT read at {got}')
                count = os.preadv(fd, [view[got:]], got)
                if count == 0:
                    break
                got += count
            data = bytes(view[:got])
            view.release()
            buffer.close()
        finally:
            os.close(fd)
        require(len(data) == size, f'{relative}: read {len(data)} of {size} bytes')
        return data, 'o_direct'

    def fetch(self, relative, destination):
        entry = self.op('copy-from-card', relative, destination)
        data, method = self.read_from_card(relative)
        Path(destination).write_bytes(data)
        self.done(entry, method=method, sha256=sha(data), bytes=len(data))

    def store(self, local, relative):
        """Write through a same-directory temporary and an atomic rename, then flush."""
        target = self.path(relative)
        temporary = target.with_name('.' + target.name + '.part')
        entry = self.op('copy-to-card', local, relative)
        require(not temporary.exists(), f'{temporary} exists')
        with open(local, 'rb') as source_handle, open(temporary, 'wb') as handle:
            shutil.copyfileobj(source_handle, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.sync()
        self.done(entry)

    def replace(self, staged_local, stage_relative, target_relative):
        # A rename on one filesystem is atomic: the loader sees the old or the new file, never a partial one.
        entry = self.op('rename-on-card', stage_relative, target_relative)
        os.replace(self.path(stage_relative), self.path(target_relative))
        os.sync()
        self.done(entry)

    def finish(self, receipt):
        receipt['all_operations_closed'] = all('finished' in entry for entry in self.commands)


class FtpCard(Card):
    """DBI FTP with fresh anonymous sessions and verified binary readbacks.

    The operation pattern follows ssbu-switch-remote/tools/card/own_install.py
    (MIT). The shared transaction owns all authorization and rollback ordering.
    """
    description = 'DBI FTP'
    closing = 'Done. The FTP sessions are closed. Close DBI when you are ready.'

    @staticmethod
    def endpoint(root):
        require(isinstance(root, str) and root.startswith('ftp://')
                and not any(c.isspace() or c in '?#' or ord(c) < 32 or ord(c) == 127 for c in root),
                'FTP root must be ftp://<host>:<port>')
        try:
            url = urlsplit(root)
            host, port = url.hostname, url.port
        except ValueError as error:
            raise Refused('FTP root must be ftp://<host>:<port>') from error
        require(host and port and url.username is None and url.password is None and url.path in ('', '/'),
                'FTP root must be ftp://<host>:<port>')
        return host, port

    def __init__(self, journal, commands, host, port):
        super().__init__(journal, commands)
        self.host, self.port = host, port

    @contextmanager
    def operation(self, name, *args):
        entry = {'op': name, 'args': list(args), 'started': now()}
        self.commands.append(entry)
        ftp = ftplib.FTP()
        try:
            ftp.connect(self.host, self.port, timeout=min(30, self.left()))
            ftp.login()
            ftp.voidcmd('TYPE I')
            yield ftp
            entry['ok'] = True
            try:
                ftp.quit()
            except ftplib.all_errors as error:
                entry['quit_error'] = repr(error)
        except BaseException as error:
            entry.update(ok=False, error=repr(error))
            raise
        finally:
            ftp.close()
            entry['finished'] = now()
            self.journal.save()

    def names(self, ftp, folder, missing_ok=False):
        try:
            ftp.cwd('/' + folder)
        except ftplib.error_perm as error:
            if not missing_ok or not folder or not str(error).startswith('550 '):
                raise
            parent, _, name = folder.rpartition('/')
            names = self.names(ftp, parent, missing_ok=True)
            if names is None or name not in names:
                return None
            raise
        # DBI ignores an NLST argument and returns absolute paths.
        prefix = '/' + (folder + '/' if folder else '')
        result = set()
        for line in ftp.nlst():
            line = line.rstrip('/')
            name = line.removeprefix(prefix)
            require(name not in ('', '.', '..') and '/' not in name and name not in result,
                    f'odd or duplicate FTP name in {folder}: {line!r}')
            result.add(name)
        return result

    def exists(self, relative):
        parent, _, name = relative.rpartition('/')
        with self.operation('exists', relative) as ftp:
            names = self.names(ftp, parent, missing_ok=True)
            return names is not None and name in names

    def mkdir_p(self, relative):
        with self.operation('mkdir', relative) as ftp:
            parent = ''
            for name in relative.split('/'):
                names = self.names(ftp, parent)
                path = parent + '/' + name if parent else name
                if name not in names:
                    ftp.mkd('/' + path)
                parent = path

    def metadata(self, relative):
        with self.operation('stat', relative) as ftp:
            path = '/' + relative
            try:
                ftp.cwd(path)
            except ftplib.error_perm as error:
                # DBI 912 rejects CWD on a file with 553. Confirm it through SIZE or readback.
                if not str(error).startswith(('550 ', '553 ')):
                    raise
            else:
                return {'size': -1, 'kind': '(directory)'}
            try:
                size = ftp.size(path)
            except ftplib.error_perm as error:
                if str(error)[:3] not in ('500', '501', '502', '504', '550'):
                    raise
                size = None
        # On DBI variants without SIZE, verify the file and measure its length
        # using the same binary reads used for the copies.
        if size is None:
            size = len(self.checked_read(relative))
        require(0 <= size <= MAX_BYTES, f'{relative}: invalid FTP file size {size}')
        return {'size': size, 'kind': REGULAR}

    def listing(self, relative):
        with self.operation('list', relative) as ftp:
            names = self.names(ftp, relative)
        return {name: self.metadata(relative + '/' + name) for name in sorted(names)}

    def read(self, relative):
        data = bytearray()

        def append(block):
            self.left()
            require(len(data) + len(block) <= MAX_BYTES, f'{relative}: FTP file exceeds {MAX_BYTES} bytes')
            data.extend(block)

        with self.operation('copy-from-card', relative) as ftp:
            ftp.retrbinary('RETR /' + relative, append, blocksize=65536)
            self.commands[-1].update(sha256=sha(data), bytes=len(data))
        return data

    def checked_read(self, relative):
        data = self.read(relative)
        require(sha(data) == sha(self.read(relative)), f'{relative}: FTP readback mismatch between connections')
        return data

    def fetch(self, relative, destination):
        Path(destination).write_bytes(self.checked_read(relative))

    def store(self, local, relative):
        with self.operation('copy-to-card', relative) as ftp, open(local, 'rb') as source:
            ftp.storbinary('STOR /' + relative, source, blocksize=65536, callback=lambda _: self.left())

    def rename(self, stage, target, allow_exists=False):
        with self.operation('rename-on-card', stage, target) as ftp:
            reply = ftp.sendcmd('RNFR /' + stage)
            require(reply.startswith('350 '), f'FTP did not accept rename source: {reply}')
            try:
                ftp.voidcmd('RNTO /' + target)
            except ftplib.error_perm as error:
                parent, _, name = target.rpartition('/')
                if not allow_exists or name not in self.names(ftp, parent):
                    raise
                self.commands[-1]['overwrite_refused'] = str(error)
                return False
        return True

    def replace(self, staged_local, stage_relative, target_relative):
        if self.rename(stage_relative, target_relative, allow_exists=True):
            return
        # Only the verified install reaches here, after backup and stage
        # readback, with shared rollback armed even if the next rename fails.
        with self.operation('delete-for-rename', target_relative) as ftp:
            ftp.delete('/' + target_relative)
        self.rename(stage_relative, target_relative)

    def finish(self, receipt):
        receipt['all_operations_closed'] = all('finished' in entry for entry in self.commands)


class Transaction:
    def __init__(self, mode, out, root=None):
        self.mode, self.out = mode, out
        self.root = root
        self.receipt_path = out / RECEIPTS[mode]
        require(not self.receipt_path.exists(), 'Preserve completed/failed evidence; inspect before a new operation')
        self.lock = (out / '.transaction.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.configure()
        except BaseException:
            self.lock.close()
            raise

    def configure(self):
        """Everything checked before the card is touched."""
        approval = json.loads((self.out / 'operator-approval.json').read_text())
        order = approval.get('operator_order')
        require(isinstance(order, str) and order.strip(), 'operator_order is missing')
        if self.mode == 'install':
            require(approval.get('operator_install_authorized') is True, 'operator_install_authorized is not true')
        self.target = approval['target']
        require(self.target in LOADER_MAGIC, f'target {self.target!r} is not one of {sorted(LOADER_MAGIC)}')
        self.name = self.target.rsplit('/', 1)[1]
        self.data = approval['data']
        require(DATA_FOLDER.fullmatch(self.data), f'data {self.data!r} is not ultimate/<name>')
        self.old, self.new = approval['old'], approval['new']
        require(all(SHA256.fullmatch(h) for h in (self.old, self.new)) and self.old != self.new,
                'old and new must be two different lower-case sha256 values')
        self.transport = approval.get('transport', 'mtp')
        require(self.transport in ('mtp', 'ums', 'ftp'), f'transport {self.transport!r} is not mtp, ums or ftp')
        require(self.root is None or self.transport == 'ftp', '--root is only for FTP')
        self.extra = approval.get('extra_protected', [])
        require(isinstance(self.extra, list) and all(isinstance(p, str) and EXTRA.fullmatch(p) and p not in PROTECTED
                                                      and p.rsplit('/', 1)[1] not in ('.', '..') for p in self.extra)
                and len(set(self.extra)) == len(self.extra),
                'extra_protected must list distinct ultimate/<folder>/<file> paths')
        self.summary = {'target': self.target, 'data': self.data, 'old': self.old, 'new': self.new,
                        'transport': self.transport, 'extra_protected': self.extra}
        if self.transport == 'ums':
            require(isinstance(approval.get('mount'), str) and approval['mount'].startswith('/'),
                    'transport ums needs the card mount point in "mount"')
            self.summary['mount'] = approval['mount']
        if self.transport == 'ftp':
            host, port = FtpCard.endpoint(self.root)
            authority = f'[{host}]' if ':' in host else host
            self.summary['root'] = f'ftp://{authority}:{port}'
        self.backup = self.data + f'/backups/pre-{self.new[:16]}.{self.name}'
        self.stage = self.data + f'/install/{self.name}-{self.new[:16]}.stage'
        source_path = self.out / self.name
        require(source_path.is_file(), f'{source_path} is missing')
        data = source_path.read_bytes()
        self.source = info(source_path)
        require(self.source['sha256'] == self.new, f'{self.name} is {self.source["sha256"]}, not the approved new build')
        require(data[:4] == LOADER_MAGIC[self.target] and 0 < len(data) <= MAX_BYTES,
                f'{self.name} does not start with {LOADER_MAGIC[self.target]!r} or has a bad size')
        self.receipt = {'state': 'started', 'started': now(), 'mode': self.mode,
                        'operator_order': order, 'approval': self.summary, 'target': self.target,
                        'old': self.old, 'new': self.new, 'backup': self.backup, 'stage': self.stage,
                        'source': self.source, 'process_id': os.getpid(),
                        'tool': {'path': str(Path(__file__).resolve()),
                                 'sha256': sha(Path(__file__).read_bytes())}}
        self.commands = []
        self.journal = Journal(self.receipt_path, self.receipt)
        if self.transport == 'mtp':
            self.card = MtpCard(self.journal, self.commands)
        elif self.transport == 'ums':
            self.card = UmsCard(self.journal, self.commands, self.summary['mount'])
        else:
            self.card = FtpCard(self.journal, self.commands, host, port)
        self.receipt.update(transport=self.card.description, **self.card.identity())
        self.receipt['commands'] = self.commands
        self.replaced = False
        self.before = {}

    def state(self, value):
        self.journal.state(value)

    def download(self, relative, group, expected_size=None):
        destination = self.out / group / relative
        require(not destination.exists(), f'Preserve local evidence: {destination}')
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.card.fetch(relative, destination)
        result = info(destination)
        require(result['bytes'] <= MAX_BYTES, f'{relative} is {result["bytes"]} bytes')
        if expected_size is not None:
            require(result['bytes'] == expected_size, f'{relative}: {result["bytes"]} bytes, expected {expected_size}')
        return result

    def inventory(self):
        """The two listings the transaction keeps unchanged: exefs/ and the plugin folder."""
        exefs, plugins = self.card.listing(EXEFS), self.card.listing(PLUGINS)
        require(all(v['kind'] == REGULAR for v in exefs.values()), f'exefs/ holds more than files: {exefs}')
        require(all(n.endswith('.nro') and v['kind'] == REGULAR for n, v in plugins.items()),
                f'the plugin folder holds more than NROs: {plugins}')
        return exefs, plugins

    def preflight(self):
        card, receipt = self.card, self.receipt
        exefs, plugins = self.inventory()
        require(self.name in exefs, f'{self.target} is not on the card; this tool replaces, it does not add')
        require(not card.exists(self.backup) and not card.exists(self.stage), 'archive or stage exists; inspect first')
        protected = [p for p in PROTECTED + self.extra if card.exists(p)]
        receipt.update(exefs_names_before=sorted(exefs), plugin_names_before=sorted(plugins), before={},
                       protected_present=protected)
        self.state('backing_up_locally')
        for folder, listed in ((EXEFS, exefs), (PLUGINS, plugins)):
            for name in sorted(listed):
                relative = folder + '/' + name
                receipt['before'][relative] = self.download(relative, 'before', listed[name]['size'])
                self.journal.save()
        for relative in protected:
            receipt['before'][relative] = self.download(relative, 'before')
            self.journal.save()
        require(receipt['before'][self.target]['sha256'] == self.old,
                'Unexpected installed build; inspect before replacement')
        receipt['finished'] = now()
        self.state('preflight_verified')

    def install(self):
        card, receipt = self.card, self.receipt
        preflight = json.loads((self.out / 'preflight.json').read_text())
        require(preflight['state'] == 'preflight_verified', 'preflight is not verified')
        require(preflight['approval'] == self.summary, 'the approval changed since preflight')
        before = self.before = preflight['before']
        require(info(before[self.target]['local'])['sha256'] == self.old, 'the local copy of the old file changed')
        exefs, plugins = self.inventory()
        require(sorted(exefs) == preflight['exefs_names_before'] and sorted(plugins) == preflight['plugin_names_before'],
                'a card listing changed since preflight')
        sizes = {EXEFS + '/' + n: v['size'] for n, v in exefs.items()}
        sizes.update({PLUGINS + '/' + n: v['size'] for n, v in plugins.items()})
        for relative, size in sizes.items():
            require(before[relative]['bytes'] == size, f'Changed since preflight: {relative}')
        receipt.update(before=before, exefs_names_before=preflight['exefs_names_before'],
                       plugin_names_before=preflight['plugin_names_before'])
        fresh = self.download(self.target, 'just-before', before[self.target]['bytes'])
        require(fresh['sha256'] == self.old, 'Changed since preflight')
        require(not card.exists(self.backup) and not card.exists(self.stage), 'archive or stage exists; inspect first')
        self.state('writing_rollback_archive')
        for folder in (self.data + '/backups', self.data + '/install'):
            if not card.exists(folder):
                card.mkdir_p(folder)
        card.store(before[self.target]['local'], self.backup)
        rollback = self.download(self.backup, 'backup-readback', before[self.target]['bytes'])
        require(rollback['sha256'] == self.old, 'Rollback archive readback mismatch')
        receipt['rollback_readback'] = rollback
        self.state('rollback_verified')
        card.store(self.source['local'], self.stage)
        staged = self.download(self.stage, 'stage-readback', self.source['bytes'])
        require(staged['sha256'] == self.new, 'Staging readback mismatch')
        receipt['stage_readback'] = staged
        self.state('staged_verified')
        self.replaced = True
        self.state('replacing_target')
        card.replace(staged['local'], self.stage, self.target)
        self.state('installed_pending_readback')
        receipt['after'] = {}
        for relative, previous in before.items():
            expected = self.source if relative == self.target else previous
            result = self.download(relative, 'after', expected['bytes'])
            receipt['after'][relative] = result
            self.journal.save()
            require(result['sha256'] == expected['sha256'], f'{relative}: readback does not match')
        exefs, plugins = self.inventory()
        receipt['exefs_names_after'], receipt['plugin_names_after'] = sorted(exefs), sorted(plugins)
        require(receipt['exefs_names_after'] == preflight['exefs_names_before'], 'exefs/ lists other names after the install')
        require(receipt['plugin_names_after'] == preflight['plugin_names_before'], 'the plugin folder lists other names after the install')
        receipt.update(other_files_unchanged=True, finished=now(), stage_moved_into_place=self.transport in ('ums', 'ftp'),
                       stage_retained_outside_loader=self.transport == 'mtp')
        self.state('installed_verified')

    def roll_back(self):
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                signal.signal(number, signal.SIG_IGN)  # the rollback runs to its end
            except ValueError:
                pass
        receipt, before = self.receipt, self.before
        try:
            self.card.allow_rollback_time()
            self.state('rolling_back')
            self.card.store(before[self.target]['local'], self.target)
            restored = self.download(self.target, 'rollback-restored', before[self.target]['bytes'])
            require(restored['sha256'] == self.old, 'the restored file does not match the old build')
            receipt.update(rollback_readback_after_failure=restored, finished=now())
            self.state('rolled_back_verified')
        except BaseException as error:
            receipt.update(rollback_error=repr(error), finished=now())
            self.state('recovery_required')

    def verify(self):
        receipt = self.receipt
        deployment = json.loads((self.out / 'deployment.json').read_text())
        require(deployment['state'] == 'installed_verified', f'deployment is {deployment["state"]}')
        require(deployment['approval'] == self.summary, 'the approval changed since the install')
        if self.transport == 'ums':
            receipt['mount_id_before'] = self.card.mount_id()
        self.state('reading_back')
        receipt['verify'] = {}
        for relative, expected in deployment['after'].items():
            result = self.download(relative, 'verify', expected['bytes'])
            receipt['verify'][relative] = result
            self.journal.save()
            require(result['sha256'] == expected['sha256'], f'{relative}: readback does not match the install')
        exefs, plugins = self.inventory()
        require(sorted(exefs) == deployment['exefs_names_after'] and sorted(plugins) == deployment['plugin_names_after'],
                'a card listing changed since the install')
        if self.transport == 'ums':
            receipt['mount_id_after'] = self.card.mount_id()
            require(receipt['mount_id_after'] == receipt['mount_id_before'], 'the card was remounted during verify')
        receipt['finished'] = now()
        self.state('verified')

    def run(self):
        ok = False
        handlers = {}

        def interrupted(number, frame):
            raise KeyboardInterrupt(f'signal {number}')

        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                handlers[number] = signal.signal(number, interrupted)
            except ValueError:  # not the main thread
                pass
        try:
            self.journal.save()
            self.card.start()
            getattr(self, self.mode)()
            ok = True
        except BaseException as error:
            self.receipt.update(error=repr(error), finished=now())
            self.state('failed')
            traceback.print_exc()
            if self.replaced:
                self.roll_back()
        finally:
            self.card.finish(self.receipt)
            self.receipt['device_operations_closed'] = now()
            self.journal.save()
            self.lock.close()
            for number, handler in handlers.items():
                signal.signal(number, handler)
        print(json.dumps({k: self.receipt.get(k) for k in ('state', 'target', 'device_operations_closed')}, indent=2))
        if ok and self.mode in ('install', 'verify'):
            # The user connects and disconnects the Switch (playbook WORKFLOW, Switch); this tool never does.
            print(self.card.closing)
        return 0 if ok else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if (len(argv) not in (2, 4) or argv[0] not in RECEIPTS
            or (len(argv) == 4 and argv[2] != '--root')):
        print('usage: replace-exefs.py preflight|install|verify <dir> [--root ftp://<host>:<port>]', file=sys.stderr)
        return 2
    root = argv[3] if len(argv) == 4 else None
    out = Path(argv[1]).resolve()
    if not out.is_dir():
        print(f'{out} is not a directory', file=sys.stderr)
        return 2
    try:
        transaction = Transaction(argv[0], out, root)
    except BlockingIOError:
        print(f'refused: another transaction holds the lock in {out}', file=sys.stderr)
        return 1
    except (Refused, KeyError, ValueError, OSError) as error:
        print(f'refused before any card access: {error!r}', file=sys.stderr)
        return 1
    return transaction.run()


if __name__ == '__main__':
    sys.exit(main())
