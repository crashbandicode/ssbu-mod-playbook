"""Single-writer, finite DBI MTP transaction: replace an installed plugin NRO
(OLD) with NEW on the Switch card, with a rollback archive on the card. The
same phases and readbacks as upgrade-mtp.py, with the plugin named by the
approval file instead of constants.
Runs on the host the Switch is plugged into, with DBI's MTP responder up.

  python3 upgrade2-mtp.py preflight <dir>
  python3 upgrade2-mtp.py install   <dir>

<dir> holds the NEW NRO and operator-approval.json ({"operator_order",
"operator_install_authorized", "name", "data", "old", "new"}); receipts and
local copies (before/, just-before/, backup-readback/, stage-readback/,
after/) go there too."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.parse import quote

URI = 'mtp://-_DBI_XAW10006803734/1%3A%20SD%20Card'
PLUGINS = 'atmosphere/contents/01006A800016E000/romfs/skyline/plugins'
PROTECTED = ['bootloader/hekate_ipl.ini', 'ultimate/ssbu_online_deluxe_rewrite/config.ini',
             'ultimate/ssbu_fx_control/config.ini']
DEADLINE = time.monotonic() + 600
mode, OUT = sys.argv[1], Path(sys.argv[2])
assert mode in ('preflight', 'install') and OUT.is_dir()
receipt_path = OUT / ('preflight.json' if mode == 'preflight' else 'deployment.json')
assert not receipt_path.exists(), 'Preserve completed/failed evidence; inspect before a new operation'
lock = (OUT / '.transaction.lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
approval = json.loads((OUT / 'operator-approval.json').read_text())
assert approval['operator_install_authorized']
NAME, DATA, OLD, NEW = approval['name'], approval['data'], approval['old'], approval['new']
assert re.fullmatch(r'lib[a-z0-9_]+\.nro', NAME) and re.fullmatch(r'ultimate/[a-z0-9_-]+', DATA)
assert all(re.fullmatch(r'[0-9a-f]{64}', h) for h in (OLD, NEW)) and OLD != NEW
TARGET = PLUGINS + '/' + NAME
BACKUP = DATA + f'/backups/pre-{NEW[:16]}.nro'
STAGE = DATA + f'/install/{NAME[:-4]}-{NEW[:16]}.stage'
env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus')
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


def uri(relative):
    return URI + '/' + quote(relative, safe='/')


def gio(*args, check=True):
    left = DEADLINE - time.monotonic()
    if left <= 0:
        raise TimeoutError('MTP phase total deadline reached')
    entry = {'argv': ['/usr/bin/gio', *args], 'started': now()}
    process = subprocess.Popen(['/usr/bin/gio', *args], env=env, stdin=subprocess.DEVNULL,
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
    entry.update(finished=now(), exit_code=process.returncode, timed_out=timed_out,
                 stdout=stdout, stderr=stderr)
    commands.append(entry)
    save()
    if check and (timed_out or process.returncode):
        raise RuntimeError(f'GIO failed/timeout: {entry!r}')
    return stdout


def listing(relative):
    result = {}
    for line in gio('list', '-l', uri(relative)).splitlines():
        name, size, kind = line.split('\t')
        assert '/' not in name and name not in ('.', '..')
        result[name] = {'size': int(size), 'kind': kind}
    return result


def exists(relative):
    gio('info', uri(relative), check=False)
    return commands[-1]['exit_code'] == 0


def download(relative, group, expected_size=None):
    destination = OUT / group / relative
    assert not destination.exists(), f'Preserve local evidence: {destination}'
    destination.parent.mkdir(parents=True, exist_ok=True)
    gio('copy', '-T', '--', uri(relative), str(destination))
    result = info(destination)
    assert result['bytes'] <= 30_000_000, relative
    if expected_size is not None:
        assert result['bytes'] == expected_size, relative
    return result


source = info(OUT / NAME)
assert source['sha256'] == NEW and (OUT / NAME).read_bytes()[0x10:0x14] == b'NRO0'
receipt = {'state': 'started', 'started': now(), 'mode': mode, 'transport': 'DBI USB MTP',
           'device_sd_uri': URI, 'operator_order': approval['operator_order'], 'target': TARGET,
           'old': OLD, 'new': NEW, 'backup': BACKUP, 'stage': STAGE, 'source': source,
           'commands': commands, 'process_id': os.getpid()}
replaced = False
try:
    save()
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
    else:
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
                gio('mkdir', '-p', uri(directory))
        gio('copy', '-T', '--', before[TARGET]['local'], uri(BACKUP))
        rollback = download(BACKUP, 'backup-readback', before[TARGET]['bytes'])
        assert rollback['sha256'] == OLD, 'Rollback archive readback mismatch'
        receipt['rollback_readback'] = rollback
        state('rollback_verified')
        gio('copy', '-T', '--', source['local'], uri(STAGE))
        staged = download(STAGE, 'stage-readback', source['bytes'])
        assert staged['sha256'] == NEW, 'Staging readback mismatch'
        receipt['stage_readback'] = staged
        state('staged_verified')
        # Proven DBI copy-replace; MTP rename is not assumed atomic or supported.
        replaced = True
        gio('copy', '-T', '--', staged['local'], uri(TARGET))
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
                       remote_stage_retained_outside_loader=True)
        state('installed_verified')
except Exception as error:
    receipt.update(error=repr(error), finished=now())
    state('failed')
    if replaced:
        try:
            DEADLINE = time.monotonic() + 240
            state('rolling_back')
            gio('copy', '-T', '--', before[TARGET]['local'], uri(TARGET))
            restored = download(TARGET, 'rollback-restored', before[TARGET]['bytes'])
            assert restored['sha256'] == OLD
            receipt.update(finished=now())
            state('rolled_back_verified')
        except Exception as rollback_error:
            receipt.update(rollback_error=repr(rollback_error), finished=now())
            state('recovery_required')
    raise
finally:
    receipt['all_command_processes_exited'] = all('exit_code' in entry for entry in commands)
    receipt['device_operations_closed'] = now()
    save()
    lock.close()
print(json.dumps({k: receipt.get(k) for k in ('state', 'target', 'device_operations_closed')}, indent=2))
if mode == 'install':
    # The user connects and disconnects the Switch (playbook WORKFLOW, Switch); this tool never does.
    print('Done. The Switch is still connected: disconnect it (close DBI) when you are ready.')
