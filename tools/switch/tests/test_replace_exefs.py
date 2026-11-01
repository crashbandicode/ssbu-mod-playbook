"""Tests for tools/switch/replace-exefs.py against a fake card: a local directory the script
reads and writes the way it reads the real card.

* MTP transport: `gio` is the real /usr/bin/gio on a `file://` tree (same list/info/copy/mkdir
  calls, same output format, same exit codes); fake_gio.py wraps it to inject faults.
* Mounted-card transport: the same transaction on a plain directory; the mount checks are
  replaced in the test process, and faults are injected by wrapping a transport method.
* Never reaches a real card: setUp refuses any URI that is not `file:///`, and the refusal
  tests that run the script as `python3 -I` fail before the first card operation.

Run: python3 -I tools/switch/tests/test_replace_exefs.py"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
# REPLACE_EXEFS_UNDER_TEST names another copy of the script (a mutated one, to see the tests fail).
SCRIPT = Path(os.environ.get('REPLACE_EXEFS_UNDER_TEST') or HERE.parent / 'replace-exefs.py').resolve()
TITLE = 'atmosphere/contents/01006A800016E000'
SUBSDK9 = TITLE + '/exefs/subsdk9'
NPDM = TITLE + '/exefs/main.npdm'
PLUGINS = TITLE + '/romfs/skyline/plugins'
DATA = 'ultimate/skyline_exefs'
PLUGIN_NAMES = ['libarcropolis.nro', 'libnro_hook.nro', 'libparam_hook.nro', 'libtraining_modpack.nro']
PROTECTED = ['bootloader/hekate_ipl.ini', 'ultimate/ssbu_online_deluxe_rewrite/config.ini',
             'ultimate/ssbu_fx_control/config.ini']
EXTRA = ['ultimate/TrainingModpack/save_states.json', 'ultimate/TrainingModpack/training_modpack.json',
         'ultimate/TrainingModpack/cycle_preload.ini']
sha = lambda data: hashlib.sha256(data).hexdigest()


def blob(name, magic, size):
    seed = hashlib.sha256(name.encode()).digest()
    return (magic + seed * (size // 32 + 1))[:size]


def load_module():
    spec = importlib.util.spec_from_file_location('replace_exefs', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    OLD = blob('old-skyline', b'NSO0', 4000)
    NEW = blob('new-skyline', b'NSO0', 4200)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='replace-exefs-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.card = self.tmp / 'card'
        self.dir = self.tmp / 'install'
        self.dir.mkdir()
        self.module = load_module()
        files = {SUBSDK9: self.OLD, NPDM: blob('old-npdm', b'META', 972),
                 PROTECTED[0]: b'[config]\nautoboot=0\n', PROTECTED[1]: b'deluxe=1\n', PROTECTED[2]: b'fx=1\n',
                 EXTRA[0]: b'{"player":[]}\n', EXTRA[1]: b'{"update":{}}\n', EXTRA[2]: b'[cycle_preload]\ngroup_swap=on\n'}
        for name in PLUGIN_NAMES:
            files[PLUGINS + '/' + name] = blob(name, b'\0' * 16 + b'NRO0', 6000)
        for relative, data in files.items():
            path = self.card / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.approval = {'operator_order': 'test order', 'operator_install_authorized': True, 'target': SUBSDK9,
                         'data': DATA, 'old': sha(self.OLD), 'new': sha(self.NEW), 'extra_protected': list(EXTRA)}
        self.write_approval()
        (self.dir / 'subsdk9').write_bytes(self.NEW)
        self.plan_path = self.tmp / 'plan.json'
        self.log = self.tmp / 'gio-calls.jsonl'
        self.set_plan([])

    def set_plan(self, rules):
        self.plan_path.write_text(json.dumps({'log': str(self.log), 'rules': rules}))
        for leftover in self.tmp.glob('plan.json.counters'):
            leftover.unlink()

    def write_approval(self):
        (self.dir / 'operator-approval.json').write_text(json.dumps(self.approval))

    def tree(self):
        return {str(p.relative_to(self.card)): sha(p.read_bytes()) for p in sorted(self.card.rglob('*')) if p.is_file()}

    def receipt(self, name):
        return json.loads((self.dir / name).read_text())

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def main(self, mode):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.module.main([mode, str(self.dir)])
        return code, out.getvalue(), err.getvalue()


class MtpBase(Base):
    """The real gio against `file://<fake card>`, through the fault-injecting wrapper."""

    def setUp(self):
        super().setUp()
        fake = self.tmp / 'gio'
        shutil.copy(HERE / 'fake_gio.py', fake)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.module.URI = 'file://' + str(self.card)
        self.module.GIO = str(fake)
        self.assertTrue(self.module.URI.startswith('file:///'), 'a test must never reach a real card')
        patcher = mock.patch.dict(os.environ, {'FAKE_GIO_PLAN': str(self.plan_path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def card_path(self, relative):
        return str(self.card / relative)

    def phase(self, mode, expect=0):
        code, out, err = self.main(mode)
        self.assertEqual(code, expect, f'{mode}: stdout={out}\nstderr={err}')
        return out, err

    def install_ready(self):
        self.phase('preflight')
        self.log.write_text('')

    def writes(self, commands):
        """Card paths the receipt's gio commands wrote, made, or removed."""
        root = 'file://' + str(self.card) + '/'
        written = set()
        for entry in commands:
            argv = entry['argv'][1:]
            self.assertIn(argv[0], ('list', 'info', 'copy', 'mkdir'), argv)
            if argv[0] == 'copy' and argv[-1].startswith(root):
                written.add(('copy', argv[-1][len(root):]))
            if argv[0] == 'mkdir':
                written.add(('mkdir', argv[-1][len(root):]))
        return written


class TestMtpTransaction(MtpBase):
    def test_success_preflight_install_verify(self):
        before = self.tree()
        self.phase('preflight')
        self.assertEqual(self.tree(), before, 'preflight must not change the card')
        preflight = self.receipt('preflight.json')
        self.assertEqual(preflight['state'], 'preflight_verified')
        expected = {SUBSDK9, NPDM, *[PLUGINS + '/' + n for n in PLUGIN_NAMES], *PROTECTED, *EXTRA}
        self.assertEqual(set(preflight['before']), expected)
        for relative, entry in preflight['before'].items():
            self.assertEqual(sha(Path(entry['local']).read_bytes()), before[relative], relative)
        self.assertEqual(preflight['exefs_names_before'], ['main.npdm', 'subsdk9'])
        self.assertEqual(self.writes(preflight['commands']), set())
        out, _ = self.phase('install')
        self.assertIn('Done. The Switch is still connected: disconnect it (close DBI) when you are ready.', out)
        deployment = self.receipt('deployment.json')
        self.assertEqual(deployment['state'], 'installed_verified')
        after = self.tree()
        backup, stage = DATA + '/backups/pre-' + sha(self.NEW)[:16] + '.subsdk9', DATA + '/install/subsdk9-' + sha(self.NEW)[:16] + '.stage'
        self.assertEqual({k for k in after if before.get(k) != after[k]}, {SUBSDK9, backup, stage})
        self.assertEqual(after[SUBSDK9], sha(self.NEW))
        self.assertEqual(after[backup], sha(self.OLD))
        self.assertEqual(after[stage], sha(self.NEW))
        self.assertEqual(deployment['backup'], backup)
        self.assertEqual(set(deployment['after']), expected)
        for name in ('writing_rollback_archive', 'rollback_verified', 'staged_verified', 'replacing_target',
                     'installed_pending_readback', 'installed_verified'):
            self.assertIn(name, out)
        self.assertEqual(self.writes(deployment['commands']),
                         {('copy', backup), ('copy', stage), ('copy', SUBSDK9),
                          ('mkdir', DATA + '/backups'), ('mkdir', DATA + '/install')})
        self.assertEqual(deployment['tool']['sha256'], sha(SCRIPT.read_bytes()))
        self.assertTrue(deployment['all_command_processes_exited'])
        out, _ = self.phase('verify')
        self.assertEqual(self.receipt('verify.json')['state'], 'verified')
        self.assertIn('disconnect it (close DBI)', out)
        self.assertEqual(self.tree(), after, 'verify must not change the card')

    def test_main_npdm_target(self):
        new = blob('new-npdm', b'META', 980)
        self.approval.update(target=NPDM, old=sha(blob('old-npdm', b'META', 972)), new=sha(new))
        self.write_approval()
        (self.dir / 'main.npdm').write_bytes(new)
        self.phase('preflight')
        self.phase('install')
        self.assertEqual(self.tree()[NPDM], sha(new))
        self.assertEqual(self.tree()[SUBSDK9], sha(self.OLD))
        self.assertTrue((self.card / DATA / 'backups' / f'pre-{sha(new)[:16]}.main.npdm').is_file())

    def test_other_files_in_exefs_are_kept(self):
        (self.card / TITLE / 'exefs' / 'rtld').write_bytes(b'extra-loader-file')
        self.phase('preflight')
        self.assertIn(TITLE + '/exefs/rtld', self.receipt('preflight.json')['before'])
        self.phase('install')
        self.assertEqual((self.card / TITLE / 'exefs' / 'rtld').read_bytes(), b'extra-loader-file')

    def test_receipts_refuse_rerun(self):
        self.phase('preflight')
        self.phase('install')
        self.phase('verify')
        snapshot = {n: (self.dir / n).read_bytes() for n in ('preflight.json', 'deployment.json', 'verify.json')}
        tree = self.tree()
        for mode in ('preflight', 'install', 'verify'):
            code, out, err = self.main(mode)
            self.assertEqual(code, 1)
            self.assertIn('Preserve completed/failed evidence', err)
        self.assertEqual({n: (self.dir / n).read_bytes() for n in snapshot}, snapshot)
        self.assertEqual(self.tree(), tree)

    def test_failed_install_receipt_also_refuses_rerun(self):
        self.install_ready()
        (self.card / PLUGINS / 'libnro_hook.nro').write_bytes(b'changed size')
        self.phase('install', expect=1)
        code, _, err = self.main('install')
        self.assertEqual(code, 1)
        self.assertIn('Preserve completed/failed evidence', err)

    def test_preflight_old_mismatch_fails_and_install_is_refused(self):
        (self.card / SUBSDK9).write_bytes(blob('somebody-elses-skyline', b'NSO0', 4000))
        tree = self.tree()
        self.phase('preflight', expect=1)
        receipt = self.receipt('preflight.json')
        self.assertEqual(receipt['state'], 'failed')
        self.assertIn('Unexpected installed build', receipt['error'])
        self.phase('install', expect=1)
        self.assertEqual(self.tree(), tree, 'nothing was written')

    def test_install_without_preflight(self):
        tree = self.tree()
        self.phase('install', expect=1)
        self.assertEqual(self.receipt('deployment.json')['state'], 'failed')
        self.assertEqual(self.tree(), tree)

    def test_install_needs_authorization_but_preflight_does_not(self):
        self.approval['operator_install_authorized'] = False
        self.write_approval()
        self.phase('preflight')
        self.log.write_text('')
        tree = self.tree()
        code, _, err = self.main('install')
        self.assertEqual(code, 1)
        self.assertIn('operator_install_authorized', err)
        self.assertEqual(self.calls(), [], 'no card access before the authorization check')
        self.assertFalse((self.dir / 'deployment.json').exists())
        self.assertEqual(self.tree(), tree)

    def test_changed_size_since_preflight_stops_before_any_write(self):
        self.install_ready()
        (self.card / PLUGINS / 'libnro_hook.nro').write_bytes(b'changed size')
        tree = self.tree()
        self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'failed')
        self.assertIn('Changed since preflight', receipt['error'])
        self.assertEqual(self.tree(), tree)
        self.assertEqual(self.writes(receipt['commands']), set())

    def test_new_file_in_exefs_since_preflight_stops_before_any_write(self):
        self.install_ready()
        (self.card / TITLE / 'exefs' / 'surprise').write_bytes(b'x')
        tree = self.tree()
        self.phase('install', expect=1)
        self.assertIn('listing changed', self.receipt('deployment.json')['error'])
        self.assertEqual(self.tree(), tree)

    def test_changed_content_same_size_stops_at_just_before(self):
        self.install_ready()
        (self.card / SUBSDK9).write_bytes(blob('another-skyline', b'NSO0', len(self.OLD)))
        tree = self.tree()
        self.phase('install', expect=1)
        self.assertIn('Changed since preflight', self.receipt('deployment.json')['error'])
        self.assertEqual(self.tree(), tree)

    def test_backup_readback_mismatch_stops_before_replace(self):
        self.install_ready()
        backup = DATA + '/backups/pre-' + sha(self.NEW)[:16] + '.subsdk9'
        self.set_plan([{'dest_endswith': self.card_path(backup), 'fault': 'flip_byte'}])
        self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'failed')
        self.assertIn('Rollback archive readback mismatch', receipt['error'])
        self.assertEqual(self.tree()[SUBSDK9], sha(self.OLD))
        self.assertFalse((self.card / DATA / 'install' / ('subsdk9-' + sha(self.NEW)[:16] + '.stage')).exists())

    def test_stage_readback_short_stops_before_replace(self):
        self.install_ready()
        stage = DATA + '/install/subsdk9-' + sha(self.NEW)[:16] + '.stage'
        self.set_plan([{'dest_endswith': self.card_path(stage), 'fault': 'truncate'}])
        self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'failed')
        self.assertIn('expected', receipt['error'])
        self.assertEqual(self.tree()[SUBSDK9], sha(self.OLD))

    def test_stage_readback_wrong_bytes_stop_before_replace(self):
        self.install_ready()
        stage = DATA + '/install/subsdk9-' + sha(self.NEW)[:16] + '.stage'
        self.set_plan([{'dest_endswith': self.card_path(stage), 'fault': 'flip_byte'}])
        self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'failed', 'stopped at the stage, so no rollback was needed')
        self.assertIn('Staging readback mismatch', receipt['error'])
        self.assertNotIn('replacing_target', (self.dir / 'deployment.json').read_text())
        self.assertEqual(self.tree()[SUBSDK9], sha(self.OLD))

    def test_target_readback_mismatch_rolls_back(self):
        self.install_ready()
        before = self.tree()
        self.set_plan([{'dest_endswith': self.card_path(SUBSDK9), 'nth': 1, 'fault': 'flip_byte'}])
        out, err = self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'rolled_back_verified')
        self.assertIn('readback does not match', receipt['error'])
        self.assertIn('rolling_back', out)
        after = self.tree()
        self.assertEqual(after[SUBSDK9], sha(self.OLD), 'the old Skyline is back on the card')
        self.assertEqual({k for k in after if before.get(k) != after.get(k)},
                         {DATA + '/backups/pre-' + sha(self.NEW)[:16] + '.subsdk9',
                          DATA + '/install/subsdk9-' + sha(self.NEW)[:16] + '.stage'})
        self.assertEqual(receipt['rollback_readback_after_failure']['sha256'], sha(self.OLD))
        self.assertNotIn('disconnect it', out, 'a failed install must not tell the user to disconnect')

    def test_target_short_transfer_rolls_back(self):
        self.install_ready()
        self.set_plan([{'dest_endswith': self.card_path(SUBSDK9), 'nth': 1, 'fault': 'fail_partial'}])
        self.phase('install', expect=1)
        self.assertEqual(self.receipt('deployment.json')['state'], 'rolled_back_verified')
        self.assertEqual(self.tree()[SUBSDK9], sha(self.OLD))

    def test_other_file_changed_during_install_rolls_the_target_back(self):
        self.install_ready()
        plugin = self.card / PLUGINS / 'libparam_hook.nro'
        self.set_plan([{'dest_endswith': self.card_path(SUBSDK9), 'nth': 1, 'fault': 'side_modify', 'file': str(plugin)}])
        self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'rolled_back_verified')
        self.assertIn('libparam_hook.nro', receipt['error'])
        self.assertEqual(self.tree()[SUBSDK9], sha(self.OLD))
        self.assertNotEqual(plugin.read_bytes(), blob('libparam_hook.nro', b'\0' * 16 + b'NRO0', 6000),
                            'the other writer\'s change is left alone')

    def test_rollback_that_cannot_complete_is_recovery_required(self):
        self.install_ready()
        self.set_plan([{'dest_endswith': self.card_path(SUBSDK9), 'nth': 1, 'every': True, 'fault': 'flip_byte'}])
        out, _ = self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'recovery_required')
        self.assertIn('rollback_error', receipt)
        self.assertEqual(sha((self.card / DATA / 'backups' / ('pre-' + sha(self.NEW)[:16] + '.subsdk9')).read_bytes()),
                         sha(self.OLD), 'the on-card rollback copy is intact')
        self.assertEqual(sha(Path(receipt['before'][SUBSDK9]['local']).read_bytes()), sha(self.OLD),
                         'the local copy of the old file is intact')

    def test_interrupt_during_replace_rolls_back(self):
        self.install_ready()
        original = self.module.MtpCard.replace

        def interrupted(card, staged, stage_relative, target_relative):
            original(card, staged, stage_relative, target_relative)
            raise KeyboardInterrupt('signal 15')

        with mock.patch.object(self.module.MtpCard, 'replace', interrupted):
            self.phase('install', expect=1)
        self.assertEqual(self.receipt('deployment.json')['state'], 'rolled_back_verified')
        self.assertEqual(self.tree()[SUBSDK9], sha(self.OLD))

    def test_card_listing_tool_failure_is_a_failed_preflight(self):
        shutil.rmtree(self.card / TITLE / 'romfs')
        self.phase('preflight', expect=1)
        self.assertEqual(self.receipt('preflight.json')['state'], 'failed')

    def test_lock_refuses_a_second_writer(self):
        import fcntl
        with open(self.dir / '.transaction.lock', 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, _, err = self.main('preflight')
        self.assertEqual(code, 1)
        self.assertIn('another transaction holds the lock', err)
        self.assertEqual(self.calls(), [])


class TestRefusalsBeforeCardAccess(MtpBase):
    """Bad approvals and bad new files are refused before the first card operation."""

    def refused(self, fragment):
        code, _, err = self.main('preflight')
        self.assertEqual(code, 1, err)
        self.assertIn('refused before any card access', err)
        self.assertIn(fragment, err)
        self.assertEqual(self.calls(), [], 'no card operation')
        self.assertFalse((self.dir / 'preflight.json').exists())

    def test_targets_outside_the_allow_list(self):
        for target in (PLUGINS + '/libtraining_modpack.nro', TITLE + '/exefs/rtld', TITLE + '/exefs/../exefs/subsdk9',
                       'bootloader/hekate_ipl.ini', TITLE + '/exefs/subsdk8'):
            with self.subTest(target=target):
                self.approval['target'] = target
                self.write_approval()
                self.refused('target')

    def test_bad_data_folders(self):
        for data in ('ultimate/../x', TITLE + '/exefs', 'ultimate', 'ultimate/a/b', '/ultimate/x', 'ultimate/x y'):
            with self.subTest(data=data):
                self.approval['data'] = data
                self.write_approval()
                self.refused('data')

    def test_bad_extra_protected(self):
        for extra in (['ultimate/x/../y'], ['bootloader/hekate_ipl.ini'], ['ultimate/x/.hidden'], PROTECTED[:1],
                      [EXTRA[0], EXTRA[0]], 'ultimate/x/y', ['atmosphere/x/y']):
            with self.subTest(extra=extra):
                self.approval['extra_protected'] = extra
                self.write_approval()
                self.refused('extra_protected')

    def test_new_file_must_match_the_approval(self):
        (self.dir / 'subsdk9').write_bytes(blob('other', b'NSO0', 4200))
        self.refused('not the approved new build')

    def test_new_file_must_be_an_nso(self):
        bad = blob('not-nso', b'MZ90', 4200)
        (self.dir / 'subsdk9').write_bytes(bad)
        self.approval['new'] = sha(bad)
        self.write_approval()
        self.refused('does not start with')

    def test_old_and_new_must_differ_and_be_sha256(self):
        self.approval['new'] = self.approval['old']
        self.write_approval()
        self.refused('different')
        self.approval.update(old='ABC', new='def')
        self.write_approval()
        self.refused('sha256')

    def test_unknown_transport_and_missing_mount(self):
        self.approval['transport'] = 'ftp'
        self.write_approval()
        self.refused('transport')
        self.approval['transport'] = 'ums'
        self.write_approval()
        self.refused('mount')

    def test_missing_order(self):
        self.approval['operator_order'] = ''
        self.write_approval()
        self.refused('operator_order')

    def test_test_uri_is_not_a_real_card(self):
        self.assertTrue(self.module.URI.startswith('file:///'))

    def test_entry_point_refuses_without_touching_a_card(self):
        """The real `python3 -I` entry point, with approvals refused before any card access."""
        run = lambda *args: subprocess.run([sys.executable, '-I', str(SCRIPT), *args], capture_output=True, text=True)
        self.assertEqual(run().returncode, 2)
        self.assertEqual(run('upgrade', str(self.dir)).returncode, 2)
        self.assertEqual(run('preflight', str(self.tmp / 'missing')).returncode, 2)
        self.approval['target'] = PLUGINS + '/libtraining_modpack.nro'
        self.write_approval()
        done = run('preflight', str(self.dir))
        self.assertEqual(done.returncode, 1)
        self.assertIn('refused before any card access', done.stderr)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ['.transaction.lock', 'operator-approval.json', 'subsdk9'])


class TestMountedCardTransaction(Base):
    """The same transaction on a mounted card (a directory here; the mount checks are replaced)."""

    def setUp(self):
        super().setUp()
        self.approval.update(transport='ums', mount=str(self.card))
        self.write_approval()
        for name, value in (('check_mount', lambda card: None), ('mount_source', lambda card: '/dev/test'),
                            ('mount_id', lambda card: 42)):
            patcher = mock.patch.object(self.module.UmsCard, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_success_preflight_install_verify(self):
        before = self.tree()
        for mode in ('preflight', 'install', 'verify'):
            code, out, err = self.main(mode)
            self.assertEqual(code, 0, out + err)
        self.assertIn('unmount or eject it yourself', out)
        deployment = self.receipt('deployment.json')
        self.assertEqual(deployment['state'], 'installed_verified')
        after = self.tree()
        backup = DATA + '/backups/pre-' + sha(self.NEW)[:16] + '.subsdk9'
        self.assertEqual({k for k in after if before.get(k) != after.get(k)}, {SUBSDK9, backup})
        self.assertEqual(after[backup], sha(self.OLD))
        self.assertFalse((self.card / DATA / 'install' / ('subsdk9-' + sha(self.NEW)[:16] + '.stage')).exists(),
                         'the stage was renamed into place')
        self.assertFalse([p for p in self.card.rglob('*.part')], 'no temporary left on the card')
        methods = {e.get('method') for e in deployment['commands'] if e['op'] == 'copy-from-card'}
        self.assertTrue(methods <= {'o_direct', 'fadvise-dontneed'}, methods)
        self.assertEqual({e['op'] for e in deployment['commands']},
                         {'exists', 'list', 'mkdir', 'copy-from-card', 'copy-to-card', 'rename-on-card'})
        verify = self.receipt('verify.json')
        self.assertEqual((verify['state'], verify['mount_id_before'], verify['mount_id_after']), ('verified', 42, 42))

    def test_readback_mismatch_rolls_back(self):
        for mode in ('preflight',):
            self.assertEqual(self.main(mode)[0], 0)
        original = self.module.UmsCard.replace

        def corrupting(card, staged, stage_relative, target_relative):
            original(card, staged, stage_relative, target_relative)
            path = self.card / target_relative
            data = bytearray(path.read_bytes())
            data[10] ^= 0xFF
            path.write_bytes(bytes(data))

        with mock.patch.object(self.module.UmsCard, 'replace', corrupting):
            code, out, err = self.main('install')
        self.assertEqual(code, 1)
        self.assertEqual(self.receipt('deployment.json')['state'], 'rolled_back_verified')
        self.assertEqual(self.tree()[SUBSDK9], sha(self.OLD))
        self.assertFalse([p for p in self.card.rglob('*.part')])

    def test_a_rollback_that_cannot_write_is_recovery_required(self):
        self.assertEqual(self.main('preflight')[0], 0)
        original_replace, original_store = self.module.UmsCard.replace, self.module.UmsCard.store

        def corrupting(card, staged, stage_relative, target_relative):
            original_replace(card, staged, stage_relative, target_relative)
            (self.card / target_relative).write_bytes(b'garbage')

        def refusing(card, local, relative):
            if relative == SUBSDK9:
                raise OSError('card went away')
            return original_store(card, local, relative)

        with mock.patch.object(self.module.UmsCard, 'replace', corrupting), \
                mock.patch.object(self.module.UmsCard, 'store', refusing):
            code, _, _ = self.main('install')
        self.assertEqual(code, 1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'recovery_required')
        self.assertIn('card went away', receipt['rollback_error'])

    def test_verify_notices_a_change_made_after_the_install(self):
        for mode in ('preflight', 'install'):
            self.assertEqual(self.main(mode)[0], 0)
        (self.card / PLUGINS / 'libarcropolis.nro').write_bytes(blob('late', b'\0' * 16 + b'NRO0', 6000))
        code, _, _ = self.main('verify')
        self.assertEqual(code, 1)
        self.assertEqual(self.receipt('verify.json')['state'], 'failed')

    def test_mount_point_must_be_a_mount(self):
        """Without the replaced checks the real one refuses a plain directory."""
        module = load_module()
        card = module.UmsCard(module.Journal(self.tmp / 'j.json', {}), [], self.card)
        with self.assertRaises(module.Refused):
            card.check_mount()


class TestToolRules(unittest.TestCase):
    """The playbook rule that a script never mounts, unmounts or ejects the card."""

    def test_no_card_commands_in_tools_switch(self):
        spec = importlib.util.spec_from_file_location('compliance', HERE.parents[1] / 'check' / 'compliance.py')
        compliance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compliance)
        tools = HERE.parent
        self.assertEqual(compliance.card_commands(sorted(tools.rglob('*')), tools.parent), [])

    def test_the_check_would_notice(self):
        spec = importlib.util.spec_from_file_location('compliance', HERE.parents[1] / 'check' / 'compliance.py')
        compliance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compliance)
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / 'bad.py'
            tool = 'udisks' + 'ctl'  # split so this file itself is not flagged
            bad.write_text(f"subprocess.run(['/usr/bin/{tool}', 'unmount', '-b', '/dev/sdb1'])\n")
            self.assertEqual(compliance.card_commands([bad], Path(tmp)), ['bad.py:1'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
