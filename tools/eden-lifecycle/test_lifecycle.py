#!/usr/bin/env python3
"""Offline only: fake host/RPC/claims; all filesystem fixtures are temporary."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path, PureWindowsPath
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import call, mock_open, patch

spec = importlib.util.spec_from_file_location('lifecycle', Path(__file__).with_name('lifecycle.py'))
life = importlib.util.module_from_spec(spec)
spec.loader.exec_module(life)
real_ssh_mux = life.SshMux
real_ssh_diagnosis = life.ssh_diagnosis


class FakeMux:
    def __init__(self, host, ssh_options=(), **kwargs):
        self.host = host
        self.run = kwargs.get('run', subprocess.run)
        self.options = ssh_options
        self.evidence_dir = kwargs.get('evidence_dir')
        self.ssh_bin = kwargs.get('ssh_bin', 'ssh')
        self.windows = kwargs.get('windows', False)
        self._temporary = tempfile.TemporaryDirectory(prefix='fake-eden-mux-')
        self.root = Path(self._temporary.name)
        self.master_log = self.root / 'master-1.log'
        self.master_log.write_text('Authenticated to fake-host\nEntering interactive session\n')
        self.attempt_deadline = None
        self.open_generation = 0
        self.rider_args = [*life.SSH_OPTIONS, *ssh_options, '-o', 'ControlMaster=no',
                           '-o', 'ControlPath=/tmp/fake-eden-ssh/%C', '-o', 'ProxyCommand=/bin/false']

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._temporary.cleanup()

    def ensure(self):
        pass

    def _control(self, operation):
        return subprocess.CompletedProcess(['ssh', '-O', operation], 0, '', '')


def setUpModule():
    temporary = tempfile.TemporaryDirectory(prefix='lifecycle-test-state-')
    unittest.addModuleCleanup(temporary.cleanup)
    environment = patch.dict(os.environ, {'XDG_STATE_HOME': temporary.name})
    environment.start()
    unittest.addModuleCleanup(environment.stop)
    mux = patch.object(life, 'SshMux', FakeMux)
    mux.start()
    unittest.addModuleCleanup(mux.stop)


def transport(text='ssh diag fake: temporary stall', transient=True, cause='temporary stall'):
    return life.LedgerTransport([{'line': text, 'transient': transient, 'cause': cause,
                                  'phase': 'auth', 'evidence': '/tmp/fake-diag'}])


def target_fixture_run(target, attempts, ping_text='0% packet loss\nrtt min/avg/max/mdev = 1.0/2.0/3.0/0.1 ms\n',
                       succeed_after_failure=False):
    def run(argv, **kwargs):
        if 'input' in kwargs:
            attempts.append(argv)
            Path(argv[argv.index('-E') + 1]).write_text(
                'Connecting to fake-host\nConnection established\nRemote protocol version 2.0\n'
                'SSH2_MSG_KEXINIT sent\nConnection to fake-host port 22 timed out\n')
            if succeed_after_failure and len(attempts) > 1:
                return subprocess.CompletedProcess(argv, 0, json.dumps({'owned_claim': True}), '')
            return subprocess.CompletedProcess(argv, 255, '', '')
        if '-G' in argv:
            options = [x for x in argv if x.startswith('ControlPath=')]
            control = ('controlpath ' + options[-1][len('ControlPath='):].replace('%C', 'fixture') + '\n') if options else ''
            return subprocess.CompletedProcess(argv, 0, 'hostname fake-host\nport 22\n' + control, '')
        if argv[0] == 'ping':
            return subprocess.CompletedProcess(argv, 0, ping_text, '')
        if '-vvv' in argv:
            return subprocess.CompletedProcess(argv, 0 if target is not None else 255, '',
                                               'Authenticated to fake-host' if target is not None else
                                               'Connecting to fake-host\nConnection established\n')
        if 'EncodedCommand' in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(target), '')
        return subprocess.CompletedProcess(argv, 0, '', '')
    return run


def health_path_cases():
    normal = {'load1': 1, 'cores': 8, 'memAvailMiB': 1000, 'oomKills5m': 0,
              'sshdProcesses': 5, 'sshdUnauth': 0, 'maxStartups': '10:30:100', 'maxStartupsLines': 0}
    loss = '20% packet loss\nrtt min/avg/max/mdev = 1.000/2.000/3.000/0.100 ms\n'
    spike = '0% packet loss\nrtt min/avg/max/mdev = 2.000/5.000/140.000/0.100 ms\n'
    quiet = '0% packet loss\nrtt min/avg/max/mdev = 1.000/2.000/3.000/0.100 ms\n'
    windows_busy = {'cpuLoadPct': 100, 'memAvailMiB': 4096, 'sshdProcesses': 3, 'sshdUnauth': None,
                    'journal': 'unavailable on Windows'}
    windows_idle = {**windows_busy, 'cpuLoadPct': 5, 'memAvailMiB': 8192}
    return (
        ('unavailable-loss', None, loss, 1, 'packet loss 20% on the path to fake-host; target health not confirmed, no retry'),
        ('missing-memory', {k: v for k, v in normal.items() if k != 'memAvailMiB'}, loss, 1,
         'packet loss 20% on the path to fake-host; target health not confirmed, no retry'),
        ('null-oom', {**normal, 'oomKills5m': None}, loss, 1,
         'packet loss 20% on the path to fake-host; target health not confirmed, no retry'),
        ('unavailable-rtt', None, spike, 1, 'RTT spikes on the path to fake-host (max 140.000 ms); target health not confirmed, no retry'),
        ('healthy-loss', normal, loss, 2, 'packet loss 20% on the path to fake-host'),
        ('healthy-rtt', normal, spike, 2, 'RTT spikes on the path to fake-host (max 140.000 ms)'),
        ('healthy-timeout', normal, quiet, 2,
         'single kex timeout with TCP 22 answering and a healthy target (load 1 on 8 cores, 1000 MiB available)'),
        ('overload-loss', {**normal, 'load1': 48, 'memAvailMiB': 150, 'oomKills5m': 2}, loss, 1,
         'target overloaded (load 48 on 8 cores, 150 MiB available, 2 OOM kills)'),
        ('throttled-loss', {**normal, 'sshdUnauth': 10}, loss, 1,
         'sshd connection throttling on the target (MaxStartups)'),
        ('windows-busy-loss', windows_busy, loss, 1,
         'packet loss 20% on the path to fake-host; target health not confirmed, no retry'),
        ('windows-idle-loss', windows_idle, loss, 1,
         'packet loss 20% on the path to fake-host; target health not confirmed, no retry'),
        ('windows-rtt', windows_idle, spike, 1,
         'RTT spikes on the path to fake-host (max 140.000 ms); target health not confirmed, no retry'),
        ('windows-timeout', windows_idle, quiet, 1,
         'target health not confirmed, no retry'),
    )


class FakeLedger:
    def __init__(self, order):
        self.order = order
        self.claimed = self.released = False
        self.refuse = False
        self.open_text = ''

    def claim(self, stale_claim_hours=None, admission=None):
        self.order.append('claim')
        if self.refuse:
            raise life.Refused('foreign claim')
        policy = life.claim_admission(self.open_text, 'surface', 'OpenCode test-session/test-stream',
                                      stale_claim_hours, admission)
        life.require(not policy['blocking'], 'Open host claim; no profile changes permitted')
        self.claimed = True
        return {'line': 'FAKE claim; no coordination file touched',
                'stale_claim_skipped': policy['stale_claim_skipped']}

    def release(self):
        self.order.append('release')
        self.released = True
        return {'line': 'FAKE release'}

    def inspect(self):
        return {'owned_claim': self.claimed and not self.released, 'owned_release': self.released}


class FakeHost:
    def __init__(self, order):
        self.order = order
        self.now = 0
        self.live = {}
        self.calls = []
        self.available = 24 * life.GIB
        self.disk = 100 * 10 ** 9
        self.foreign = []
        self.socket = True
        self.loading = True
        self.mute = {'value': 0, 'effective': 0}
        self.boot = {}
        self.resolution_reply = None
        self.stop_mode = 'done'
        self.fail_launch = None
        self.leftover = False
        self.job_mode = 'done'
        self.admissions = []
        self.fail_watchdog = False
        self.started_watchdogs = []
        self.stopped_watchdogs = []
        self.watchdog_commands = []
        self.started_settles = []
        self.stopped_settles = []
        self.settle_units = set()
        self.fail_settle = False

    def clock(self):
        return self.now

    def name(self):
        return 'offline-test-host'

    def sleep(self, seconds):
        self.now += seconds

    def resources(self, path, owned=()):
        self.admissions.append(list(owned))
        return {'mem_available': self.available, 'disk_free': self.disk, 'foreign_processes': self.foreign}

    def start_display(self, item, config):
        self.order.append('display-' + item['key'])
        if config.get('display_helper'):
            for i, unit in enumerate(item['display_units']):
                self.live[unit] = [{'pid': 200 + i, 'start': '11', 'boot': 'fake'}]
        return {unit: self.live[unit] for unit in item['display_units']} if config.get('display_helper') else {}

    def metrics(self, item):
        return {'memory_peak': 1234, 'scope': 'synthetic offline fixture'}

    def launch(self, item, config):
        self.order.append('launch-' + item['key'])
        Path(item['run']).mkdir(parents=True, exist_ok=True)
        Path(item['console_log']).write_text('fake console\n')
        self.live[item['unit']] = [{'pid': 100 + len(self.live), 'start': '10', 'boot': 'fake'}]
        if self.fail_launch == item['key']:
            raise RuntimeError('synthetic partial-launch failure')
        return self.live[item['unit']]

    def owned(self, item):
        return self.live.get(item['unit'], [])

    def socket_ready(self, item):
        return self.socket

    def call(self, item, method, params, timeout):
        self.calls.append((item['key'], method, params, timeout))
        self.order.append(method + '-' + item['key'])
        if method == 'settings.set':
            self.boot[params['key']] = params['value']
            result = {}
        elif method == 'settings.get':
            if params.get('key') == 'core.volume':
                result = self.mute
            elif params.get('key') == 'core.resolution_setup' and self.resolution_reply is not None:
                result = self.resolution_reply
            else:
                result = {'value': self.boot[params['key']]}
        elif method == 'status':
            result = {'running': True, 'loading_complete': self.loading, 'last_frame_age_ms': 0}
        elif method == 'game.stop':
            if self.stop_mode == 'reject':
                return {'ok': False, 'error': 'synthetic stop rejection'}
            if self.stop_mode == 'immediate':
                return {'ok': True, 'result': {}}
            result = {'accepted': True, 'job': 'stop'}
        elif method == 'game.load':
            result = {'accepted': True, 'job': 'load'}
        elif method == 'jobs.get':
            mode = self.stop_mode if params['job'] == 'stop' else self.job_mode
            result = {'state': 'running'} if mode == 'timeout' else {
                'state': 'done', 'response': {'ok': mode == 'done'}}
        else:
            result = {}
        return {'ok': True, 'result': result}

    def force_stop(self, item, cap):
        self.order.append('forced-' + item['unit'])
        if self.leftover:
            raise life.Refused('synthetic owned PID left alive')
        before = self.live.pop(item['unit'], [])
        return {'owned_pids_stopped': before}

    def start_watchdog(self, unit, command, directory, runtime):
        if self.fail_watchdog:
            raise RuntimeError('synthetic watchdog start failure')
        self.started_watchdogs.append(unit)
        self.watchdog_commands.append(command)
        self.live[unit] = [{'pid': 9, 'start': 'wd', 'boot': 'fake'}]

    def start_settle(self, unit, command, directory, runtime):
        if self.fail_settle:
            raise RuntimeError('synthetic settle start failure')
        self.started_settles.append((unit, command, directory, runtime))
        self.settle_units.add(unit)

    def stop_settle(self, unit):
        self.stopped_settles.append(unit)
        self.settle_units.discard(unit)

    def settle_running(self, unit):
        return unit in self.settle_units

    def stop_watchdog(self, unit):
        self.stopped_watchdogs.append(unit)
        self.live.pop(unit, None)

    def watchdog_running(self, unit):
        return unit in self.live


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='eden-life-', dir='/tmp/opencode')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        environment = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.base / 'state-home')})
        environment.start()
        self.addCleanup(environment.stop)
        self.source = self.base / 'source-b'
        self.source.mkdir()
        config = self.source / life.CONFIG
        config.parent.mkdir(parents=True)
        config.write_text('[Data Storage]\nnand_directory=' + str(self.source / 'user/nand')
                          + '\nsdmc_directory=' + str(self.source / 'user/sdmc')
                          + '\n[Audio]\nvolume=83\nvolume\\default=true\n')
        for rel in (life.READ_ONLY[0] + '/game.nca', life.READ_ONLY[1] + '/system.nca',
                    'user/firmware/firmware.bin', 'user/nand/user/save/record.bin', 'unknown/nested/data'):
            path = self.source / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'original ' + rel.encode())
        (self.source / 'empty-directory').mkdir()
        old = self.source / life.PLUGINS / 'old.nro'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'old plugin')
        self.app = self.base / 'Eden.AppImage'
        self.app.write_bytes(b'NOT an executable; tests never launch it')
        self.game = self.base / 'game.xci'
        self.game.write_bytes(b'fake game')
        self.plugin = self.base / 'candidate.nro'
        self.plugin.write_bytes(b'fake candidate')
        self.settings = self.base / 'settings.json'
        self.settings.write_text('{"menu": {"x": 1}}')
        self.ledger_path = self.base / 'coordination.txt'
        self.ledger_path.write_text('# TEMPORARY test data; not OWNER-COORDINATION\n')
        self.runner = self.base / 'fake_runner.py'
        self.runner.write_text('def dry_run(path):\n return {"result":"READY","rows":1,"errors":[]}\n'
                               'def up_check(path, config=None):\n return dry_run(path)\n')
        self.rows = self.base / 'rows.json'
        self.rows.write_text('{"fake": true}')
        self.order = []
        self.host = FakeHost(self.order)
        self.ledger = FakeLedger(self.order)
        pin = lambda path: {'path': str(path), 'sha256': life.digest(path)}
        self.config = {'host': 'surface', 'hostname': 'offline-test-host', 'owner': 'OpenCode test-session/test-stream', 'label': 'test-01',
            'work_root': str(self.base / 'owned'), 'state_dir': str(self.base / 'state'),
            'ledger': str(self.ledger_path), 'runner': pin(self.runner), 'rows': pin(self.rows),
            'socket_cap': .3, 'load_cap': .3, 'stop_cap': .3, 'force_cap': .3,
            'instances': [{'key': 'a', 'display': ':101', 'source': str(self.source), 'game': str(self.game),
                'app': pin(self.app), 'plugins': [pin(self.plugin)],
                'files': [{**pin(self.settings), 'to': 'user/sdmc/ultimate/TrainingRooms/settings.json'}]}]}
        self.before_source = life.inventory(self.source)

    def controller(self, **kwargs):
        return life.Lifecycle(self.config, self.host, self.ledger, **kwargs)

    def state(self):
        return json.loads((Path(self.config['state_dir']) / 'lifecycle.json').read_text())

    def pair(self):
        self.config['pair_authorized'] = True
        self.host.max_eden = 2
        second = deepcopy(self.config['instances'][0])
        second.update(key='b', display=':102')
        self.config['instances'].append(second)

    def test_pinned_runner_without_up_check_is_refused_before_a_claim(self):
        self.runner.write_text('def dry_run(path):\n return {"result":"READY","rows":1,"errors":[]}\n')
        self.config['runner']['sha256'] = life.digest(self.runner)
        with self.assertRaisesRegex(life.Refused, 'Pinned runner has no up_check'):
            self.controller().up()
        self.assertFalse(self.ledger.claimed)
        self.assertFalse((Path(self.config['state_dir']) / 'lifecycle.json').exists())
        self.assertNotIn('claim', self.order)

    def test_the_lifecycle_passes_its_config_to_the_pinned_up_check(self):
        self.runner.write_text('def up_check(path, config=None):\n'
                               ' return {"result":"READY","rows":1,"errors":[], "has_config": config is not None}\n')
        self.config['runner']['sha256'] = life.digest(self.runner)
        self.assertTrue(self.controller().check_rows()['has_config'])

    def test_target_not_in_plugin_pins_refuses_before_a_claim_or_journal(self):
        tree = self.base / 'target'
        tree.mkdir()
        subprocess.run(['git', '-C', str(tree), 'init', '-q'], check=True)
        subprocess.run(['git', '-C', str(tree), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '--allow-empty', '-qm', 'fixture'], check=True)
        commit = subprocess.run(['git', '-C', str(tree), 'rev-parse', 'HEAD'], check=True,
                                capture_output=True, text=True).stdout.strip()
        self.rows.write_text(json.dumps({
            'target': {'plugin': {'tree': str(tree), 'commit': commit, 'nro_sha256': 'a' * 64}},
            'start': 'main', 'rows': [{'id': 'r', 'start': 'main', 'end': 'main', 'role': 'setup',
                                       'steps': [{'call': 'status'}]}]}))
        actual_runner = Path(__file__).resolve().parents[1] / 'eden-runner/runner.py'
        self.config['runner'] = {'path': str(actual_runner), 'sha256': life.digest(actual_runner)}
        self.config['rows']['sha256'] = life.digest(self.rows)
        with self.assertRaisesRegex(life.Refused, 'Shared runner dry-run failed'):
            self.controller().up()
        self.assertFalse(self.ledger.claimed)
        self.assertFalse((Path(self.config['state_dir']) / 'lifecycle.json').exists())
        self.assertNotIn('claim', self.order)

    def test_single_up_writes_shared_runner_instances_and_mutes_before_load(self):
        value = self.controller().up()
        self.assertEqual(value['phase'], 'up')
        instances = json.loads((Path(self.config['state_dir']) / 'instances.json').read_text())
        self.assertEqual(set(instances), {'a'})
        self.assertTrue({'socket', 'console_log', 'settings', 'detector_log', 'diag_log'} <= set(instances['a']))
        methods = [method for _, method, _, _ in self.host.calls]
        self.assertLess(methods.index('settings.save'), methods.index('game.load'))
        self.assertLess(methods.index('settings.get'), methods.index('game.load'))
        self.assertEqual(life.private_config(instances['a']['profile'])['volume'], '0')
        self.assertFalse(self.ledger.released)
        self.assertEqual(life.inventory(self.source), self.before_source)
        self.assertNotIn('core.resolution_setup', [params.get('key') for _, _, params, _ in self.host.calls])

    def resolution_sets_before_load(self, calls):
        load = next(i for i, (_, method, _, _) in enumerate(calls) if method == 'game.load')
        before = calls[:load]
        return [params for _, method, params, _ in before
                if method == 'settings.set' and params.get('key') == 'core.resolution_setup']

    def test_xps_sets_half_resolution_and_reads_it_back_before_game_load(self):
        self.config['host'] = 'xps'
        self.pair()
        self.controller().up()
        for key in ('a', 'b'):
            calls = [call for call in self.host.calls if call[0] == key]
            sets = self.resolution_sets_before_load(calls)
            self.assertEqual(sets, [{'key': 'core.resolution_setup', 'value': 1}])
            load = next(i for i, (_, method, _, _) in enumerate(calls) if method == 'game.load')
            set_at = next(i for i, (_, method, params, _) in enumerate(calls)
                          if method == 'settings.set' and params.get('key') == 'core.resolution_setup')
            get_at = next(i for i, (_, method, params, _) in enumerate(calls)
                          if method == 'settings.get' and params.get('key') == 'core.resolution_setup')
            self.assertLess(set_at, get_at)
            self.assertLess(get_at, load)
            self.assertTrue(any(method == 'settings.save' for _, method, _, _ in calls[set_at + 1:get_at]))

    def test_surface_and_yoga_do_not_set_resolution(self):
        for host in ('surface', 'yoga'):
            self.config['host'] = host
            self.config['work_root'] = str(self.base / ('owned-' + host))
            self.config['state_dir'] = str(self.base / ('state-' + host))
            fake = FakeHost(self.order)
            life.Lifecycle(self.config, fake, FakeLedger(self.order)).up()
            self.assertIn('game.load', [method for _, method, _, _ in fake.calls])
            self.assertNotIn('core.resolution_setup', [params.get('key') for _, _, params, _ in fake.calls])

    def test_explicit_instance_setting_overrides_the_xps_default(self):
        self.config['host'] = 'xps'
        self.config['eden_settings'] = {'core.resolution_setup': 0}
        self.config['instances'][0]['eden_settings'] = {'core.resolution_setup': 3}
        self.controller().up()
        self.assertEqual(self.resolution_sets_before_load(self.host.calls),
                         [{'key': 'core.resolution_setup', 'value': 3}])
        self.assertEqual(life.eden_boot_settings({'host': 'xps'}, {}), {'core.resolution_setup': 1})
        self.assertEqual(life.eden_boot_settings({'host': 'surface'}, {}), {})
        self.assertEqual(life.eden_boot_settings({'host': 'yoga'}, {}), {})
        self.assertEqual(life.eden_boot_settings(
            {'host': 'xps', 'eden_settings': {'core.resolution_setup': 0}}, {}),
            {'core.resolution_setup': 0})

    def test_xps_resolution_readback_mismatch_refuses_game_load(self):
        self.config['host'] = 'xps'
        self.host.resolution_reply = {'value': 3}
        with self.assertRaisesRegex(life.Refused, 'readback'):
            self.controller().up()
        self.assertNotIn('game.load', [method for _, method, _, _ in self.host.calls])
        self.assertTrue(self.ledger.released)

    def test_defer_load_leaves_the_frontend_ready_for_room_setup(self):
        # Online pairs: Eden refuses to create a room while a game runs, so the rows load the game themselves.
        self.config['defer_load'] = True
        value = self.controller().up()
        self.assertEqual(value['phase'], 'up')
        methods = [method for _, method, _, _ in self.host.calls]
        self.assertNotIn('game.load', methods)
        self.assertIn('settings.save', methods)  # still muted before anything can load
        instances = json.loads((Path(self.config['state_dir']) / 'instances.json').read_text())
        self.assertEqual(instances['a']['game'], self.config['instances'][0]['game'])

    def test_down_restores_full_profile_then_separately_verifies_then_releases(self):
        controller = self.controller()
        controller.up()
        item = controller.state['instances'][0]
        profile = Path(item['profile'])
        (profile / 'new-run-output').write_text('new bytes')
        (profile / 'unknown/nested/data').write_text('changed')
        (profile / 'empty-directory').rmdir()
        expected = json.loads((controller.directory / 'a-snapshot.json').read_text())
        restore, verify = controller.restore, controller.verify
        controller.restore = lambda i: (self.order.append('restore'), restore(i))[-1]
        controller.verify = lambda i: (self.order.append('verify'), verify(i))[-1]
        result = controller.down(keep_state=True)  # the restored copies are what this test reads
        self.assertEqual(result['phase'], 'down')
        self.assertEqual(life.inventory(profile), expected)
        self.assertTrue((controller.directory / 'post-run-a/new-run-output').exists())
        self.assertTrue((controller.directory / 'a-restore.json').exists())
        self.assertTrue((controller.directory / 'a-verify.json').exists())
        self.assertLess(self.order.index('restore'), self.order.index('verify'))
        self.assertLess(self.order.index('verify'), self.order.index('release'))
        self.assertFalse(self.host.live)
        self.assertEqual(life.inventory(self.source), self.before_source)
        self.assertEqual(json.loads((Path(item['run']) / 'run.json').read_text())['state'], 'stopped')
        self.assertEqual((Path(item['run']) / 'memory.peak').read_text(), '1234\n')

    def run_writes(self, item):
        profile = Path(item['profile'])
        (profile / 'user/sdmc/ultimate/TrainingRooms/rooms.log').write_text('run log line\n')
        (profile / 'unknown/nested/data').write_text('changed by the run')
        (profile / 'user/cache/shader/01006a800016e000').mkdir(parents=True)
        (profile / 'user/cache/shader/01006a800016e000/vulkan.bin').write_bytes(b'rebuilt by Eden')

    def test_down_saves_what_the_run_wrote_then_prunes_the_run_copies(self):
        c = self.controller()
        c.up()
        item = c.state['instances'][0]
        self.run_writes(item)
        out = self.base / 'run/out'
        result = c.down(evidence_dir=str(out))
        self.assertEqual(result['phase'], 'down')
        self.assertTrue(self.ledger.released)
        # The run's own files are in out/ first: new, changed and the rewritten config. Pinned inputs and the
        # snapshot's plugin moved aside are known bytes; the shader cache is listed, not copied.
        manifest = json.loads((out / 'post-run-a.json').read_text())
        self.assertEqual(sorted(entry['path'] for entry in manifest['copied']),
                         ['unknown/nested/data', life.CONFIG, 'user/sdmc/ultimate/TrainingRooms/rooms.log'])
        self.assertEqual(manifest['listed'], [{'path': 'user/cache/shader/01006a800016e000/vulkan.bin', 'size': 15}])
        self.assertEqual(manifest['known_content'], 3)
        self.assertEqual(manifest['gone'], [life.PLUGINS + '/old.nro'])
        self.assertEqual((out / 'post-run-a/user/sdmc/ultimate/TrainingRooms/rooms.log').read_text(), 'run log line\n')
        self.assertEqual((out / 'post-run-a/unknown/nested/data').read_text(), 'changed by the run')
        # Then the copies are gone: the work_root profile, the snapshot and the post-run profile.
        self.assertFalse(Path(self.config['work_root']).exists())
        self.assertFalse((c.directory / 'snapshot-a').exists())
        self.assertFalse((c.directory / 'post-run-a').exists())
        # The journal, actions, receipts and the run's logs stay; so do the source profile and its game content.
        for name in ('lifecycle.json', 'actions.jsonl', 'a-snapshot.json', 'a-restore.json', 'a-verify.json',
                     'run-a/console.log', 'run-a/run.json', 'run-a/memory.peak'):
            self.assertTrue((c.directory / name).is_file(), name)
        self.assertEqual(life.inventory(self.source), self.before_source)
        prune = self.state()['prune']
        self.assertEqual(result['prune'], prune)
        self.assertEqual((prune['copied_files'], prune['listed_files']), (3, 1))
        # Game content is linked to the source, so it counts in bytes but not in unique_bytes.
        self.assertGreater(prune['bytes'], prune['unique_bytes'])
        self.assertGreater(prune['unique_bytes'], 0)
        self.assertTrue(prune['done'] and prune['disk_free_before'] and prune['disk_free_after'])
        self.assertEqual((prune['skipped'], prune['left']), ([], []))
        again = c.down()
        self.assertTrue(again['already_down'])
        self.assertEqual(again['prune'], prune)

    def test_keep_state_keeps_the_copies_until_a_down_without_it(self):
        c = self.controller()
        c.up()
        self.run_writes(c.state['instances'][0])
        result = c.down(keep_state=True)
        self.assertIn('kept', result['prune'])
        self.assertTrue((Path(self.config['work_root']) / 'profiles/profile-a').is_dir())
        self.assertTrue((c.directory / 'snapshot-a').is_dir() and (c.directory / 'post-run-a').is_dir())
        self.assertFalse((c.directory / 'evidence').exists())
        later = self.controller().down()  # a fresh process, without the flag
        self.assertTrue(later['already_down'] and later['prune']['done'])
        self.assertNotIn('kept', later['prune'])
        self.assertFalse(Path(self.config['work_root']).exists())
        self.assertEqual((c.directory / 'evidence/post-run-a/unknown/nested/data').read_text(), 'changed by the run')
        self.assertEqual(self.order.count('release'), 1)
        path = self.base / 'config.json'
        path.write_text(json.dumps(self.config))
        with patch('sys.stderr', new=io.StringIO()) as error:
            self.assertEqual(life.main(['status', str(path), '--keep-state']), 1)
        self.assertIn('down options', error.getvalue())

    def test_failed_down_prunes_nothing_and_the_clean_retry_does(self):
        c = self.controller()
        c.up()
        item = c.state['instances'][0]
        self.run_writes(item)
        self.host.leftover = True
        with self.assertRaisesRegex(life.Refused, 'Cleanup incomplete'):
            c.down()
        self.assertNotIn('prune', self.state())
        self.assertEqual((Path(item['profile']) / 'unknown/nested/data').read_text(), 'changed by the run')
        self.assertTrue((c.directory / 'snapshot-a').is_dir())
        self.assertFalse((c.directory / 'evidence').exists())
        self.host.leftover = False
        result = c.down()
        self.assertTrue(result['prune']['done'])
        self.assertFalse(Path(self.config['work_root']).exists())
        self.assertEqual((c.directory / 'evidence/post-run-a/unknown/nested/data').read_text(), 'changed by the run')

    def test_prune_failure_keeps_the_release_and_the_next_down_finishes_without_copying_again(self):
        c = self.controller()
        c.up()
        self.run_writes(c.state['instances'][0])
        with patch.object(life, 'remove_tree', side_effect=OSError('synthetic busy file')):
            result = c.down()
        self.assertEqual(result['phase'], 'down')
        self.assertTrue(self.ledger.released)
        self.assertIn('synthetic busy file', result['prune']['error'])
        self.assertTrue(result['prune']['evidence_saved'])
        with patch.object(life.shutil, 'copy2', side_effect=AssertionError('evidence copied twice')):
            again = self.controller().down()
        self.assertTrue(again['prune']['done'])
        self.assertNotIn('error', again['prune'])
        self.assertFalse(Path(self.config['work_root']).exists())
        self.assertTrue((c.directory / 'evidence/post-run-a/unknown/nested/data').is_file())

    def test_evidence_folder_inside_a_pruned_tree_is_refused_before_anything_is_deleted(self):
        c = self.controller()
        c.up()
        for folder in (c.directory, c.directory / 'post-run-a/out', Path(self.config['work_root']) / 'out'):
            with self.subTest(folder=folder):
                result = c.down(evidence_dir=str(folder))
                self.assertIn('inside a pruned tree', result['prune']['error'])
                self.assertTrue((c.directory / 'post-run-a').is_dir() and (c.directory / 'snapshot-a').is_dir())
                self.assertTrue(Path(self.config['work_root']).is_dir())

    def test_prune_never_follows_a_link_out_of_its_tree(self):
        tree = self.base / 'tree'
        (tree / 'd').mkdir(parents=True)
        (tree / 'd/file').write_text('mine')
        (tree / 'd/link').symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(life.Refused, 'Link inside a pruned tree'):
            life.remove_tree(tree, [])
        self.assertTrue((tree / 'd/file').is_file())
        self.assertEqual(life.inventory(self.source), self.before_source)

    def test_disk_floor_defaults_to_60_gb_and_is_configurable(self):
        self.assertEqual(life.MIN_DISK, 60 * 10 ** 9)
        self.host.disk = 59 * 10 ** 9
        with self.assertRaisesRegex(life.Refused, 'Free disk below 60 GB'):
            self.controller().up()
        self.assertFalse(self.ledger.claimed)
        self.config['disk_floor_bytes'] = 50 * 10 ** 9
        self.config['state_dir'] += '-floor'
        self.assertEqual(self.controller().up()['phase'], 'up')
        self.host.disk = 49 * 10 ** 9
        report = self.controller().resource_report(self.host.resources(self.base))
        self.assertEqual(report[0], ['Free disk below 50 GB'])
        for bad in (0, -1, 1.5, True, '60'):
            config = deepcopy(self.config)
            config['disk_floor_bytes'] = bad
            with self.assertRaisesRegex(life.Refused, 'disk_floor_bytes must be a positive integer'):
                life.Lifecycle(config, self.host, self.ledger)

    def test_pair_uses_one_claim_and_fresh_admission_before_each_launch(self):
        self.pair()
        c = self.controller()
        self.assertEqual(set(c.up()['instances']), {'a', 'b'})
        self.assertEqual(self.order.count('claim'), 1)
        self.assertEqual(len(self.host.admissions), 3)
        self.assertEqual(len(self.host.admissions[-1]), 1)
        c.down()
        self.assertEqual(self.order.count('release'), 1)
        self.assertTrue(all(x['verified'] for x in c.state['instances']))

    def test_main_stop_failure_retains_claim_and_never_restores_or_releases(self):
        c = self.controller()
        c.up()
        item = c.state['instances'][0]
        main = item['owned_pids'][0]
        native, now = life.Host(), [0.0]
        native.unit = lambda _: {'MainPID': str(main['pid']), 'ActiveState': 'active'}
        native.owned = lambda _: [main]
        native.identity = lambda _: main
        native.clock = lambda: now[0]
        native.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        self.host.force_stop = native.force_stop
        with patch.object(native, 'run') as run, patch.object(os, 'pidfd_open', return_value=99), \
                patch.object(os, 'close'), patch.object(life.signal, 'pidfd_send_signal') as send:
            with self.assertRaisesRegex(life.Refused, 'Cleanup incomplete'):
                c.down()
            run.assert_not_called()  # No group stop while main survives even SIGKILL.
            self.assertEqual([call.args[1] for call in send.call_args_list], [life.signal.SIGTERM, life.signal.SIGKILL])
        self.assertFalse(self.ledger.released)
        self.assertEqual(self.state()['phase'], 'cleanup-failed')
        self.assertFalse((c.directory / 'a-restore.json').exists())
        self.assertFalse((c.directory / 'a-verify.json').exists())

    def test_ordered_native_stop_precedes_full_restore_separate_verify_and_release(self):
        c = self.controller()
        c.up()
        item = c.state['instances'][0]
        main = item['owned_pids'][0]
        mount = {'pid': 1, 'start': 'synthetic-fuse', 'boot': 'fake'}
        self.host.live[item['unit']].insert(0, mount)
        native = life.Host()
        native.owned = self.host.owned
        native.identity = lambda pid: next((p for p in self.host.live.get(item['unit'], []) if p['pid'] == pid), None)
        native.unit = lambda _: {'MainPID': str(main['pid']), 'ActiveState': 'active', 'KillMode': 'control-group'}
        def send(fd, sig):
            self.assertEqual((fd, sig), (main['pid'] + 1000, life.signal.SIGTERM))
            self.assertIn(mount, self.host.live[item['unit']])
            self.order.append('main-gone')
            self.host.live[item['unit']].remove(main)
        def group_stop(argv, cap=30):
            self.assertEqual(argv, ['systemctl', '--user', 'stop', item['unit']])
            self.assertNotIn(main, self.host.live[item['unit']])
            self.order.append('backing-group-gone')
            self.host.live.pop(item['unit'])
            return ''
        native.run = group_stop
        self.host.force_stop = native.force_stop
        restore, verify = c.restore, c.verify
        c.restore = lambda i: (self.order.append('restore'), restore(i))[-1]
        c.verify = lambda i: (self.order.append('verify'), verify(i))[-1]
        with patch.object(os, 'pidfd_open', side_effect=lambda pid: pid + 1000), \
                patch.object(os, 'close'), patch.object(life.signal, 'pidfd_send_signal', side_effect=send):
            self.assertEqual(c.down()['phase'], 'down')
        phases = [self.order.index(name) for name in ('main-gone', 'backing-group-gone', 'restore', 'verify', 'release')]
        self.assertEqual(phases, sorted(phases))
        self.assertTrue(c.state['instances'][0]['verified'] and self.ledger.released)
        self.assertEqual(life.inventory(self.source), self.before_source)

    def test_pair_requires_explicit_exception(self):
        self.pair()
        self.config['pair_authorized'] = False
        with self.assertRaisesRegex(life.Refused, 'Pair exception'):
            self.controller()
        self.assertFalse(Path(self.config['work_root']).exists())

    def test_immutable_game_content_linked_mutable_files_private(self):
        self.controller().up()
        profile = Path(self.state()['instances'][0]['profile'])
        for prefix in life.READ_ONLY:
            src = next(p for p in (self.source / prefix).rglob('*') if p.is_file())
            self.assertEqual(src.stat().st_ino, (profile / src.relative_to(self.source)).stat().st_ino)
        for rel in (life.CONFIG, 'user/nand/user/save/record.bin', 'unknown/nested/data'):
            self.assertNotEqual((self.source / rel).stat().st_ino, (profile / rel).stat().st_ino)

    def test_bad_memory_disk_or_foreign_process_refuses_before_claim_or_profile(self):
        for field, value in (('available', life.MIN_MEMORY - 1), ('disk', life.MIN_DISK - 1),
                             ('foreign', [{'pid': 999, 'name': 'eden'}])):
            with self.subTest(field=field):
                config = deepcopy(self.config)
                config['state_dir'] += '-' + field
                host, ledger = FakeHost([]), FakeLedger([])
                setattr(host, field, value)
                with self.assertRaises(life.Refused):
                    life.Lifecycle(config, host, ledger).up()
                self.assertFalse(ledger.claimed)
                self.assertFalse(Path(config['work_root']).exists())

    def test_foreign_claim_refuses_before_any_clone(self):
        self.ledger.refuse = True
        with self.assertRaisesRegex(life.Refused, 'foreign claim'):
            self.controller().up()
        self.assertFalse(Path(self.config['work_root']).exists())
        self.assertFalse(self.host.calls)

    def test_old_claim_no_foreign_process_is_skipped_and_reported(self):
        self.config['stale_claim_hours'] = 24
        lines = ['2026-10-31T12:00:00.000+00:00 CLAIMED: Codex01abc/project on Surface old',
                 '2026-10-30 12:00Z CLAIMED: Claude old/stream host=surface old']
        self.ledger.open_text = '\n'.join(lines)
        c = self.controller()
        with patch.object(life, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
            value = c.up()
        receipts = value['stale_claim_skipped']
        self.assertEqual([r['line'] for r in receipts], lines)
        self.assertEqual([r['age_hours'] for r in receipts], [72, 96])
        actions = [json.loads(line) for line in (c.directory / 'actions.jsonl').read_text().splitlines()]
        self.assertEqual([a['value'] for a in actions if a['kind'] == 'stale_claim_skipped'], receipts)
        self.assertEqual(self.state()['stale_claim_skipped'], receipts)
        self.assertEqual(self.ledger.open_text, '\n'.join(lines))
        self.assertEqual(c.down()['phase'], 'down')
        self.assertEqual(self.ledger.open_text, '\n'.join(lines))
        self.assertEqual(self.ledger_path.read_text(), '# TEMPORARY test data; not OWNER-COORDINATION\n')

    def test_old_claim_with_foreign_process_still_blocks_before_claim(self):
        self.config['stale_claim_hours'] = 24
        self.ledger.open_text = '2026-10-01 12:00Z CLAIMED: OpenCode foreign/stream on Surface old'
        self.host.foreign = [{'pid': 999, 'name': 'rustc'}]
        with self.assertRaisesRegex(life.Refused, 'Compiler already active'):
            self.controller().up()
        self.assertNotIn('claim', self.order)
        self.assertFalse(self.ledger.claimed or self.ledger.released)
        self.assertFalse(Path(self.config['work_root']).exists())

    def test_a_second_eden_is_admitted_on_a_two_instance_host(self):
        # Operator 2026-11-04: the Surface and the Yoga run two emulators. Launcher/helper processes are not instances.
        Path(self.config['state_dir']).mkdir(parents=True, exist_ok=True)
        self.host.max_eden = 2
        self.host.foreign = [{'pid': 501, 'name': 'eden'}, {'pid': 502, 'name': 'eden.appimage'},
                             {'pid': 503, 'name': 'eden-lan-runtim'}]
        self.controller().admission()

    # Operator 2026-11-10: "if there's headroom it should be allowed". Compiles at nice >= 10 admit an Up when
    # MemAvailable covers the instance floor plus the compile reserve; un-niced compiles and perf_sensitive configs refuse.
    def niced(self):
        return [{'pid': 901, 'name': 'cargo', 'nice': 19}, {'pid': 902, 'name': 'rustc', 'nice': 19},
                {'pid': 903, 'name': 'cc1', 'nice': 10}]

    def test_niced_compile_with_headroom_admits_with_the_reserve_on_the_floor(self):
        # The default: the Surface's two-job TrainingRooms build/test peak (2.43 GiB) plus a margin (README).
        self.assertEqual((life.COMPILE_RESERVE, life.COMPILE_NICE_MIN), (4 * life.GIB, 10))
        Path(self.config['state_dir']).mkdir(parents=True, exist_ok=True)
        self.host.foreign = self.niced()
        self.host.available = life.MIN_MEMORY + life.COMPILE_RESERVE + life.GIB
        self.controller().admission()
        report = self.controller().preflight()
        self.assertTrue(report['ok'], report['message'])
        compiler = next(line for line in report['lines'] if line.startswith(('OK compiler', 'FAIL compiler')))
        self.assertTrue(compiler.startswith('OK compiler: 3 running, all at nice >= 10 (lowest 10); compile reserve '
                                            f'{life.COMPILE_RESERVE} bytes'), compiler)
        memory = next(line for line in report['lines'] if line.startswith(('OK memory', 'FAIL memory')))
        self.assertEqual(memory, f'OK memory: {self.host.available} bytes available, floor {life.MIN_MEMORY} + compile '
                                 f'reserve {life.COMPILE_RESERVE}; headroom {life.GIB}')
        check = next(c for c in report['checks'] if c['name'] == 'memory')
        self.assertEqual((check['reserve'], check['headroom']), (life.COMPILE_RESERVE, life.GIB))
        # The whole Up goes through, claim and launch included.
        self.assertEqual(self.controller().up()['phase'], 'up')

    def test_unniced_compile_still_refuses(self):
        Path(self.config['state_dir']).mkdir(parents=True, exist_ok=True)
        self.host.available = 30 * life.GIB
        for unniced in ({'pid': 904, 'name': 'rustc', 'nice': 0}, {'pid': 905, 'name': 'cc1', 'nice': 9},
                        {'pid': 906, 'name': 'cargo'}):   # no nice read (the Windows host): un-niced
            self.host.foreign = self.niced() + [unniced]
            with self.assertRaisesRegex(life.Refused, 'Compiler already active on host: 1 of 4 below nice 10'):
                self.controller().admission()
            report = self.controller().preflight()
            self.assertFalse(report['ok'])
            self.assertTrue(any(line.startswith('FAIL compiler: Compiler already active on host: 1 of 4 below nice 10 '
                                                '(build under nice -n 19); seen: cargo 901 nice 19')
                                for line in report['lines']), report['lines'])
        self.assertFalse(self.ledger.claimed)

    def test_niced_compile_without_headroom_refuses(self):
        Path(self.config['state_dir']).mkdir(parents=True, exist_ok=True)
        self.host.foreign = self.niced()
        self.host.available = life.MIN_MEMORY + life.COMPILE_RESERVE - 1
        with self.assertRaisesRegex(life.Refused, 'below the floor .* plus the compile reserve .* while niced compilers run'):
            self.controller().admission()
        # Below the plain floor the plain message stands.
        self.host.available = life.MIN_MEMORY - 1
        with self.assertRaisesRegex(life.Refused, 'MemAvailable below 10 GiB'):
            self.controller().admission()
        # A second instance: 9 GiB plus the reserve.
        self.host.foreign = self.niced() + [{'pid': 501, 'name': 'eden'}]
        self.host.max_eden = 2
        self.host.available = life.MIN_MEMORY_SECOND + life.COMPILE_RESERVE
        self.controller().admission()
        self.host.available -= 1
        with self.assertRaisesRegex(life.Refused, f'below the floor {life.MIN_MEMORY_SECOND} plus the compile reserve'):
            self.controller().admission()
        # compile_reserve_bytes overrides the default.
        self.host.foreign = self.niced()
        self.config['compile_reserve_bytes'] = life.GIB
        self.host.available = life.MIN_MEMORY + life.GIB
        self.controller().admission()
        self.host.available -= 1
        with self.assertRaisesRegex(life.Refused, f'compile reserve {life.GIB} '):
            self.controller().admission()
        for bad in (0, -1, 1.5, True, '1'):
            config = deepcopy(self.config)
            config['compile_reserve_bytes'] = bad
            with self.assertRaisesRegex(life.Refused, 'compile_reserve_bytes must be a positive integer'):
                life.Lifecycle(config, self.host, self.ledger)

    def test_perf_sensitive_refuses_any_compiler(self):
        self.config['perf_sensitive'] = True
        self.host.foreign = self.niced()
        self.host.available = 30 * life.GIB
        with self.assertRaisesRegex(life.Refused, r'Compiler already active on host \(perf_sensitive'):
            self.controller().up()
        self.assertFalse(self.ledger.claimed)
        self.assertFalse(Path(self.config['work_root']).exists())
        report = self.controller().preflight()
        self.assertIn('perf_sensitive: no compiler may run', report['message'])
        check = next(c for c in report['checks'] if c['name'] == 'compiler')
        self.assertEqual((check['ok'], check['perf_sensitive']), (False, True))
        # With no compiler a perf_sensitive run is admitted as before.
        self.host.foreign = []
        self.assertTrue(self.controller().preflight()['ok'])
        for bad in ('yes', 1, None):
            config = deepcopy(self.config)
            config['perf_sensitive'] = bad
            with self.assertRaisesRegex(life.Refused, 'perf_sensitive must be true or false'):
                life.Lifecycle(config, self.host, self.ledger)

    def test_no_compiler_keeps_the_plain_floor(self):
        Path(self.config['state_dir']).mkdir(parents=True, exist_ok=True)
        self.host.foreign = []
        self.host.available = life.MIN_MEMORY
        self.controller().admission()
        report = self.controller().preflight()
        self.assertIn('OK compiler: none running', report['lines'])
        self.assertIn(f'OK memory: {life.MIN_MEMORY} bytes available, floor {life.MIN_MEMORY}', report['lines'])
        check = next(c for c in report['checks'] if c['name'] == 'memory')
        self.assertNotIn('reserve', check)
        self.host.available -= 1
        with self.assertRaisesRegex(life.Refused, 'MemAvailable below 10 GiB'):
            self.controller().admission()

    def test_stat_nice_reads_field_19_past_the_comm(self):
        line = '4242 (rustc) S 1 4242 4242 0 -1 4194560 100 0 0 0 5 1 0 0 39 19 1 0 123 456 789 0'
        self.assertEqual(life.stat_nice(line), 19)
        self.assertEqual(life.stat_nice(line.replace('(rustc)', '(a b) c)')), 19)
        self.assertEqual(life.stat_nice(line.replace(' 39 19 ', ' 20 0 ')), 0)
        self.assertEqual(life.stat_nice(line.replace(' 39 19 ', ' 30 10 ')), 10)
        self.assertEqual(life.stat_nice(Path('/proc/self/stat').read_text()), os.nice(0))

    def test_a_second_eden_needs_9_gib_and_the_first_10(self):
        # Operator 2026-11-04: lower the free-memory check to 9 GiB for a second instance (each is capped at 8 GiB).
        Path(self.config['state_dir']).mkdir(parents=True, exist_ok=True)
        self.host.max_eden = 2
        self.host.available = int(9.5 * life.GIB)
        self.host.foreign = []
        with self.assertRaisesRegex(life.Refused, 'MemAvailable below 10 GiB'):
            self.controller().admission()
        self.host.foreign = [{'pid': 501, 'name': 'eden'}]
        self.controller().admission()
        self.host.available = int(8.5 * life.GIB)
        with self.assertRaisesRegex(life.Refused, 'MemAvailable below 9 GiB'):
            self.controller().admission()

    def test_pair_up_counts_its_first_instance_and_admits_the_second_at_9_5_gib(self):
        self.pair()
        self.host.available = 12 * life.GIB
        real = self.host.resources

        def resources(path, owned=()):
            if owned:
                self.host.available = int(9.5 * life.GIB)
            return real(path, owned)

        self.host.resources = resources
        value = self.controller().up()
        self.assertEqual(set(value['instances']), {'a', 'b'})
        counts = [json.loads(line)['value'].get('owned_running')
                  for line in (Path(self.config['state_dir']) / 'actions.jsonl').read_text().splitlines()
                  if '"kind": "admission"' in line]
        self.assertEqual(counts, [0, 0, 1])

    def test_pair_up_refuses_its_second_instance_at_8_5_gib(self):
        self.pair()
        self.host.available = 12 * life.GIB
        real = self.host.resources

        def resources(path, owned=()):
            if owned:
                self.host.available = int(8.5 * life.GIB)
            return real(path, owned)

        self.host.resources = resources
        with self.assertRaisesRegex(life.Refused, 'MemAvailable below 9 GiB'):
            self.controller().up()
        self.assertIn('launch-a', self.order)
        self.assertNotIn('launch-b', self.order)

    def test_pair_up_refuses_when_one_foreign_and_one_owned_fill_the_slots(self):
        self.pair()
        self.host.available = 16 * life.GIB
        self.host.foreign = [{'pid': 501, 'name': 'eden'}]
        with self.assertRaisesRegex(life.Refused, r'2 Eden instance\(s\) already active on host \(limit 2\)'):
            self.controller().up()
        self.assertIn('launch-a', self.order)
        self.assertNotIn('launch-b', self.order)

    def test_a_third_eden_is_refused_on_a_two_instance_host(self):
        Path(self.config['state_dir']).mkdir(parents=True, exist_ok=True)
        self.host.max_eden = 2
        self.host.foreign = [{'pid': 501, 'name': 'eden'}, {'pid': 601, 'name': 'eden'}]
        with self.assertRaisesRegex(life.Refused, r'2 Eden instance\(s\) already active on host \(limit 2\)'):
            self.controller().admission()

    def test_a_one_instance_host_refuses_a_second_eden(self):
        Path(self.config['state_dir']).mkdir(parents=True, exist_ok=True)
        self.host.max_eden = 1
        self.host.foreign = [{'pid': 501, 'name': 'eden.exe'}]
        with self.assertRaisesRegex(life.Refused, r'1 Eden instance\(s\) already active on host \(limit 1\)'):
            self.controller().admission()
        self.assertFalse(self.host.calls)

    LIFECYCLE_CLAIM = ('2026-11-04T16:45:00.000000+00:00 CLAIMED: OpenCode lane/yoga host=surface '
                       'lifecycle=run-a Eden lifecycle admitted')

    def test_a_second_lifecycle_claim_is_admitted_on_a_two_instance_host(self):
        # Operator 2026-11-04: two Edens per Linux host; the ledger must not undo the process limit.
        policy = life.claim_admission(self.LIFECYCLE_CLAIM, 'surface', 'OpenCode lane/yoga',
                                      admission={'max_eden': 2, 'foreign_processes': [{'pid': 501, 'name': 'eden'}]})
        self.assertEqual(policy['blocking'], [])
        self.assertEqual(policy['concurrent_claims'], [self.LIFECYCLE_CLAIM])

    def test_a_third_lifecycle_claim_is_refused_on_a_two_instance_host(self):
        second = self.LIFECYCLE_CLAIM.replace('lifecycle=run-a', 'lifecycle=run-b')
        text = self.LIFECYCLE_CLAIM + '\n' + second
        policy = life.claim_admission(text, 'surface', 'OpenCode lane/yoga', admission={'max_eden': 2})
        self.assertEqual(policy['blocking'], [self.LIFECYCLE_CLAIM, second])
        self.assertNotIn('concurrent_claims', policy)

    def test_a_second_lifecycle_claim_is_refused_on_a_one_instance_host(self):
        for admission in ({'max_eden': 1}, None):
            policy = life.claim_admission(self.LIFECYCLE_CLAIM, 'surface', 'OpenCode lane/yoga', admission=admission)
            self.assertEqual(policy['blocking'], [self.LIFECYCLE_CLAIM])

    def test_a_manual_claim_still_blocks_on_a_two_instance_host(self):
        manual = '2026-11-04 16:00Z CLAIMED: OpenCode foreign/stream on Surface display :78'
        other_host = self.LIFECYCLE_CLAIM.replace('host=surface', 'host=yoga')
        policy = life.claim_admission(manual + '\n' + self.LIFECYCLE_CLAIM, 'surface', 'OpenCode lane/yoga',
                                      admission={'max_eden': 2})
        self.assertEqual(policy['blocking'], [manual])
        policy = life.claim_admission(other_host, 'yoga', 'OpenCode lane/yoga', admission={'max_eden': 2})
        self.assertEqual(policy['blocking'], [])

    def test_admission_reports_the_host_instance_limit(self):
        Path(self.config['state_dir']).mkdir(parents=True, exist_ok=True)
        self.host.max_eden = 2
        self.host.foreign = []
        self.assertEqual(self.controller().admission()['max_eden'], 2)

    def test_young_claim_still_blocks_with_stale_option(self):
        self.config['stale_claim_hours'] = 24
        self.ledger.open_text = '2026-11-03 11:00Z CLAIMED: OpenCode foreign/stream on Surface young'
        with patch.object(life, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
            with self.assertRaisesRegex(life.Refused, 'Open host claim'):
                self.controller().up()
        self.assertFalse(self.ledger.claimed or self.ledger.released)
        self.assertFalse(Path(self.config['work_root']).exists())
        self.assertFalse(self.host.calls)

    def test_no_stale_field_preserves_strict_old_claim_blocking(self):
        self.ledger.open_text = '2026-10-01 12:00Z CLAIMED: OpenCode foreign/stream on Surface old'
        c = self.controller()
        self.assertNotIn('stale_claim_hours', c.c)
        with self.assertRaisesRegex(life.Refused, 'Open host claim'):
            c.up()
        self.assertFalse(self.ledger.claimed or self.ledger.released)
        self.assertFalse(Path(self.config['work_root']).exists())
        self.assertFalse(self.host.calls)

    def test_stale_option_rejects_invalid_values_without_any_action(self):
        for value in (None, True, False, 0, -1, '24', float('nan'), float('inf')):
            with self.subTest(value=value):
                self.config['stale_claim_hours'] = value
                with self.assertRaisesRegex(life.Refused, 'stale_claim_hours'):
                    self.controller()
        self.assertFalse(self.order)
        self.assertFalse(Path(self.config['state_dir']).exists())

    def test_pins_checked_before_claim(self):
        self.plugin.write_bytes(b'changed')
        with self.assertRaisesRegex(life.Refused, 'Pinned input mismatch'):
            self.controller().up()
        self.assertFalse(self.ledger.claimed)

    def test_broken_runner_dryrun_refuses_before_claim(self):
        with self.assertRaisesRegex(life.Refused, 'dry-run failed'):
            self.controller(plan_check=lambda: {'result': 'BROKEN', 'rows': 1, 'errors': ['missing gate']}).up()
        self.assertFalse(self.ledger.claimed)

    def test_no_rows_is_not_ready(self):
        with self.assertRaisesRegex(life.Refused, 'dry-run failed'):
            self.controller(plan_check=lambda: {'result': 'READY', 'rows': 0, 'errors': []}).up()

    def test_check_rows_uses_up_check_when_the_runner_has_it(self):
        self.runner.write_text(
            'def dry_run(path):\n return {"result":"READY","rows":1,"errors":[]}\n'
            'def up_check(path, config=None):\n return {"result":"BROKEN","rows":1,"errors":["no target"]}\n')
        self.config['runner']['sha256'] = life.digest(self.runner)
        self.assertEqual(self.controller().check_rows()['errors'], ['no target'])

    def test_real_shared_runner_dryrun_integration_is_offline(self):
        shared = Path(__file__).resolve().parents[1] / 'eden-runner/runner.py'
        self.config['runner'] = {'path': str(shared), 'sha256': life.digest(shared)}
        tree = self.base / 'target'
        tree.mkdir()
        subprocess.run(['git', '-C', str(tree), 'init', '-q'], check=True)
        (tree / 'fixture').write_text('target')
        subprocess.run(['git', '-C', str(tree), 'add', 'fixture'], check=True)
        subprocess.run(['git', '-C', str(tree), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-qm', 'fixture'], check=True)
        commit = subprocess.run(['git', '-C', str(tree), 'rev-parse', 'HEAD'], check=True,
                                capture_output=True, text=True).stdout.strip()
        self.plugin.write_bytes(b'fake candidate ' + commit[:8].encode('ascii'))
        self.config['instances'][0]['plugins'][0]['sha256'] = life.digest(self.plugin)
        self.rows.write_text(json.dumps({'target': {'plugin': {'tree': str(tree), 'commit': commit,
                                                               'nro_sha256': life.digest(self.plugin)}},
            'start': 'menu', 'rows': [{'id': 'observe', 'start': 'menu', 'end': 'menu',
            'steps': [{'gate': 'settings'}]}], 'gates': {'settings': {'kind': 'json', 'source': 'settings', 'expect': {'x': 1}, 'cap': 1, 'effect': True}}}))
        self.config['rows']['sha256'] = life.digest(self.rows)
        self.assertEqual(self.controller().up()['phase'], 'up')

    def test_second_launch_failure_stops_both_and_restores_both(self):
        self.pair()
        self.host.fail_launch = 'b'
        c = self.controller()
        with self.assertRaisesRegex(RuntimeError, 'partial-launch'):
            c.up()
        self.assertFalse(self.host.live)
        self.assertTrue(self.ledger.released)
        self.assertEqual(c.state['phase'], 'down')
        self.assertTrue(all(x['verified'] for x in c.state['instances']))

    def test_socket_timeout_rolls_back_with_no_game_load(self):
        self.host.socket = False
        c = self.controller()
        with self.assertRaisesRegex(life.Refused, 'control socket'):
            c.up()
        self.assertTrue(self.ledger.released)
        self.assertFalse(self.host.live)
        self.assertNotIn('game.load', [m for _, m, _, _ in self.host.calls])

    def test_nonzero_cli_mute_refuses_game_load(self):
        self.host.mute = {'value': 0, 'effective': 75}
        with self.assertRaisesRegex(life.Refused, 'mute readback'):
            self.controller().up()
        self.assertNotIn('game.load', [m for _, m, _, _ in self.host.calls])
        self.assertTrue(self.ledger.released)

    def test_terminal_load_failure_is_not_accepted_as_load_success(self):
        self.host.job_mode = 'bad'
        with self.assertRaisesRegex(life.Refused, 'Terminal job lacks'):
            self.controller().up()
        self.assertTrue(self.ledger.released)

    def test_loading_timeout_is_bounded_and_cleanup_still_happens(self):
        self.host.loading = False
        with self.assertRaisesRegex(life.Refused, 'game loading complete'):
            self.controller().up()
        self.assertLess(self.host.now, 1)
        self.assertTrue(self.ledger.released)

    def test_game_stop_timeout_preserved_but_forced_cleanup_and_release_finish(self):
        c = self.controller()
        c.up()
        self.host.stop_mode = 'timeout'
        result = c.down()
        self.assertEqual(result['phase'], 'down')
        self.assertTrue(result['game_stop_errors'])
        self.assertLess(self.host.now, 1)
        self.assertTrue(self.ledger.released)

    def test_leftover_owned_pid_prevents_restore_and_release(self):
        c = self.controller()
        c.up()
        self.host.leftover = True
        with self.assertRaisesRegex(life.Refused, 'Cleanup incomplete'):
            c.down()
        self.assertFalse(self.ledger.released)
        self.assertFalse((c.directory / 'a-restore.json').exists())
        self.assertEqual(c.state['phase'], 'cleanup-failed')

    def test_tampered_snapshot_prevents_release(self):
        c = self.controller()
        c.up()
        (Path(c.state['instances'][0]['snapshot']) / 'unknown/nested/data').write_bytes(b'tampered')
        with self.assertRaisesRegex(life.Refused, 'Cleanup incomplete'):
            c.down()
        self.assertFalse(self.ledger.released)

    def test_separate_verification_failure_retains_claim_and_can_retry(self):
        c = self.controller()
        c.up()
        with patch.object(c, 'verify', side_effect=life.Refused('synthetic separate verify failure')):
            with self.assertRaisesRegex(life.Refused, 'Cleanup incomplete'):
                c.down()
        self.assertFalse(self.ledger.released)
        self.assertTrue(c.state['instances'][0]['restored'])
        c.down()
        self.assertTrue(self.ledger.released)

    def test_fresh_process_controller_can_down_existing_journal(self):
        self.controller().up()
        self.assertEqual(self.controller().down()['phase'], 'down')

    def test_staged_targets_survive_validation_and_journal_reopen(self):
        self.config['staged_targets'] = ['a' * 40, 'b' * 40]
        self.assertEqual(life.validate(self.config)['staged_targets'], self.config['staged_targets'])
        self.controller().up()
        self.assertEqual(self.state()['config']['staged_targets'], self.config['staged_targets'])
        self.assertEqual(self.controller().down()['phase'], 'down')

    def test_down_is_idempotent_and_no_second_release(self):
        c = self.controller()
        c.up()
        c.down()
        self.assertTrue(c.down()['already_down'])
        self.assertEqual(self.order.count('release'), 1)

    def test_up_never_blindly_restarts_existing_run(self):
        c = self.controller()
        c.up()
        with self.assertRaisesRegex(life.Refused, 'already journalled'):
            self.controller().up()
        self.assertEqual(self.order.count('claim'), 1)

    def test_mismatched_owner_or_config_cannot_down(self):
        self.controller().up()
        self.config['owner'] = 'OpenCode different/owner'
        with self.assertRaisesRegex(life.Refused, 'Owner/config differs'):
            self.controller().down()
        self.assertFalse(self.ledger.released)

    def test_tampered_journal_cannot_stop_a_foreign_unit(self):
        c = self.controller()
        c.up()
        value = self.state()
        value['instances'][0]['unit'] = 'foreign-owner.service'
        life.save(c.journal, value)
        previous = list(self.order)
        with self.assertRaisesRegex(life.Refused, 'Foreign journal unit'):
            self.controller().down()
        self.assertEqual(self.order, previous)
        self.assertFalse(self.ledger.released)

    def test_status_absent_does_not_create_state_or_profile(self):
        self.assertEqual(self.controller().status()['phase'], 'absent')
        self.assertFalse(Path(self.config['state_dir']).exists())
        self.assertFalse(Path(self.config['work_root']).exists())

    def test_wrong_ssh_executor_host_cannot_claim_or_clean_a_same_named_path(self):
        self.config['hostname'] = 'different-host'
        c = self.controller()
        for action in (c.up, c.down, c.status):
            with self.assertRaisesRegex(life.Refused, 'Wrong executor hostname'):
                action()
        self.assertFalse(self.ledger.claimed)
        self.assertFalse(Path(self.config['state_dir']).exists())

    def test_status_only_inspects_owned_instance_pids(self):
        c = self.controller()
        c.up()
        self.assertEqual(set(c.status()['owned_pids']), {'a'})
        self.assertEqual(self.order.count('claim'), 1)

    def test_optional_display_helper_units_stopped_before_release(self):
        helper = self.base / 'fake-display.ps1'
        helper.write_text('# fake, never executed')
        self.config['display_helper'] = {'path': str(helper), 'sha256': life.digest(helper)}
        c = self.controller()
        c.up()
        self.assertEqual(len(self.host.live), 3)
        c.down()
        self.assertFalse(self.host.live)
        self.assertTrue(self.ledger.released)

    def test_unsafe_paths_displays_profiles_and_duplicate_plugins_refuse(self):
        for mutate in (lambda c: c['instances'][0].update(display=':61'),
                       lambda c: c['instances'][0].update(source='/tmp/profiles/a'),
                       lambda c: c['instances'][0]['files'][0].update(to='../outside'),
                       lambda c: c['instances'][0]['files'][0].update(to=life.READ_ONLY[0] + '/bad'),
                       lambda c: c['instances'][0].update(sources={'socket': 'not-a-socket'}),
                       lambda c: c['instances'][0]['plugins'].append(c['instances'][0]['plugins'][0])):
            config = deepcopy(self.config)
            mutate(config)
            with self.assertRaises(life.Refused):
                life.Lifecycle(config, self.host, self.ledger)

    def test_symlink_source_ancestor_refuses_before_claim(self):
        link = self.base / 'source-alias'
        link.symlink_to(self.source, target_is_directory=True)
        self.config['instances'][0]['source'] = str(link)
        with self.assertRaisesRegex(life.Refused, 'Symlink ancestor'):
            self.controller()

    def test_no_execute_cli_is_inert_even_with_valid_config(self):
        path = self.base / 'config.json'
        path.write_text(json.dumps(self.config))
        with patch('sys.stderr', new=io.StringIO()):
            self.assertEqual(life.main(['up', str(path)]), 1)
        self.assertFalse(Path(self.config['state_dir']).exists())
        self.assertEqual(self.ledger_path.read_text(), '# TEMPORARY test data; not OWNER-COORDINATION\n')

    def test_claim_append_ack_loss_is_read_back_and_released_without_boot(self):
        def lost_ack(**kwargs):
            self.ledger.claimed = True
            raise RuntimeError('synthetic claim ACK lost')
        self.ledger.claim = lost_ack
        with self.assertRaisesRegex(RuntimeError, 'ACK lost'):
            self.controller().up()
        self.assertTrue(self.ledger.released)
        self.assertFalse(self.host.calls)
        self.assertFalse(Path(self.config['work_root']).exists())

    def test_contradictory_ledger_history_releases_the_open_claim(self):
        for already_released in (False, True):
            with self.subTest(already_released=already_released):
                self.ledger_path.write_text('')
                token = ('a' if already_released else 'b') * 32
                ledger = life.Ledger(self.ledger_path, 'surface', self.config['owner'], token)
                claim = ledger.claim(admission={'max_eden': 2, 'foreign_processes': []})['line']
                ledger.release()
                with self.ledger_path.open('a') as stream:
                    stream.write(claim + '\n')
                before = ledger.inspect()
                self.assertEqual((before['owned_claim'], before['owned_release']), (True, True))
                original = self.ledger_path.read_bytes()
                self.assertTrue(ledger.claim(admission={'max_eden': 2, 'foreign_processes': []})['already_claimed'])
                self.assertEqual(self.ledger_path.read_bytes(), original)
                config = dict(self.config, state_dir=str(self.base / ('state-' + token)))
                c = life.Lifecycle(config, self.host)
                c.directory.mkdir(parents=True)
                c.state = {'token': token, 'claimed': True, 'released': already_released, 'release_pending': True}
                c.ledger = lambda: ledger
                with patch.object(ledger, 'inspect', wraps=ledger.inspect) as inspect:
                    c._settle_ledger()
                self.assertEqual(inspect.call_count, 2)
                self.assertTrue(c.state['released'])
                self.assertFalse(c.state['release_pending'])
                self.assertEqual((ledger.inspect()['owned_claim'], ledger.inspect()['owned_release']), (False, True))
                self.assertEqual(self.ledger_path.read_text().count(' RELEASED:'), 2)
                actions = [json.loads(line)['kind'] for line in (c.directory / 'actions.jsonl').read_text().splitlines()]
                self.assertIn('settle-contradiction', actions)

    def test_contradictory_release_readback_refuses_if_claim_reappears(self):
        self.ledger_path.write_text('')
        token = 'c' * 32
        ledger = life.Ledger(self.ledger_path, 'surface', self.config['owner'], token)
        claim = ledger.claim(admission={'max_eden': 2, 'foreign_processes': []})['line']
        ledger.release()
        with self.ledger_path.open('a') as stream:
            stream.write(claim + '\n')
        c = life.Lifecycle(self.config, self.host)
        c.directory.mkdir(parents=True)
        c.state = {'token': token, 'claimed': True, 'released': True, 'release_pending': True}
        c.ledger = lambda: ledger
        actual_release = ledger.release
        def release_then_reopen():
            result = actual_release()
            with self.ledger_path.open('a') as stream:
                stream.write(claim + '\n')
            return result
        with patch.object(ledger, 'release', side_effect=release_then_reopen), \
                self.assertRaisesRegex(life.Refused, 'Open lifecycle claim remains after release'):
            c._settle_ledger()
        self.assertTrue(ledger.inspect()['owned_claim'])
        self.assertFalse(c.state['released'])
        self.assertTrue(c.state['release_pending'])
        self.assertFalse(json.loads(c.journal.read_text())['released'])

    def test_settle_and_settle_pending_release_contradictory_real_ledger(self):
        for method in ('settle', 'settle_pending'):
            with self.subTest(method=method):
                config = dict(self.config, state_dir=str(self.base / ('state-' + method)),
                              work_root=str(self.base / ('work-' + method)))
                c = life.Lifecycle(config, self.host)
                c.up()
                token = c.state['token']
                ledger = life.Ledger(self.ledger_path, 'surface', self.config['owner'], token)
                claim = next(line for line in ledger.inspect()['open'] if f'lifecycle={token} ' in line)
                c.down(keep_state=True)
                with self.ledger_path.open('a') as stream:
                    stream.write(claim + '\n')
                c.state['released'] = False
                c.state['release_pending'] = True
                c.write()
                c._write_pending()
                self.assertEqual((ledger.inspect()['owned_claim'], ledger.inspect()['owned_release']), (True, True))
                if method == 'settle':
                    self.assertTrue(c.settle(minutes=2)['released'])
                else:
                    c.settle_pending()
                self.assertEqual((ledger.inspect()['owned_claim'], ledger.inspect()['owned_release']), (False, True))
                self.assertFalse(json.loads(c.journal.read_text())['release_pending'])
                actions = [json.loads(line)['kind'] for line in (c.directory / 'actions.jsonl').read_text().splitlines()]
                self.assertIn('settle-contradiction', actions)

    def test_release_ack_loss_recovers_exact_release_on_next_down(self):
        c = self.controller()
        c.up()
        def lost_ack():
            self.ledger.released = True
            raise RuntimeError('synthetic release ACK lost')
        self.ledger.release = lost_ack
        with self.assertRaisesRegex(life.Refused, 'Cleanup incomplete'):
            c.down()
        self.assertEqual(c.state['phase'], 'cleanup-failed')
        self.assertEqual(c.down()['phase'], 'down')

    def test_ledger_readback_outage_still_stops_and_restores_but_retains_claim(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=RuntimeError('synthetic ledger unavailable')):
            with self.assertRaisesRegex(life.Refused, 'Cleanup incomplete'):
                c.down()
        self.assertFalse(self.host.live)
        self.assertTrue(c.state['instances'][0]['verified'])
        self.assertFalse(self.ledger.released)
        c.down()
        self.assertTrue(self.ledger.released)

    def test_down_readback_transport_failure_then_later_settlement(self):
        c = self.controller()
        c.up()
        def readback():
            self.assertTrue(c.state['instances'][0]['verified'])
            if readback.calls == 0:
                readback.calls += 1
                raise transport('ssh diag fake: read unavailable')
            return {'owned_claim': True, 'owned_release': False}
        readback.calls = 0
        with patch.object(self.ledger, 'inspect', side_effect=readback):
            result = c.down()
        self.assertEqual(result['phase'], 'down')
        self.assertFalse(result['released'])
        self.assertTrue(result['release_pending'])
        self.assertEqual(self.order.count('release'), 0)
        actions = [json.loads(line)['kind'] for line in (c.directory / 'actions.jsonl').read_text().splitlines()]
        self.assertIn('release-pending', actions)
        self.assertTrue(c.down()['released'])
        self.assertEqual(self.order.count('release'), 1)

    def test_down_records_pending_then_second_down_releases_once_and_prunes(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'release', side_effect=transport('ssh diag fake: release unavailable')):
            result = c.down()
        self.assertEqual(result['phase'], 'down')
        self.assertFalse(result['released'])
        self.assertTrue(result['release_pending'])
        self.assertFalse(self.host.live)
        self.assertTrue(c.state['instances'][0]['verified'])
        self.assertIsNone(result['prune'])
        path = life.pending_release_dir() / (c.state['token'] + '.json')
        pending = json.loads(path.read_text())
        self.assertEqual(pending['ledger'], self.config['ledger'])
        self.assertEqual(pending['journal_path'], str(c.journal))
        self.assertEqual(pending['host'], 'surface')
        self.assertTrue(Path(pending['config_path']).is_file())
        self.assertEqual(len(self.host.started_settles), 1)
        self.assertEqual(result['settle_unit'], 'eden-settle-' + c.state['token'][:8] + '.service')
        status = c.status()
        self.assertTrue(status['release_pending'])
        self.assertTrue(status['settle_running'])
        self.assertEqual(status['release_pending_last_line'], 'notify not configured')
        self.assertIn('ssh diag fake: release unavailable', (c.directory / 'RELEASE-PENDING.md').read_text())
        stopped = self.order.count('game.stop-a')
        result = c.down()
        self.assertTrue(result['released'])
        self.assertFalse(result['release_pending'])
        self.assertTrue(result['prune']['done'])
        self.assertFalse(path.exists())
        self.assertEqual(self.host.stopped_settles, [result.get('settle_unit') or self.host.started_settles[0][0]])
        self.assertEqual(self.order.count('release'), 1)
        self.assertEqual(self.order.count('game.stop-a'), stopped)
        self.assertEqual(len(self.host.started_settles), 1)

    def test_nontransient_down_notifies_without_starting_settle(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport(
                'ssh diag fake: host key', False, 'host key')), patch.object(c, 'notify') as notify:
            result = c.down()
        self.assertEqual(result['phase'], 'down')
        self.assertTrue(result['release_pending'])
        self.assertIsNone(result['settle_unit'])
        self.assertEqual(self.host.started_settles, [])
        line = 'release pending: not retried (cause: host key); manual Down needed'
        notify.assert_called_once_with(line)
        receipt = (c.directory / 'RELEASE-PENDING.md').read_text()
        self.assertIn('ssh diag fake: host key', receipt)
        self.assertIn(line, receipt)
        self.assertTrue(c.down()['released'])

    def test_later_nontransient_down_stops_existing_settle(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            first = c.down()
        unit = first['settle_unit']
        with patch.object(self.ledger, 'inspect', side_effect=transport(
                'ssh diag fake: host key', False, 'host key')):
            second = c.down()
        self.assertTrue(second['release_pending'])
        self.assertIsNone(second['settle_unit'])
        self.assertEqual(self.host.stopped_settles, [unit])
        self.assertFalse(c.status()['settle_running'])
        c.down()

    def test_notify_local_format_and_ssh_options(self):
        target = self.base / 'notifications.md'
        self.config['notify'] = {'file': str(target), 'from': 'source', 'to': 'recipient'}
        c = self.controller()
        c.up()
        c.notify('one message')
        self.assertRegex(target.read_text(),
                         r'^\n## \d\d:\d\d:\d\dZ source test-01 → recipient \| one message\n$')
        c.down()
        config = deepcopy(self.config)
        config.update(label='ssh-notify', state_dir=str(self.base / 'notify-state'),
                      work_root=str(self.base / 'notify-work'))
        config['notify'] = {'file': '/tmp/remote messages.md', 'ssh': 'fake-notify'}
        remote = life.Lifecycle(config, self.host, self.ledger)
        remote.up()
        with patch.object(life.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            remote.notify('remote message')
        argv = run.call_args.args[0]
        for option in ('BatchMode=yes', 'ConnectTimeout=10', 'ServerAliveInterval=5', 'ServerAliveCountMax=2'):
            self.assertIn(option, argv)
        self.assertEqual(run.call_args.kwargs['timeout'], 10)
        self.assertEqual(argv[-1], "cat >> '/tmp/remote messages.md'")
        self.assertIn('remote message', run.call_args.kwargs['input'])
        remote.down()

    def test_ruled_notify_validation_and_local_file_creation(self):
        ruled = {'file': '/home/intpa/eden-run-notify/NOTIFY.md', 'to': 'tr-primary-2'}
        config = deepcopy(self.config)
        config['notify'] = ruled
        self.assertEqual(life.validate(config)['notify'], ruled)
        target = self.base / 'new-notify' / 'nested' / 'NOTIFY.md'
        self.config['notify'] = {'file': str(target), 'to': 'tr-primary-2'}
        c = self.controller()
        c.up()
        c.pending_receipt('existing receipt')
        with patch.object(life.subprocess, 'run') as run:
            c.notify('one message')
        run.assert_not_called()
        first = target.read_text()
        self.assertRegex(first, r'^\n## \d\d:\d\d:\d\dZ eden-lifecycle test-01 → tr-primary-2 \| one message\n$')
        self.assertTrue(target.parent.is_dir())
        self.assertNotIn('notify not configured', (c.directory / 'RELEASE-PENDING.md').read_text())
        c.notify('second message')
        self.assertTrue(target.read_text().startswith(first))
        self.assertRegex(target.read_text()[len(first):],
                         r'^\n## \d\d:\d\d:\d\dZ eden-lifecycle test-01 → tr-primary-2 \| second message\n$')
        c.down()

    def test_pending_down_notifies_local_file_and_receipt(self):
        target = self.base / 'new-notify' / 'nested' / 'NOTIFY.md'
        self.config['notify'] = {'file': str(target), 'to': 'tr-primary-2'}
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport('ssh diag fake: release unavailable')), \
                patch.object(life.subprocess, 'run') as run:
            result = c.down()
        run.assert_not_called()
        self.assertEqual(result['phase'], 'down')
        self.assertTrue(result['release_pending'])
        receipt = (c.directory / 'RELEASE-PENDING.md').read_text()
        line = next(line for line in receipt.splitlines() if line.startswith('release pending since '))
        self.assertIn('ssh diag fake: release unavailable', line)
        self.assertNotIn('notify not configured', receipt)
        self.assertRegex(target.read_text(), r'^\n## \d\d:\d\d:\d\dZ eden-lifecycle test-01 → tr-primary-2 \| '
                         + re.escape(line) + r'\n$')
        self.assertNotIn('notify_errors', json.loads(c.journal.read_text()))
        c.down()

    def test_failing_notify_does_not_fail_down(self):
        occupied = self.base / 'occupied'
        occupied.write_text('not a directory')
        self.config['notify'] = {'file': str(occupied / 'missing' / 'notifications.md')}
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            result = c.down()
        self.assertEqual(result['phase'], 'down')
        self.assertTrue(result['release_pending'])
        journal = json.loads(c.journal.read_text())
        self.assertEqual(len(journal['notify_errors']), 1)
        actions = [json.loads(line)['kind'] for line in (c.directory / 'actions.jsonl').read_text().splitlines()]
        self.assertIn('notify-error', actions)
        c.down()

    def test_notify_validation_rejects_unsafe_fields(self):
        for spec in ({'file': 'relative'}, {'file': '/tmp/file;touch'}, {'file': '/tmp/../file'},
                     {'file': r'C:\Users\intpa\eden-run-notify\NOTIFY.md'},
                     {'file': '/tmp/file', 'ssh': 'host;bad'}, {'file': '/tmp/file', 'from': 'bad\nname'}):
            with self.subTest(spec=spec):
                config = deepcopy(self.config)
                config['notify'] = spec
                with self.assertRaises(life.Refused):
                    life.validate(config)

    def test_down_lost_release_ack_is_not_appended_again(self):
        c = self.controller()
        c.up()
        def lost_ack():
            self.order.append('release')
            self.ledger.released = True
            raise transport('ssh diag fake: lost release ACK')
        with patch.object(self.ledger, 'release', side_effect=lost_ack):
            self.assertTrue(c.down()['release_pending'])
        self.assertTrue(c.down()['released'])
        self.assertEqual(self.order.count('release'), 1)

    def test_clean_down_without_ever_claiming_still_prunes(self):
        c = self.controller()
        with patch.object(self.ledger, 'claim', side_effect=RuntimeError('claim refused before append')):
            with self.assertRaisesRegex(RuntimeError, 'claim refused before append'):
                c.up()
        self.assertFalse(c.state['claimed'])
        self.assertFalse(c.state['released'])
        result = c.down()
        self.assertTrue(result['prune']['done'])

    def test_recovered_claim_receipt_is_visible_in_the_journal(self):
        c = self.controller()
        original = self.ledger.claim
        def recovered(**kwargs):
            return {**original(**kwargs), 'already_claimed': True}
        with patch.object(self.ledger, 'claim', side_effect=recovered):
            c.up()
        self.assertTrue(json.loads(c.journal.read_text())['claim_recovered_after_transport_failure'])
        actions = [json.loads(line) for line in (c.directory / 'actions.jsonl').read_text().splitlines()]
        self.assertTrue(next(a for a in actions if a['kind'] == 'claim')['value']['already_claimed'])
        c.down()

    def test_local_stop_failure_does_not_attempt_the_unreachable_ledger_release(self):
        c = self.controller()
        c.up()
        self.host.leftover = True
        with patch.object(self.ledger, 'inspect', side_effect=transport()) as read, \
                patch.object(self.ledger, 'release') as release:
            with self.assertRaisesRegex(life.Refused, 'Cleanup incomplete'):
                c.down()
        read.assert_not_called()
        release.assert_not_called()
        self.assertEqual(c.state['phase'], 'cleanup-failed')
        self.assertTrue(c.state['claimed'])
        self.assertFalse(c.state['released'])

    def test_settle_start_failure_is_recorded_without_failing_down(self):
        c = self.controller()
        c.up()
        self.host.fail_settle = True
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            result = c.down()
        self.assertTrue(result['release_pending'])
        self.assertIsNone(result['settle_unit'])
        actions = [json.loads(line)['kind'] for line in (c.directory / 'actions.jsonl').read_text().splitlines()]
        self.assertIn('settle-start-error', actions)

    def test_settle_retries_twice_then_releases_and_later_down_prunes(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            self.assertTrue(c.down()['release_pending'])
        path = life.pending_release_dir() / (c.state['token'] + '.json')
        before = self.host.now
        with patch.object(self.ledger, 'inspect', side_effect=[
                transport(),
                transport(),
                {'owned_claim': True, 'owned_release': False}]) as inspect:
            value = c.settle(minutes=4)
        self.assertEqual(inspect.call_count, 3)
        self.assertEqual(self.host.now - before, 180)
        receipt = (c.directory / 'RELEASE-PENDING.md').read_text()
        self.assertEqual(receipt.count('release pending since '), 3)
        self.assertIn('after 3 attempts', receipt)
        self.assertTrue(value['released'])
        self.assertFalse(path.exists())
        self.assertNotIn('prune', c.state)
        self.assertTrue(c.down()['prune']['done'])

    def test_settle_stops_at_deadline_and_cli_reports_pending(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            c.down()
            before = self.host.now
            result = c.settle(minutes=0.5)
        self.assertEqual(self.host.now - before, 29)
        self.assertTrue(result['release_pending'])
        config_path = self.base / 'config.json'
        config_path.write_text(json.dumps(self.config))
        with patch.object(life, 'Lifecycle', return_value=c), patch.object(c, 'settle', return_value=result) as settle, \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(life.main(['settle', str(config_path), '--minutes', '0.5', '--execute']), 1)
            settle.assert_called_once_with(minutes=0.5)

    def test_settle_stops_on_second_nontransient_failure(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            c.down()
        before = self.host.now
        with patch.object(self.ledger, 'inspect', side_effect=[
                transport('ssh diag fake: temporary'),
                transport('ssh diag fake: host key', False, 'host key')]) as inspect, \
                patch.object(c, 'notify') as notify:
            result = c.settle(minutes=4)
        self.assertTrue(result['release_pending'])
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(self.host.now - before, 120)
        line = 'release still pending: stopped on a non-transient cause: ssh diag fake: host key; manual Down needed'
        notify.assert_called_once_with(line)
        self.assertIn(line, (c.directory / 'RELEASE-PENDING.md').read_text())
        c.down()

    def test_settle_stops_on_target_overload(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            c.down()
        facts = {'load1': 48, 'cores': 8, 'memAvailMiB': 150, 'oomKills5m': 2}
        cause, transient, _ = life.ssh_cause('timed out', 'kex', 'ok in 0.5 s', 'ok', '0%', facts, 'fake-host')
        with patch.object(self.ledger, 'inspect', side_effect=transport('ssh diag fake: ' + cause, transient, cause)) as inspect, \
                patch.object(c, 'notify') as notify:
            result = c.settle(minutes=4)
        self.assertTrue(result['release_pending'])
        inspect.assert_called_once()
        self.assertFalse(transient)
        self.assertIn('stopped on a non-transient cause: ssh diag fake: target overloaded',
                      notify.call_args.args[0])
        c.down()

    def test_settle_stops_on_loss_without_target_evidence(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            c.down()
        with tempfile.TemporaryDirectory(prefix='eden-diag-') as tmp:
            attempt = Path(tmp) / 'attempt.log'
            attempt.write_text('Connecting to x\nConnection established\nRemote protocol version 2.0\n')
            ping = '20% packet loss\nrtt min/avg/max/mdev = 1.000/2.000/3.000/0.100 ms\n'
            with patch('sys.stderr', new=io.StringIO()):
                diagnosis = real_ssh_diagnosis('fake-host', run=target_fixture_run(None, [], ping),
                                               evidence_dir=tmp, tcp=lambda *_: True, attempt_log=attempt)
            self.assertFalse(diagnosis['transient'])
            self.assertIn('packet loss 20% on the path to fake-host; target health not confirmed, no retry',
                          diagnosis['cause'])
            with patch.object(self.ledger, 'inspect', side_effect=life.LedgerTransport([diagnosis])) as inspect, \
                    patch.object(c, 'notify') as notify:
                result = c.settle(minutes=4)
            self.assertTrue(result['release_pending'])
            inspect.assert_called_once()
            self.assertIn('stopped on a non-transient cause', notify.call_args.args[0])
        c.down()

    def test_settle_30_minute_giveup_receipt_and_notification(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            c.down()
        before = self.host.now
        starts = []
        def fail():
            starts.append(self.host.now - before)
            raise transport()
        with patch.object(self.ledger, 'inspect', side_effect=fail), patch.object(c, 'notify') as notify:
            result = c.settle()
        self.assertTrue(result['release_pending'])
        self.assertEqual(starts, list(range(60, 1741, 60)))
        self.assertEqual(self.host.now - before, 1799)
        line = 'release still pending after 30 minutes: manual Down needed; last: ssh diag fake: temporary stall'
        notify.assert_called_once_with(line)
        self.assertIn(line, (c.directory / 'RELEASE-PENDING.md').read_text())
        c.down()

    def test_settle_interrupts_a_blocked_attempt_and_writes_giveup(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            c.down()
        deadline = life.settle_attempt_deadline
        with patch.object(c, '_settle_ledger', side_effect=lambda **_kw: time.sleep(1)), \
                patch.object(life, 'settle_attempt_deadline', side_effect=lambda _seconds: deadline(.02)):
            result = c.settle(minutes=2)
        self.assertTrue(result['release_pending'])
        self.assertLessEqual(self.host.now, 120)
        self.assertIn('last: attempt exceeded the settle deadline',
                      (c.directory / 'RELEASE-PENDING.md').read_text())
        c.down()

    def test_settle_retries_when_starting_before_down_releases_the_journal_lock(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            c.down()
        original_sleep = self.host.sleep
        with (c.directory / '.lock').open('a+') as held:
            life.fcntl.flock(held, life.fcntl.LOCK_EX | life.fcntl.LOCK_NB)
            def release_after_sleep(seconds):
                original_sleep(seconds)
                if seconds == 1:
                    life.fcntl.flock(held, life.fcntl.LOCK_UN)
            with patch.object(self.host, 'sleep', side_effect=release_after_sleep) as sleep:
                result = c.settle(minutes=3)
        self.assertEqual(sleep.call_args_list, [call(60), call(1)])
        self.assertTrue(result['released'])
        self.assertFalse(result['release_pending'])

    def test_stopped_settle_unit_is_noted_once_on_status_and_down(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            c.down()
        self.host.settle_units.clear()
        status = c.status()
        self.assertEqual(status['release_pending_last_line'], 'settle unit stopped by its time cap')
        c.status()
        self.assertEqual((c.directory / 'RELEASE-PENDING.md').read_text().count(
            'settle unit stopped by its time cap'), 1)
        c.down()
        self.assertFalse(c.state['release_pending'])

    def test_old_settle_outcome_does_not_mask_new_unit_cap(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            c.down()
            c.pending_receipt('release still pending after 30 minutes: manual Down needed; last: old')
            self.host.settle_units.clear()
            c.down()
        self.assertEqual(len(self.host.started_settles), 2)
        self.host.settle_units.clear()
        self.assertEqual(c.status()['release_pending_last_line'], 'settle unit stopped by its time cap')
        c.down()

    def test_settle_does_not_treat_a_ledger_lock_error_as_journal_lock_contention(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            c.down()
        with patch.object(self.ledger, 'inspect', side_effect=BlockingIOError('ledger busy')), \
                patch.object(self.host, 'sleep', wraps=self.host.sleep) as sleep:
            with self.assertRaisesRegex(BlockingIOError, 'ledger busy'):
                c.settle(minutes=2)
        sleep.assert_called_once_with(60)

    def test_up_settles_only_its_host_and_ledger_before_claim(self):
        old = self.controller()
        old.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            old.down()
        pending_dir = life.pending_release_dir()
        old_path = pending_dir / (old.state['token'] + '.json')
        foreign_path = pending_dir / ('b' * 32 + '.json')
        foreign_path.write_text(json.dumps({'host': 'xps', 'ledger': self.config['ledger'],
                                            'ledger_ssh': self.config.get('ledger_ssh')}))
        other_ledger_path = pending_dir / ('c' * 32 + '.json')
        other_ledger_path.write_text(json.dumps({'host': 'surface', 'ledger': '/tmp/other-ledger'}))
        other_authority = pending_dir / ('d' * 32 + '.json')
        other_authority.write_text(json.dumps({'host': 'surface', 'ledger': self.config['ledger'],
                                               'ledger_ssh': 'another-authority'}))
        config = deepcopy(self.config)
        config.update(label='next-run', state_dir=str(self.base / 'next-state'),
                      work_root=str(self.base / 'next-owned'))
        current = life.Lifecycle(config, self.host, self.ledger)
        original_claim = self.ledger.claim
        def claim(**kwargs):
            self.assertTrue(self.ledger.released)
            self.ledger.released = False
            return original_claim(**kwargs)
        self.order.clear()
        with patch.object(self.ledger, 'claim', side_effect=claim):
            current.up()
        self.assertLess(self.order.index('release'), self.order.index('claim'))
        self.assertFalse(old_path.exists())
        self.assertEqual(self.host.stopped_settles, [self.host.started_settles[0][0]])
        self.assertTrue(foreign_path.exists())
        self.assertTrue(other_ledger_path.exists())
        self.assertTrue(other_authority.exists())
        current.down()

    def test_up_ignores_pending_scan_errors(self):
        c = self.controller()
        with patch.object(c, 'settle_pending', side_effect=OSError('synthetic pending scan error')):
            self.assertEqual(c.up()['phase'], 'up')
        actions = [json.loads(line) for line in (c.directory / 'actions.jsonl').read_text().splitlines()]
        self.assertTrue(any(a['kind'] == 'pending-settle-error' for a in actions))
        c.down()

    def test_pending_release_continues_in_a_fresh_process(self):
        config_path = self.base / 'resume-config.json'
        config_path.write_text(json.dumps(self.config))
        ledger_path = self.base / 'fake-durable-ledger.json'
        ledger_path.write_text(json.dumps({'claimed': False, 'released': False, 'releases': 0}))
        script = self.base / 'resume-fake.py'
        script.write_text("""
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('offline_tests', sys.argv[1])
tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tests)
life = tests.life
ledger_path = Path(sys.argv[3])
class FileLedger:
    open_text = ''
    def claim(self, **kwargs):
        ledger_path.write_text(json.dumps({'claimed': True, 'released': False, 'releases': 0}))
        return {'line': 'fake claim', 'stale_claim_skipped': []}
    def inspect(self):
        if sys.argv[4] == 'save':
            raise tests.transport('ssh diag fake: outage')
        value = json.loads(ledger_path.read_text())
        return {'owned_claim': value['claimed'] and not value['released'], 'owned_release': value['released']}
    def release(self):
        value = json.loads(ledger_path.read_text())
        value['released'] = True
        value['releases'] += 1
        ledger_path.write_text(json.dumps(value))
        return {'line': 'fake release'}
config = json.loads(Path(sys.argv[2]).read_text())
c = life.Lifecycle(config, tests.FakeHost([]), FileLedger())
if sys.argv[4] == 'save':
    c.up()
print(json.dumps(c.down()))
""")
        command = [sys.executable, '-I', '-B', str(script), str(Path(__file__).resolve()), str(config_path),
                   str(ledger_path)]
        first = subprocess.run([*command, 'save'], capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(json.loads(first.stdout)['release_pending'])
        self.assertEqual(json.loads(ledger_path.read_text())['releases'], 0)
        second = subprocess.run([*command, 'resume'], capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(json.loads(second.stdout)['released'])
        self.assertEqual(json.loads(ledger_path.read_text())['releases'], 1)

    def test_staging_foreign_nand_path_is_refused_before_launch(self):
        bad = self.base / 'bad-qt.ini'
        bad.write_text('[Data Storage]\nnand_directory=/foreign/nand\nsdmc_directory=/foreign/sd\n[Audio]\nvolume=0\n')
        self.config['instances'][0]['files'].append({'path': str(bad), 'sha256': life.digest(bad), 'to': life.CONFIG})
        with self.assertRaisesRegex(life.Refused, 'Config escapes'):
            self.controller().up()
        self.assertFalse(self.host.calls)
        self.assertTrue(self.ledger.released)

    def test_trace_config_is_written_and_passed_at_launch(self):
        points = [{'target': 'libtraining_rooms.nro!hook_entry', 'registers': ['x0'], 'on_return': True},
                  {'target': 'main+0x15d9a70', 'mode': 'counters'}]
        elf = Path(self.config['state_dir']).parent / 'plugin.elf'
        elf.write_bytes(b'elf fixture')
        self.config['instances'][0]['trace'] = {'points': points, 'symbols': [
            {'module': 'libtraining_rooms.nro', 'elf': str(elf)}]}
        c = self.controller()
        c.up()
        instances = json.loads((Path(self.config['state_dir']) / 'instances.json').read_text())
        self.assertTrue(instances['a']['trace_jsonl'].endswith('/run-a/a.trace.jsonl'))
        item = c.state['instances'][0]
        host, commands = life.Host(), []
        host.run = lambda argv, cap=30: commands.append(argv) or ''
        host.unit = lambda _: {'ActiveState': 'active', 'MemoryMax': str(8 * life.GIB), 'MemorySwapMax': '0', 'TasksMax': '256',
                               'KillMode': 'mixed', 'KillSignal': '15', 'FinalKillSignal': '9'}
        host.owned = lambda _: [{'pid': 999, 'start': 'fake', 'boot': 'fake'}]
        exists = Path.exists
        with patch.object(Path, 'exists', lambda path: True if str(path).startswith('/tmp/.X11-unix/X') else exists(path)):
            with patch.object(Path, 'is_symlink', return_value=False):
                host.launch(item, self.config)
        command = commands[0]
        trace_json = Path(item['run']) / 'trace.json'
        self.assertEqual(command[command.index('--trace-config') + 1], str(trace_json))
        written = json.loads(trace_json.read_text())
        self.assertEqual((written['run_directory'], written['label'], written['points']), (item['run'], 'a', points))
        # Bad trace configs are refused before anything happens.
        self.config['instances'][0]['trace'] = {'points': []}
        with self.assertRaises(life.Refused):
            life.validate(self.config)

    def test_native_backend_launch_command_is_bounded_and_sanitized_without_executing(self):
        c = self.controller()
        c.up()
        item = c.state['instances'][0]
        host, commands = life.Host(), []
        host.run = lambda argv, cap=30: commands.append(argv) or ''
        host.unit = lambda _: {'ActiveState': 'active', 'MemoryMax': str(8 * life.GIB), 'MemorySwapMax': '0', 'TasksMax': '256',
                               'KillMode': 'mixed', 'KillSignal': '15', 'FinalKillSignal': '9'}
        host.owned = lambda _: [{'pid': 999, 'start': 'fake', 'boot': 'fake'}]
        exists = Path.exists
        with patch.object(Path, 'exists', lambda path: True if str(path).startswith('/tmp/.X11-unix/X') else exists(path)):
            with patch.object(Path, 'is_symlink', return_value=False):
                host.launch(item, self.config)
        command = commands[0]
        self.assertIn('--property=MemoryMax=8G', command)
        self.assertIn('--property=MemorySwapMax=0', command)
        self.assertIn('--property=TasksMax=256', command)
        self.assertIn('--property=RuntimeMaxSec=5400', command)
        self.assertIn('--property=KillMode=mixed', command)
        self.assertIn('--property=KillSignal=SIGTERM', command)
        self.assertIn('--property=FinalKillSignal=SIGKILL', command)
        self.assertNotIn('--property=KillMode=control-group', command)
        self.assertIn('-i', command)
        self.assertIn('--control-background', command)
        # A launch without a log filter runs Eden at Debug (lr-04: 1 GB of console in 6 minutes).
        self.assertIn('EDEN_LOG_FILTER=' + life.DEFAULT_LOG_FILTER, command)
        self.assertNotIn('pkill', ' '.join(command))

    def test_native_launch_refuses_unsafe_stop_policy_readback(self):
        c = self.controller()
        c.up()
        host = life.Host()
        host.run = lambda *_args, **_kwargs: ''
        safe = {'ActiveState': 'active', 'MemoryMax': str(8 * life.GIB), 'MemorySwapMax': '0',
                'TasksMax': '256', 'KillMode': 'mixed', 'KillSignal': '15', 'FinalKillSignal': '9'}
        exists = Path.exists
        with patch.object(Path, 'exists', lambda path: True if str(path).startswith('/tmp/.X11-unix/X') else exists(path)), \
                patch.object(Path, 'is_symlink', return_value=False):
            for wrong in ({'KillMode': 'control-group'}, {'KillMode': None}, {'KillSignal': '9'}, {'FinalKillSignal': '6'}):
                with self.subTest(wrong=wrong):
                    host.unit = lambda _, values={**safe, **wrong}: values
                    with self.assertRaisesRegex(life.Refused, 'stop policy'):
                        host.launch(c.state['instances'][0], self.config)

    def test_preflight_accepts_own_reservation_without_consuming_it(self):
        reservation = (f"{life.stamp()} CLAIMED: {self.config['owner']} host=surface "
                       "lifecycle=0ade00000000000000000000000000ed "
                       "Eden reservation run test-01 approver=tr-primary-2\n")
        self.ledger_path.write_text(reservation)
        report = life.Lifecycle(self.config, self.host).preflight()
        self.assertTrue(report['ok'], report['message'])
        self.assertFalse(report['mutation'])
        self.assertEqual(self.ledger_path.read_text(), reservation)
        self.assertFalse(Path(self.config['state_dir']).exists())

    def test_reservation_cli_up_conversion_and_normal_down(self):
        config = self.base / 'config.json'
        config.write_text(json.dumps(self.config))
        argv = ['reserve', str(config), '--approver', 'tr-primary-2', '--execute']
        with patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(life.main(argv), 0)
            reserved = json.loads(stdout.getvalue())
        self.assertEqual(reserved['expires_in_seconds'], 1200)
        self.assertIn('Eden reservation run test-01 approver=tr-primary-2', reserved['line'])
        controller = life.Lifecycle(self.config, self.host)
        self.assertEqual(controller.up()['phase'], 'up')
        text = self.ledger_path.read_text()
        opened = life.active_claims(text, 'surface')
        self.assertEqual(len(opened), 1)
        self.assertIn('Eden lifecycle admitted', opened[0])
        self.assertIn('Eden reservation converted to lifecycle claim', text)
        self.assertLess(text.index('Eden lifecycle admitted'), text.index('Eden reservation converted'))
        self.assertEqual(controller.down()['phase'], 'down')
        self.assertEqual(life.active_claims(self.ledger_path.read_text(), 'surface'), [])

    def test_reservation_rejects_multiline_owner_before_append(self):
        before = self.ledger_path.read_bytes()
        reserve = life.Ledger(self.ledger_path, 'surface', 'Codex\nmultiline', 'a' * 32, label='test-01')
        with self.assertRaisesRegex(life.Refused, 'valid Linux host, owner, run label and approver'):
            reserve.operate('reserve', approver='tr-primary-2')
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_exclusive_reservation_converts_and_blocks_until_normal_down(self):
        config = dict(self.config, exclusive_host=True)
        config_path = self.base / 'exclusive.json'
        config_path.write_text(json.dumps(config))
        self.host.max_eden = 2
        owner = life.Lifecycle(config, self.host)
        self.assertIn('requires a matching approved reservation', owner.preflight()['message'])
        with patch.object(life, 'Host', return_value=self.host), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(life.main(['reserve', str(config_path), '--approver', 'tr-primary-2', '--execute']), 0)
        self.assertTrue(owner.preflight()['ok'])
        self.assertFalse(life.Lifecycle(self.config, self.host).preflight()['ok'])
        foreign = life.Ledger(self.ledger_path, 'surface', 'Codex other/stream', 'b' * 32, label='other-run')
        capacity = {'max_eden': 2, 'foreign_processes': []}
        for after_up in (False, True):
            if after_up:
                self.assertEqual(owner.up()['phase'], 'up')
            opened = life.active_claims(self.ledger_path.read_text(), 'surface')
            self.assertEqual(len(opened), 1)
            self.assertIn('exclusive_host=true', opened[0])
            self.assertEqual('Eden lifecycle admitted' in opened[0], after_up)
            before = self.ledger_path.read_bytes()
            with self.assertRaises(life.Refused):
                foreign.operate('reserve', approver='tr-primary-2')
            with self.assertRaisesRegex(life.Refused, 'Open host claim'):
                foreign.claim(stale_claim_hours=1e-12, admission=capacity)
            self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertEqual(owner.down()['phase'], 'down')
        foreign.operate('reserve', approver='tr-primary-2')
        self.assertIn('consumed_reservation', foreign.claim(admission=capacity))

    def test_exclusive_reserve_scans_executor_and_forwards_to_authoritative_ledger(self):
        config = dict(self.config, host='xps', exclusive_host=True, ledger_ssh='ledger-host')
        config_path = self.base / 'exclusive.json'
        config_path.write_text(json.dumps(config))
        calls = []

        def forward(host, mode, packet, execute):
            calls.append((host, mode, execute))
            return life.dispatch(mode, packet, execute)

        def ledger_forward(host, operation, packet, **kwargs):
            return forward(host, 'ledger-' + operation, packet, True)

        with patch.object(life, 'forward', forward), patch.object(life, 'ledger_forward', ledger_forward), \
                patch.object(life, 'Host', return_value=self.host), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(life.main(['reserve', str(config_path), '--ssh', 'executor',
                                       '--approver', 'tr-primary-2', '--execute']), 0)
            controller = life.Lifecycle(config, self.host)
            self.assertTrue(controller.preflight()['ok'])
            self.assertEqual(controller.up()['phase'], 'up')
            self.assertEqual(controller.down()['phase'], 'down')
        self.assertEqual(calls[:2], [('executor', 'reserve-exclusive', True), ('ledger-host', 'ledger-reserve', True)])
        self.assertIn(('ledger-host', 'ledger-claim', True), calls)
        self.assertIn(('ledger-host', 'ledger-release', True), calls)
        self.assertEqual(life.active_claims(self.ledger_path.read_text(), 'xps'), [])

    def test_exclusive_reservation_expires_without_up(self):
        now = datetime.now(timezone.utc)
        line = (f"CLAIMED: Codex other/stream host=surface lifecycle={'a' * 32} "
                "Eden reservation run other-run approver=tr-primary-2 exclusive_host=true")
        young = f'{(now - timedelta(minutes=19)).isoformat()} {line}'
        expired = f'{(now - timedelta(minutes=20)).isoformat()} {line}'
        self.assertEqual(life.claim_admission(young, 'surface', self.config['owner'], now=now)['blocking'], [young])
        self.assertEqual(life.claim_admission(expired, 'surface', self.config['owner'], now=now)['blocking'], [])
        self.ledger_path.write_text(expired + '\n')
        ordinary = life.Ledger(self.ledger_path, 'surface', self.config['owner'], 'b' * 32, label='test-01')
        ordinary.operate('reserve', approver='tr-primary-2')
        self.assertIn('Eden reservation expired', self.ledger_path.read_text())

    def test_exclusive_request_refuses_existing_claim_or_untracked_eden(self):
        config = dict(self.config, exclusive_host=True)
        config_path = self.base / 'exclusive.json'
        config_path.write_text(json.dumps(config))
        self.host.max_eden = 2
        existing = (f"{life.stamp()} CLAIMED: Codex other/stream host=surface lifecycle={'c' * 32} "
                    "Eden lifecycle admitted\n")
        pristine = self.ledger_path.read_text()
        for existing_text, processes in ((existing, []), (pristine, [{'name': 'eden', 'pid': 1234}])):
            self.ledger_path.write_text(existing_text)
            self.host.foreign = processes
            with patch.object(life, 'Host', return_value=self.host), patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(life.main(['reserve', str(config_path), '--approver', 'tr-primary-2', '--execute']), 1)
            self.assertEqual(self.ledger_path.read_text(), existing_text)
            self.assertEqual(self.host.foreign, processes)

    def test_reservation_refuses_foreign_owner_or_label_at_preflight_and_claim(self):
        owner, label = self.config['owner'], self.config['label']
        reservation = life.Ledger(self.ledger_path, 'surface', owner, 'a' * 32, label=label)
        line = reservation.operate('reserve', approver='tr-primary-2')['line']
        before = self.ledger_path.read_bytes()
        for config in (dict(self.config, owner='Codex other/stream'), dict(self.config, label='other-run')):
            with self.subTest(owner=config['owner'], label=config['label']):
                config['stale_claim_hours'] = 0.0000001
                report = life.Lifecycle(config, self.host).preflight()
                self.assertIn('Open host claim', report['message'])
                foreign = life.Ledger(self.ledger_path, 'surface', config['owner'], 'b' * 32,
                                      label=config['label'])
                with self.assertRaisesRegex(life.Refused, 'Open host claim'):
                    foreign.claim(config['stale_claim_hours'], {'foreign_processes': []})
                with self.assertRaisesRegex(life.Refused, 'already has an open'):
                    foreign.operate('reserve', approver='tr-primary-2')
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertEqual(life.active_claims(self.ledger_path.read_text(), 'surface'), [line])

    def test_reservation_expires_at_twenty_minutes(self):
        now = datetime.now(timezone.utc)
        owner = 'Codex priority/run-owner'
        prefix = (f'CLAIMED: {owner} host=surface lifecycle=' + 'a' * 32
                  + ' Eden reservation run test-01 approver=tr-primary-2')
        expired = f'{(now - timedelta(minutes=20)).isoformat()} {prefix}'
        live = f'{(now - timedelta(minutes=20) + timedelta(seconds=1)).isoformat()} {prefix}'
        self.assertEqual(life.claim_admission(live, 'surface', self.config['owner'], now=now)['blocking'], [live])
        self.assertEqual(life.claim_admission(expired, 'surface', self.config['owner'], now=now)['blocking'], [])
        self.ledger_path.write_text(expired + '\n')
        report = life.Lifecycle(self.config, self.host).preflight()
        self.assertTrue(report['ok'], report['message'])
        later = life.Ledger(self.ledger_path, 'surface', self.config['owner'], 'b' * 32, label='test-01')
        self.assertNotIn('consumed_reservation', later.claim())
        opened = later.inspect()['open']
        self.assertEqual(len(opened), 1)
        self.assertIn('Eden lifecycle admitted', opened[0])
        self.assertIn('Eden reservation expired', self.ledger_path.read_text())

    def test_reservation_leaves_ordinary_claim_capacity_in_force(self):
        owner, label = self.config['owner'], self.config['label']
        self.ledger_path.write_text(
            f"{life.stamp()} CLAIMED: Codex present/ordinary host=surface lifecycle={'c' * 32} Eden lifecycle admitted\n")
        reservation = life.Ledger(self.ledger_path, 'surface', owner, 'a' * 32, label=label)
        reservation.operate('reserve', approver='tr-primary-2')
        claimant = life.Ledger(self.ledger_path, 'surface', owner, 'b' * 32, label=label)
        with self.assertRaisesRegex(life.Refused, 'Open host claim'):
            claimant.claim(admission={'max_eden': 1})
        claim = claimant.claim(admission={'max_eden': 2})
        self.assertEqual(claim['consumed_reservation'], 'a' * 32)
        self.assertEqual(len(life.active_claims(self.ledger_path.read_text(), 'surface')), 2)

    def test_forwarded_reservation_and_claim_carry_the_run_label(self):
        def ledger_forward(host, operation, config, **kwargs):
            self.assertEqual(host, 'fake-ledger-host')
            return life.dispatch('ledger-' + operation, config, True)
        reserve = life.Ledger(self.ledger_path, 'xps', self.config['owner'], 'a' * 32,
                              'fake-ledger-host', label='test-01')
        claimant = life.Ledger(self.ledger_path, 'xps', self.config['owner'], 'b' * 32,
                               'fake-ledger-host', label='test-01')
        with patch.object(life, 'ledger_forward', ledger_forward):
            reserve.operate('reserve', approver='tr-primary-2')
            claim = claimant.claim(admission={'max_eden': 2})
            self.assertEqual(claim['consumed_reservation'], 'a' * 32)
            claimant.release()

    def test_preflight_is_read_only_and_names_a_bad_pin(self):
        value = self.controller().preflight()
        self.assertTrue(value['ok'])
        self.assertFalse(value['mutation'])
        self.assertFalse(Path(self.config['state_dir']).exists())
        self.assertFalse(Path(self.config['work_root']).exists())
        self.assertFalse(self.ledger.claimed)
        self.assertTrue(any(line.startswith('OK pin:') for line in value['lines']))
        slots = next(check for check in value['checks'] if check['name'] == 'eden-slots')
        self.assertEqual(slots, {'name': 'eden-slots', 'ok': True, 'running': 0, 'limit': 1,
                                 'requesting': 1})
        self.assertTrue(any(line == 'OK eden slots: 0 running, limit 1, requesting 1' for line in value['lines']))
        self.assertEqual(self.ledger_path.read_text(), '# TEMPORARY test data; not OWNER-COORDINATION\n')
        self.plugin.write_bytes(b'changed')
        bad = self.controller().preflight()
        self.assertFalse(bad['ok'])
        self.assertIn('Pinned input mismatch', bad['message'])
        self.assertTrue(any(line.startswith('FAIL pin:') for line in bad['lines']))
        self.assertFalse(self.ledger.claimed)

    def test_preflight_requires_a_declared_update_version_and_commit(self):
        self.config['expected_game_version'] = '13.0.5'
        missing = self.controller().preflight()
        self.assertFalse(missing['ok'])
        self.assertIn('No game update pin for version 13.0.5', missing['message'])
        update = self.base / 'update.nsp'
        update.write_bytes(b'13.0.5 update')
        self.config['game_updates'] = [{**{'path': str(update), 'sha256': life.digest(update)}, 'version': '13.0.5'}]
        self.config['playbook'] = {'root': str(self.base), 'commit': 'abc1234'}
        self.host.playbook_head = lambda root: 'abc1234deadbeef'
        ready = self.controller().preflight()
        self.assertTrue(ready['ok'], ready['message'])
        self.assertTrue(any('13.0.5' in line for line in ready['lines']))
        self.config['playbook']['commit'] = 'fff0000'
        refused = self.controller().preflight()
        self.assertFalse(refused['ok'])
        self.assertIn('Playbook commit', refused['message'])

    def test_preflight_reports_memory_compiler_and_claims_without_writing(self):
        self.host.available = life.MIN_MEMORY - 1
        low = self.controller().preflight()
        self.assertIn('MemAvailable below 10 GiB', low['message'])
        self.host.available = 24 * life.GIB
        self.config['memory_floor_bytes'] = 30 * life.GIB
        floored = self.controller().preflight()
        self.assertIn('per-instance floor', floored['message'])
        self.config.pop('memory_floor_bytes')
        self.host.foreign = [{'pid': 9, 'name': 'rustc'}]
        compiling = self.controller().preflight()
        self.assertIn('Compiler already active on host', compiling['message'])
        self.host.foreign = []
        self.ledger.open_text = '2026-11-07T12:00:00+00:00 CLAIMED: OpenCode other/stream on Surface busy'
        claimed = self.controller().preflight()
        self.assertIn('Open host claim', claimed['message'])
        self.assertFalse(Path(self.config['state_dir']).exists())
        self.assertEqual(self.ledger_path.read_text(), '# TEMPORARY test data; not OWNER-COORDINATION\n')

    def test_display_ready_follows_the_declaration(self):
        host = life.Host()
        with patch.object(life.Path, 'exists', return_value=False), patch.object(life.Path, 'is_symlink', return_value=False):
            missing = host.display_ready({'display': ':244'}, {})
            helper = host.display_ready({'display': ':244'}, {'display_helper': {'path': '/x'}})
        self.assertFalse(missing['ready'])
        self.assertIn('missing', missing['detail'])
        self.assertTrue(helper['ready'])
        with patch.object(life.Path, 'exists', return_value=True), patch.object(life.Path, 'is_symlink', return_value=False):
            present = host.display_ready({'display': ':244'}, {})
            busy = host.display_ready({'display': ':244'}, {'display_helper': {'path': '/x'}})
        self.assertTrue(present['ready'])
        self.assertFalse(busy['ready'])

    def test_up_runs_preflight_before_any_state(self):
        self.plugin.write_bytes(b'changed')
        with self.assertRaisesRegex(life.Refused, 'Pinned input mismatch'):
            self.controller().up()
        self.assertFalse(self.ledger.claimed)
        self.assertFalse(Path(self.config['state_dir']).exists())

    def test_watchdog_idle_safe_state_demo_and_park(self):
        now = 1000
        idle = life.IdleWatchdog({'safe_states': ['training']})
        sample = {'activity': now, 'state': 'menu', 'new_files': [], 'log_delta': '', 'screen_class': 'menu'}
        self.assertIsNone(idle.decide(now, {'a': sample}))
        self.assertEqual(idle.decide(now, {'a': {**sample, 'activity': now - 301}})['action'], 'down')
        self.assertIsNone(idle.decide(now, {'a': {**sample, 'activity': now - 9999, 'state': 'training'}}))
        demo = life.IdleWatchdog({'idle_s': 300, 'detectors': [
            {'kind': 'new-files', 'pattern': '*-doubles-*', 'directory': '/tmp/unused'}]})
        drifted = demo.decide(now, {'a': {**sample, 'new_files': ['match-doubles-1']}})
        self.assertEqual(drifted['reason'], 'demo-drift')
        self.assertEqual(drifted['action'], 'down')
        self.assertEqual(drifted['detector'], 'new-files')
        screen = life.IdleWatchdog({'detectors': [{'kind': 'screen-class', 'demo_states': ['attract']}]})
        immediate = screen.decide(now, {'a': {**sample, 'screen_class': 'attract'}})
        self.assertEqual(immediate['reason'], 'demo-drift')
        logged = life.IdleWatchdog({'detectors': [{'kind': 'log-line', 'pattern': 'attract'}]})
        self.assertEqual(logged.decide(now, {'a': {**sample, 'log_delta': 'entered attract demo'}})['detector'], 'log-line')
        parked = life.IdleWatchdog({'idle_s': 10, 'action': 'park', 'park': [{'buttons': 'b', 'hold': 150}]})
        decision = parked.decide(100, {'a': {**sample, 'activity': 0, 'state': None}})
        self.assertEqual(decision['action'], 'park')
        self.assertEqual(decision['park'], [{'buttons': 'b', 'hold': 150}])
        seen = []
        self.config['watchdog'] = {'idle_s': 5, 'action': 'down'}
        controller = self.controller()
        controller.watchdog_actor = seen.append
        receipt = controller.watchdog(now=100, samples={'a': {**sample, 'activity': 0}})
        self.assertEqual(receipt['action'], 'down')
        self.assertEqual(seen, [receipt])
        self.assertFalse(self.ledger.claimed)
        self.assertIn('ACTION watchdog', receipt['lines'][0])

    def test_up_starts_the_watchdog_sidecar_and_down_stops_it(self):
        self.config['watchdog'] = {'idle_s': 300, 'poll_s': 2}
        value = self.controller().up()
        unit = value['watchdog_unit']
        self.assertRegex(unit, r'^eden-life-[0-9a-f]{32}-watchdog\.service$')
        self.assertEqual(self.host.started_watchdogs, [unit])
        self.assertIn('watchdog-serve', self.host.watchdog_commands[0])
        self.assertTrue((Path(self.config['state_dir']) / 'lifecycle-watchdog.py').is_file())
        status = self.controller().status()
        self.assertEqual(status['watchdog_unit'], unit)
        self.assertTrue(status['watchdog_running'])
        self.controller().down()
        self.assertEqual(self.host.stopped_watchdogs, [unit])
        self.assertFalse(self.controller().status()['watchdog_running'])
        self.assertTrue(self.ledger.released)

    def test_watchdog_auto_false_does_not_start_a_sidecar(self):
        self.config['watchdog'] = {'idle_s': 300, 'auto': False}
        value = self.controller().up()
        self.assertIsNone(value['watchdog_unit'])
        self.assertEqual(self.host.started_watchdogs, [])

    def test_watchdog_serve_exits_without_a_journal_and_down_starts_nothing(self):
        self.config['watchdog'] = {'idle_s': 30}
        controller = self.controller()
        receipt = controller.watchdog_serve()
        self.assertEqual(receipt['lines'], ['OK watchdog: no journal'])
        self.assertEqual(self.host.started_watchdogs, [])
        with self.assertRaisesRegex(life.Refused, 'No owned lifecycle journal'):
            controller.down()
        self.assertEqual(self.host.stopped_watchdogs, [])
        self.assertFalse(self.ledger.claimed)

    def test_missing_control_log_is_not_idle(self):
        self.config['watchdog'] = {'idle_s': 300, 'auto': False}
        controller = self.controller()
        controller.up()
        samples = controller.observe_watchdog({})
        started = datetime.fromisoformat(controller.state['started']).timestamp()
        self.assertAlmostEqual(samples['a']['activity'], started, delta=1)
        self.assertGreater(samples['a']['activity'], 1_000_000_000)
        self.assertIsNone(life.IdleWatchdog({'idle_s': 300}).decide(samples['a']['activity'] + 30, samples))
        control = Path(controller.state['instances'][0]['run']) / 'control.jsonl'
        older = started - 100
        control.write_text('')
        os.utime(control, (older, older))
        self.assertAlmostEqual(controller.observe_watchdog({})['a']['activity'], started, delta=1)
        newer = started + 50
        os.utime(control, (newer, newer))
        self.assertAlmostEqual(controller.observe_watchdog({})['a']['activity'], newer, delta=1)

    def test_watchdog_action_downs_without_stopping_its_own_unit(self):
        self.config['watchdog'] = {'idle_s': 1, 'auto': False}
        controller = self.controller()
        controller.up()
        controller.state['started'] = '2000-01-01T00:00:00+00:00'
        controller.write()
        receipt = controller.watchdog_serve()
        self.assertEqual(receipt['action'], 'down')
        self.assertEqual(controller.state['phase'], 'down')
        self.assertEqual(self.host.stopped_watchdogs, [])
        self.assertTrue(self.ledger.released)

    def test_sidecar_keeps_the_new_file_baseline_across_rounds(self):
        drift = self.base / 'drift'
        drift.mkdir()
        (drift / 'already-there-doubles-0').write_text('baseline')
        self.config['watchdog'] = {
            'idle_s': 300, 'auto': False, 'poll_s': 1, 'action': 'down',
            'detectors': [{'kind': 'new-files', 'directory': str(drift), 'pattern': '*-doubles-*'}]}
        controller = self.controller()
        controller.up()
        polls = {'n': 0}

        def sleep(seconds):
            if controller.state.get('phase') == 'up':
                polls['n'] += 1
                if polls['n'] == 1:
                    (drift / 'match-doubles-2').write_text('new')
                if polls['n'] > 2:
                    raise AssertionError('new file was treated as a fresh baseline')
            self.host.now += seconds

        self.host.sleep = sleep
        receipt = controller.watchdog_serve()
        self.assertEqual(polls['n'], 1)
        self.assertEqual(receipt['action'], 'down')
        self.assertEqual(receipt['reason'], 'demo-drift')
        self.assertEqual(receipt['detector'], 'new-files')
        self.assertEqual(controller.state['phase'], 'down')
        seen = controller._watchdog_seen['a']
        self.assertIn('already-there-doubles-0', seen)
        self.assertIn('match-doubles-2', seen)

    def test_log_line_keeps_matching_the_recent_window(self):
        self.config['watchdog'] = {
            'idle_s': 300, 'auto': False, 'poll_s': 1,
            'detectors': [{'kind': 'log-line', 'pattern': 'attract demo'}],
            'state_probe': {'source': 'diag_log', 'rules': []}}
        controller = self.controller()
        controller.up()
        instances = json.loads((Path(self.config['state_dir']) / 'instances.json').read_text())
        diag = Path(instances['a']['diag_log'])
        diag.parent.mkdir(parents=True, exist_ok=True)
        diag.write_text('entered attract demo\n')
        self.assertIn('attract demo', controller.observe_watchdog({})['a']['log_delta'])
        self.assertIn('attract demo', controller.observe_watchdog({})['a']['log_delta'])
        diag.write_text('entered attract demo\n' + ''.join('later %d\n' % i for i in range(40)))
        self.assertNotIn('attract demo', controller.observe_watchdog({})['a']['log_delta'])
        diag.write_text('tail\nattract demo\n')
        receipt = controller.watchdog_serve()
        self.assertEqual(receipt['detector'], 'log-line')
        self.assertEqual(receipt['action'], 'down')
        self.assertEqual(controller.state['phase'], 'down')

    def test_declared_sources_are_written_for_the_runner(self):
        self.config['sources'] = {'notes': {'sdmc': 'ultimate/TrainingRooms/notes.log'}}
        self.config['instances'][0]['sources'] = {
            'gates': {'beside': 'diag_log', 'name': 'practice-gates.log'}}
        self.controller().up()
        instances = json.loads((Path(self.config['state_dir']) / 'instances.json').read_text())
        diag = Path(instances['a']['diag_log'])
        self.assertEqual(Path(instances['a']['gates']), diag.with_name('practice-gates.log'))
        self.assertTrue(str(diag).endswith('user/sdmc/ultimate/TrainingRooms/practice-diagnostics.log'))
        self.assertEqual(Path(instances['a']['notes']),
                         Path(instances['a']['profile']) / 'user' / 'sdmc' / 'ultimate' / 'TrainingRooms' / 'notes.log')

    def test_watchdog_start_failure_still_stops_and_releases(self):
        self.config['watchdog'] = {'idle_s': 300}
        self.host.fail_watchdog = True
        with self.assertRaisesRegex(RuntimeError, 'watchdog start failure'):
            self.controller().up()
        self.assertEqual(len(self.host.stopped_watchdogs), 1)
        self.assertTrue(self.ledger.released)
        self.assertEqual(self.state()['phase'], 'down')

    def test_linux_and_windows_watchdog_commands_are_named_for_the_run(self):
        unit = 'eden-life-' + ('ab' * 16) + '-watchdog.service'
        linux = life.Host()
        linux_commands = []
        linux.run = lambda argv, cap=30: linux_commands.append(list(argv)) or ''
        linux.start_watchdog(unit, ['/usr/bin/python3', '-I', '-B', 'lifecycle-watchdog.py',
                                    'watchdog-serve', 'config.json', '--execute'], self.base, 90)
        self.assertEqual(linux_commands[0][0], 'systemd-run')
        self.assertIn('--unit=' + unit, linux_commands[0])
        self.assertIn('--setenv=EDEN_WATCHDOG_SIDECAR=1', linux_commands[0])
        windows = life.WindowsHost({})
        windows_commands = []
        windows.ps = lambda script, cap=30: 'S-1-5-21-1'
        windows.run = lambda argv, cap=30: windows_commands.append(list(argv)) or ''
        directory = self.base / 'win-watchdog'
        directory.mkdir()
        windows.start_watchdog(unit, [r'C:\Python\python.exe', '-I', '-B', r'C:\state\lifecycle-watchdog.py',
                                      'watchdog-serve', r'C:\state\lifecycle-config.json', '--execute'],
                               directory, 90)
        self.assertEqual(windows_commands[0][:2], ['schtasks.exe', '/create'])
        self.assertIn('EdenLife-' + ('ab' * 16) + '-watchdog', windows_commands[0])
        self.assertIn('watchdog-serve', (directory / 'watchdog-task.xml').read_text(encoding='utf-16'))
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as stopped:
            windows.stop_watchdog(unit)
        self.assertIn('/end', stopped.call_args_list[0].args[0])
        self.assertIn('/delete', stopped.call_args_list[1].args[0])

    def test_preflight_reports_a_stale_socket_and_trace_without_removing_them(self):
        run = Path(self.config['state_dir']) / 'run-a'
        run.mkdir(parents=True)
        socket_path = run / 'control.sock'
        socket_path.write_bytes(b'not a live listener')
        trace = run / 'a.trace.jsonl'
        trace.write_text('old trace\n')
        report = self.controller().preflight()
        self.assertTrue(report['ok'], report['message'])
        self.assertTrue(any(line.startswith('REPORT stale control socket:') for line in report['lines']))
        self.assertTrue(any(line.startswith('REPORT stale trace log:') for line in report['lines']))
        self.assertTrue(socket_path.exists())
        self.assertEqual(trace.read_text(), 'old trace\n')

    def test_up_removes_a_stale_socket_and_moves_a_trace_log(self):
        run = Path(self.config['state_dir']) / 'run-a'
        run.mkdir(parents=True)
        import socket
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(run / 'control.sock'))
        trace = run / 'a.trace.jsonl'
        trace.write_text('old trace\n')
        self.controller().up()
        self.assertFalse((run / 'control.sock').exists())
        asides = list(run.glob('a.trace-aside-*.jsonl'))
        self.assertEqual([path.read_text() for path in asides], ['old trace\n'])
        self.assertFalse(trace.exists())
        text = (Path(self.config['state_dir']) / 'actions.jsonl').read_text()
        self.assertIn('stale-runtime', text)
        self.assertIn('removed', text)
        self.assertIn('moved', text)

    def test_socket_ready_is_a_status_reply_not_a_file(self):
        host = life.Host()
        item = {'socket': str(self.base / 'control.sock'), 'app': {'path': str(self.app)}}
        (self.base / 'control.sock').write_bytes(b'stale')
        host.call = lambda *args: (_ for _ in ()).throw(ConnectionRefusedError(111))
        self.assertFalse(host.socket_ready(item))
        host.call = lambda *args: {'ok': True, 'result': {'running': True}}
        self.assertTrue(host.socket_ready(item))
        self.assertTrue((self.base / 'control.sock').exists())

    def test_preflight_requires_a_declared_trace_elf(self):
        self.config['instances'][0]['trace'] = {
            'points': ['guest-point'],
            'symbols': [{'module': 'main', 'elf': str(self.base / 'missing.elf')}]}
        missing = self.controller().preflight()
        self.assertFalse(missing['ok'])
        self.assertIn('Trace ELF missing', missing['message'])
        self.assertFalse(Path(self.config['state_dir']).exists())
        elf = self.base / 'main.elf'
        elf.write_bytes(b'not an executable; existence only')
        self.config['instances'][0]['trace']['symbols'][0]['elf'] = str(elf)
        ready = self.controller().preflight()
        self.assertTrue(ready['ok'], ready['message'])
        self.assertTrue(any(line.startswith('OK trace elf:') for line in ready['lines']))

    def test_pair_preflight_refuses_lan_online_unless_declared(self):
        config = self.source / life.CONFIG
        config.write_text(config.read_text() + '[Network]\nlan_online\\default=false\nlan_online=true\n')
        single = self.controller().preflight()
        self.assertTrue(single['ok'], single['message'])  # a single instance may use LAN Online
        self.pair()
        refused = self.controller().preflight()
        self.assertFalse(refused['ok'])
        self.assertIn('Pair instance a boots with lan_online=true', refused['message'])
        self.assertIn('Pair instance b boots with lan_online=true', refused['message'])
        with self.assertRaises(Exception):
            self.controller().up()
        self.assertFalse(Path(self.config['state_dir']).exists())
        self.assertFalse(self.ledger.claimed)
        fixture = self.base / 'qt-lan-off.ini'
        fixture.write_text(config.read_text().replace('lan_online=true', 'lan_online=false'))
        for item in self.config['instances']:
            item['files'].append({'path': str(fixture), 'sha256': life.digest(fixture), 'to': life.CONFIG})
        pinned = self.controller().preflight()
        self.assertTrue(pinned['ok'], pinned['message'])
        lan = [check for check in pinned['checks'] if check['name'] == 'pair-lan-online']
        self.assertEqual([(check['instance'], check['value'], check['ok']) for check in lan],
                         [('a', 'false', True), ('b', 'false', True)])
        odd = self.base / 'qt-lan-odd.ini'  # fail closed: anything but false/absent refuses the pair
        odd.write_text(config.read_text().replace('lan_online=true', 'lan_online=1'))
        self.config['instances'][1]['files'][-1] = {'path': str(odd), 'sha256': life.digest(odd), 'to': life.CONFIG}
        unclear = self.controller().preflight()
        self.assertFalse(unclear['ok'])
        self.assertIn('Pair instance b boots with lan_online=1', unclear['message'])
        self.assertNotIn('Pair instance a', unclear['message'])
        for item in self.config['instances']:
            item['files'] = [pin for pin in item['files'] if pin['to'] != life.CONFIG]
        self.config['lan_online_pair'] = True
        declared = self.controller().preflight()
        self.assertTrue(declared['ok'], declared['message'])
        self.config['lan_online_pair'] = 'yes'
        with self.assertRaises(Exception):
            life.validate(self.config)


class LinuxStopTests(unittest.TestCase):
    """Synthetic process graph: main, its AppImage mount and LAN helper; no processes run."""
    def setUp(self):
        self.main = {'pid': 30, 'start': 'main-birth', 'boot': 'fake'}
        self.mount = {'pid': 10, 'start': 'fuse-birth', 'boot': 'fake'}
        self.helper = {'pid': 20, 'start': 'helper-birth', 'boot': 'fake'}
        self.live = {p['pid']: p for p in (self.mount, self.helper, self.main)}
        self.item = {'unit': 'eden-life-offline-a.service', 'app': {'path': '/not/executed/Eden.AppImage'},
                     'owned_pids': [self.main, self.mount]}
        self.host, self.now, self.events = life.Host(), 0.0, []
        self.term_exits, self.kill_exits, self.group_exits = True, True, True
        self.host.clock = lambda: self.now
        self.host.sleep = self.wait
        # Deliberately put FUSE/helper BEFORE the main PID. Numeric/snapshot order is not a safe stop order.
        self.host.owned = lambda _: list(self.live.values())
        # The exact same ordering also protects old control-group journals such as lr09.
        self.host.unit = lambda _: {'MainPID': '30' if 30 in self.live else '0', 'ActiveState': 'active',
                                    'KillMode': 'control-group'}
        self.host.identity = lambda pid: self.live.get(pid)
        self.host.run = self.group_stop
        for target in (patch.object(os, 'pidfd_open', side_effect=lambda pid: pid + 1000),
                       patch.object(os, 'close'),
                       patch.object(life.signal, 'pidfd_send_signal', side_effect=self.signal)):
            target.start()
            self.addCleanup(target.stop)

    def wait(self, seconds):
        self.events.append(('wait', seconds))
        self.now += seconds

    def signal(self, fd, sig):
        pid = fd - 1000
        self.events.append(('signal', pid, sig))
        if pid == 30:
            self.assertIn(10, self.live, 'FUSE must still exist while signalling main')
            self.assertIn(20, self.live, 'Helper must not be group-terminated before main')
            exits = self.term_exits if sig == life.signal.SIGTERM else self.kill_exits
            if exits:
                self.live.pop(pid, None)
        else:
            self.assertNotIn(30, self.live, 'Never tear down backing processes before main exits')
            self.live.pop(pid, None)

    def group_stop(self, argv, cap=30):
        self.events.append(('group-stop', list(argv), cap))
        self.assertNotIn(30, self.live, 'lr09 regression: group SIGTERM can remove FUSE while main still runs')
        self.assertEqual(argv, ['systemctl', '--user', 'stop', self.item['unit']])
        self.assertGreater(cap, 0)
        if self.group_exits:
            self.live.clear()
        else:
            raise subprocess.TimeoutExpired(argv, cap)
        return ''

    def signals(self):
        return [(event[1], event[2]) for event in self.events if event[0] == 'signal']

    def test_main_term_exit_precedes_group_even_with_lower_numbered_fuse(self):
        result = self.host.force_stop(self.item, 1)
        self.assertEqual(self.signals(), [(30, life.signal.SIGTERM)])
        self.assertEqual(result['main_pid_stopped'], self.main)
        self.assertEqual(result['main_stop_signals'], ['SIGTERM'])
        self.assertEqual(result['owned_pids_stopped'], [self.main, self.mount, self.helper])
        self.assertFalse(self.live)

    def test_main_ignores_term_kill_main_then_wait_before_any_group_stop(self):
        self.term_exits = False
        result = self.host.force_stop(self.item, 1)
        self.assertEqual(self.signals(), [(30, life.signal.SIGTERM), (30, life.signal.SIGKILL)])
        self.assertEqual(result['main_stop_signals'], ['SIGTERM', 'SIGKILL'])
        self.assertGreater(self.now, 0)
        self.assertLessEqual(self.now, 1)
        self.assertFalse(self.live)

    def test_surviving_main_blocks_group_stop(self):
        self.term_exits = self.kill_exits = False
        with self.assertRaisesRegex(life.Refused, 'main PID remains'):
            self.host.force_stop(self.item, 1)
        self.assertFalse(any(event[0] == 'group-stop' for event in self.events))
        self.assertEqual(set(self.live), {10, 20, 30})
        self.assertLessEqual(self.now, 1)

    def test_group_stop_timeout_only_kills_helpers_after_main_is_gone(self):
        self.group_exits = False
        self.host.force_stop(self.item, 1)
        self.assertEqual(self.signals(), [(30, life.signal.SIGTERM), (10, life.signal.SIGKILL), (20, life.signal.SIGKILL)])
        self.assertFalse(self.live)

    def test_already_gone_main_still_cleans_owned_mount_and_helper(self):
        self.live.pop(30)
        result = self.host.force_stop(self.item, 1)
        self.assertIsNone(result['main_pid_stopped'])
        self.assertEqual(self.signals(), [])
        self.assertFalse(self.live)

    def test_main_pid_not_in_owned_cgroup_refuses_every_signal_and_group_stop(self):
        self.host.owned = lambda _: [self.mount, self.helper]
        with self.assertRaisesRegex(life.Refused, 'Main PID not in owned cgroup'):
            self.host.force_stop(self.item, 1)
        self.assertEqual(self.events, [])

    def test_main_pid_changed_from_journal_refuses_before_signalling(self):
        self.live[30] = {**self.main, 'start': 'new-birth'}
        with self.assertRaisesRegex(life.Refused, 'Main PID differs from journal'):
            self.host.force_stop(self.item, 1)
        self.assertEqual(self.events, [])

    def test_main_pid_reuse_after_pidfd_open_refuses_without_group_stop(self):
        def changed(pid):
            self.live[pid] = {**self.main, 'start': 'reused-after-open'}
            return pid + 1000
        with patch.object(os, 'pidfd_open', side_effect=changed):
            with self.assertRaisesRegex(life.Refused, 'PID changed'):
                self.host.force_stop(self.item, 1)
        self.assertEqual(self.events, [])

    def test_partial_launch_without_pid_receipt_uses_current_owned_main(self):
        self.item.pop('owned_pids')
        self.host.force_stop(self.item, 1)
        self.assertEqual(self.signals(), [(30, life.signal.SIGTERM)])
        self.assertFalse(self.live)

    def test_unsettled_main_pid_refuses_group_stop(self):
        self.host.unit = lambda _: {'MainPID': '0', 'ActiveState': 'activating'}
        with self.assertRaisesRegex(life.Refused, 'Main PID not settled'):
            self.host.force_stop(self.item, 1)
        self.assertEqual(self.events, [])

    def test_display_unit_keeps_its_existing_group_cleanup_path(self):
        item = {'unit': self.item['unit'], 'owned_pids': [self.main, self.mount]}
        self.host.run = lambda *_args, **_kwargs: self.live.clear()
        with patch.object(self.host, 'stop_main', side_effect=AssertionError('Display has no AppImage main')):
            self.host.force_stop(item, 1)
        self.assertEqual(self.signals(), [])
        self.assertFalse(self.live)


class LedgerForwardTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.plan = []
        self.temporary = tempfile.TemporaryDirectory(prefix='ledger-forward-state-')
        self.addCleanup(self.temporary.cleanup)
        self.config = {'ledger': '/tmp/unused-fake-ledger', 'host': 'xps', 'owner': 'Codex test/owner',
                       'token': 'a' * 32, 'state_dir': self.temporary.name}
        self.line = f"2026 CLAIMED: {self.config['owner']} host=xps lifecycle={'a' * 32} Eden lifecycle admitted"
        self.diagnoses = []
        self.diagnosis = patch.object(life, 'ssh_diagnosis', side_effect=lambda *a, **k: self.diagnoses.pop(0))
        self.diagnosis_mock = self.diagnosis.start()
        self.addCleanup(self.diagnosis.stop)

    def diag(self, transient=True, cause='synthetic'):
        return {'line': 'ssh diag fake -> fake-yoga: ' + cause, 'transient': transient, 'phase': 'auth',
                'cause': cause, 'evidence': '/tmp/fake-diag'}

    def run_fake(self, argv, **kwargs):
        mode = json.loads(kwargs['input'])['mode']
        self.calls.append((mode, argv, kwargs))
        response = self.plan.pop(0) if self.plan else 255
        if isinstance(response, Exception):
            raise response
        if isinstance(response, int):
            return subprocess.CompletedProcess(argv, response, '', 'synthetic SSH failure')
        if isinstance(response, str):
            return subprocess.CompletedProcess(argv, 0, response, '')
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), '')

    def request(self, operation):
        return life.ledger_forward('fake-yoga', operation, self.config, run=self.run_fake)

    def read(self, *, claim=False, release=False, opened=None):
        return {'owned_claim': claim, 'owned_release': release, 'open': opened or []}

    def test_success_first_try_has_bounded_ssh_options(self):
        self.plan = [self.read()]
        self.assertEqual(self.request('read'), self.read())
        self.assertEqual([call[0] for call in self.calls], ['ledger-read'])
        for _, argv, kwargs in self.calls:
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], 10)
            for option in ('BatchMode=yes', 'ConnectTimeout=10', 'ServerAliveInterval=5',
                           'ServerAliveCountMax=2'):
                self.assertIn(option, argv)
            self.assertIn('ControlMaster=no', argv)
            self.assertIn('ProxyCommand=/bin/false', argv)
            self.assertNotIn('IPQoS=none', argv)
            self.assertIn('/usr/bin/timeout 6', base64.b64decode(argv[-1]).decode('utf-16-le'))
        self.diagnosis_mock.assert_not_called()

    def test_master_open_consumes_the_same_ledger_attempt_budget(self):
        class BudgetMux(FakeMux):
            def __enter__(self):
                self.attempt_deadline = time.monotonic() + 2
                return self
        self.plan = [self.read()]
        with patch.object(life, 'SshMux', BudgetMux):
            self.assertEqual(self.request('read'), self.read())
        self.assertGreater(self.calls[0][2]['timeout'], 0)
        self.assertLessEqual(self.calls[0][2]['timeout'], 2)

    def test_expired_master_budget_diagnoses_without_starting_a_rider(self):
        class ExpiredMux(FakeMux):
            def __enter__(self):
                self.attempt_deadline = time.monotonic() - 1
                return self
        self.diagnoses = [self.diag(False, 'not established')]
        with patch.object(life, 'SshMux', ExpiredMux), self.assertRaises(life.LedgerTransport):
            self.request('read')
        self.assertEqual(self.calls, [])
        self.diagnosis_mock.assert_called_once()

    def test_bad_json_and_other_nonzero_refuse_without_diagnosis(self):
        self.plan = ['not-json', self.read()]
        with self.assertRaisesRegex(life.Refused, 'invalid ledger reply'):
            self.request('read')
        self.assertEqual(len(self.calls), 1)
        self.diagnosis_mock.assert_not_called()
        self.calls.clear()
        self.plan = [2]
        with self.assertRaisesRegex(life.Refused, 'SSH lifecycle failed: synthetic SSH failure'):
            self.request('read')
        self.assertEqual(len(self.calls), 1)
        self.diagnosis_mock.assert_not_called()

    def test_transient_transport_diagnoses_without_replaying(self):
        self.plan = [255, self.read()]
        self.diagnoses = [self.diag()]
        with self.assertRaises(life.LedgerTransport):
            self.request('read')
        self.assertEqual([c[0] for c in self.calls], ['ledger-read'])
        self.diagnosis_mock.assert_called_once()
        self.assertFalse(self.diagnosis_mock.call_args.kwargs['allow_retry'])
        self.assertEqual(len((Path(self.temporary.name) / 'ssh-diag.md').read_text().splitlines()), 1)

    def test_transport_failure_preserves_one_diagnosis_without_replaying_claim(self):
        self.plan = [255, 255]
        self.diagnoses = [self.diag(True, 'first'), self.diag(True, 'second')]
        with self.assertRaises(life.LedgerTransport) as error:
            self.request('claim')
        self.assertEqual(len(error.exception.diagnoses), 1)
        self.assertFalse(error.exception.transient)
        self.assertIn('first', str(error.exception))
        self.assertNotIn('second', str(error.exception))
        self.assertEqual([c[0] for c in self.calls], ['ledger-claim'])

    def test_non_transient_and_timeout(self):
        self.plan = [255]
        self.diagnoses = [self.diag(False, 'host key')]
        with self.assertRaises(life.LedgerTransport) as error:
            self.request('read')
        self.assertFalse(error.exception.transient)
        self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        self.plan = [subprocess.TimeoutExpired('ssh', 10), self.read()]
        self.diagnoses = [self.diag()]
        with self.assertRaises(life.LedgerTransport):
            self.request('read')
        self.assertEqual(len(self.calls), 1)

    def test_real_classifier_without_target_never_retries_fake_packet_loss(self):
        for loss, expected_attempts in ((20, 1), (0, 1)):
            with self.subTest(loss=loss), tempfile.TemporaryDirectory(prefix='eden-diag-') as tmp:
                attempts = []
                def run(argv, **kwargs):
                    if 'input' in kwargs:
                        attempts.append(argv)
                        return subprocess.CompletedProcess(argv, 255 if len(attempts) == 1 else 0,
                                                           '' if len(attempts) == 1 else json.dumps(self.read()), '')
                    if '-G' in argv:
                        return subprocess.CompletedProcess(argv, 0, 'hostname fake-host\nport 22\n', '')
                    if argv[0] == 'ping':
                        return subprocess.CompletedProcess(argv, 0, f'{loss}% packet loss\n', '')
                    return subprocess.CompletedProcess(argv, 255, '', 'Connecting to fake-host\nConnection established\n')
                def diagnose(host, **kwargs):
                    kwargs['evidence_dir'] = tmp
                    return real_ssh_diagnosis(host, **kwargs, tcp=lambda *_: True)
                self.diagnosis_mock.side_effect = diagnose
                with patch('sys.stderr', new=io.StringIO()):
                    with self.assertRaises(life.LedgerTransport) as error:
                        life.ledger_forward('fake-host', 'read', self.config, run=run)
                    self.assertIn('not established', str(error.exception))
                self.assertEqual(len(attempts), expected_attempts)

    def test_real_classifier_target_health_matrix_for_ledger(self):
        for label, facts, ping, expected_attempts, expected_cause in health_path_cases():
            with self.subTest(label=label), tempfile.TemporaryDirectory(prefix='eden-diag-') as tmp:
                attempts = []
                run = target_fixture_run(facts, attempts, ping, succeed_after_failure=True)
                def diagnose(host, **kwargs):
                    kwargs['evidence_dir'] = tmp
                    return real_ssh_diagnosis(host, **kwargs, tcp=lambda *_: True)
                self.diagnosis_mock.side_effect = diagnose
                with patch('sys.stderr', new=io.StringIO()):
                    with self.assertRaises(life.LedgerTransport) as error:
                        life.ledger_forward('fake-host', 'read', self.config, run=run)
                    if label in ('overload-loss', 'throttled-loss'):
                        self.assertIn(expected_cause, error.exception.diagnoses[0]['cause'])
                    else:
                        self.assertIn('not established (attempt stalled in session;', error.exception.diagnoses[0]['cause'])
                    self.assertFalse(error.exception.transient)
                self.assertEqual(len(attempts), 1)
                self.assertFalse(self.diagnosis_mock.call_args.kwargs['allow_retry'])

    def test_real_overloaded_target_stops_ledger_after_one_attempt(self):
        facts = {'load1': 48, 'cores': 8, 'memAvailMiB': 150, 'oomKills5m': 2,
                 'sshdProcesses': 5, 'sshdUnauth': 0, 'maxStartups': '10:30:100', 'maxStartupsLines': 0}
        with tempfile.TemporaryDirectory(prefix='eden-diag-') as tmp:
            attempts = []
            run = target_fixture_run(facts, attempts)
            def diagnose(host, **kwargs):
                kwargs['evidence_dir'] = tmp
                return real_ssh_diagnosis(host, **kwargs, tcp=lambda *_: True)
            self.diagnosis_mock.side_effect = diagnose
            with patch('sys.stderr', new=io.StringIO()), self.assertRaises(life.LedgerTransport) as error:
                life.ledger_forward('fake-host', 'read', self.config, run=run)
            self.assertEqual(len(attempts), 1)
            self.assertFalse(error.exception.transient)
            self.assertIn('target overloaded (load 48 on 8 cores, 150 MiB available, 2 OOM kills)',
                          str(error.exception))

    def test_claim_lost_ack_is_not_replayed(self):
        with tempfile.TemporaryDirectory(prefix='eden-ledger-ack-') as temporary:
            path = Path(temporary) / 'ledger.txt'
            path.write_text('')
            ledger = life.Ledger(path, 'xps', self.config['owner'], self.config['token'])
            admission = {'max_eden': 2, 'foreign_processes': []}
            calls = []
            def run(argv, **kwargs):
                self.assertEqual(json.loads(kwargs['input'])['mode'], 'ledger-claim')
                calls.append(argv)
                result = ledger.claim(admission=admission)
                return subprocess.CompletedProcess(argv, 255, '', 'lost ACK') if len(calls) == 1 else \
                    subprocess.CompletedProcess(argv, 0, json.dumps(result), '')
            self.diagnoses = [self.diag()]
            with self.assertRaises(life.LedgerTransport):
                life.ledger_forward('fake-yoga', 'claim', self.config, run=run)
            self.assertEqual(len(calls), 1)
            self.assertEqual(path.read_text().count(' CLAIMED:'), 1)

    def test_idempotent_claim_release_and_absent_release(self):
        with tempfile.TemporaryDirectory(prefix='eden-ledger-idempotent-') as temporary:
            path = Path(temporary) / 'ledger.txt'
            path.write_text('')
            ledger = life.Ledger(path, 'xps', self.config['owner'], 'a' * 32)
            admission = {'max_eden': 2, 'foreign_processes': []}
            with self.assertRaisesRegex(life.Refused, 'Exact owned lifecycle claim not found'):
                ledger.release()
            first = ledger.claim(admission=admission)
            second = ledger.claim(admission=admission)
            self.assertEqual(first['line'], second['line'])
            self.assertTrue(second['already_claimed'])
            self.assertEqual(second['stale_claim_skipped'], [])
            released = ledger.release()
            again = ledger.release()
            self.assertEqual(released['line'], again['line'])
            self.assertTrue(again['already_released'])
            self.assertEqual(path.read_text().count(' CLAIMED:'), 1)
            self.assertEqual(path.read_text().count(' RELEASED:'), 1)

    def test_released_lifecycle_token_is_terminal_and_new_token_still_works(self):
        with tempfile.TemporaryDirectory(prefix='eden-ledger-terminal-') as temporary:
            path = Path(temporary) / 'ledger.txt'
            path.write_text('')
            ledger = life.Ledger(path, 'xps', self.config['owner'], 'a' * 32)
            admission = {'max_eden': 2, 'foreign_processes': []}
            ledger.claim(admission=admission)
            self.assertTrue(ledger.claim(admission=admission)['already_claimed'])
            ledger.release()
            self.assertTrue(ledger.release()['already_released'])
            before = path.read_bytes()
            with self.assertRaisesRegex(life.Refused, 'Lifecycle token already released; a new run needs a new token'):
                ledger.claim(admission=admission)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual((ledger.inspect()['owned_claim'], ledger.inspect()['owned_release']), (False, True))
            new = life.Ledger(path, 'xps', self.config['owner'], 'b' * 32)
            new.claim(admission=admission)
            self.assertTrue(new.inspect()['owned_claim'])
            new.release()
            self.assertEqual((new.inspect()['owned_claim'], new.inspect()['owned_release']), (False, True))

    def test_idempotent_reserve(self):
        with tempfile.TemporaryDirectory(prefix='eden-ledger-reserve-') as temporary:
            path = Path(temporary) / 'ledger.txt'
            path.write_text('')
            ledger = life.Ledger(path, 'xps', self.config['owner'], 'b' * 32, label='test-run')
            first = ledger.operate('reserve', approver='operator')
            again = ledger.operate('reserve', approver='operator')
            self.assertEqual(first['line'], again['line'])
            self.assertEqual(first['expires_in_seconds'], 1200)
            self.assertGreaterEqual(again['expires_in_seconds'], 1198)
            self.assertLessEqual(again['expires_in_seconds'], 1200)
            self.assertTrue(again['already_reserved'])
            self.assertEqual(path.read_text().count(' CLAIMED:'), 1)

    def test_reservation_replay_uses_remaining_time_and_renews_expired_token(self):
        with tempfile.TemporaryDirectory(prefix='eden-ledger-age-') as temporary:
            path = Path(temporary) / 'ledger.txt'
            ledger = life.Ledger(path, 'xps', self.config['owner'], 'b' * 32, label='test-run')
            suffix = (f"CLAIMED: {self.config['owner']} host=xps lifecycle={'b' * 32} "
                      'Eden reservation run test-run approver=operator')
            now = datetime.now(timezone.utc)
            path.write_text(f'{(now - timedelta(minutes=10)).isoformat()} {suffix}\n')
            result = ledger.operate('reserve', approver='operator')
            self.assertTrue(result['already_reserved'])
            self.assertGreaterEqual(result['expires_in_seconds'], 598)
            self.assertLessEqual(result['expires_in_seconds'], 600)
            old = f'{(now - timedelta(minutes=22)).isoformat()} {suffix}'
            path.write_text(old + '\n')
            renewed = ledger.operate('reserve', approver='operator')
            self.assertNotIn('already_reserved', renewed)
            self.assertEqual(renewed['expires_in_seconds'], 1200)
            text = path.read_text()
            self.assertLess(text.index('Eden reservation expired'), text.index(renewed['line']))
            self.assertEqual(life.active_claims(text, 'xps'), [renewed['line']])
            self.assertTrue(ledger.operate('reserve', approver='operator')['already_reserved'])
            path.write_text(old + '\n')
            claim = ledger.claim(admission={'max_eden': 2, 'foreign_processes': []})
            text = path.read_text()
            self.assertLess(text.index('Eden reservation expired'), text.index(claim['line']))
            self.assertTrue(ledger.inspect()['owned_claim'])
            self.assertFalse(ledger.inspect()['owned_release'])
            ledger.release()

    def test_converted_reservation_replay_refused_for_same_or_new_claim_token(self):
        for same_token in (False, True):
            with self.subTest(same_token=same_token), tempfile.TemporaryDirectory(prefix='eden-ledger-convert-') as tmp:
                path = Path(tmp) / 'ledger.txt'
                path.write_text('')
                reservation = life.Ledger(path, 'xps', self.config['owner'], 'a' * 32, label='test-run')
                reservation.operate('reserve', approver='operator')
                claimant = life.Ledger(path, 'xps', self.config['owner'],
                                       ('a' if same_token else 'b') * 32, label='test-run')
                self.assertEqual(claimant.claim(admission={'max_eden': 2})['consumed_reservation'], 'a' * 32)
                self.assertTrue(claimant.inspect()['owned_claim'])
                self.assertFalse(claimant.inspect()['owned_release'])
                before = path.read_bytes()
                with self.assertRaisesRegex(life.Refused, 'Reservation already converted to a lifecycle claim'):
                    reservation.operate('reserve', approver='operator')
                self.assertEqual(path.read_bytes(), before)
                claimant.release()
                self.assertFalse(claimant.inspect()['owned_claim'])

    def test_different_tokens_can_still_claim_two_instance_host(self):
        with tempfile.TemporaryDirectory(prefix='eden-ledger-concurrent-') as temporary:
            path = Path(temporary) / 'ledger.txt'
            path.write_text('')
            first = life.Ledger(path, 'xps', self.config['owner'], 'a' * 32)
            second = life.Ledger(path, 'xps', self.config['owner'], 'b' * 32)
            admission = {'max_eden': 2, 'foreign_processes': []}
            first.claim(admission=admission)
            second.claim(admission=admission)
            self.assertEqual(len(first.inspect()['open']), 2)

    def test_linux_settle_unit_is_bounded_collected_and_low_priority(self):
        host = life.Host()
        command = ['/usr/bin/python3', 'fake-script']
        with patch.dict(os.environ, {'XDG_STATE_HOME': '/tmp/fake-state-home'}), patch.object(host, 'run') as run:
            host.start_settle('eden-settle-1234abcd.service', command, Path('/tmp/fake-state'), 1800)
        argv = run.call_args.args[0]
        self.assertIn('--collect', argv)
        self.assertIn('--property=Nice=19', argv)
        self.assertIn('--property=MemoryMax=256M', argv)
        self.assertIn('--property=RuntimeMaxSec=1860', argv)
        self.assertIn('--setenv=XDG_STATE_HOME=/tmp/fake-state-home', argv)
        self.assertEqual(argv[-2:], command)
        with self.assertRaisesRegex(life.Refused, 'Foreign settle unit'):
            host.start_settle('foreign.service', command, Path('/tmp/fake-state'), 1800)
        with patch.object(life.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as stop:
            host.stop_settle('eden-settle-1234abcd.service')
        self.assertEqual(stop.call_args.args[0], ['systemctl', '--user', 'stop', 'eden-settle-1234abcd.service'])
        self.assertEqual(stop.call_args.kwargs['timeout'], 15)
        with self.assertRaisesRegex(life.Refused, 'Foreign settle unit'):
            host.stop_settle('foreign.service')
        for state in ('active', 'activating', 'reloading', 'deactivating', 'inactive', 'failed'):
            with self.subTest(state=state), patch.object(host, 'unit', return_value={'ActiveState': state}):
                self.assertEqual(host.settle_running('eden-settle-1234abcd.service'),
                                 state in ('active', 'activating', 'reloading', 'deactivating'))


class SshDiagnosisTests(unittest.TestCase):
    def test_transport_classifier_and_receipt_preserve_application_failures(self):
        with tempfile.TemporaryDirectory(prefix='transport-test-') as temporary:
            mux = FakeMux('fake-host')
            self.addCleanup(mux._temporary.cleanup)
            diagnosis = {'line': 'ssh diag fake: fixture', 'phase': 'session', 'transient': False}
            cases = [('ssh', 255, '', True), ('ssh', 1, 'remote application failed', False),
                     ('rsync', 255, '', True), ('rsync', 10, '', True), ('rsync', 12, '', True),
                     ('rsync', 30, '', True), ('rsync', 35, '', True), ('rsync', 23, '', False),
                     ('scp', 255, '', True), ('scp', 1, 'Connection reset by peer', True),
                     ('scp', 1, 'no such file', False)]
            for kind, code, stderr, expected in cases:
                with self.subTest(kind=kind, code=code, stderr=stderr), \
                        patch.object(life, 'ssh_diagnosis', return_value=diagnosis) as diagnose:
                    value = life.ssh_transport_failure(mux, kind, code, stderr)
                    self.assertEqual(value is not None, expected)
                    self.assertEqual(diagnose.call_count, int(expected))
                    if expected:
                        self.assertEqual(diagnose.call_args.kwargs['attempt_log'], mux.master_log)
                        self.assertFalse(diagnose.call_args.kwargs['allow_retry'])
                        self.assertTrue(diagnose.call_args.kwargs['rider'])
            with patch.object(mux, '_control', return_value=subprocess.CompletedProcess([], 255)), \
                    patch.object(life, 'ssh_diagnosis', return_value=diagnosis) as diagnose:
                self.assertEqual(life.ssh_transport_failure(mux, 'scp', 1, 'no such file'), diagnosis)
                diagnose.assert_called_once()
            for kind in ('ssh', 'rsync', 'scp'):
                with self.subTest(timeout=kind), patch.object(life, 'ssh_diagnosis', return_value=diagnosis) as diag:
                    self.assertEqual(life.ssh_transport_failure(mux, kind, None, '', timed_out=True), diagnosis)
                    self.assertTrue(diag.call_args.kwargs['attempt_timed_out'])
            life.ssh_receipt({**diagnosis, 'kind': 'scp', 'what': 'report'}, temporary)
            lines = (Path(temporary) / 'ssh-diag.md').read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn(' scp report : ssh diag fake: fixture', lines[0])
            with patch.object(Path, 'mkdir', side_effect=PermissionError('read only')):
                life.ssh_receipt({**diagnosis, 'kind': 'ssh', 'what': 'probe'}, temporary)

    def test_rider_uses_master_session_and_separate_diagnostic_login(self):
        with tempfile.TemporaryDirectory(prefix='rider-phase-') as temporary:
            attempt = Path(temporary) / 'master.log'
            attempt.write_text('SSH2_MSG_NEWKEYS received\nAuthenticated to fake-host\n')
            run = target_fixture_run(None, [])
            with patch('sys.stderr', new=io.StringIO()):
                result = real_ssh_diagnosis('fake-host', run=run, evidence_dir=temporary,
                                            tcp=lambda *_: True, attempt_log=attempt, rider=True,
                                            allow_retry=False)
            self.assertEqual(result['phase'], 'session')
            self.assertIn('attempt phase session (rider on an open master)', result['line'])
            self.assertIn('diag login failed, phase banner', result['line'])
            self.assertIn('retry: no', result['line'])

    def test_forward_and_notify_diagnose_once_without_replay(self):
        for code in (255, 1):
            with self.subTest(code=code), tempfile.TemporaryDirectory(prefix='rider-forward-') as temporary:
                diagnosis = {'line': 'ssh diag named', 'phase': 'session', 'transient': False}
                response = subprocess.CompletedProcess([], code, 'application output', 'application error')
                run = unittest.mock.Mock(return_value=response)
                with patch.object(life, 'ssh_diagnosis', return_value=diagnosis) as diag:
                    with self.assertRaises(life.Refused) as error:
                        life.forward('fake-host', 'status', {'state_dir': temporary}, False, run=run)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(diag.call_count, int(code == 255))
                self.assertEqual('ssh diag named' in str(error.exception), code == 255)
                receipt = Path(temporary) / 'ssh-diag.md'
                self.assertEqual(receipt.is_file(), code == 255)
                if receipt.exists():
                    self.assertEqual(len(receipt.read_text().splitlines()), 1)

                notify_dir = Path(temporary) / 'notify'
                notify_dir.mkdir()
                controller = life.Lifecycle.__new__(life.Lifecycle)
                controller.c = {'notify': {'ssh': 'fake-host', 'file': '/fake/notice'},
                                'label': 'fixture', 'owner': 'Codex test/owner'}
                controller.directory, controller.state = notify_dir, {}
                controller.record = unittest.mock.Mock()
                controller.write = unittest.mock.Mock()
                with patch.object(life.subprocess, 'run', return_value=response) as run, \
                        patch.object(life, 'ssh_diagnosis', return_value=diagnosis) as diag:
                    controller.notify('failure')
                self.assertEqual(run.call_count, 1)
                self.assertEqual(diag.call_count, int(code == 255))
                self.assertEqual('ssh diag named' in controller.state['notify_errors'][0]['error'], code == 255)
                self.assertEqual((notify_dir / 'ssh-diag.md').is_file(), code == 255)

    def test_receipt_write_failure_does_not_mask_forward_stop(self):
        with tempfile.TemporaryDirectory(prefix='receipt-readonly-') as temporary:
            response = subprocess.CompletedProcess([], 255, '', 'connection reset')
            diagnosis = {'line': 'ssh diag named', 'phase': 'session', 'transient': False}
            original_open = Path.open
            def open_file(path, *args, **kwargs):
                if path.name == 'ssh-diag.md':
                    raise PermissionError('read only')
                return original_open(path, *args, **kwargs)
            with patch.object(life, 'ssh_diagnosis', return_value=diagnosis), patch.object(Path, 'open', open_file):
                with self.assertRaises(life.Refused) as error:
                    life.forward('fake-host', 'status', {'state_dir': temporary}, False,
                                 run=lambda *_args, **_kwargs: response)
            self.assertIn('SSH lifecycle failed: connection reset; ssh diag named', str(error.exception))

    def test_unwritable_diagnostic_directory_uses_temporary_evidence(self):
        with tempfile.TemporaryDirectory(prefix='unwritable-diag-') as temporary:
            blocked = Path(temporary) / 'not-a-directory'
            blocked.write_text('fixture')
            run = target_fixture_run(None, [])
            with patch('sys.stderr', new=io.StringIO()):
                diagnosis = real_ssh_diagnosis('fake-host', run=run, evidence_dir=blocked,
                                               tcp=lambda *_: True, allow_retry=False)
            evidence = Path(diagnosis['evidence'])
            try:
                self.assertTrue(evidence.is_file())
                self.assertIn('attempt phase unknown', diagnosis['line'])
            finally:
                shutil.rmtree(evidence.parent)

    def test_windows_disconnection_diagnoses_255_not_application_exit(self):
        class Process:
            def __init__(self, code):
                self.code = code
                self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(''), io.StringIO('remote failed')
            def wait(self, timeout=None):
                return self.code
            def poll(self):
                return self.code
        for code in (255, 1):
            with self.subTest(code=code), tempfile.TemporaryDirectory(prefix='rider-windows-') as temporary:
                process = Process(code)
                calls = unittest.mock.Mock(return_value=process)
                diagnosis = {'line': 'ssh diag named', 'phase': 'session', 'transient': False}
                with patch.object(life, 'ssh_diagnosis', return_value=diagnosis) as diag:
                    with self.assertRaises(life.Refused) as error:
                        life.forward_windows('fake-ally', 'status', {'state_dir': temporary}, False, calls)
                self.assertEqual(calls.call_count, 1)
                self.assertEqual(diag.call_count, int(code == 255))
                self.assertEqual('ssh diag named' in str(error.exception), code == 255)
                self.assertEqual((Path(temporary) / 'ssh-diag.md').is_file(), code == 255)

    def test_inaccessible_or_disappearing_socket_does_not_mask_diagnosis(self):
        with tempfile.TemporaryDirectory(prefix='eden-socket-') as tmp:
            (Path(tmp) / 'socket').touch()
            with patch.object(Path, 'lstat', side_effect=OSError('socket vanished')):
                self.assertEqual(life._control_socket_count(tmp), 0)

    def test_gate_fixture_phases_and_causes(self):
        fixtures = Path(__file__).with_name('test-data') / 'ssh-vvv'
        phases = {'tcp': 'tcp', 'banner': 'kex', 'kex': 'kex', 'auth-timeout': 'auth',
                  'auth-refused': 'auth', 'session': 'session', 'later-kex': 'kex', 'unrelated': 'unknown'}
        for name, phase in phases.items():
            with self.subTest(name=name):
                text = (fixtures / (name + '.txt')).read_text()
                self.assertEqual(life.ssh_phase(text), phase)
                cause, transient, _ = life.ssh_cause(text, phase, f'failed, phase {phase}', 'ok', '0%')
                expected = 'authentication refused' if name == 'auth-refused' else \
                    f'not established (attempt stalled in {phase}; failed, phase {phase})'
                self.assertEqual((cause, transient), (expected, False))
        text = (fixtures / 'review-kex.txt').read_text()
        self.assertEqual(life.ssh_phase(text), 'kex')
        cause, transient, _ = life.ssh_cause(text, 'kex', 'failed, phase kex', 'ok', '0%')
        self.assertEqual(cause, 'not established (attempt stalled in kex; failed, phase kex)')
        self.assertFalse(transient)
        prefix = 'Connecting to x\nConnection established\nRemote protocol version 2.0\n'
        for sample, phase in (('', 'unknown'), ('other', 'unknown'), ('Connecting to x', 'tcp'),
                              ('Connecting to x\nConnection established', 'banner'),
                              (prefix, 'kex'), (prefix + 'SSH2_MSG_NEWKEYS received', 'auth'),
                              (prefix + 'SSH2_MSG_NEWKEYS received\nAuthenticated to x', 'session'),
                              ('SSH2_MSG_NEWKEYS received', 'auth'), ('Authenticated to x', 'session'),
                              ('Entering interactive session', 'session')):
            self.assertEqual(life.ssh_phase(sample), phase)

    def test_cause_precedence_and_tcp_failure(self):
        for text, cause in (('Could not resolve hostname x', 'name does not resolve'),
                            ('Host key verification failed', 'host key'),
                            ('Permission denied', 'authentication refused'),
                            ('Connection refused', 'host down or sshd not listening')):
            with self.subTest(text=text):
                actual, transient, _ = life.ssh_cause(text, 'unknown', 'failed, phase unknown', 'fail', 'n/a')
                self.assertEqual(actual, cause)
                self.assertFalse(transient)

    def test_named_cause_rows_and_preauth_only(self):
        throttled = {'maxStartupsLines': 0, 'sshdUnauth': 10, 'maxStartups': '10:30:100'}
        overload = {'load1': 32, 'cores': 8, 'memAvailMiB': 999, 'oomKills5m': 0}
        history = {'maxStartupsLines': 0, 'sshdUnauth': 0, 'maxStartups': '10:30:100',
                   'sshdPreauth5m': {'total': 2, 'client': 1}, 'sshdProcesses': 20}
        cases = (
            ({'maxStartupsLines': 1}, '0%', 'sshd connection throttling on the target (MaxStartups)', False),
            (throttled, '0%', 'sshd connection throttling on the target (MaxStartups)', False),
            (overload, '0%', 'target overloaded (load 32 on 8 cores, 999 MiB available, 0 OOM kills)', False),
            (None, '20%', 'packet loss 20% on the path to fake-host; target health not confirmed, no retry', False),
            (history, '0%', 'not established (attempt stalled in kex; ok in 0.5 s)', False),
            ({**history, 'sshdUnauth': '10', 'maxStartupsLines': -1}, '0%',
             'not established (attempt stalled in kex; ok in 0.5 s)', False),
        )
        for facts, loss, expected, transient in cases:
            with self.subTest(expected=expected):
                cause, actual, _ = life.ssh_cause('', 'kex', 'ok in 0.5 s', 'ok', loss, facts, 'fake-host')
                self.assertEqual((cause, actual), (expected, transient))

    def test_i6_health_facts_and_windows_gate(self):
        normal = {'load1': 1, 'cores': 8, 'memAvailMiB': 1000, 'oomKills5m': 0,
                  'maxStartupsLines': 0, 'sshdUnauth': 0, 'maxStartups': '10:30:100'}
        for facts in (None, {k: v for k, v in normal.items() if k != 'memAvailMiB'},
                      {**normal, 'oomKills5m': None}, {**normal, 'oomKills5m': '0'},
                      {**normal, 'oomKills5m': False}, {**normal, 'cores': 0},
                      {k: v for k, v in normal.items() if k != 'maxStartups'}):
            with self.subTest(facts=facts):
                self.assertFalse(life.target_healthy('ok in 0.5 s', facts))
                for loss, low, high in (('20%', None, None), ('0%', '2', '140')):
                    cause, transient, _ = life.ssh_cause('', 'kex', 'ok in 0.5 s', 'ok', loss, facts,
                                                          'x', low, high, True)
                    self.assertFalse(transient)
                    self.assertTrue(cause.endswith('; target health not confirmed, no retry'))
                self.assertFalse(life.ssh_cause('', 'kex', 'ok in 0.5 s', 'ok', '0%', facts, 'x',
                                                attempt_timed_out=True)[1])
        self.assertFalse(life.target_healthy('failed, phase kex', normal))
        for cpu, memory in ((100, 4096), (5, 8192), (None, 1000)):
            windows = {'cpuLoadPct': cpu, 'memAvailMiB': memory, 'sshdUnauth': None,
                       'journal': 'unavailable on Windows'}
            self.assertFalse(life.target_healthy('ok in 0.5 s', windows))
            for loss, low, high in (('20%', None, None), ('0%', '2', '140')):
                cause, transient, _ = life.ssh_cause('', 'kex', 'ok in 0.5 s', 'ok', loss, windows, 'x', low, high)
                self.assertFalse(transient)
                self.assertTrue(cause.endswith('; target health not confirmed, no retry'))
            cause, transient, _ = life.ssh_cause('', 'kex', 'ok in 0.5 s', 'ok', '0%', windows, 'x',
                                                  attempt_timed_out=True)
            self.assertEqual(cause, 'not established (attempt stalled in kex; ok in 0.5 s); '
                                    'target health not confirmed, no retry')
            self.assertFalse(transient)
        self.assertFalse(life.target_healthy('ok in 0.5 s', {**normal, 'cpuLoadPct': 5}))

    def test_i5_rtt_timeout_and_real_message_boundaries(self):
        normal = {'load1': 1, 'cores': 8, 'memAvailMiB': 1000, 'oomKills5m': 0,
                  'maxStartupsLines': 0, 'sshdUnauth': 0, 'maxStartups': '10:30:100'}
        red = {**normal, 'load1': 48, 'memAvailMiB': 150, 'oomKills5m': 2}
        for facts, expected in ((red, 'target overloaded (load 48 on 8 cores, 150 MiB available, 2 OOM kills)'),
                                ({**normal, 'sshdUnauth': 10}, 'sshd connection throttling on the target (MaxStartups)')):
            cause, transient, _ = life.ssh_cause('Connection to x timed out', 'kex', 'ok in 0.5 s',
                                                  'ok', '0%', facts, 'x', '1', '200', True)
            self.assertEqual((cause, transient), (expected, False))
        spike = life.ssh_cause('', 'kex', 'ok in 0.5 s', 'ok', '0%', normal, 'x', '10.000', '100.000')
        self.assertEqual(spike[:2], ('RTT spikes on the path to x (max 100.000 ms)', True))
        for low, high in (('10.001', '100.000'), ('1', '99.999')):
            self.assertFalse(life.ssh_cause('', 'kex', 'ok in 0.5 s', 'ok', '0%', normal, 'x', low, high)[1])
        for phase in ('tcp', 'banner', 'kex'):
            cause, transient, _ = life.ssh_cause('', phase, 'ok in 0.5 s', 'ok', '0%', normal, 'x',
                                                 attempt_timed_out=True)
            self.assertEqual((cause, transient),
                             (f'single {phase} timeout with TCP 22 answering and a healthy target '
                              '(load 1 on 8 cores, 1000 MiB available)', True))
        for phase, diag, target in (('kex', 'failed, phase kex', None), ('auth', 'ok in 0.5 s', normal),
                                    ('kex', 'failed, phase kex', normal)):
            self.assertFalse(life.ssh_cause('', phase, diag, 'ok', '0%', target, 'x',
                                             attempt_timed_out=True)[1])
        retained = (Path(__file__).with_name('test-data') / 'ssh-vvv' / 'i4-kex-timeout.txt').read_text()
        self.assertEqual(life.ssh_cause(retained, 'kex', 'failed, phase kex', 'ok', '0%')[2],
                         'Connection to 127.0.0.1 port 56029 timed out')
        self.assertEqual(life.ssh_cause('Connection reset by old\n  Connection to x timed out  ',
                                        'kex', 'failed', 'ok', '0%')[2], 'Connection to x timed out')
        self.assertEqual(len(life.ssh_cause('Permission denied ' + 'x' * 200, 'auth', '', 'ok', '0%')[2]), 160)
        self.assertEqual(life.ssh_cause('', 'kex', 'failed', 'ok', '0%', attempt_killed=True)[2],
                         'none (attempt killed at the 10 s bound)')
        self.assertEqual(life.ssh_cause('', 'kex', 'failed', 'ok', '0%')[2], '')

    def test_diagnosis_parses_rtt_and_qualifying_timeout(self):
        target = {'load1': 1, 'cores': 8, 'memAvailMiB': 1000, 'oomKills5m': 0,
                  'sshdProcesses': 5, 'sshdUnauth': 0, 'maxStartups': '10:30:100', 'maxStartupsLines': 0}
        with tempfile.TemporaryDirectory(prefix='eden-diag-') as tmp:
            attempt = Path(tmp) / 'master.log'
            attempt.write_text('Connecting to x\nConnection established\nRemote protocol version 2.0\n')
            for ping_text, timed, expected in (
                    ('0% packet loss\nrtt min/avg/max/mdev = 10.000/20.000/100.000/1.000 ms\n', False,
                     'RTT spikes on the path to fake-host (max 100.000 ms)'),
                    ('0% packet loss\nrtt min/avg/max/mdev = 1.000/2.000/3.000/1.000 ms\n', True,
                     'single kex timeout with TCP 22 answering and a healthy target (load 1 on 8 cores, 1000 MiB available)')):
                with self.subTest(expected=expected), patch('sys.stderr', new=io.StringIO()):
                    result = life.ssh_diagnosis('fake-host', run=target_fixture_run(target, [], ping_text),
                                                evidence_dir=tmp, tcp=lambda *_: True, attempt_log=attempt,
                                                attempt_timed_out=timed, attempt_killed=timed)
                self.assertEqual((result['cause'], result['transient']), (expected, True))
                if timed:
                    self.assertIn('message none (attempt killed at the 10 s bound);', result['line'])

    def test_attempt_log_is_distinct_from_diagnostic_login(self):
        with tempfile.TemporaryDirectory(prefix='eden-diag-') as tmp:
            attempt = Path(tmp) / 'master.log'
            text = (Path(__file__).with_name('test-data') / 'ssh-vvv' / 'review-kex.txt').read_text()
            attempt.write_text(text)
            def run(argv, **kwargs):
                if '-vvv' in argv:
                    return subprocess.CompletedProcess(argv, 255, '', 'Connecting to x\nConnection established\n')
                if '-G' in argv:
                    return subprocess.CompletedProcess(argv, 0, 'hostname fake-host\nport 22\n', '')
                if argv[0] == 'ping':
                    return subprocess.CompletedProcess(argv, 0, '10 packets transmitted, 10 received, 0% packet loss\n', '')
                return subprocess.CompletedProcess(argv, 255, '', '')
            with patch('sys.stderr', new=io.StringIO()):
                result = life.ssh_diagnosis('fake-host', run=run, evidence_dir=tmp,
                                            tcp=lambda *_: True, attempt_log=attempt)
            self.assertEqual((result['phase'], result['transient']), ('kex', False))
            self.assertEqual(Path(result['evidence']).read_text(), text)
            self.assertIn('diag login failed, phase banner', result['line'])
            self.assertIn('retry: no', result['line'])

    def test_successful_diagnostic_collects_target_facts(self):
        with tempfile.TemporaryDirectory(prefix='eden-diag-') as tmp:
            attempt = Path(tmp) / 'master.log'
            attempt.write_text((Path(__file__).with_name('test-data') / 'ssh-vvv' / 'review-kex.txt').read_text())
            calls = []
            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                if '-vvv' in argv:
                    return subprocess.CompletedProcess(argv, 0, '', 'Authenticated to fake-host')
                if '-G' in argv:
                    return subprocess.CompletedProcess(argv, 0, 'hostname fake-host\nport 22\n', '')
                if argv[0] == 'ping':
                    return subprocess.CompletedProcess(argv, 0, '0% packet loss\n', '')
                if 'EncodedCommand' in argv[-1]:
                    target = {'sshdProcesses': 12, 'sshdUnauth': 10, 'maxStartups': '10:30:100',
                              'maxStartupsLines': 0, 'memAvailMiB': 1000}
                    return subprocess.CompletedProcess(argv, 0, json.dumps(target), '')
                return subprocess.CompletedProcess(argv, 0, '', '')
            with patch('sys.stderr', new=io.StringIO()):
                result = life.ssh_diagnosis('fake-host', run=run, evidence_dir=tmp,
                                            tcp=lambda *_: True, attempt_log=attempt)
            self.assertEqual(result['cause'], 'sshd connection throttling on the target (MaxStartups)')
            self.assertFalse(result['transient'])
            self.assertIn('unauth 10/10', result['line'])
            self.assertEqual([kwargs['timeout'] for _, kwargs in calls], [10, 10, 5, 8, 10])


class SshMuxTests(unittest.TestCase):
    def test_real_classifier_target_health_matrix_for_master(self):
        for label, facts, ping, expected_attempts, expected_cause in health_path_cases():
            with self.subTest(label=label), tempfile.TemporaryDirectory(prefix='emux-') as tmp, \
                    patch.dict(os.environ, {'XDG_RUNTIME_DIR': tmp}):
                attempts, diagnoses = [], []
                run = target_fixture_run(facts, [], ping)
                class Process:
                    pid = 123456
                    def __init__(self, status):
                        self.status = status
                    def wait(self, timeout=None):
                        return self.status
                    def poll(self):
                        return self.status
                def popen(argv, **_kwargs):
                    attempts.append(argv)
                    Path(argv[argv.index('-E') + 1]).write_text(
                        'Connecting to fake-host\nConnection established\nRemote protocol version 2.0\n'
                        'SSH2_MSG_KEXINIT sent\nConnection to fake-host port 22 timed out\n')
                    return Process(0 if len(attempts) > 1 else 255)
                def diagnose(host, **kw):
                    result = real_ssh_diagnosis(host, **kw, tcp=lambda *_: True)
                    diagnoses.append(result)
                    return result
                with patch.object(life, 'ssh_diagnosis', side_effect=diagnose), patch('sys.stderr', new=io.StringIO()):
                    if expected_attempts == 2:
                        with real_ssh_mux('fake-host', run=run, popen=popen, evidence_dir=tmp,
                                          windows=label.startswith('windows-')):
                            pass
                    else:
                        with self.assertRaises(life.LedgerTransport):
                            with real_ssh_mux('fake-host', run=run, popen=popen, evidence_dir=tmp,
                                              windows=label.startswith('windows-')):
                                self.fail('unexpected successful master')
                self.assertEqual(len(attempts), expected_attempts)
                self.assertEqual(len(diagnoses), 1)
                self.assertIn(expected_cause, diagnoses[0]['cause'])
                self.assertEqual(diagnoses[0]['transient'], expected_attempts == 2)

    def test_real_overloaded_target_stops_master_after_one_attempt(self):
        facts = {'load1': 48, 'cores': 8, 'memAvailMiB': 150, 'oomKills5m': 2,
                 'sshdProcesses': 5, 'sshdUnauth': 0, 'maxStartups': '10:30:100', 'maxStartupsLines': 0}
        with tempfile.TemporaryDirectory(prefix='emux-') as tmp, patch.dict(os.environ, {'XDG_RUNTIME_DIR': tmp}):
            attempts = []
            run = target_fixture_run(facts, [])
            class Process:
                pid = 123456
                def wait(self, timeout=None):
                    return 255
                def poll(self):
                    return 255
            def popen(argv, **_kwargs):
                attempts.append(argv)
                Path(argv[argv.index('-E') + 1]).write_text(
                    'Connecting to fake-host\nConnection established\nRemote protocol version 2.0\n'
                    'SSH2_MSG_KEXINIT sent\nConnection to fake-host port 22 timed out\n')
                return Process()
            def diagnose(host, **kw):
                return real_ssh_diagnosis(host, **kw, tcp=lambda *_: True)
            with patch.object(life, 'ssh_diagnosis', side_effect=diagnose), patch('sys.stderr', new=io.StringIO()), \
                    self.assertRaises(life.LedgerTransport) as error:
                with real_ssh_mux('fake-host', run=run, popen=popen, evidence_dir=tmp):
                    self.fail('overloaded target retried')
            self.assertEqual(len(attempts), 1)
            self.assertFalse(error.exception.transient)
            self.assertIn('target overloaded (load 48 on 8 cores, 150 MiB available, 2 OOM kills)',
                          str(error.exception))

    def test_master_riders_nested_cleanup_and_stale_socket(self):
        commands, masters = [], []
        class Process:
            pid = 123456
            def wait(self, timeout=None):
                return 0
            def poll(self):
                return 0
        def popen(argv, **kwargs):
            masters.append((argv, kwargs))
            return Process()
        def run(argv, **kwargs):
            commands.append((argv, kwargs))
            if '-G' in argv:
                template = next(item for item in argv if item.startswith('ControlPath='))[len('ControlPath='):]
                return subprocess.CompletedProcess(argv, 0, 'controlpath ' + template.replace('%C', 'resolved') + '\n', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        with tempfile.TemporaryDirectory(prefix='emux-') as temporary, \
                patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary}):
            stale = Path(temporary) / 'eden-ssh-999999999'
            stale.mkdir()
            recycled = Path(temporary) / f'eden-ssh-{os.getpid()}'
            recycled.mkdir()
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(stale / 'socket'))
            old_sock = socket.socket(socket.AF_UNIX)
            old_sock.bind(str(recycled / 'socket'))
            try:
                with real_ssh_mux('fake-host', run=run, popen=popen) as outer:
                    self.assertEqual(outer.root.stat().st_mode & 0o777, 0o700)
                    self.assertFalse(stale.exists())
                    with real_ssh_mux('fake-host', run=run, popen=popen) as inner:
                        inner.ensure()
                        self.assertEqual(outer.rider_args, inner.rider_args)
                        self.assertIn('ProxyCommand=/bin/false', inner.rider_args)
                        self.assertEqual(len(masters), 1)
                    with real_ssh_mux('another-alias', run=run, popen=popen,
                                      ssh_options=('-o', 'IdentitiesOnly=yes')) as alias:
                        self.assertEqual(outer.key, alias.key)
                        self.assertEqual(len(masters), 1)
                    self.assertTrue(outer.root.is_dir())
                self.assertFalse(outer.root.exists())
            finally:
                sock.close()
                old_sock.close()
            self.assertEqual(len([argv for argv, _ in commands if '-O' in argv and
                                  argv[argv.index('-O') + 1] == 'exit']), 3)
            self.assertIn('ControlMaster=yes', masters[0][0])
            self.assertIn('ControlPersist=120', masters[0][0])
            self.assertTrue(masters[0][1]['start_new_session'])
            for option in life.SSH_OPTIONS:
                self.assertIn(option, masters[0][0])

    def test_unavailable_ssh_config_is_a_diagnosed_transport_failure(self):
        calls = []
        def run(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 255, '', 'cannot read configuration')
        diagnosis = {'line': 'ssh diag fake: not established', 'transient': False,
                     'cause': 'not established', 'phase': 'unknown', 'evidence': '/tmp/diag'}
        with tempfile.TemporaryDirectory(prefix='emux-') as temporary, \
                patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary}), \
                patch.object(life, 'ssh_diagnosis', return_value=diagnosis) as diag:
            with self.assertRaisesRegex(life.LedgerTransport, 'ssh diag fake: not established'):
                with real_ssh_mux('fake-host', run=run):
                    self.fail('master started')
        self.assertEqual(len(calls), 1)
        self.assertFalse(diag.call_args.kwargs['allow_retry'])

    def test_nontransient_master_failure_never_starts_rider(self):
        class Process:
            pid = 123456
            def wait(self, timeout=None):
                return 255
            def poll(self):
                return 255
        calls = []
        def popen(argv, **_kwargs):
            calls.append(argv)
            return Process()
        def run(argv, **_kwargs):
            if '-G' in argv:
                template = next(item for item in argv if item.startswith('ControlPath='))[len('ControlPath='):]
                return subprocess.CompletedProcess(argv, 0, 'controlpath ' + template.replace('%C', 'resolved') + '\n', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        diagnosis = {'line': 'ssh diag fake: host key', 'transient': False,
                     'cause': 'host key', 'phase': 'kex', 'evidence': '/tmp/diag'}
        with tempfile.TemporaryDirectory(prefix='emux-') as temporary, \
                patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary}), \
                patch.object(life, 'ssh_diagnosis', return_value=diagnosis):
            with self.assertRaisesRegex(life.Refused, 'ssh diag fake: host key'):
                with real_ssh_mux('fake-host', run=run, popen=popen):
                    self.fail('rider started')
        self.assertEqual(len(calls), 1)

    def _expired_master_ledger(self, restart_ok):
        with tempfile.TemporaryDirectory(prefix='emux-') as temporary, \
                patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary}):
            checks, masters, riders = [], [], []
            class Process:
                pid = 123456
                def __init__(self, status):
                    self.status = status
                def wait(self, timeout=None):
                    return self.status
                def poll(self):
                    return self.status
            def popen(argv, **_kwargs):
                if masters:
                    self.assertFalse(Path(control_path).exists(), 'expired socket was not removed')
                masters.append(argv)
                return Process(0 if len(masters) == 1 or restart_ok else 255)
            def run(argv, **kwargs):
                if '-G' in argv:
                    template = next(x for x in argv if x.startswith('ControlPath='))[len('ControlPath='):]
                    return subprocess.CompletedProcess(argv, 0, 'controlpath ' + template.replace('%C', 'c') + '\n', '')
                if '-O' in argv and argv[argv.index('-O') + 1] == 'check':
                    checks.append(argv)
                    return subprocess.CompletedProcess(argv, 255 if len(checks) == 2 else 0, '', '')
                if 'input' in kwargs:
                    riders.append(argv)
                    return subprocess.CompletedProcess(argv, 0, json.dumps({'owned_claim': True}), '')
                return subprocess.CompletedProcess(argv, 0, '', '')
            diagnosis = {'line': 'ssh diag fake: not established', 'transient': False,
                         'cause': 'not established', 'phase': 'kex', 'evidence': '/tmp/diag'}
            config = {'ledger': '/tmp/fake-ledger', 'host': 'xps', 'owner': 'test', 'token': 'a' * 32}
            with patch.object(life, 'ssh_diagnosis', return_value=diagnosis) as diag:
                with real_ssh_mux('fake-host', run=run, popen=popen) as mux:
                    control_path = mux.key
                    with socket.socket(socket.AF_UNIX) as stale:
                        stale.bind(control_path)
                    with patch.object(life, 'SshMux') as constructor:
                        constructor.return_value.__enter__.return_value = mux
                        if restart_ok:
                            self.assertEqual(life.ledger_forward('fake-host', 'read', config, run=run),
                                             {'owned_claim': True})
                        else:
                            with self.assertRaises(life.LedgerTransport) as error:
                                life.ledger_forward('fake-host', 'read', config, run=run)
                            self.assertEqual(error.exception.diagnoses, [diagnosis])
                    self.assertEqual(len(masters), 2)
                    self.assertEqual(len(riders), 1 if restart_ok else 0)
                    if restart_ok:
                        diag.assert_not_called()
                    else:
                        diag.assert_called_once()
                        self.assertTrue(diag.call_args.kwargs['allow_retry'])

    def test_expired_master_reopens_and_ledger_operation_succeeds(self):
        self._expired_master_ledger(True)

    def test_expired_master_failed_restart_gets_one_diagnosis(self):
        self._expired_master_ledger(False)

    def test_attempt_alarm_interrupts_a_blocked_operation(self):
        with self.assertRaises(life.SettleDeadline):
            with life.settle_attempt_deadline(.02):
                time.sleep(1)

    def test_attempt_alarm_refuses_a_worker_thread(self):
        errors = []
        def worker():
            try:
                with life.settle_attempt_deadline(.02):
                    self.fail('worker installed an alarm')
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], life.Refused)
        self.assertIn('Settle deadline requires the main Linux process', str(errors[0]))


@unittest.skipIf(os.environ.get('EDEN_SKIP_SSH_STALL_TESTS'), 'real SSH loopback tests explicitly skipped')
class SshLoopbackTests(unittest.TestCase):
    def test_auth_stall_after_real_key_exchange_and_successful_control(self):
        def free_port():
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                return sock.getsockname()[1]

        with tempfile.TemporaryDirectory(prefix='ssh-auth-rig-') as temporary:
            root = Path(temporary)
            key = root / 'key'
            subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                           check=True, capture_output=True, timeout=10)
            (root / 'authorized_keys').write_text(key.with_suffix('.pub').read_text())
            target_port = free_port()
            config = root / 'sshd_config'
            config.write_text(f'Port {target_port}\nListenAddress 127.0.0.1\nHostKey {key}\nPidFile none\n'
                              f'UsePAM no\nAuthorizedKeysFile {root / "authorized_keys"}\nStrictModes no\n'
                              f'PubkeyAuthentication yes\nPasswordAuthentication no\n'
                              f'AllowUsers {pwd.getpwuid(os.getuid()).pw_name}\n')
            sshd_log = (root / 'sshd.log').open('w+')
            sshd = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(config)],
                                    stdout=subprocess.DEVNULL, stderr=sshd_log, start_new_session=True)
            proxies, logs = [], []
            def stop(process):
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)
            def proxy(stall):
                port = free_port()
                log = (root / ('stall.log' if stall else 'pass.log')).open('w+')
                logs.append(log)
                argv = ['/usr/bin/python3', '-I', '-B', str(Path(__file__).with_name('test-data') / 'stall_proxy.py'),
                        '--port', str(port), '--target', str(target_port)]
                if stall:
                    argv.append('--stall')
                process = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
                proxies.append(process)
                for _ in range(100):
                    log.flush()
                    if 'READY' in Path(log.name).read_text():
                        return port
                    if process.poll() is not None:
                        break
                    time.sleep(.02)
                self.fail(f'loopback proxy did not start: {Path(log.name).read_text()}')
            def options(port):
                return ('-F', '/dev/null', '-o', f'Port={port}', '-o', 'StrictHostKeyChecking=no',
                        '-o', 'UserKnownHostsFile=/dev/null', '-o', f'IdentityFile={key}',
                        '-o', 'IdentitiesOnly=yes')
            def run(argv, **kwargs):
                if argv[0] == 'ping':
                    argv = ['/usr/bin/ping', *argv[1:]]
                return subprocess.run(argv, **kwargs)
            try:
                time.sleep(.3)
                if sshd.poll() is not None:
                    sshd_log.flush()
                    self.fail(f'unprivileged sshd failed: {(root / "sshd.log").read_text()}')
                pass_port = proxy(False)
                started = time.monotonic()
                with patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary}):
                    with real_ssh_mux('127.0.0.1', run=run, ssh_bin='/usr/bin/ssh',
                                      ssh_options=options(pass_port), evidence_dir=root / 'pass-evidence') as mux:
                        rider = subprocess.run(['/usr/bin/ssh', *mux.rider_args, '127.0.0.1', 'exit'],
                                               capture_output=True, text=True, timeout=5)
                        self.assertEqual(rider.returncode, 0, rider.stderr)
                positive = time.monotonic() - started
                self.assertLess(positive, 10)
                stall_port = proxy(True)
                timings, processes = [], []
                class TimedProcess:
                    def __init__(self, argv, **kwargs):
                        self.process = subprocess.Popen(argv, **kwargs)
                        self.pid, self.started = self.process.pid, time.monotonic()
                        processes.append(self.process)
                    def wait(self, timeout=None):
                        try:
                            return self.process.wait(timeout=timeout)
                        finally:
                            if not timings:
                                timings.append(time.monotonic() - self.started)
                    def poll(self):
                        return self.process.poll()
                    def kill(self):
                        self.process.kill()
                evidence = Path(os.environ.get('EDEN_TEST_EVIDENCE_DIR') or root / 'stall-evidence')
                started = time.monotonic()
                with patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary}), patch('sys.stderr', new=io.StringIO()):
                    with self.assertRaises(life.LedgerTransport) as caught:
                        with real_ssh_mux('127.0.0.1', run=run, ssh_bin='/usr/bin/ssh',
                                          popen=TimedProcess, ssh_options=options(stall_port),
                                          evidence_dir=evidence):
                            self.fail('stalled master unexpectedly opened')
                total = time.monotonic() - started
                self.assertEqual(len(processes), 1)
                self.assertTrue(9.5 <= timings[0] <= 11, timings)
                self.assertIsNotNone(processes[0].poll())
                diagnosis = caught.exception.diagnoses[0]
                self.assertEqual(diagnosis['phase'], 'auth')
                self.assertIn('attempt phase auth;', diagnosis['line'])
                self.assertIn('diag login failed, phase auth', diagnosis['line'])
                self.assertIn('cause: not established', diagnosis['line'])
                self.assertIn('retry: no', diagnosis['line'])
                self.assertEqual(len((evidence / 'ssh-diag.md').read_text().splitlines()), 1)
                self.assertIn('BOTH_NEWKEYS', (root / 'stall.log').read_text())
                print(f'loopback auth: positive {positive:.3f}s; master {timings[0]:.3f}s; total {total:.3f}s')
                print(diagnosis['line'])
            finally:
                for process in proxies:
                    stop(process)
                stop(sshd)
                for log in logs:
                    log.close()
                sshd_log.close()
            self.assertIsNotNone(sshd.poll())
            self.assertTrue(all(process.poll() is not None for process in proxies))
            owned_groups = {sshd.pid, *(process.pid for process in proxies)}
            remaining = []
            for entry in Path('/proc').iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    group = os.getpgid(int(entry.name))
                    parts = (entry / 'cmdline').read_bytes().split(b'\0')
                    name = Path(os.fsdecode(parts[0])).name if parts else ''
                    command = b'\0'.join(parts)
                    if (group in owned_groups or
                            (name in ('ssh', 'sshd', 'sshd-session') and
                         (str(root).encode() in command or f'Port={pass_port}'.encode() in command
                          or f'Port={stall_port}'.encode() in command)) or
                            (b'stall_proxy.py' in command and str(root).encode() in command)):
                        remaining.append(entry.name)
                except (OSError, ValueError):
                    pass
            self.assertEqual(remaining, [])

    def _stall(self, banner, expected):
        with tempfile.TemporaryDirectory(prefix='es-') as temporary:
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(.2)
            port = listener.getsockname()[1]
            stop = threading.Event()
            accepted = []
            def handle(conn, event):
                try:
                    if banner:
                        conn.sendall(b'SSH-2.0-OpenSSH_9.9\r\n')
                    conn.settimeout(.5)
                    while not stop.is_set():
                        try:
                            data = conn.recv(4096)
                            if not data:
                                break
                            event['bytes'].extend(data)
                        except socket.timeout:
                            pass
                finally:
                    conn.close()
            def serve():
                while not stop.is_set():
                    try:
                        conn, _ = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    event = {'time': time.monotonic(), 'bytes': bytearray()}
                    accepted.append(event)
                    threading.Thread(target=handle, args=(conn, event), daemon=True).start()
            server = threading.Thread(target=serve, daemon=True)
            server.start()
            timings, processes, logs, master_starts, diag_args = [], [], [], [], []
            class TimedProcess:
                def __init__(self, argv, **kwargs):
                    self.process = subprocess.Popen(argv, **kwargs)
                    self.pid = self.process.pid
                    self.started = time.monotonic()
                    processes.append(self.process)
                    master_starts.append(self.started)
                def wait(self, timeout=None):
                    try:
                        return self.process.wait(timeout=timeout)
                    finally:
                        if not timings:
                            timings.append(time.monotonic() - self.started)
                def poll(self):
                    return self.process.poll()
                def kill(self):
                    self.process.kill()
            def run(argv, **kwargs):
                if argv[0] == 'ping':
                    argv = ['/usr/bin/ping', *argv[1:]]
                return subprocess.run(argv, **kwargs)
            real_diag = life.ssh_diagnosis
            def diagnose(*args, **kwargs):
                logs.extend(Path(kwargs['mux_dir']).glob('master-*.log'))
                diag_args.append(kwargs)
                return real_diag(*args, **kwargs)
            options = ('-F', '/dev/null', '-o', f'Port={port}',
                       '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null')
            started = time.monotonic()
            try:
                with patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary}), \
                        patch.object(life, 'ssh_diagnosis', side_effect=diagnose), \
                        patch('sys.stderr', new=io.StringIO()):
                    with self.assertRaises(life.LedgerTransport) as caught:
                        with real_ssh_mux('127.0.0.1', run=run, popen=TimedProcess, ssh_bin='/usr/bin/ssh',
                                          ssh_options=options,
                                          evidence_dir=os.environ.get('EDEN_TEST_EVIDENCE_DIR') or temporary):
                            self.fail('unexpected master')
                total = time.monotonic() - started
                self.assertEqual(len(processes), 1)
                self.assertEqual(len(logs), 1)
                self.assertTrue(9.5 <= timings[0] <= 11, timings)
                self.assertIsNotNone(processes[0].poll())
                master_connections = [event for event in accepted
                                      if event['time'] <= master_starts[0] + timings[0]
                                      and event['bytes'].startswith(b'SSH-2.0-')]
                self.assertEqual(len(master_connections), 1)
                remaining = []
                for entry in Path('/proc').iterdir():
                    if entry.name.isdigit():
                        try:
                            parts = (entry / 'cmdline').read_bytes().split(b'\0')
                            if parts and Path(os.fsdecode(parts[0])).name == 'ssh' and \
                                    f'Port={port}'.encode() in parts:
                                remaining.append(entry.name)
                        except (OSError, ValueError):
                            pass
                self.assertEqual(remaining, [])
                diagnosis = caught.exception.diagnoses[0]
                self.assertEqual(diagnosis['phase'], expected)
                self.assertFalse(diagnosis['transient'])
                self.assertIn(f'diag login failed, phase {expected}', diagnosis['line'])
                self.assertIn('tcp22 ok; ping loss 0%', diagnosis['line'])
                self.assertIn('cause: not established', diagnosis['line'])
                self.assertIn('retry: no', diagnosis['line'])
                text = Path(diagnosis['evidence']).read_text()
                matches = [line.strip() for line in text.splitlines()
                           if re.search(r'timed out|Connection (?:closed|reset|refused)|Permission denied|'
                                        r'Host key verification failed|Could not resolve|No route to host', line, re.I)]
                expected_message = matches[-1][:160] if matches else (
                    'none (attempt killed at the 10 s bound)' if diag_args[0]['attempt_killed'] else 'none')
                self.assertIn(f'message {expected_message};', diagnosis['line'])
                print(f'loopback {expected}: master {timings[0]:.3f}s total {total:.3f}s, one master, no ssh left')
                print(diagnosis['line'])
            finally:
                stop.set()
                listener.close()
                server.join(timeout=2)

    def test_banner_stall(self):
        self._stall(False, 'banner')

    def test_kex_stall(self):
        self._stall(True, 'kex')


class ProtocolTests(unittest.TestCase):
    def test_stale_policy_fails_closed_on_missing_process_evidence_or_ambiguous_claims(self):
        now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        owner = 'OpenCode test/owner'
        old = '2026-10-01 12:00Z CLAIMED: OpenCode foreign/stream host=surface old'
        for admission in (None, {}, {'foreign_processes': None},
                          {'foreign_processes': [{'pid': 123, 'name': 'eden'}]}):
            with self.subTest(admission=admission):
                self.assertEqual(life.claim_admission(old, 'surface', owner, 24, admission, now)['blocking'], [old])
        for line in (
            '2026-10-01 12:00Z CLAIMED: OpenCode foreign/stream unknown host',
            '2026-10-01 12:00Z CLAIMED: host=surface owner-less',
            '2026-10-01 12:00Z CLAIMED: OpenCode on Surface owner-less',
            '2026-10-01 12:00Z CLAIMED: OpenCode host=surface owner-less',
            '2026-10-01 12:00Z CLAIMED: Claude run e04 on Surface owner-less',
            '2026-10-01 12:00Z CLAIMED: OpenCode foreign/stream host=surface on Yoga conflict',
            '2026-10-01 12:00Z CLAIMED: OpenCode test/owner host=surface same owner',
            'invalid-time CLAIMED: OpenCode foreign/stream host=surface old',
            '2026-02-30 12:00Z CLAIMED: OpenCode foreign/stream host=surface invalid date',
            '2026-10-01T12:00:00 CLAIMED: OpenCode foreign/stream host=surface naive time',
            '2026-11-04 12:00Z CLAIMED: OpenCode foreign/stream host=surface future',
            '2026-11-02 12:00Z CLAIMED: OpenCode foreign/stream host=surface exactly threshold',
        ):
            with self.subTest(line=line):
                policy = life.claim_admission(line, 'surface', owner, 24, {'foreign_processes': []}, now)
                self.assertEqual(policy, {'blocking': [line], 'stale_claim_skipped': []})

    def test_remote_ledger_policy_round_trip_uses_in_memory_stream_only(self):
        old = '2026-10-01 12:00Z CLAIMED: OpenCode foreign/stream host=surface old\n'
        file = mock_open(read_data=old)
        path = Path('/tmp/opencode/in-memory-ledger-not-created')
        remote = life.Ledger(path, 'surface', 'OpenCode test/owner', 'own-token', remote='yoga')
        packets = []
        def ledger_forward(host, operation, config, **kwargs):
            packets.append((host, 'ledger-' + operation, json.loads(json.dumps(config)), True))
            # Exercise the same dispatch boundary as SSH, without a subprocess/file write.
            return life.dispatch('ledger-' + operation, packets[-1][2], True)
        with patch.object(life, 'ledger_forward', side_effect=ledger_forward), patch.object(Path, 'is_file', return_value=True), \
                patch.object(Path, 'open', file), patch.object(life.fcntl, 'flock'), patch.object(life.os, 'fsync'), \
                patch.object(life, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
            result = remote.claim(stale_claim_hours=24, admission={'foreign_processes': []})
            self.assertEqual(result['stale_claim_skipped'][0]['line'], old.rstrip('\n'))
            self.assertGreater(result['stale_claim_skipped'][0]['age_hours'], 24)
            self.assertEqual(packets[0][1], 'ledger-claim')
            self.assertEqual(packets[0][2]['stale_claim_hours'], 24)
            self.assertEqual(packets[0][2]['admission'], {'foreign_processes': []})
            appended = ''.join(call.args[0] for call in file().write.call_args_list)
            self.assertIn('CLAIMED: OpenCode test/owner host=surface lifecycle=own-token ', appended)
            self.assertNotIn('RELEASED:', appended)
            self.assertNotIn('foreign/stream', appended)
            file().write.reset_mock()
            with self.assertRaisesRegex(life.Refused, 'Open host claim'):
                remote.claim(stale_claim_hours=24, admission={'foreign_processes': [{'pid': 1, 'name': 'cargo'}]})
            file().write.assert_not_called()
            # A successful own Down never closes or suppresses the skipped foreign record.
            text = old + result['line'] + '\n'
            file().read.return_value = text
            released = remote.release()
            appended = ''.join(call.args[0] for call in file().write.call_args_list)
            self.assertIn('RELEASED: OpenCode test/owner host=surface lifecycle=own-token ', appended)
            self.assertNotIn('foreign/stream', appended)
            file().read.return_value = text + released['line'] + '\n'
            self.assertEqual(remote.inspect()['open'], [old.rstrip('\n')])
            file().write.reset_mock()
            remote = life.Ledger(path, 'surface', 'OpenCode test/owner', 'new-token', remote='yoga')
            with self.assertRaisesRegex(life.Refused, 'Open host claim'):
                remote.claim()
            file().write.assert_not_called()

    def test_real_ledger_shapes_from_2026_11_03(self):
        # Shapes from the live ledger that the first parser misread as six open Surface claims (lr-04 refusal).
        text = (
            # Release that doesn't repeat "on Surface".
            '2026-10-31 23:17Z CLAIMED: Claude f9d4e7a2-902a/eden-merged ssbu-cpu-pilot run g04 on Surface, display :78\n'
            '2026-10-31 23:22Z RELEASED: Claude f9d4e7a2-902a/eden-merged ssbu-cpu-pilot run g04 stopped Eden and Surface display :78\n'
            # One claim carried across runs, released once naming only the last run.
            '2026-10-31 22:37Z CLAIMED: Claude 63392ddd/slot-park-probe Training diag run e03 on Yoga, display :68\n'
            '2026-10-31 22:43Z NOTE: Claude 63392ddd/slot-park-probe run e03 stopped; the same claim continues with run e04 on Yoga.\n'
            '2026-10-31 22:43Z CLAIMED: Claude 63392ddd/slot-park-probe Training diag run e04 on Yoga, display :68\n'
            '2026-10-31 22:55Z RELEASED: Claude 63392ddd/slot-park-probe run e04 stopped Eden, Yoga display :68\n'
            # A release naming another host leaves this host's claim open.
            '2026-11-01 10:00Z CLAIMED: Claude aaaa/two-hosts run x on Surface\n'
            '2026-11-01 10:01Z CLAIMED: Claude aaaa/two-hosts run y on Yoga\n'
            '2026-11-01 10:30Z RELEASED: Claude aaaa/two-hosts run y on Yoga done\n'
            # A live claim with no release stays open.
            '2026-11-03 18:21Z CLAIMED: OpenCode ses_e5e69459/pkg4-pair-runner3 on Yoga Eden Phase1\n')
        self.assertEqual([l[:17] for l in life.active_claims(text, 'surface')], ['2026-11-01 10:00Z'])
        self.assertEqual([l[:17] for l in life.active_claims(text, 'yoga')], ['2026-11-03 18:21Z'])

    def test_legacy_and_fractional_claim_release_formats_and_host_isolation(self):
        text = ('2026-11-03T15:00:00.123+00:00 CLAIMED: Codex01abc/project on Yoga busy\n'
                '2026-11-03 15:01Z RELEASED: Codex 01abc/project on Yoga done\n'
                '2026-11-03 15:02Z CLAIMED: OpenCode other/stream on Surface Eden\n')
        self.assertEqual(life.active_claims(text, 'yoga'), [])
        self.assertEqual(len(life.active_claims(text, 'surface')), 1)

    def test_xps_claims_are_scoped_to_the_xps(self):
        text = ('2026-11-06 08:00:00Z CLAIMED: OpenCode a/b host=xps lifecycle=t1 Eden lifecycle admitted\n'
                '2026-11-06 08:01Z CLAIMED: OpenCode c/d on XPS manual check\n')
        self.assertEqual(life.active_claims(text, 'surface'), [])
        self.assertEqual(life.active_claims(text, 'yoga'), [])
        self.assertEqual(len(life.active_claims(text, 'xps')), 2)
        released = text + '2026-11-06 09:00Z RELEASED: OpenCode a/b host=xps lifecycle=t1 Eden stopped\n'
        self.assertEqual(len(life.active_claims(released, 'xps')), 1)
        # An XPS claim must still block the XPS itself, and a hostless claim blocks it too.
        self.assertEqual(len(life.active_claims(text + 'CLAIMED: OpenCode g/h busy\n', 'xps')), 3)

    def test_xps_is_a_two_instance_linux_host(self):
        self.assertIn('xps', life.HOSTS)
        self.assertEqual(life.LINUX_MAX_EDEN, {'surface': 2, 'yoga': 2, 'xps': 2})
        self.assertEqual(life.Host.max_eden, 2)  # the class default the Surface and Yoga use

    def test_unknown_host_and_unparseable_claim_fail_closed(self):
        for text in ('CLAIMED: OpenCode unknown/stream busy\n', 'CLAIMED: malformed record\n'):
            self.assertTrue(life.active_claims(text, 'surface'))

    def test_foreign_lifecycle_release_does_not_release_other_token(self):
        text = ('CLAIMED: OpenCode x/y host=yoga lifecycle=first busy\n'
                'RELEASED: OpenCode x/y host=yoga lifecycle=second done\n')
        self.assertEqual(len(life.active_claims(text, 'yoga')), 1)

    def test_real_ledger_append_preserves_all_foreign_bytes_in_temp_file_only(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            path = Path(directory) / 'fake-ledger.txt'
            original = '# preserved comment\nCLAIMED: OpenCode foreign/x on Surface old\nRELEASED: OpenCode foreign/x on Surface done\n'
            path.write_text(original)
            ledger = life.Ledger(path, 'yoga', 'OpenCode test/owner', 'test-token')
            ledger.claim()
            self.assertTrue(path.read_text().startswith(original))
            with self.assertRaisesRegex(life.Refused, 'Open host claim'):
                life.Ledger(path, 'yoga', 'OpenCode other/owner', 'other').claim()
            ledger.release()
            self.assertFalse(life.active_claims(path.read_text(), 'yoga'))
            self.assertTrue(path.read_text().startswith(original))

    def test_wrong_release_token_is_refused(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            path = Path(directory) / 'fake-ledger.txt'
            path.write_text('CLAIMED: OpenCode x/y host=yoga lifecycle=owned busy\n')
            with self.assertRaisesRegex(life.Refused, 'Exact owned'):
                life.Ledger(path, 'yoga', 'OpenCode x/y', 'different').release()

    def test_cli_mute_supports_numeric_nested_values_and_rejects_false_or_nonzero(self):
        for value in (0, '0', 0.0, {'value': 0}, {'effective': 0, 'current': '0'}, {'nested': {'value': 0}}):
            self.assertTrue(life.mute_is_zero(value), value)
        for value in (False, True, {}, None, {'value': 0, 'effective': 75}, {'value': False}):
            self.assertFalse(life.mute_is_zero(value), value)

    def test_ssh_uses_remote_login_pwsh_encoded_command_and_stdin_only(self):
        captured = {}
        def run(command, **kwargs):
            captured.update(command=command, **kwargs)
            return subprocess.CompletedProcess(command, 0, '{"phase":"absent"}', '')
        result = life.forward('memento-claw-yoga-ubuntu', 'status', {'fake': 'config'}, False, run=run)
        self.assertEqual(result['phase'], 'absent')
        self.assertIn('/usr/bin/pwsh', captured['command'])
        self.assertIn('-NoProfile', captured['command'])
        script = base64.b64decode(captured['command'][-1]).decode('utf-16-le')
        self.assertIn('/usr/bin/python3 -I -B', script)
        self.assertNotIn('bash', script)
        packet = json.loads(captured['input'])
        self.assertEqual(packet['mode'], 'status')
        self.assertFalse(packet['execute'])
        self.assertIn('class Lifecycle', packet['source'])
        self.assertGreater(captured['timeout'], 0)
        life.forward('memento-claw-yoga-ubuntu', 'down', {'fake': 'config'}, True, run=run, keep_state=True,
                     evidence_dir='/home/intpa/eden-runs/x/out')
        packet = json.loads(captured['input'])
        self.assertEqual((packet['keep_state'], packet['evidence_dir']), (True, '/home/intpa/eden-runs/x/out'))

    def test_ssh_failure_is_not_success_and_unsafe_host_refuses(self):
        def fail(command, **kwargs):
            return subprocess.CompletedProcess(command, 255, '', 'synthetic ssh transport failure')
        with patch.object(life, 'ssh_diagnosis', return_value={'line': 'fake diagnosis'}):
            with self.assertRaisesRegex(life.Refused, 'SSH lifecycle failed'):
                life.forward('yoga', 'status', {}, False, run=fail)
        with self.assertRaisesRegex(life.Refused, 'Unsafe SSH host'):
            life.forward('yoga;bad', 'status', {}, False, run=fail)

    def test_ssh_rider_timeout_is_diagnosed_without_retry(self):
        calls = []
        def timeout(command, **kwargs):
            calls.append(command)
            raise subprocess.TimeoutExpired(command, 900)
        with patch.object(life, 'ssh_diagnosis', return_value={'line': 'named diagnosis'}) as diagnose:
            with self.assertRaisesRegex(life.Refused, 'named diagnosis'):
                life.forward('fake-host', 'status', {}, False, run=timeout)
        self.assertEqual(len(calls), 1)
        self.assertFalse(diagnose.call_args.kwargs['allow_retry'])

    def test_real_host_rejects_input_before_loading_any_cli_client(self):
        with self.assertRaisesRegex(life.Refused, 'cannot send input'):
            life.Host().call({}, 'input.button', {}, 1)

    def test_foreign_cgroup_is_not_adopted_as_owned_pid(self):
        host = life.Host()
        host.unit = lambda _: {'ControlGroup': '/user.slice/foreign.service', 'MainPID': '123'}
        with self.assertRaisesRegex(life.Refused, 'cgroup identity'):
            host.owned({'unit': 'eden-life-owned.service'})

    def test_pid_reuse_after_pidfd_open_refuses_signal(self):
        host = life.Host()
        old = {'pid': 123, 'start': 'old', 'boot': 'fake'}
        new = {'pid': 123, 'start': 'new', 'boot': 'fake'}
        host.owned = lambda _: [old]
        host.run = lambda *_args, **_kw: ''
        with patch.object(host, 'identity', side_effect=[old, new]), patch.object(os, 'pidfd_open', return_value=99), \
                patch.object(os, 'close'), patch.object(life.signal, 'pidfd_send_signal') as send:
            with self.assertRaisesRegex(life.Refused, 'PID changed'):
                host.force_stop({'unit': 'eden-life-own.service'}, .1)
            send.assert_not_called()

    def test_already_gone_owned_unit_never_signals_reused_foreign_pid(self):
        host = life.Host()
        host.run = lambda *_args, **_kw: ''
        host.owned = lambda _: []
        with patch.object(os, 'pidfd_open') as open_pid:
            self.assertEqual(host.force_stop({'unit': 'eden-life-own.service'}, .1)['owned_pids_stopped'], [])
            open_pid.assert_not_called()


class FakeWindowsHost(FakeHost):
    clone_source = life.WindowsHost.clone_source

    def name(self):
        return 'OFFLINE-TEST-HOST'


class WindowsTests(unittest.TestCase):
    controller = LifecycleTests.controller
    state = LifecycleTests.state

    def test_windows_ini_paths_use_qt_forward_slashes_not_native_backslashes(self):
        self.assertEqual(life.profile_directories(PureWindowsPath(r'C:\Eden\owned\profile')),
                         {'nand_directory': 'C:/Eden/owned/profile/user/nand',
                          'sdmc_directory': 'C:/Eden/owned/profile/user/sdmc'})

    def setUp(self):
        LifecycleTests.setUp(self)
        appdir = self.base / 'app'
        appdir.mkdir()
        app = appdir / 'eden.exe'
        app.write_bytes(b'fake Windows runtime')
        (appdir / 'dependency.dll').write_bytes(b'fake dependency')
        self.config.update(host='ally', interactive_session=1, lifecycle_only=True,
            windows_job={'path': str(self.runner), 'sha256': life.digest(self.runner)},
            windows_control={'path': str(self.settings), 'sha256': life.digest(self.settings)},
            windows_python={'path': str(app), 'sha256': life.digest(app)})
        item = self.config['instances'][0]
        item['source'] = str(self.source / 'user')
        item['app'] = {'path': str(app), 'sha256': life.digest(app)}
        item.pop('display')
        self.host = FakeWindowsHost(self.order)

    def test_ally_pending_release_does_not_start_a_settle_unit(self):
        c = self.controller()
        c.up()
        with patch.object(self.ledger, 'inspect', side_effect=transport()):
            result = c.down()
        self.assertTrue(result['release_pending'])
        self.assertIsNone(result['settle_unit'])
        self.assertEqual(self.host.started_settles, [])
        self.assertTrue(c.down()['released'])

    def test_ally_notify_accepts_windows_only_and_writes_through_local_path(self):
        value = r'C:\Users\intpa\eden-run-notify\NOTIFY.md'
        self.config['notify'] = {'file': value, 'to': 'tr-primary-2'}
        self.assertEqual(life.validate(self.config)['notify']['file'], value)
        for invalid in ('/home/intpa/eden-run-notify/NOTIFY.md', r'C:\Users\intpa\..\NOTIFY.md',
                        r'C:Users\intpa\NOTIFY.md', r'\\server\share\NOTIFY.md',
                        r'C:\Users\intpa\bad;name.md'):
            with self.subTest(path=invalid):
                config = deepcopy(self.config)
                config['notify']['file'] = invalid
                with self.assertRaisesRegex(life.Refused, 'notify file must be an absolute safe path'):
                    life.validate(config)
        target = self.base / 'windows-profile' / 'eden-run-notify' / 'NOTIFY.md'
        c = self.controller()
        with patch.object(life, 'safe_absolute', return_value=target) as resolve:
            c.notify('pending')
        resolve.assert_called_once_with(value)
        self.assertRegex(target.read_text(), r'^\n## \d\d:\d\d:\d\dZ eden-lifecycle test-01 → tr-primary-2 \| pending\n$')

    def test_windows_bridge_preserves_ledger_transport_classification(self):
        token = 'a' * 32
        def dispatch(*args):
            with self.assertRaises(life.LedgerTransport) as error:
                life._LEDGER_BRIDGE('read', token, None, None)
            self.assertTrue(error.exception.transient)
            self.assertEqual(error.exception.diagnoses[0]['cause'], 'temporary stall')
            return {'ok': True}
        packet = {'source': 'unused', 'ledger_bridge': True, 'mode': 'down',
                  'config': {}, 'execute': True}
        detail = transport().diagnoses
        reply = io.StringIO(json.dumps({'error': 'Ledger transport: ' + json.dumps(detail)}) + '\n')
        with patch.dict(life.__dict__, {'_LEDGER_BRIDGE': None}), \
                patch.object(life, 'dispatch', side_effect=dispatch), \
                patch('sys.stdin', reply), patch('sys.stdout', new_callable=io.StringIO):
            life.remote_main(packet)

    def test_ally_down_receives_transport_errors_over_the_existing_bridge_reply(self):
        initial = self.controller()
        initial.up()
        c = life.Lifecycle(self.config, self.host)
        packet = {'source': 'unused', 'ledger_bridge': True, 'mode': 'down',
                  'config': self.config, 'execute': True}
        reply = json.dumps({'error': 'Ledger transport: read unavailable'}) + '\n'
        output = io.StringIO()
        with patch.dict(life.__dict__, {'_LEDGER_BRIDGE': None}), \
                patch.object(life, 'Lifecycle', return_value=c), \
                patch('sys.stdin', io.StringIO(reply * 2)), patch('sys.stdout', output):
            life.remote_main(packet)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([line['ledger_request']['operation'] for line in lines[:-1]], ['read'])
        result = lines[-1]['lifecycle_result']
        self.assertEqual(result['phase'], 'down')
        self.assertTrue(result['release_pending'])
        self.assertFalse(result['released'])
        self.assertEqual(self.host.started_settles, [])

    def test_ally_up_skips_pending_and_bridge_mutates_only_its_claim(self):
        folder = life.pending_release_dir()
        folder.mkdir(parents=True)
        pending = folder / ('a' * 32 + '.json')
        pending.write_text(json.dumps({'host': 'ally', 'ledger': self.config['ledger'], 'token': 'a' * 32}))
        requests = []
        def bridge(operation, token, stale, admission):
            requests.append({'ledger_request': {'operation': operation, 'token': token,
                                                 'stale_claim_hours': stale, 'admission': admission}})
            if operation == 'read':
                return {'open': [], 'owned_claim': False, 'owned_release': False}
            return {'line': 'fake claim', 'stale_claim_skipped': []}
        c = life.Lifecycle(self.config, self.host)
        with patch.dict(life.__dict__, {'_LEDGER_BRIDGE': bridge}), patch.object(c, 'settle_pending') as settle:
            self.assertEqual(c.up()['phase'], 'up')
        settle.assert_not_called()
        self.assertTrue(pending.exists())
        self.assertEqual([r['ledger_request']['operation'] for r in requests if r['ledger_request']['operation'] != 'read'],
                         ['claim'])
        class Process:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(''.join(json.dumps(x) + '\n' for x in requests) +
                                          json.dumps({'lifecycle_result': {'phase': 'up'}}) + '\n')
                self.stderr = io.StringIO()
            def poll(self):
                return 0
            def wait(self, timeout):
                return 0
        mutations = []
        class Ledger:
            def __init__(self, *args):
                pass
            def operate(self, operation, stale, admission):
                if operation != 'read':
                    mutations.append(operation)
                return {'open': []} if operation == 'read' else {'line': 'fake claim'}
        value = life.forward_windows('fake-ally', 'up', self.config, True, lambda *a, **kw: Process(), Ledger)
        self.assertEqual(value['phase'], 'up')
        self.assertEqual(mutations, ['claim'])

    def test_windows_full_fake_up_down_pipe_private_clone_and_source_verification(self):
        original = life.inventory(self.source)
        c = self.controller()
        result = c.up()
        item = c.state['instances'][0]
        self.assertEqual(result['instances']['a']['socket'], '\\\\.\\pipe\\eden-life-' + c.state['token'] + '-a')
        self.assertEqual(result['instances']['a']['screenshot_target'], 'screen')
        self.assertTrue((Path(item['profile']) / 'eden.exe').is_file())
        self.assertNotEqual((Path(item['profile']) / life.CONFIG).stat().st_ino,
                            (self.source / life.CONFIG).stat().st_ino)
        self.assertEqual(c.down()['phase'], 'down')
        self.assertEqual(life.inventory(self.source), original)
        self.assertTrue(json.loads((c.directory / 'a-source-verify.json').read_text())['unchanged'])

    def test_windows_prune_removes_the_profile_links_but_never_the_installed_app_or_source(self):
        original = life.inventory(self.source)
        app = Path(self.config['instances'][0]['app']['path']).parent
        installed = life.inventory(app)
        c = self.controller()
        c.up()
        self.assertEqual((app / 'eden.exe').stat().st_nlink, 2)
        self.assertTrue(c.down()['prune']['done'])
        self.assertFalse(Path(self.config['work_root']).exists())
        self.assertFalse((c.directory / 'post-run-a').exists() or (c.directory / 'snapshot-a').exists())
        self.assertEqual((life.inventory(app), life.inventory(self.source)), (installed, original))
        self.assertEqual((app / 'eden.exe').stat().st_nlink, 1)

    def test_windows_read_only_attribute_is_cleared_only_on_a_private_file(self):
        # The attribute belongs to the file's data, which a hard link shares with the installed app or source.
        tree = self.base / 'tree'
        (tree / 'd').mkdir(parents=True)
        installed = self.base / 'installed.dll'
        installed.write_bytes(b'installed')
        os.link(installed, tree / 'd/shared.dll')
        (tree / 'd/private.ini').write_text('private')
        unlink, refused = Path.unlink, set()
        def read_only(path, *args, **kwargs):
            if path.name in ('shared.dll', 'private.ini') and path not in refused:
                refused.add(path)
                raise PermissionError('synthetic read-only attribute')
            return unlink(path, *args, **kwargs)
        skipped = []
        with patch.object(Path, 'unlink', read_only), patch.object(Path, 'chmod', autospec=True) as chmod:
            life.remove_tree(tree, skipped, windows=True)
        self.assertEqual(skipped, [str(tree / 'd/shared.dll')])
        self.assertEqual(chmod.call_args_list, [call(tree / 'd/private.ini', life.stat.S_IWRITE)])
        self.assertFalse((tree / 'd/private.ini').exists())
        self.assertTrue((tree / 'd/shared.dll').is_file())  # with the directories above it
        self.assertEqual(installed.read_bytes(), b'installed')

    def test_windows_fresh_controller_can_down_nondefault_named_pipe_state(self):
        self.controller().up()
        self.assertEqual(self.controller().status()['phase'], 'up')
        self.assertEqual(self.controller().down()['phase'], 'down')
        self.assertTrue(self.ledger.released)

    def test_windows_source_drift_retains_claim_after_owned_stop(self):
        c = self.controller()
        c.up()
        (self.source / 'user/config/qt-config.ini').write_text('foreign change')
        with self.assertRaisesRegex(life.Refused, 'Cleanup incomplete'):
            c.down()
        self.assertFalse(self.ledger.released)
        self.assertFalse(self.host.live)

    def test_windows_pipe_tampering_refuses_before_cleanup(self):
        c = self.controller()
        c.up()
        value = self.state()
        value['instances'][0]['socket'] = '\\\\.\\pipe\\foreign-owner'
        life.save(c.journal, value)
        previous = list(self.order)
        with self.assertRaisesRegex(life.Refused, 'Foreign journal profile/run'):
            self.controller().down()
        self.assertEqual(self.order, previous)

    def test_windows_helper_drift_refuses_before_claim(self):
        self.config['windows_job']['sha256'] = '0' * 64
        with self.assertRaisesRegex(life.Refused, 'Windows helper mismatch'):
            self.controller().up()
        self.assertFalse(self.ledger.claimed)

    def test_windows_native_task_command_is_interactive_bounded_and_one_shot(self):
        c = self.controller()
        c.up()
        item = c.state['instances'][0]
        run = Path(item['run'])
        life.save(run / 'launcher.json', {'session': 1, 'limits': {'JobMemoryLimit': 8 * life.GIB,
            'KillOnJobClose': True, 'ActiveProcessLimit': 256}, 'owned_pids': [{'pid': 123, 'start': 'fake'}]})
        host = life.WindowsHost(self.config, job_api=object())
        commands = []
        host.run = lambda argv, cap=30: commands.append(argv) or ''
        scripts = []
        host.ps = lambda script, **kwargs: scripts.append(script) or (
            'S-1-5-21-1-2-3-1001' if 'WindowsIdentity' in script else '{}')
        with patch.dict(os.environ, {'USERDOMAIN': 'TEST', 'USERNAME': 'tester'}):
            self.assertEqual(host.launch(item, self.config)[0]['pid'], 123)
        xml = (run / 'task.xml').read_text(encoding='utf-16')
        self.assertIn('<LogonType>InteractiveToken</LogonType>', xml)
        self.assertIn('<UserId>S-1-5-21-1-2-3-1001</UserId>', xml)
        self.assertNotIn('TEST\\tester', xml)  # SSH USERDOMAIN can be a workgroup, not an account domain.
        self.assertIn('<ExecutionTimeLimit>PT5430S</ExecutionTimeLimit>', xml)
        self.assertIn('<Enabled>false</Enabled>', xml)
        self.assertIn('_windows-launch', xml)
        self.assertEqual([cmd[1] for cmd in commands], ['/create', '/run'])
        self.assertTrue((run / 'lifecycle-backend.py').is_file())
        self.assertIn('rules=@($rules.Name)', scripts[0])
        self.assertIn('created=$created', scripts[0])
        self.assertNotIn('New-NetFirewallRule', scripts[0])  # no silent permission to create a rule

    def test_windows_control_uses_pinned_async_client_and_forbids_input(self):
        host = life.WindowsHost(self.config, job_api=object())
        scripts = []
        host.ps = lambda script, cap=30: scripts.append((script, cap)) or '{"ok":true,"result":{}}'
        item = {'socket': '\\\\.\\pipe\\own-pipe'}
        self.assertTrue(host.call(item, 'game.stop', {}, .3)['ok'])
        self.assertIn("-PipeName 'own-pipe'", scripts[0][0])
        self.assertIn('-TimeoutMs 300', scripts[0][0])
        with self.assertRaisesRegex(life.Refused, 'cannot send input'):
            host.call(item, 'input.button', {}, 1)
        self.assertEqual(len(scripts), 1)

    def test_windows_invalid_sid_never_creates_or_starts_a_task(self):
        c = self.controller()
        c.up()
        host = life.WindowsHost(self.config, job_api=object())
        commands = []
        host.run = lambda argv, cap=30: commands.append(argv) or ''
        host.ps = lambda script, **kwargs: 'WORKGROUP\\tester' if 'WindowsIdentity' in script else '{}'
        with self.assertRaisesRegex(life.Refused, 'current Windows user SID'):
            host.launch(c.state['instances'][0], self.config)
        self.assertEqual(commands, [])

    def test_windows_task_xml_preserves_python_path_with_spaces_and_xml_characters(self):
        import xml.etree.ElementTree as ET
        python = self.base / 'Program Files & Pýthon' / 'pythonw.exe'
        python.parent.mkdir()
        python.write_bytes(b'fake Python, never executed')
        self.config['windows_python'] = {'path': str(python), 'sha256': life.digest(python)}
        c = self.controller()
        c.up()
        item = c.state['instances'][0]
        run = Path(item['run'])
        life.save(run / 'launcher.json', {'session': 1, 'limits': {'JobMemoryLimit': 8 * life.GIB,
            'KillOnJobClose': True, 'ActiveProcessLimit': 256}, 'owned_pids': [{'pid': 123, 'start': 'fake'}]})
        host = life.WindowsHost(self.config, job_api=object())
        commands = []
        host.run = lambda argv, cap=30: commands.append(argv) or ''
        host.ps = lambda script, **kwargs: 'S-1-5-21-1-2-3-1001' if 'WindowsIdentity' in script else '{}'
        host.launch(item, self.config)
        xml = ET.fromstring((run / 'task.xml').read_text(encoding='utf-16'))
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(xml.findtext('t:Actions/t:Exec/t:Command', namespaces=ns), str(python))
        self.assertEqual(xml.findtext('t:Actions/t:Exec/t:WorkingDirectory', namespaces=ns), str(run))
        self.assertEqual(xml.findtext('t:Principals/t:Principal/t:UserId', namespaces=ns), 'S-1-5-21-1-2-3-1001')
        self.assertIn('_windows-launch', xml.findtext('t:Actions/t:Exec/t:Arguments', namespaces=ns))
        self.assertEqual([cmd[1] for cmd in commands], ['/create', '/run'])

    def test_ally_hold_template_is_unpinned_single_boot_and_not_executable_as_delivered(self):
        packet = json.loads(Path(__file__).with_name('ally-proof-HOLD.template.json').read_text())
        draft = packet['draft_config']
        self.assertEqual(packet['status'], 'HOLD_WAITING_FOR_PRIMARY_GO')
        self.assertIsNone(packet['go_reference'])
        self.assertIsNone(packet['observed_after_go_utc'])
        self.assertTrue(packet['not_for_execution'])
        self.assertTrue(draft['lifecycle_only'])
        self.assertEqual(draft['notify'],
                         {'file': r'C:\Users\intpa\eden-run-notify\NOTIFY.md', 'to': 'tr-primary-2'})
        self.assertFalse(draft['defer_load'])
        self.assertFalse(draft['allow_inbound_block'])
        self.assertEqual(len(draft['instances']), 1)
        self.assertEqual(draft['instances'][0]['plugins'], [])
        self.assertNotIn('stale_claim_hours', draft)
        work, state = (PureWindowsPath(draft[key]) for key in ('work_root', 'state_dir'))
        self.assertTrue(work.is_absolute() and state.is_absolute())
        self.assertNotEqual(work, state)
        self.assertNotIn(work, state.parents)
        self.assertNotIn(state, work.parents)
        self.assertEqual(work.parent.name, 'life-p6-go01')
        for key in ('windows_job', 'windows_control', 'windows_python'):
            invalid = deepcopy(self.config)
            invalid[key]['sha256'] = draft[key]['sha256']
            with self.assertRaisesRegex(life.Refused, 'Windows helper pins'):
                life.validate(invalid)
        invalid = deepcopy(self.config)
        invalid['instances'][0]['app']['sha256'] = draft['instances'][0]['app']['sha256']
        with self.assertRaisesRegex(life.Refused, 'Input SHA256 required'):
            life.validate(invalid)
        invalid = deepcopy(self.config)
        invalid['interactive_session'] = draft['interactive_session']
        with self.assertRaisesRegex(life.Refused, 'interactive_session required'):
            life.validate(invalid)

    def test_windows_task_creation_failure_rolls_back_and_releases_without_spawning(self):
        # Original 20:08 attempt: scheduler rejected UserId before any task/launcher/Eden existed.
        class NoJob:
            def open_job(self, name, access):
                return None
        native = life.WindowsHost(self.config, job_api=NoJob())
        native.ps = lambda script, **kwargs: 'S-1-5-21-1-2-3-1001' if 'WindowsIdentity' in script else '{}'
        commands = []
        def rejected(command, cap=30):
            commands.append(command)
            raise life.Refused('No mapping between account names and security IDs was done; UserId')
        native.run = rejected
        self.host.launch = native.launch
        self.host.force_stop = native.force_stop
        self.host.metrics = native.metrics
        c = self.controller()
        with patch.object(life.subprocess, 'Popen') as spawn, \
                patch.object(life.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as tasks:
            with self.assertRaisesRegex(life.Refused, 'No mapping between account names'):
                c.up()
        spawn.assert_not_called()
        self.assertEqual([cmd[1] for cmd in commands], ['/create'])
        self.assertEqual([call.args[0][1] for call in tasks.call_args_list], ['/end', '/delete'])
        self.assertEqual(c.state['phase'], 'down')
        self.assertTrue(self.ledger.claimed and self.ledger.released)
        self.assertTrue(c.state['instances'][0]['restored'] and c.state['instances'][0]['verified'])
        self.assertFalse(c.state['cleanup_failures'])
        self.assertTrue(json.loads((c.directory / 'a-source-verify.json').read_text())['unchanged'])
        self.assertNotIn('game.load', [method for _, method, _, _ in self.host.calls])
        self.assertFalse((Path(c.state['instances'][0]['run']) / 'launcher.json').exists())

    def test_windows_job_kill_is_session_scoped_and_never_targets_a_reused_pid(self):
        class Job:
            def __init__(self):
                self.names, self.killed = [], []
            def open_job(self, name, access):
                self.names.append(name)
                return 42
            def pids(self, handle):
                return []
            def terminate(self, handle, code):
                self.killed.append((handle, code))
            def close(self, handle):
                pass
        job = Job()
        host = life.WindowsHost(self.config, job)
        host.identity = lambda pid: {'pid': pid, 'start': 'new'}
        unit = 'eden-life-' + 'a' * 32 + '-a.service'
        with patch.object(life.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            result = host.force_stop({'unit': unit, 'owned_pids': [{'pid': 55, 'start': 'old'}]}, .1)
        self.assertTrue(result['job_terminated'])
        self.assertEqual(job.killed, [(42, 137)])
        self.assertEqual(set(job.names), {'Session\\1\\' + unit})
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ['/end', '/delete'])
        with self.assertRaisesRegex(life.Refused, 'Foreign Windows'):
            host.force_stop({'unit': 'foreign-task'}, .1)

    def test_windows_transport_keeps_authoritative_ledger_on_controller(self):
        token = 'b' * 32
        request = {'ledger_request': {'operation': 'claim', 'token': token,
                   'admission': {'foreign_processes': []}, 'stale_claim_hours': None}}
        class Input(io.StringIO):
            def close(self):
                self.saved = self.getvalue()
                super().close()
        class Process:
            stdin = Input()
            stdout = io.StringIO(json.dumps(request) + '\n' + json.dumps({'lifecycle_result': {'phase': 'up'}}) + '\n')
            stderr = io.StringIO()
            def poll(self):
                return 0
            def wait(self, timeout):
                return 0
        process = Process()
        commands, ledgers = [], []
        def popen(command, **kwargs):
            commands.append(command)
            return process
        class Ledger:
            def __init__(self, *args):
                ledgers.append(args)
            def operate(self, operation, stale, admission):
                self.test = operation
                return {'line': 'FAKE claim'}
        self.config['ledger_ssh'] = 'yoga'
        value = life.forward_windows('intpa@dreamland-ally', 'up', self.config, True, popen, Ledger)
        self.assertEqual(value['phase'], 'up')
        self.assertEqual(ledgers, [(self.config['ledger'], 'ally', self.config['owner'], token, 'yoga')])
        self.assertTrue(commands[0][-1].startswith('"C:\\Program Files\\PowerShell\\7\\pwsh.exe"'))
        packet = json.loads(process.stdin.saved.splitlines()[0])
        self.assertTrue(packet['ledger_bridge'])
        self.assertEqual(json.loads(process.stdin.saved.splitlines()[1]), {'result': {'line': 'FAKE claim'}})

    def test_windows_controller_serializes_transport_in_the_existing_error_reply(self):
        request = {'ledger_request': {'operation': 'read', 'token': 'a' * 32,
                                      'admission': None, 'stale_claim_hours': None}}
        class Input(io.StringIO):
            def close(self):
                self.saved = self.getvalue()
                super().close()
        class Process:
            def __init__(self):
                self.stdin = Input()
                self.stdout = io.StringIO(json.dumps(request) + '\n' +
                                          json.dumps({'lifecycle_result': {'phase': 'down'}}) + '\n')
                self.stderr = io.StringIO()
            def poll(self):
                return 0
            def wait(self, timeout):
                return 0
        class Ledger:
            def __init__(self, *args):
                pass
            def operate(self, operation, stale, admission):
                raise transport('ssh diag fake: outage')
        process = Process()
        life.forward_windows('fake-ally', 'down', self.config, True, lambda *args, **kwargs: process, Ledger)
        reply = json.loads(process.stdin.saved.splitlines()[1])
        self.assertEqual(set(reply), {'error'})
        self.assertTrue(reply['error'].startswith('Ledger transport: '))
        diagnoses = json.loads(reply['error'].removeprefix('Ledger transport: '))
        self.assertTrue(diagnoses[0]['transient'])
        self.assertEqual(diagnoses[0]['line'], 'ssh diag fake: outage')

    def test_windows_up_reads_with_the_inspect_token_then_claims_with_its_own(self):
        # Up's admission reads the ledger with the read-only inspect token ('0' * 32, Lifecycle.ledger_text) before
        # it claims with the run token. Reads bind nothing; a second mutating token is still refused.
        run_token, other = 'c' * 32, 'd' * 32
        def stream(*requests):
            lines = [json.dumps({'ledger_request': {'operation': op, 'token': tok, 'admission': {},
                                                    'stale_claim_hours': None}}) for op, tok in requests]
            return '\n'.join(lines + [json.dumps({'lifecycle_result': {'phase': 'up'}})]) + '\n'
        def transport(text):
            class Process:
                stdin = io.StringIO()
                stdout = io.StringIO(text)
                stderr = io.StringIO()
                def poll(self):
                    return 0
                def wait(self, timeout):
                    return 0
            return lambda command, **kwargs: Process()
        tokens = []
        class Ledger:
            def __init__(self, *args):
                tokens.append(args[3])
            def operate(self, operation, stale, admission):
                return {'open': []} if operation == 'read' else {'line': 'FAKE ' + operation}
        value = life.forward_windows('intpa@dreamland-ally', 'up', self.config, True,
                                     transport(stream(('read', '0' * 32), ('claim', run_token), ('read', '0' * 32),
                                                      ('release', run_token))), Ledger)
        self.assertEqual(value['phase'], 'up')
        self.assertEqual(tokens, ['0' * 32, run_token, '0' * 32, run_token])
        with self.assertRaisesRegex(life.Refused, 'Foreign bridge token'):
            life.forward_windows('intpa@dreamland-ally', 'up', self.config, True,
                                 transport(stream(('claim', run_token), ('release', other))), Ledger)

    def test_windows_1312_never_replays_the_operation(self):
        error = life.Refused('CreateProcessAsUserW failed: 1312')
        error.transport_not_started = True
        with patch.object(life, 'ssh_diagnosis') as diag, \
                patch.object(life, '_forward_windows_once', side_effect=error) as once:
            with self.assertRaises(life.Refused):
                life.forward_windows('intpa@dreamland-ally', 'status', self.config, False)
            self.assertEqual(once.call_count, 1)
            diag.assert_not_called()
        error.transport_not_started = False
        with patch.object(life, 'ssh_diagnosis') as diag, \
                patch.object(life, '_forward_windows_once', side_effect=error) as once:
            with self.assertRaises(life.Refused):
                life.forward_windows('intpa@dreamland-ally', 'up', self.config, True)
            self.assertEqual(once.call_count, 1)
            diag.assert_not_called()

    def test_windows_real_transport_1312_does_not_replay(self):
        class BrokenInput:
            def write(self, text):
                raise BrokenPipeError('server did not start a remote process')
            def close(self):
                raise BrokenPipeError('flush during close')
        class Input(io.StringIO):
            def close(self):
                self.saved = self.getvalue()
                super().close()
        class Process:
            def __init__(self, failed):
                self.stdin = BrokenInput() if failed else Input()
                self.stdout = io.StringIO('' if failed else '{"lifecycle_result":{"phase":"absent"}}\n')
                self.stderr = io.StringIO('CreateProcessAsUserW failed: 1312\n' if failed else '')
            def poll(self):
                return 0
            def wait(self, timeout):
                return 0
        processes = [Process(True), Process(False)]
        commands = []
        def popen(command, **kwargs):
            commands.append(command)
            return processes[len(commands) - 1]
        with patch.object(life, 'Ledger') as ledger, patch.object(life, 'ssh_diagnosis') as diag:
            with self.assertRaisesRegex(life.Refused, 'CreateProcessAsUserW failed: 1312'):
                life.forward_windows('intpa@dreamland-ally', 'status', self.config, False, popen, ledger)
        self.assertEqual(len(commands), 1)
        self.assertIn('-NoProfile -NonInteractive -EncodedCommand', commands[0][-1])
        self.assertEqual(commands[0][1:3], ['-T', '-x'])
        ledger.assert_not_called()
        diag.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
