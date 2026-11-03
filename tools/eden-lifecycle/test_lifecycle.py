#!/usr/bin/env python3
"""Offline only: fake host/RPC/claims; all filesystem fixtures are temporary."""
import base64
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('lifecycle', Path(__file__).with_name('lifecycle.py'))
life = importlib.util.module_from_spec(spec)
spec.loader.exec_module(life)


class FakeLedger:
    def __init__(self, order):
        self.order = order
        self.claimed = self.released = False
        self.refuse = False

    def claim(self):
        self.order.append('claim')
        if self.refuse:
            raise life.Refused('foreign claim')
        self.claimed = True
        return {'line': 'FAKE claim; no coordination file touched'}

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
        self.stop_mode = 'done'
        self.fail_launch = None
        self.leftover = False
        self.job_mode = 'done'
        self.admissions = []

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
        Path(item['run']).mkdir()
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
        if method == 'settings.get':
            result = self.mute
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
        def lost_ack():
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

    def test_native_backend_launch_command_is_bounded_and_sanitized_without_executing(self):
        c = self.controller()
        c.up()
        item = c.state['instances'][0]
        host, commands = life.Host(), []
        host.run = lambda argv, cap=30: commands.append(argv) or ''
        host.unit = lambda _: {'ActiveState': 'active', 'MemoryMax': str(8 * life.GIB), 'MemorySwapMax': '0', 'TasksMax': '256'}
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
        self.assertIn('-i', command)
        self.assertIn('--control-background', command)
        self.assertNotIn('pkill', ' '.join(command))


class ProtocolTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
