"""Single-writer, finite transaction on a card mounted as a USB disk (hekate Nyx UMS, a
card reader): swap one installed plugin NRO for another, as ONE transaction.

    remove  <old>.nro   archive it on the card first, then take it out of the plugin folder
    add     <new>.nro   stage it, rename it into the plugin folder, add its data-folder files

Why one transaction and not remove + deploy: the state to avoid is "both plugins installed"
(plugins that hook the same sites), and the safe state in between is "neither". The swap
orders archive -> stage -> remove -> rename in -> data files and, on any readback mismatch or
interrupt, undoes in reverse (delete the data files it made, delete the new NRO, put the old NRO
back from the local copy), so the card ends fully swapped or as it was. The same tool with
`remove` and `add` exchanged undoes a completed swap (the old NRO's local copy is in the first
run's before/ folder). It never mounts, unmounts or ejects anything and never starts or stops
hekate UMS: the user connects and disconnects the Switch (playbook WORKFLOW, Switch) and
exposes the card. Every read of the card skips the page cache (O_DIRECT; where a filesystem
refuses it, the file's clean cached pages are dropped first), so a readback shows what the card
holds. The card is read in place and never remounted.

  python3 -I swap-ums.py preflight <dir>   # read-only: listings, local copies, free space, `old` hash
  python3 -I swap-ums.py install   <dir>   # archive, stage, remove, rename in, data files, read everything back
  python3 -I swap-ums.py verify    <dir>   # read every file the install touched once more, in place

<dir> holds the new NRO under its own name, each added data file under data-files/<card path>,
and operator-approval.json:

  operator_order, operator_swap_authorized (install only), mount (the card's mount point),
  remove {name, sha256}     the installed plugin to archive and remove
  add    {name, sha256, files: [{path, sha256}, ...]}
                            the plugin to add; `files` are `ultimate/<folder>/<file>` paths that
                            must NOT exist on the card (never overwritten) and may not be the
                            import marker `migration.json`
  data                      `ultimate/<folder>` that holds the archive `backups/removed-<old16>.nro`
                            and the stage `install/<new stem>-<new16>.swap-stage`
  extra_protected           optional `ultimate/<folder>/<file>` card files read before and after
  watch                     optional `ultimate/<folder>` trees: every file up to 30 MB is hashed
                            and every other entry listed before and after, so "the tree is
                            unchanged" is read back; `<data>/backups` and `<data>/install` are
                            checked by name instead (they gain the archive and lose the stage)
  min_free_bytes            optional, default 64 MiB: free space left after the writes

Preflight lists the plugin folder and exefs/, keeps a local copy of every file in both, of the
protected configs (hekate_ipl.ini, the Deluxe and fx-control config.ini), of the extras and of the
watched trees, checks that `remove` is installed with the approved sha256, that `add` and every
added file are absent, that the archive and the stage do not exist and that the card has room.
Install re-checks all of it, writes the archive (read back), the stage (read back), removes the
old NRO, renames the stage into the plugin folder, places the data files, then reads back the new
NRO, every added file, the archive and every file preflight copied, and checks that the folders
list the names they should. A mismatch or an interrupt after the first removal undoes the swap and
reads the old NRO back (`rolled_back_verified`, or `recovery_required` when even that fails; a
foreign file at the old path is never overwritten). A phase whose receipt exists is not run again:
inspect it first. A failed install leaves its archive and stage on the card, so a retry needs a
new `data` folder (or the user removes them).

Tests drive main() on a plain directory standing in for the mount (tools/switch/tests/)."""
from datetime import datetime, timezone
import errno
import fcntl
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

TITLE = 'atmosphere/contents/01006A800016E000'
EXEFS = TITLE + '/exefs'
PLUGINS = TITLE + '/romfs/skyline/plugins'
PROTECTED = ['bootloader/hekate_ipl.ini', 'ultimate/ssbu_online_deluxe_rewrite/config.ini',
             'ultimate/ssbu_fx_control/config.ini']
NRO_NAME = re.compile(r'lib[a-z0-9_]+\.nro')
DATA_FOLDER = re.compile(r'ultimate/[A-Za-z0-9_-]+')
CARD_FILE = re.compile(r'ultimate/[A-Za-z0-9_-]+/[A-Za-z0-9_-][A-Za-z0-9_.-]*')
SHA256 = re.compile(r'[0-9a-f]{64}')
RECEIPTS = {'preflight': 'preflight.json', 'install': 'deployment.json', 'verify': 'verify.json'}
NEVER_PLACED = {'migration.json'}  # the clean room's import marker: a pre-placed one would suppress the import
MAX_BYTES = 30_000_000
MAX_DATA_FILE = 8 * 1024 * 1024
DEFAULT_MIN_FREE = 64 * 1024 * 1024
ALIGN = 4096  # O_DIRECT buffer, offset and length alignment (covers 512 B and 4 KiB sectors)
REGULAR, DIRECTORY, OTHER = '(regular)', '(directory)', '(other)'
CLOSING = 'Done. The card is still mounted: unmount or eject it yourself when you are ready.'

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
    """The card mounted as a USB disk. Every operation is logged in the receipt's `commands`."""

    def __init__(self, journal, commands, root):
        self.journal, self.commands, self.root = journal, commands, Path(root)
        self.deadline = time.monotonic() + 600

    def left(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('card phase total deadline reached')
        return left

    def allow_rollback_time(self):
        self.deadline = time.monotonic() + 240

    def start(self):
        self.check_mount()
        self.journal.receipt['mount_source'] = self.mount_source()

    def check_mount(self):
        require(self.root.is_absolute() and self.root.is_mount(), f'{self.root} is not a mount point')

    def mount_source(self):
        argv = ['/usr/bin/findmnt', '-n', '-o', 'SOURCE', '--target', str(self.root)]
        entry = self.op('exec', *argv)
        process = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
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
            if item.is_symlink():
                kind = OTHER
            elif item.is_file():
                kind = REGULAR
            elif item.is_dir():
                kind = DIRECTORY
            else:
                kind = OTHER
            result[item.name] = {'size': item.stat().st_size if kind == REGULAR else -1, 'kind': kind}
        self.done(entry, names=sorted(result))
        return result

    def exists(self, relative):
        entry = self.op('exists', relative)
        result = os.path.lexists(self.path(relative))
        self.done(entry, exists=result)
        return result

    def free_bytes(self):
        entry = self.op('statvfs', '/')
        stat = os.statvfs(self.root)
        free = stat.f_bavail * stat.f_frsize
        self.done(entry, free_bytes=free, read_only=bool(stat.f_flag & os.ST_RDONLY))
        return free, bool(stat.f_flag & os.ST_RDONLY)

    def mkdir_p(self, relative):
        """Make the folder and its missing parents; return the ones this call made, outermost first."""
        entry = self.op('mkdir', relative)
        made, current = [], ''
        for part in relative.split('/'):
            current = f'{current}/{part}' if current else part
            if not self.path(current).exists():
                self.path(current).mkdir()
                made.append(current)
        self.done(entry, made=made)
        return made

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

    def store(self, local, relative, overwrite=False):
        """Write through a same-directory temporary and an atomic rename, then flush."""
        target = self.path(relative)
        temporary = target.with_name('.' + target.name + '.part')
        entry = self.op('copy-to-card', local, relative)
        require(not temporary.exists(), f'{temporary} exists')
        with open(local, 'rb') as source_handle, open(temporary, 'wb') as handle:
            shutil.copyfileobj(source_handle, handle)
            handle.flush()
            os.fsync(handle.fileno())
        require(overwrite or not os.path.lexists(target), f'{relative} exists; this tool never overwrites')
        os.replace(temporary, target)
        os.sync()
        self.done(entry)

    def rename(self, source, destination):
        """A rename on one filesystem is atomic: the loader sees the old state or the new one."""
        entry = self.op('rename-on-card', source, destination)
        require(not os.path.lexists(self.path(destination)), f'{destination} exists')
        os.replace(self.path(source), self.path(destination))
        os.sync()
        self.done(entry)

    def unlink(self, relative):
        entry = self.op('remove-on-card', relative)
        self.path(relative).unlink()
        os.sync()
        self.done(entry)

    def rmdir(self, relative):
        entry = self.op('rmdir-on-card', relative)
        self.path(relative).rmdir()
        os.sync()
        self.done(entry)

    def finish(self, receipt):
        receipt['all_operations_closed'] = all('finished' in entry for entry in self.commands)


def inside(path, folder):
    return path == folder or path.startswith(folder + '/')


class Transaction:
    def __init__(self, mode, out):
        self.mode, self.out = mode, out
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
            require(approval.get('operator_swap_authorized') is True, 'operator_swap_authorized is not true')
        mount = approval.get('mount')
        require(isinstance(mount, str) and mount.startswith('/'), 'mount must be the card mount point (absolute path)')
        remove, add = approval['remove'], approval['add']
        self.old_name, self.old = remove['name'], remove['sha256']
        self.new_name, self.new = add['name'], add['sha256']
        require(all(isinstance(n, str) and NRO_NAME.fullmatch(n) for n in (self.old_name, self.new_name)),
                'remove.name and add.name must be lib<name>.nro')
        require(self.old_name != self.new_name, 'remove and add have the same name: this tool swaps two plugins, use upgrade2-ums.py')
        require(all(isinstance(h, str) and SHA256.fullmatch(h) for h in (self.old, self.new)) and self.old != self.new,
                'remove.sha256 and add.sha256 must be two different lower-case sha256 values')
        self.data = approval['data']
        require(isinstance(self.data, str) and DATA_FOLDER.fullmatch(self.data), f'data {self.data!r} is not ultimate/<name>')
        self.watch = approval.get('watch', [])
        require(isinstance(self.watch, list) and all(isinstance(w, str) and DATA_FOLDER.fullmatch(w) for w in self.watch)
                and len(set(self.watch)) == len(self.watch), 'watch must list distinct ultimate/<folder> paths')
        self.extra = approval.get('extra_protected', [])
        require(isinstance(self.extra, list) and all(isinstance(p, str) and CARD_FILE.fullmatch(p) and p not in PROTECTED
                                                      for p in self.extra) and len(set(self.extra)) == len(self.extra),
                'extra_protected must list distinct ultimate/<folder>/<file> paths')
        files = add.get('files', [])
        require(isinstance(files, list) and all(isinstance(f, dict) for f in files), 'add.files must be a list of {path, sha256}')
        self.files = []
        for entry in files:
            path, digest = entry.get('path'), entry.get('sha256')  # a dict, checked above
            require(isinstance(path, str) and CARD_FILE.fullmatch(path), f'added file {path!r} is not ultimate/<folder>/<file>')
            require(path.rsplit('/', 1)[1].lower() not in NEVER_PLACED, f'{path} must never be placed: the plugin writes it itself')
            require(isinstance(digest, str) and SHA256.fullmatch(digest), f'{path}: sha256 is not lower-case sha256')
            self.files.append({'path': path, 'sha256': digest})
        paths = [f['path'] for f in self.files]
        require(len(set(paths)) == len(paths), 'add.files lists a path twice')
        self.min_free = approval.get('min_free_bytes', DEFAULT_MIN_FREE)
        require(isinstance(self.min_free, int) and not isinstance(self.min_free, bool) and self.min_free >= 0,
                'min_free_bytes must be a non-negative integer')
        # Folder rules: the archive/stage folder is not an added-file or watched location; nothing is covered twice.
        self.ours = [self.data + '/backups', self.data + '/install']
        for path in paths:
            require(not inside(path, self.data), f'{path} is inside the archive folder {self.data}')
            require(not any(inside(path, tree) for tree in self.watch), f'{path} is inside a watched tree')
        for path in self.extra + PROTECTED:
            require(not any(inside(path, tree) for tree in self.watch), f'{path} is inside a watched tree (it is already read back)')
        self.summary = {'mount': mount, 'remove': {'name': self.old_name, 'sha256': self.old},
                        'add': {'name': self.new_name, 'sha256': self.new, 'files': self.files},
                        'data': self.data, 'extra_protected': self.extra, 'watch': self.watch, 'min_free_bytes': self.min_free}
        self.target_old, self.target_new = f'{PLUGINS}/{self.old_name}', f'{PLUGINS}/{self.new_name}'
        self.archive = f'{self.data}/backups/removed-{self.old[:16]}.nro'
        self.stage = f'{self.data}/install/{self.new_name[:-4]}-{self.new[:16]}.swap-stage'
        # The new NRO and the added files, from the folder.
        source_path = self.out / self.new_name
        require(source_path.is_file(), f'{source_path} is missing')
        binary = source_path.read_bytes()
        self.source = info(source_path)
        require(self.source['sha256'] == self.new, f'{self.new_name} is {self.source["sha256"]}, not the approved new build')
        require(binary[0x10:0x14] == b'NRO0' and 0 < len(binary) <= MAX_BYTES, f'{self.new_name} is not an NRO or has a bad size')
        self.local_files = {}
        for entry in self.files:
            local = self.out / 'data-files' / entry['path']
            require(local.is_file(), f'{local} is missing')
            result = info(local)
            require(result['sha256'] == entry['sha256'], f'{entry["path"]} is {result["sha256"]}, not the approved file')
            require(result['bytes'] <= MAX_DATA_FILE, f'{entry["path"]} is larger than {MAX_DATA_FILE} bytes')
            self.local_files[entry['path']] = result
        self.receipt = {'state': 'started', 'started': now(), 'mode': self.mode, 'transport': 'mounted card (USB disk)',
                        'mount': mount, 'operator_order': order, 'approval': self.summary,
                        'target_old': self.target_old, 'target_new': self.target_new, 'old': self.old, 'new': self.new,
                        'archive': self.archive, 'stage': self.stage, 'source': self.source,
                        'data_files': self.local_files, 'process_id': os.getpid(),
                        'tool': {'path': str(Path(__file__).resolve()), 'sha256': sha(Path(__file__).read_bytes())}}
        self.commands = []
        self.journal = Journal(self.receipt_path, self.receipt)
        self.card = Card(self.journal, self.commands, mount)
        self.receipt['commands'] = self.commands
        self.removed = self.swapped_in = False
        self.created_files, self.stored_files, self.created_dirs = [], set(), []  # attempted, finished, folders made
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
        """The two listings the transaction keeps in step: exefs/ and the plugin folder."""
        exefs, plugins = self.card.listing(EXEFS), self.card.listing(PLUGINS)
        require(all(v['kind'] == REGULAR for v in exefs.values()), f'exefs/ holds more than files: {exefs}')
        require(all(n.endswith('.nro') and v['kind'] == REGULAR for n, v in plugins.items()),
                f'the plugin folder holds more than NROs: {plugins}')
        return exefs, plugins

    def walk(self, tree, group):
        """Every entry under a watched tree: kind and size, and (group given) the sha256 of every
        regular file up to MAX_BYTES, read back with a local copy. Our own archive and stage folders
        are recorded by name only."""
        found = {}

        def visit(relative):
            for name, item in sorted(self.card.listing(relative).items()):
                child = f'{relative}/{name}'
                entry = {'kind': item['kind'], 'size': item['size']}
                if child in self.ours:
                    entry['skipped'] = 'archive/stage folder: checked by name'
                elif item['kind'] == DIRECTORY:
                    found[child] = entry
                    visit(child)
                    continue
                elif item['kind'] == REGULAR and group and item['size'] <= MAX_BYTES:
                    entry['sha256'] = self.download(child, group, item['size'])['sha256']
                elif item['kind'] == REGULAR and group:
                    entry['sha256'] = None
                    entry['note'] = 'larger than the read-back cap: size only'
                found[child] = entry

        require(self.card.exists(tree), f'watched tree {tree} is not on the card')
        visit(tree)
        return found

    @staticmethod
    def differences(before, after, with_hash=True):
        keys = ('kind', 'size', 'sha256') if with_hash else ('kind', 'size')
        found = [f'only before: {k}' for k in sorted(set(before) - set(after))]
        found += [f'only after: {k}' for k in sorted(set(after) - set(before))]
        found += [f'changed: {k}' for k in sorted(set(before) & set(after))
                  if any(before[k].get(x) != after[k].get(x) for x in keys)]
        return found

    def folder_names(self, relative):
        return sorted(self.card.listing(relative)) if self.card.exists(relative) else []

    def check_room(self, needed):
        free, read_only = self.card.free_bytes()
        require(free >= needed + self.min_free, f'{free} bytes free, {needed} needed plus {self.min_free} kept free')
        return free, read_only

    def needed_bytes(self):
        return self.source['bytes'] * 2 + sum(f['bytes'] for f in self.local_files.values()) + 2 * 4096

    def preflight(self):
        card, receipt = self.card, self.receipt
        exefs, plugins = self.inventory()
        require(self.old_name in plugins, f'{self.target_old} is not on the card: nothing to remove')
        require(self.new_name not in plugins, f'{self.target_new} is already on the card; inspect first')
        require(not card.exists(self.archive) and not card.exists(self.stage), 'archive or stage exists; inspect first')
        for entry in self.files:
            require(not card.exists(entry['path']), f'{entry["path"]} exists on the card; this tool never overwrites')
        free, read_only = self.check_room(self.needed_bytes())
        protected = [p for p in PROTECTED + self.extra if card.exists(p)]
        receipt.update(exefs_names_before=sorted(exefs), plugin_names_before=sorted(plugins), before={},
                       protected_present=protected, free_bytes=free, card_read_only=read_only,
                       backups_names_before=self.folder_names(self.data + '/backups'),
                       install_names_before=self.folder_names(self.data + '/install'), watch_before={})
        self.state('backing_up_locally')
        for folder, listed in ((EXEFS, exefs), (PLUGINS, plugins)):
            for name in sorted(listed):
                relative = f'{folder}/{name}'
                receipt['before'][relative] = self.download(relative, 'before', listed[name]['size'])
                self.journal.save()
        for relative in protected:
            receipt['before'][relative] = self.download(relative, 'before')
            self.journal.save()
        for tree in self.watch:
            receipt['watch_before'][tree] = self.walk(tree, 'before')
            self.journal.save()
        require(receipt['before'][self.target_old]['sha256'] == self.old, 'Unexpected installed build; inspect before the swap')
        receipt['finished'] = now()
        self.state('preflight_verified')

    def install(self):
        card, receipt = self.card, self.receipt
        preflight = json.loads((self.out / 'preflight.json').read_text())
        require(preflight['state'] == 'preflight_verified', 'preflight is not verified')
        require(preflight['approval'] == self.summary, 'the approval changed since preflight')
        before = self.before = preflight['before']
        require(info(before[self.target_old]['local'])['sha256'] == self.old, 'the local copy of the old NRO changed')
        free, read_only = self.check_room(self.needed_bytes())
        require(not read_only, 'the card is mounted read-only')
        exefs, plugins = self.inventory()
        require(sorted(exefs) == preflight['exefs_names_before'] and sorted(plugins) == preflight['plugin_names_before'],
                'a card listing changed since preflight')
        sizes = {f'{EXEFS}/{n}': v['size'] for n, v in exefs.items()}
        sizes.update({f'{PLUGINS}/{n}': v['size'] for n, v in plugins.items()})
        for relative, size in sizes.items():
            require(before[relative]['bytes'] == size, f'Changed since preflight: {relative}')
        for tree in self.watch:
            moved = self.differences(preflight['watch_before'][tree], self.walk(tree, None), with_hash=False)
            require(not moved, f'a watched tree changed since preflight: {moved[:5]}')
        receipt.update(before=before, exefs_names_before=preflight['exefs_names_before'],
                       plugin_names_before=preflight['plugin_names_before'], watch_before=preflight['watch_before'],
                       backups_names_before=preflight['backups_names_before'], install_names_before=preflight['install_names_before'])
        fresh = self.download(self.target_old, 'just-before', before[self.target_old]['bytes'])
        require(fresh['sha256'] == self.old, 'Changed since preflight')
        require(not card.exists(self.archive) and not card.exists(self.stage), 'archive or stage exists; inspect first')
        for entry in self.files:
            require(not card.exists(entry['path']), f'{entry["path"]} exists on the card; this tool never overwrites')
        self.state('writing_rollback_archive')
        for folder in self.ours:
            if not card.exists(folder):
                self.created_dirs += card.mkdir_p(folder)
        card.store(before[self.target_old]['local'], self.archive)
        archived = self.download(self.archive, 'archive-readback', before[self.target_old]['bytes'])
        require(archived['sha256'] == self.old, 'Rollback archive readback mismatch')
        receipt['archive_readback'] = archived
        self.state('archive_verified')
        card.store(self.source['local'], self.stage)
        staged = self.download(self.stage, 'stage-readback', self.source['bytes'])
        require(staged['sha256'] == self.new, 'Staging readback mismatch')
        receipt['stage_readback'] = staged
        self.state('staged_verified')
        # From here a failure undoes the swap. Flags are set before the operation they cover.
        self.state('removing_old')
        self.removed = True
        card.unlink(self.target_old)
        require(not card.exists(self.target_old), 'the old NRO is still in the plugin folder after the removal')
        self.state('swapping_in')
        self.swapped_in = True
        card.rename(self.stage, self.target_new)
        self.state('placing_data_files')
        for entry in self.files:
            path = entry['path']
            self.created_files.append(path)
            folder = path.rsplit('/', 1)[0]
            if not card.exists(folder):
                self.created_dirs += card.mkdir_p(folder)
            card.store(self.local_files[path]['local'], path)
            self.stored_files.add(path)
        self.state('installed_pending_readback')
        after = receipt['after'] = {}
        after[self.target_new] = self.download(self.target_new, 'after', self.source['bytes'])
        require(after[self.target_new]['sha256'] == self.new, f'{self.target_new}: readback does not match')
        self.journal.save()
        for entry in self.files:
            result = after[entry['path']] = self.download(entry['path'], 'after', self.local_files[entry['path']]['bytes'])
            require(result['sha256'] == entry['sha256'], f'{entry["path"]}: readback does not match')
            self.journal.save()
        after[self.archive] = self.download(self.archive, 'after', before[self.target_old]['bytes'])
        require(after[self.archive]['sha256'] == self.old, f'{self.archive}: readback does not match')
        self.journal.save()
        for relative, previous in before.items():
            if relative == self.target_old:
                continue
            result = after[relative] = self.download(relative, 'after', previous['bytes'])
            require(result['sha256'] == previous['sha256'], f'{relative}: readback does not match')
            self.journal.save()
        self.check_names(preflight, receipt)
        receipt['watch_after'] = {}
        for tree in self.watch:
            receipt['watch_after'][tree] = self.walk(tree, 'watch-after')
            changed = self.differences(preflight['watch_before'][tree], receipt['watch_after'][tree])
            require(not changed, f'watched tree {tree} changed during the install: {changed[:5]}')
            self.journal.save()
        receipt.update(other_files_unchanged=True, watched_trees_unchanged=True, finished=now(), stage_moved_into_place=True)
        self.state('installed_verified')

    def check_names(self, preflight, receipt):
        """The folders list exactly the names the swap should leave."""
        card = self.card
        exefs, plugins = self.inventory()
        receipt['exefs_names_after'], receipt['plugin_names_after'] = sorted(exefs), sorted(plugins)
        require(receipt['exefs_names_after'] == preflight['exefs_names_before'], 'exefs/ lists other names after the install')
        expected = sorted([n for n in preflight['plugin_names_before'] if n != self.old_name] + [self.new_name])
        require(receipt['plugin_names_after'] == expected, f'the plugin folder lists {receipt["plugin_names_after"]}, expected {expected}')
        require(not card.exists(self.target_old), 'the old NRO is back in the plugin folder')
        backups = self.folder_names(self.data + '/backups')
        require(backups == sorted(preflight['backups_names_before'] + [self.archive.rsplit('/', 1)[1]]),
                f'{self.data}/backups lists {backups}')
        require(self.folder_names(self.data + '/install') == preflight['install_names_before'],
                f'{self.data}/install lists other names after the install')
        receipt['backups_names_after'] = backups

    def roll_back(self):
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                signal.signal(number, signal.SIG_IGN)  # the rollback runs to its end
            except ValueError:
                pass
        card, receipt, before = self.card, self.receipt, self.before
        try:
            card.allow_rollback_time()
            self.state('rolling_back')
            left_alone = receipt['rollback_left_alone'] = []
            for path in reversed(self.created_files):  # what this run added, newest first
                temporary = path.rsplit('/', 1)[0] + '/.' + path.rsplit('/', 1)[1] + '.part'
                if card.exists(temporary):
                    card.unlink(temporary)
                if card.exists(path):
                    # Removed: a file this run finished writing (even if it reads back wrong), or one with the
                    # bytes this run was writing (an interrupt after the write). A file that appeared at the path
                    # meanwhile, which this run's write refused to replace, is not ours.
                    if path in self.stored_files or self.download(path, 'rollback-found')['sha256'] == self.local_files[path]['sha256']:
                        card.unlink(path)
                    else:
                        left_alone.append(path)
            for folder in reversed(self.created_dirs):
                if card.exists(folder) and not card.listing(folder):
                    card.rmdir(folder)
            if self.swapped_in and card.exists(self.target_new):
                card.unlink(self.target_new)
            if card.exists(self.target_old):
                present = self.download(self.target_old, 'rollback-found')
                require(present['sha256'] == self.old, f'a different file is at {self.target_old}; not overwriting it')
            else:
                card.store(before[self.target_old]['local'], self.target_old)
            restored = self.download(self.target_old, 'rollback-restored', before[self.target_old]['bytes'])
            require(restored['sha256'] == self.old, 'the restored NRO does not match the old build')
            require(not card.exists(self.target_new), 'the new NRO is still in the plugin folder')
            # Another writer's changes to the folder are not this tool's to undo: they are recorded, not failed.
            names = sorted(card.listing(PLUGINS))
            receipt['plugin_names_after_rollback'] = names
            receipt['plugin_names_changed_by_others'] = sorted(set(names) ^ set(receipt['plugin_names_before']))
            receipt.update(rollback_readback_after_failure=restored, finished=now())
            self.state('rolled_back_verified')
        except BaseException as error:
            receipt.update(rollback_error=repr(error), finished=now())
            self.state('recovery_required')

    def verify(self):
        card, receipt = self.card, self.receipt
        deployment = json.loads((self.out / 'deployment.json').read_text())
        require(deployment['state'] == 'installed_verified', f'deployment is {deployment["state"]}')
        require(deployment['approval'] == self.summary, 'the approval changed since the install')
        receipt['mount_id_before'] = card.mount_id()
        self.state('reading_back')
        receipt['verify'] = {}
        for relative, expected in deployment['after'].items():
            result = receipt['verify'][relative] = self.download(relative, 'verify', expected['bytes'])
            self.journal.save()
            require(result['sha256'] == expected['sha256'], f'{relative}: readback does not match the install')
        exefs, plugins = self.inventory()
        require(sorted(exefs) == deployment['exefs_names_after'] and sorted(plugins) == deployment['plugin_names_after'],
                'a card listing changed since the install')
        require(self.folder_names(self.data + '/backups') == deployment['backups_names_after'], f'{self.data}/backups changed since the install')
        receipt['watch_verify'] = {}
        for tree in self.watch:
            receipt['watch_verify'][tree] = self.walk(tree, 'watch-verify')
            changed = self.differences(deployment['watch_after'][tree], receipt['watch_verify'][tree])
            require(not changed, f'watched tree {tree} changed since the install: {changed[:5]}')
        receipt['mount_id_after'] = card.mount_id()
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
            if self.removed:
                self.roll_back()
        finally:
            self.card.finish(self.receipt)
            self.receipt['device_operations_closed'] = now()
            self.journal.save()
            self.lock.close()
            for number, handler in handlers.items():
                signal.signal(number, handler)
        print(json.dumps({k: self.receipt.get(k) for k in ('state', 'target_old', 'target_new', 'device_operations_closed')}, indent=2))
        if ok and self.mode in ('install', 'verify'):
            # The user connects and disconnects the Switch (playbook WORKFLOW, Switch); this tool never does.
            print(CLOSING)
        return 0 if ok else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 2 or argv[0] not in RECEIPTS:
        print('usage: swap-ums.py preflight|install|verify <dir>', file=sys.stderr)
        return 2
    out = Path(argv[1]).resolve()
    if not out.is_dir():
        print(f'{out} is not a directory', file=sys.stderr)
        return 2
    try:
        transaction = Transaction(argv[0], out)
    except BlockingIOError:
        print(f'refused: another transaction holds the lock in {out}', file=sys.stderr)
        return 1
    except (Refused, KeyError, ValueError, TypeError, AttributeError, OSError) as error:
        # An approval of the wrong shape ends here too: configure() reads no card.
        print(f'refused before any card access: {error!r}', file=sys.stderr)
        return 1
    return transaction.run()


if __name__ == '__main__':
    sys.exit(main())
