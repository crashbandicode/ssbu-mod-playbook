"""Single-writer, finite card-mount transaction: replace an installed plugin NRO
(OLD) with NEW on the Switch SD card while it is mounted as a USB disk (hekate
Nyx UMS or a card reader), with a rollback archive on the card. The phases,
receipts and readbacks mirror upgrade2-mtp.py; only the transport differs.
Runs on the host the card is mounted on, as the user who owns the mount.

  python3 upgrade2-ums.py preflight <dir>
  python3 upgrade2-ums.py install   <dir>
  python3 upgrade2-ums.py verify    <dir>   # re-read what install touched, from the card

<dir> holds the NEW NRO and operator-approval.json ({"operator_order",
"operator_install_authorized", "name", "data", "old", "new", "mount"}); receipts
and local copies (before/, just-before/, backup-readback/, stage-readback/,
after/, verify/) go there too. "mount" is the card's mount point.

Every read of a card file bypasses the page cache (O_DIRECT; where a
filesystem refuses it, the file's clean cached pages are dropped first), so a
readback shows what the card holds, not what this process just wrote. The card
is never unmounted or remounted (operator rule, 2026-11-01): verify reads every
file in place."""
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
import subprocess
import sys
import time

PLUGINS = 'atmosphere/contents/01006A800016E000/romfs/skyline/plugins'
PROTECTED = ['bootloader/hekate_ipl.ini', 'ultimate/ssbu_online_deluxe_rewrite/config.ini',
             'ultimate/ssbu_fx_control/config.ini']
DEADLINE = time.monotonic() + 600
mode, OUT = sys.argv[1], Path(sys.argv[2])
assert mode in ('preflight', 'install', 'verify') and OUT.is_dir()
receipt_path = OUT / {'preflight': 'preflight.json', 'install': 'deployment.json', 'verify': 'verify.json'}[mode]
assert not receipt_path.exists(), 'Preserve completed/failed evidence; inspect before a new operation'
lock = (OUT / '.transaction.lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
approval = json.loads((OUT / 'operator-approval.json').read_text())
assert approval['operator_install_authorized']
NAME, DATA, OLD, NEW = approval['name'], approval['data'], approval['old'], approval['new']
ROOT = Path(approval['mount'])
assert re.fullmatch(r'lib[a-z0-9_]+\.nro', NAME) and re.fullmatch(r'ultimate/[A-Za-z0-9_-]+', DATA)
assert all(re.fullmatch(r'[0-9a-f]{64}', h) for h in (OLD, NEW)) and OLD != NEW
assert ROOT.is_absolute() and ROOT.is_mount(), f'{ROOT} is not a mount point'
TARGET = PLUGINS + '/' + NAME
BACKUP = DATA + f'/backups/pre-{NEW[:16]}.nro'
STAGE = DATA + f'/install/{NAME[:-4]}-{NEW[:16]}.stage'
commands = []
sha = lambda data: hashlib.sha256(data).hexdigest()
now = lambda: datetime.now(timezone.utc).isoformat()


def info(path):
    data = path.read_bytes()
    return {'sha256': sha(data), 'bytes': len(data), 'local': str(path)}


def save():
    receipt['updated'] = now()
    temporary = receipt_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(receipt, indent=2) + '\n')
    temporary.replace(receipt_path)


def state(value):
    receipt['state'] = value
    save()
    print(value, flush=True)


def card(relative):
    assert not relative.startswith('/') and '..' not in relative.split('/')
    return ROOT / relative


def op(name, *args):
    """Record one card operation in the receipt (the analogue of upgrade2-mtp.py's gio log)."""
    if DEADLINE - time.monotonic() <= 0:
        raise TimeoutError('card phase total deadline reached')
    entry = {'op': name, 'args': [str(a) for a in args], 'started': now()}
    commands.append(entry)
    return entry


def done(entry, **more):
    entry.update(finished=now(), **more)
    save()


def run(*argv):
    entry = op('exec', *argv)
    process = subprocess.run(list(argv), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    done(entry, exit_code=process.returncode, stdout=process.stdout, stderr=process.stderr)
    if process.returncode:
        raise RuntimeError(f'command failed: {entry!r}')
    return process.stdout


def listing(relative):
    entry = op('list', relative)
    result = {}
    for item in sorted(card(relative).iterdir()):
        assert item.name not in ('.', '..')
        kind = '(regular)' if item.is_file() and not item.is_symlink() else '(other)'
        result[item.name] = {'size': item.stat().st_size if kind == '(regular)' else -1, 'kind': kind}
    done(entry, names=sorted(result))
    return result


def exists(relative):
    entry = op('exists', relative)
    result = card(relative).exists()
    done(entry, exists=result)
    return result


ALIGN = 4096  # O_DIRECT buffer, offset and length alignment (covers 512 B and 4 KiB sectors)


def read_from_card(relative):
    """`(bytes, method)` of a card file as the card holds it. O_DIRECT reads
    into a page-aligned buffer, skipping the page cache; if the open refuses
    O_DIRECT, the file's clean cached pages are dropped (every write here was
    fsynced) and it is read normally."""
    path = card(relative)
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
            assert got % ALIGN == 0, f'{relative}: short O_DIRECT read at {got}'
            count = os.preadv(fd, [view[got:]], got)
            if count == 0:
                break
            got += count
        data = bytes(view[:got])
        view.release()
        buffer.close()
    finally:
        os.close(fd)
    assert len(data) == size, f'{relative}: read {len(data)} of {size} bytes'
    return data, 'o_direct'


def download(relative, group, expected_size=None):
    destination = OUT / group / relative
    assert not destination.exists(), f'Preserve local evidence: {destination}'
    destination.parent.mkdir(parents=True, exist_ok=True)
    entry = op('copy-from-card', relative, destination)
    data, method = read_from_card(relative)
    destination.write_bytes(data)
    result = info(destination)
    done(entry, method=method, **{k: result[k] for k in ('sha256', 'bytes')})
    assert result['bytes'] <= 30_000_000, relative
    if expected_size is not None:
        assert result['bytes'] == expected_size, relative
    return result


def upload(local, relative):
    """Write a card file through a same-directory temporary and an atomic rename, then flush."""
    target = card(relative)
    temporary = target.with_name('.' + target.name + '.part')
    entry = op('copy-to-card', local, relative)
    assert not temporary.exists(), temporary
    with open(local, 'rb') as source_handle, open(temporary, 'wb') as handle:
        shutil.copyfileobj(source_handle, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    os.sync()
    done(entry)


def move_on_card(source, destination):
    entry = op('rename-on-card', source, destination)
    os.replace(card(source), card(destination))
    os.sync()
    done(entry)


def mkdir(relative):
    entry = op('mkdir', relative)
    card(relative).mkdir(parents=True, exist_ok=True)
    done(entry)


def mount_source():
    return run('/usr/bin/findmnt', '-n', '-o', 'SOURCE', '--target', str(ROOT)).strip()


def mount_id():
    """The kernel's id of the mount at ROOT: unchanged across verify shows the
    card stayed mounted the whole time."""
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        if fields[4].replace('\\040', ' ') == str(ROOT):
            return int(fields[0])
    raise AssertionError(f'{ROOT} is not in mountinfo')


source = info(OUT / NAME)
assert source['sha256'] == NEW and (OUT / NAME).read_bytes()[0x10:0x14] == b'NRO0'
receipt = {'state': 'started', 'started': now(), 'mode': mode, 'transport': 'mounted card (USB disk)',
           'mount': str(ROOT), 'operator_order': approval['operator_order'], 'target': TARGET,
           'old': OLD, 'new': NEW, 'backup': BACKUP, 'stage': STAGE, 'source': source,
           'commands': commands, 'process_id': os.getpid()}
replaced = False
try:
    save()
    receipt['mount_source'] = mount_source()
    if mode == 'preflight':
        inventory = listing(PLUGINS)
        assert NAME in inventory, 'not installed; use a deploy script'
        assert all(n.endswith('.nro') and v['kind'] == '(regular)' for n, v in inventory.items()), inventory
        assert not exists(BACKUP) and not exists(STAGE), 'archive or stage exists; inspect first'
        receipt.update(plugin_names_before=sorted(inventory), before={},
                       protected_present=[p for p in PROTECTED if exists(p)])
        state('backing_up_locally')
        for name in sorted(inventory):
            receipt['before'][PLUGINS + '/' + name] = download(PLUGINS + '/' + name, 'before', inventory[name]['size'])
            save()
        for relative in receipt['protected_present']:
            receipt['before'][relative] = download(relative, 'before')
            save()
        assert receipt['before'][TARGET]['sha256'] == OLD, 'Unexpected installed build; inspect before replacement'
        receipt['finished'] = now()
        state('preflight_verified')
    elif mode == 'install':
        preflight = json.loads((OUT / 'preflight.json').read_text())
        assert preflight['state'] == 'preflight_verified'
        before = preflight['before']
        assert sorted(listing(PLUGINS)) == preflight['plugin_names_before']
        receipt.update(before=before, plugin_names_before=preflight['plugin_names_before'])
        fresh = download(TARGET, 'just-before', before[TARGET]['bytes'])
        assert fresh['sha256'] == OLD, 'Changed since preflight'
        assert not exists(BACKUP) and not exists(STAGE)
        state('writing_rollback_archive')
        for directory in (DATA + '/backups', DATA + '/install'):
            if not exists(directory):
                mkdir(directory)
        upload(before[TARGET]['local'], BACKUP)
        rollback = download(BACKUP, 'backup-readback', before[TARGET]['bytes'])
        assert rollback['sha256'] == OLD, 'Rollback archive readback mismatch'
        receipt['rollback_readback'] = rollback
        state('rollback_verified')
        upload(source['local'], STAGE)
        staged = download(STAGE, 'stage-readback', source['bytes'])
        assert staged['sha256'] == NEW, 'Staging readback mismatch'
        receipt['stage_readback'] = staged
        state('staged_verified')
        # A rename on one filesystem is atomic: the loader sees OLD or NEW, never a partial file.
        replaced = True
        move_on_card(STAGE, TARGET)
        state('installed_pending_readback')
        receipt['after'] = {}
        for relative, previous in before.items():
            expected = source['bytes'] if relative == TARGET else previous['bytes']
            result = download(relative, 'after', expected)
            receipt['after'][relative] = result
            save()
            assert result['sha256'] == (NEW if relative == TARGET else previous['sha256']), relative
        receipt['plugin_names_after'] = sorted(listing(PLUGINS))
        assert receipt['plugin_names_after'] == preflight['plugin_names_before']
        receipt.update(other_plugins_unchanged=True, protected_unchanged=True, finished=now(),
                       stage_moved_into_place=True, stage_retained=False)
        state('installed_verified')
    else:
        deployment = json.loads((OUT / 'deployment.json').read_text())
        assert deployment['state'] == 'installed_verified'
        receipt['mount_id_before'] = mount_id()
        state('reading_in_place')
        receipt['verify'] = {}
        for relative, expected in deployment['after'].items():
            result = download(relative, 'verify', expected['bytes'])
            receipt['verify'][relative] = result
            save()
            assert result['sha256'] == expected['sha256'], relative
        receipt['plugin_names_verify'] = sorted(listing(PLUGINS))
        assert receipt['plugin_names_verify'] == deployment['plugin_names_after']
        receipt['mount_id_after'] = mount_id()
        assert receipt['mount_id_after'] == receipt['mount_id_before'], 'the card was remounted during verify'
        receipt['finished'] = now()
        state('verified_in_place')
except Exception as error:
    receipt.update(error=repr(error), finished=now())
    state('failed')
    if replaced:
        try:
            DEADLINE = time.monotonic() + 240
            state('rolling_back')
            upload(before[TARGET]['local'], TARGET)
            restored = download(TARGET, 'rollback-restored', before[TARGET]['bytes'])
            assert restored['sha256'] == OLD
            receipt.update(finished=now())
            state('rolled_back_verified')
        except Exception as rollback_error:
            receipt.update(rollback_error=repr(rollback_error), finished=now())
            state('recovery_required')
    raise
finally:
    receipt['all_operations_closed'] = all('finished' in entry for entry in commands)
    receipt['device_operations_closed'] = now()
    save()
    lock.close()
print(json.dumps({k: receipt.get(k) for k in ('state', 'target', 'device_operations_closed')}, indent=2))
if mode in ('install', 'verify'):
    # The user connects and disconnects the Switch (playbook WORKFLOW, Switch); this tool never does.
    print('Done. The card is still mounted: unmount or eject it yourself when you are ready.')
