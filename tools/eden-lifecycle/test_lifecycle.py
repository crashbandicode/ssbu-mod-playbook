#!/usr/bin/env python3
"""Offline only: fake host/RPC/claims; all filesystem fixtures are temporary."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import tempfile
import unittest
from unittest.mock import mock_open, patch

spec = importlib.util.spec_from_file_location('lifecycle', Path(__file__).with_name('lifecycle.py'))
life = importlib.util.module_from_spec(spec)
spec.loader.exec_module(life)


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
        self.runner.write_text('def dry_run(path):\n return {"result":"READY","rows":1,"errors":[]}\n')
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
        result = controller.down()
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

    def test_real_shared_runner_dryrun_integration_is_offline(self):
        shared = Path(__file__).resolve().parents[1] / 'eden-runner/runner.py'
        self.config['runner'] = {'path': str(shared), 'sha256': life.digest(shared)}
        self.rows.write_text(json.dumps({'start': 'menu', 'rows': [{'id': 'observe', 'start': 'menu', 'end': 'menu',
            'steps': [{'gate': 'settings'}]}], 'gates': {'settings': {'kind': 'json', 'source': 'settings', 'expect': {'x': 1}, 'cap': 1}}}))
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
        def forward(host, mode, config, execute):
            packets.append((host, mode, json.loads(json.dumps(config)), execute))
            # Exercise the same dispatch boundary as SSH, without a subprocess/file write.
            return life.dispatch(mode, packets[-1][2], execute)
        with patch.object(life, 'forward', side_effect=forward), patch.object(Path, 'is_file', return_value=True), \
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
            with self.assertRaisesRegex(life.Refused, 'Open host claim'):
                remote.claim()  # same foreign history still blocks the strict policy
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

    def test_ssh_failure_is_not_success_and_unsafe_host_refuses(self):
        def fail(command, **kwargs):
            return subprocess.CompletedProcess(command, 255, '', 'synthetic ssh transport failure')
        with self.assertRaisesRegex(life.Refused, 'SSH lifecycle failed'):
            life.forward('yoga', 'status', {}, False, run=fail)
        with self.assertRaisesRegex(life.Refused, 'Unsafe SSH host'):
            life.forward('yoga;bad', 'status', {}, False, run=fail)

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

    def test_windows_1312_retries_once_only_before_any_ledger_action(self):
        error = life.Refused('CreateProcessAsUserW failed: 1312')
        error.transport_not_started = True
        with patch.object(life, '_forward_windows_once', side_effect=[error, {'phase': 'absent'}]) as once:
            self.assertEqual(life.forward_windows('intpa@dreamland-ally', 'status', self.config, False), {'phase': 'absent'})
            self.assertEqual(once.call_count, 2)
        with patch.object(life, '_forward_windows_once', side_effect=error) as once:
            with self.assertRaises(life.Refused):
                life.forward_windows('intpa@dreamland-ally', 'status', self.config, False)
            self.assertEqual(once.call_count, 2)
        error.transport_not_started = False
        with patch.object(life, '_forward_windows_once', side_effect=error) as once:
            with self.assertRaises(life.Refused):
                life.forward_windows('intpa@dreamland-ally', 'up', self.config, True)
            self.assertEqual(once.call_count, 1)

    def test_windows_real_transport_classifies_broken_pipe_1312_without_replaying_a_claim(self):
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
        with patch.object(life, 'Ledger') as ledger:
            result = life.forward_windows('intpa@dreamland-ally', 'status', self.config, False, popen, ledger)
        self.assertEqual(result, {'phase': 'absent'})
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0], commands[1])
        self.assertIn('-NoProfile -NonInteractive -EncodedCommand', commands[0][-1])
        self.assertEqual(commands[0][1:3], ['-T', '-x'])
        ledger.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
