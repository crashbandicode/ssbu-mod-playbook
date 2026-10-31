"""Single-writer, finite DBI MTP transaction: add gsp-visualizer to the
Switch card (a new plugin; no existing file is replaced). Adapted from the
proven deploy-fx021-sgpo012-mtp-1018.py (same gio calls and readbacks).
Runs on the host the Switch is plugged into, with DBI's MTP responder up.

  python3 deploy-mtp.py preflight <dir>
  python3 deploy-mtp.py install   <dir>

<dir> holds libgsp_visualizer.nro and operator-approval.json; receipts and
local copies (before/, stage-readback/, after/) go there too."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import quote

URI = 'mtp://-_DBI_XAW10006803734/1%3A%20SD%20Card'
PLUGINS = 'atmosphere/contents/01006A800016E000/romfs/skyline/plugins'
NAME = 'libgsp_visualizer.nro'
TARGET = PLUGINS + '/' + NAME
DATA = 'ultimate/gsp-visualizer'
PROTECTED = ['bootloader/hekate_ipl.ini', 'ultimate/ssbu_online_deluxe_rewrite/config.ini',
             'ultimate/ssbu_fx_control/config.ini']
EXPECTED = '80af4f63c672b82c0453aefd08fb9c550a8977e41472cd4f6c10b74ef88dab30'
DEADLINE = time.monotonic() + 600
mode, OUT = sys.argv[1], Path(sys.argv[2])
assert mode in ('preflight', 'install') and OUT.is_dir()
receipt_path = OUT / ('preflight.json' if mode == 'preflight' else 'deployment.json')
assert not receipt_path.exists(), 'Preserve completed/failed evidence; inspect before a new operation'
lock = (OUT / '.transaction.lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
approval = json.loads((OUT / 'operator-approval.json').read_text())
assert approval['operator_install_authorized']
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
    return gio('info', uri(relative), check=False) and commands[-1]['exit_code'] == 0


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
binary = (OUT / NAME).read_bytes()
assert source['sha256'] == EXPECTED and binary[0x10:0x14] == b'NRO0'
STAGE = DATA + f'/install/{NAME[:-4]}-{EXPECTED[:16]}.stage'
receipt = {'state': 'started', 'started': now(), 'mode': mode, 'transport': 'DBI USB MTP',
           'device_sd_uri': URI, 'operator_order': approval['operator_order'],
           'target': TARGET, 'stage': STAGE, 'source': source, 'commands': commands,
           'process_id': os.getpid()}
written = False
try:
    save()
    if mode == 'preflight':
        inventory = listing(PLUGINS)
        assert NAME not in inventory, 'already installed; inspect first'
        assert all(n.endswith('.nro') and v['kind'] == '(regular)' for n, v in inventory.items()), inventory
        receipt.update(plugin_names_before=sorted(inventory), before={},
                       protected_present=[p for p in PROTECTED if exists(p)])
        state('backing_up_locally')
        for name in sorted(inventory):
            receipt['before'][PLUGINS + '/' + name] = download(PLUGINS + '/' + name, 'before', inventory[name]['size'])
            save()
        for relative in receipt['protected_present']:
            receipt['before'][relative] = download(relative, 'before')
            save()
        receipt['finished'] = now()
        state('preflight_verified')
    else:
        preflight = json.loads((OUT / 'preflight.json').read_text())
        assert preflight['state'] == 'preflight_verified'
        before = preflight['before']
        inventory = listing(PLUGINS)
        assert sorted(inventory) == preflight['plugin_names_before'] and NAME not in inventory
        for relative, previous in before.items():
            if relative.startswith(PLUGINS + '/'):
                assert inventory[Path(relative).name]['size'] == previous['bytes'], f'Changed since preflight: {relative}'
        receipt.update(before=before, plugin_names_before=preflight['plugin_names_before'])
        state('staging')
        for directory in (DATA, DATA + '/install'):
            if not exists(directory):
                gio('mkdir', uri(directory))
        gio('copy', '-T', '--', source['local'], uri(STAGE))
        staged = download(STAGE, 'stage-readback', source['bytes'])
        assert staged['sha256'] == EXPECTED, 'Staging readback mismatch'
        receipt['stage_readback'] = staged
        state('staged_verified')
        written = True
        gio('copy', '-T', '--', staged['local'], uri(TARGET))
        state('installed_pending_readback')
        receipt['after'] = {TARGET: download(TARGET, 'after', source['bytes'])}
        assert receipt['after'][TARGET]['sha256'] == EXPECTED, 'Target readback mismatch'
        for relative, previous in before.items():
            result = download(relative, 'after', previous['bytes'])
            receipt['after'][relative] = result
            save()
            assert result['sha256'] == previous['sha256'], f'Changed: {relative}'
        receipt['plugin_names_after'] = sorted(listing(PLUGINS))
        assert receipt['plugin_names_after'] == sorted(preflight['plugin_names_before'] + [NAME])
        receipt.update(other_plugins_unchanged=True, protected_unchanged=True, finished=now(),
                       remote_stage_retained_outside_loader=True)
        state('installed_verified')
except Exception as error:
    receipt.update(error=repr(error), finished=now())
    state('failed')
    if written:
        try:
            DEADLINE = time.monotonic() + 240
            state('removing_new_plugin')
            gio('remove', uri(TARGET), check=False)
            assert NAME not in listing(PLUGINS)
            receipt.update(finished=now())
            state('removed_verified')
        except Exception as removal_error:
            receipt.update(removal_error=repr(removal_error), finished=now())
            state('recovery_required')
    raise
finally:
    receipt['all_command_processes_exited'] = all('exit_code' in entry for entry in commands)
    receipt['device_operations_closed'] = now()
    save()
    lock.close()
print(json.dumps({k: receipt.get(k) for k in ('state', 'target', 'device_operations_closed')}, indent=2))
