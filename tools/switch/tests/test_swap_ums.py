"""Tests for tools/switch/swap-ums.py against a fake card: a plain directory the script reads and
writes the way it reads and writes the mounted card (the mount checks are replaced in the test
process, and faults are injected by wrapping one of the Card's methods or by changing a file on
the fake card at the moment the wrapped call returns).

* Never reaches a real card: every approval's mount is a directory under the test's own
  temporary folder, and the refusal tests that run the script as `python3 -I` fail before the
  first card operation (a plain directory is not a mount point).
* Runs on any filesystem: O_DIRECT reads fall back to dropping the page cache where the
  filesystem refuses them (tmpfs); point TMPDIR at a disk to exercise the O_DIRECT path.

Run: python3 -I tools/switch/tests/test_swap_ums.py"""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
# SWAP_UMS_UNDER_TEST names another copy of the script (a mutated one, to see the tests fail).
SCRIPT = Path(os.environ.get('SWAP_UMS_UNDER_TEST') or HERE.parent / 'swap-ums.py').resolve()
TITLE = 'atmosphere/contents/01006A800016E000'
EXEFS = TITLE + '/exefs'
PLUGINS = TITLE + '/romfs/skyline/plugins'
OLD_NRO, NEW_NRO = PLUGINS + '/libtraining_modpack.nro', PLUGINS + '/libtraining_rooms.nro'
FORK = 'ultimate/TrainingModpack'
ROOMS = 'ultimate/TrainingRooms'
DEVELOPER = ROOMS + '/developer.ini'
PLUGIN_NAMES = ['libarcropolis.nro', 'libnro_hook.nro', 'libparam_hook.nro', 'libssbu_cpu_pilot.nro']
PROTECTED = ['bootloader/hekate_ipl.ini', 'ultimate/ssbu_online_deluxe_rewrite/config.ini',
             'ultimate/ssbu_fx_control/config.ini']
RECEIPTS = {'preflight': 'preflight.json', 'install': 'deployment.json', 'verify': 'verify.json'}
sha = lambda data: hashlib.sha256(data).hexdigest()


def nro(name, size):
    seed = hashlib.sha256(name.encode()).digest()
    return (b'\0' * 16 + b'NRO0' + seed * (size // 32 + 1))[:size]


def blob(name, size):
    return (hashlib.sha256(name.encode()).digest() * (size // 32 + 1))[:size]


def load_module():
    spec = importlib.util.spec_from_file_location('swap_ums', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    OLD = nro('fork', 6000)
    NEW = nro('clean-room', 7000)
    DEV = b'developer_page=on\n'
    BIG = 'practice-trace-swap.bin'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='swap-ums-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.card = self.tmp / 'card'
        self.dir = self.tmp / 'install'
        (self.dir / 'data-files' / ROOMS).mkdir(parents=True)
        self.module = load_module()
        # A "large" file for the read-back cap, without a large file.
        self.cap = mock.patch.object(self.module, 'MAX_BYTES', 50_000)
        self.cap.start()
        self.addCleanup(self.cap.stop)
        files = {OLD_NRO: self.OLD, EXEFS + '/subsdk9': nro('subsdk9', 4000), EXEFS + '/main.npdm': blob('npdm', 972),
                 PROTECTED[0]: b'[config]\nautoboot=0\n', PROTECTED[1]: b'deluxe=1\n', PROTECTED[2]: b'fx=1\n',
                 'ultimate/ssbu_other/config.ini': b'other=1\n',
                 FORK + '/save_states.json': b'{"player":[{"held_item":null}]}\n',
                 FORK + '/training_modpack_menu.conf': b'{"menu":{"character_item":[1]}}\n',
                 FORK + '/button_config.ini': b'[menu]\nshortcut_1=Minus\n', FORK + '/replay_capture.ini': b'[capture]\nenabled=on\n',
                 FORK + '/mii-presets.json': b'[]\n', FORK + '/macros/replay-p2.json': b'{"m":1}\n',
                 FORK + '/replay_captures/7265.smd': blob('smd', 3000), FORK + '/replay_captures/.7265.smd-0.tmp': blob('tmp', 100),
                 FORK + '/' + self.BIG: blob('big', 60_000),
                 FORK + '/backups/pre-aaaaaaaaaaaaaaaa.nro': blob('pre', 5000), FORK + '/install/old-aaaa.stage': blob('stage', 5000)}
        for name in PLUGIN_NAMES:
            files[PLUGINS + '/' + name] = nro(name, 6000)
        for relative, data in files.items():
            path = self.card / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.approval = {'operator_order': 'test order', 'operator_swap_authorized': True, 'mount': str(self.card),
                         'remove': {'name': 'libtraining_modpack.nro', 'sha256': sha(self.OLD)},
                         'add': {'name': 'libtraining_rooms.nro', 'sha256': sha(self.NEW),
                                 'files': [{'path': DEVELOPER, 'sha256': sha(self.DEV)}]},
                         'data': FORK, 'extra_protected': ['ultimate/ssbu_other/config.ini'], 'watch': [FORK]}
        self.write_approval()
        (self.dir / 'libtraining_rooms.nro').write_bytes(self.NEW)
        (self.dir / 'data-files' / DEVELOPER).write_bytes(self.DEV)
        for name, value in (('check_mount', lambda card: None), ('mount_source', lambda card: '/dev/test'),
                            ('mount_id', lambda card: 42)):
            patcher = mock.patch.object(self.module.Card, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ops = []
        original = self.module.Card.op

        def spy(card, name, *args):
            self.ops.append((name, args))
            return original(card, name, *args)

        patcher = mock.patch.object(self.module.Card, 'op', spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_approval(self):
        (self.dir / 'operator-approval.json').write_text(json.dumps(self.approval))

    def tree(self):
        return {str(p.relative_to(self.card)): sha(p.read_bytes()) for p in sorted(self.card.rglob('*')) if p.is_file()}

    def dirs(self):
        return {str(p.relative_to(self.card)) for p in self.card.rglob('*') if p.is_dir()}

    def receipt(self, name):
        return json.loads((self.dir / name).read_text())

    def main(self, mode):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.module.main([mode, str(self.dir)])
        return code, out.getvalue(), err.getvalue()

    def phase(self, mode, expect=0):
        code, out, err = self.main(mode)
        self.assertEqual(code, expect, f'{mode}: stdout={out}\nstderr={err}')
        return out, err

    def changed(self, before, after):
        return {k for k in set(before) | set(after) if before.get(k) != after.get(k)}

    @property
    def archive(self):
        return f'{FORK}/backups/removed-{sha(self.OLD)[:16]}.nro'

    @property
    def stage(self):
        return f'{FORK}/install/libtraining_rooms-{sha(self.NEW)[:16]}.swap-stage'

    def preflighted(self):
        self.phase('preflight')
        self.ops.clear()

    def flip(self, relative):
        path = self.card / relative
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        path.write_bytes(bytes(data))

    def after(self, method, hook):
        """Wrap Card.<method> so `hook(card, *args)` runs after the real call."""
        original = getattr(self.module.Card, method)

        def wrapper(card, *args, **kwargs):
            result = original(card, *args, **kwargs)
            hook(card, *args)
            return result

        return mock.patch.object(self.module.Card, method, wrapper)

    def card_writes(self, commands):
        return [(e['op'], e['args'][-1]) for e in commands
                if e['op'] in ('copy-to-card', 'rename-on-card', 'remove-on-card', 'mkdir', 'rmdir-on-card')]


class TestSwap(Base):
    def test_success_preflight_install_verify(self):
        before, dirs = self.tree(), self.dirs()
        self.phase('preflight')
        self.assertEqual(self.tree(), before, 'preflight must not change the card')
        self.assertEqual(self.dirs(), dirs)
        preflight = self.receipt('preflight.json')
        self.assertEqual(preflight['state'], 'preflight_verified')
        self.assertEqual(self.card_writes(preflight['commands']), [])
        names = set(preflight['before'])
        self.assertIn(OLD_NRO, names)
        self.assertIn(EXEFS + '/subsdk9', names)
        self.assertIn('ultimate/ssbu_other/config.ini', names)
        self.assertEqual(set(preflight['watch_before']), {FORK})
        tree = preflight['watch_before'][FORK]
        self.assertEqual(tree[FORK + '/' + self.BIG]['sha256'], None, 'over the cap: size only')
        self.assertEqual(tree[FORK + '/' + self.BIG]['size'], 60_000)
        self.assertIn(FORK + '/replay_captures/.7265.smd-0.tmp', tree, 'hidden files are watched too')
        self.assertEqual(tree[FORK + '/backups']['skipped'], 'archive/stage folder: checked by name')
        for relative, entry in preflight['before'].items():
            self.assertEqual(sha(Path(entry['local']).read_bytes()), before[relative], relative)
        out, _ = self.phase('install')
        for name in ('writing_rollback_archive', 'archive_verified', 'staged_verified', 'removing_old', 'swapping_in',
                     'placing_data_files', 'installed_pending_readback', 'installed_verified'):
            self.assertIn(name, out)
        self.assertIn('unmount or eject it yourself', out)
        deployment = self.receipt('deployment.json')
        self.assertEqual(deployment['state'], 'installed_verified')
        after = self.tree()
        self.assertEqual(self.changed(before, after), {OLD_NRO, NEW_NRO, self.archive, DEVELOPER})
        self.assertNotIn(OLD_NRO, after)
        self.assertEqual(after[NEW_NRO], sha(self.NEW))
        self.assertEqual(after[self.archive], sha(self.OLD))
        self.assertEqual(after[DEVELOPER], sha(self.DEV))
        self.assertNotIn(DEVELOPER.replace('developer.ini', 'migration.json'), after, 'the import marker is never placed')
        self.assertFalse((self.card / self.stage).exists(), 'the stage was renamed into place')
        self.assertFalse(list(self.card.rglob('*.part')), 'no temporary left on the card')
        self.assertEqual(self.card_writes(deployment['commands']),
                         [('copy-to-card', self.archive), ('copy-to-card', self.stage), ('remove-on-card', OLD_NRO),
                          ('rename-on-card', NEW_NRO), ('mkdir', ROOMS), ('copy-to-card', DEVELOPER)])
        methods = {e.get('method') for e in deployment['commands'] if e['op'] == 'copy-from-card'}
        self.assertTrue(methods <= {'o_direct', 'fadvise-dontneed'}, methods)
        if os.environ.get('SWAP_UMS_EXPECT_DIRECT'):  # set it with TMPDIR on a disk that accepts O_DIRECT
            self.assertEqual(methods, {'o_direct'})
        self.assertEqual({e['op'] for e in deployment['commands']},
                         {'exists', 'list', 'mkdir', 'copy-from-card', 'copy-to-card', 'rename-on-card', 'remove-on-card', 'statvfs'})
        self.assertTrue(set(deployment['after']) >= {NEW_NRO, DEVELOPER, self.archive, EXEFS + '/subsdk9'})
        self.assertNotIn(OLD_NRO, deployment['after'])
        self.assertTrue(deployment['watched_trees_unchanged'] and deployment['other_files_unchanged'])
        self.assertEqual(deployment['plugin_names_after'], sorted(PLUGIN_NAMES + ['libtraining_rooms.nro']))
        self.assertEqual(deployment['tool']['sha256'], sha(SCRIPT.read_bytes()))
        self.assertTrue(deployment['all_operations_closed'])
        for relative in (FORK + '/save_states.json', FORK + '/training_modpack_menu.conf', FORK + '/replay_captures/7265.smd'):
            self.assertEqual(after[relative], before[relative], f'the fork data is untouched: {relative}')
        out, _ = self.phase('verify')
        verify = self.receipt('verify.json')
        self.assertEqual((verify['state'], verify['mount_id_before'], verify['mount_id_after']), ('verified', 42, 42))
        self.assertEqual(self.tree(), after, 'verify must not change the card')
        self.assertEqual(self.card_writes(verify['commands']), [])

    def test_only_the_swapped_plugin_changes_and_other_plugin_bytes_match(self):
        before = self.tree()
        self.phase('preflight')
        self.phase('install')
        after = self.tree()
        for name in PLUGIN_NAMES:
            self.assertEqual(after[PLUGINS + '/' + name], before[PLUGINS + '/' + name])
        for relative in PROTECTED + [EXEFS + '/subsdk9', EXEFS + '/main.npdm']:
            self.assertEqual(after[relative], before[relative])

    def test_swap_without_data_files_and_without_watch(self):
        self.approval['add']['files'] = []
        self.approval['watch'] = []
        self.write_approval()
        before = self.tree()
        for mode in ('preflight', 'install', 'verify'):
            self.phase(mode)
        self.assertEqual(self.changed(before, self.tree()), {OLD_NRO, NEW_NRO, self.archive})
        self.assertNotIn(ROOMS, {str(Path(d)) for d in self.dirs()}, 'no data folder was made')

    def test_creates_the_archive_folders_when_the_data_folder_is_new(self):
        shutil.rmtree(self.card / FORK / 'backups')
        shutil.rmtree(self.card / FORK / 'install')
        self.approval['watch'] = []
        self.write_approval()
        for mode in ('preflight', 'install', 'verify'):
            self.phase(mode)
        self.assertEqual(sha((self.card / self.archive).read_bytes()), sha(self.OLD))
        self.assertEqual(list((self.card / FORK / 'install').iterdir()), [])

    def test_receipts_refuse_rerun(self):
        for mode in ('preflight', 'install', 'verify'):
            self.phase(mode)
        snapshot = {n: (self.dir / n).read_bytes() for n in ('preflight.json', 'deployment.json', 'verify.json')}
        tree = self.tree()
        for mode in ('preflight', 'install', 'verify'):
            code, out, err = self.main(mode)
            self.assertEqual(code, 1)
            self.assertIn('Preserve completed/failed evidence', err)
        self.assertEqual({n: (self.dir / n).read_bytes() for n in snapshot}, snapshot)
        self.assertEqual(self.tree(), tree)

    def test_failed_receipt_also_refuses_rerun(self):
        self.preflighted()
        (self.card / PLUGINS / 'libnro_hook.nro').write_bytes(b'changed size')
        self.phase('install', expect=1)
        code, _, err = self.main('install')
        self.assertEqual(code, 1)
        self.assertIn('Preserve completed/failed evidence', err)

    def test_reverse_swap_undoes_a_completed_swap(self):
        """The same tool with remove and add exchanged: the clean room out, the archived fork NRO back in."""
        original = self.tree()
        for mode in ('preflight', 'install', 'verify'):
            self.phase(mode)
        swapped = self.tree()
        undo = self.tmp / 'undo'
        undo.mkdir()
        old_copy = Path(self.receipt('preflight.json')['before'][OLD_NRO]['local'])
        shutil.copy(old_copy, undo / 'libtraining_modpack.nro')
        self.dir = undo
        self.approval = {'operator_order': 'undo', 'operator_swap_authorized': True, 'mount': str(self.card),
                         'remove': {'name': 'libtraining_rooms.nro', 'sha256': sha(self.NEW)},
                         'add': {'name': 'libtraining_modpack.nro', 'sha256': sha(self.OLD), 'files': []},
                         'data': FORK, 'extra_protected': [], 'watch': [FORK]}
        self.write_approval()
        for mode in ('preflight', 'install', 'verify'):
            self.phase(mode)
        final = self.tree()
        self.assertEqual(final[OLD_NRO], sha(self.OLD), 'the fork NRO is back, byte for byte')
        self.assertNotIn(NEW_NRO, final)
        self.assertEqual(final[f'{FORK}/backups/removed-{sha(self.NEW)[:16]}.nro'], sha(self.NEW), 'the clean room NRO is archived')
        self.assertEqual(final[DEVELOPER], sha(self.DEV), 'the clean room data is left alone')
        for relative in original:
            if relative not in (OLD_NRO,):
                self.assertEqual(final.get(relative), original[relative], relative)
        self.assertEqual(self.changed(swapped, final) - {OLD_NRO, NEW_NRO}, {f'{FORK}/backups/removed-{sha(self.NEW)[:16]}.nro'})


class TestFailuresBeforeTheRemoval(Base):
    """A mismatch or error before the old NRO is touched stops there: nothing to undo."""

    def test_archive_readback_mismatch_stops_before_the_removal(self):
        self.preflighted()
        before = self.tree()
        with self.after('store', lambda card, local, relative: self.flip(relative) if relative.endswith('removed-' + sha(self.OLD)[:16] + '.nro') else None):
            self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'failed')
        self.assertIn('Rollback archive readback mismatch', receipt['error'])
        self.assertEqual(self.tree()[OLD_NRO], sha(self.OLD), 'the old NRO is still installed')
        self.assertNotIn(NEW_NRO, self.tree())
        self.assertEqual(self.card_writes(receipt['commands']), [('copy-to-card', self.archive)])
        self.assertNotIn('remove-on-card', {e['op'] for e in receipt['commands']})
        self.assertEqual(self.changed(before, self.tree()), {self.archive})

    def test_stage_readback_wrong_bytes_stop_before_the_removal(self):
        self.preflighted()
        with self.after('store', lambda card, local, relative: self.flip(relative) if relative == self.stage else None):
            self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'failed', 'stopped at the stage, so no rollback was needed')
        self.assertIn('Staging readback mismatch', receipt['error'])
        self.assertNotIn('removing_old', (self.dir / 'deployment.json').read_text())
        self.assertEqual(self.tree()[OLD_NRO], sha(self.OLD))
        self.assertNotIn(NEW_NRO, self.tree())

    def test_stage_short_write_stops_before_the_removal(self):
        self.preflighted()

        def truncate(card, local, relative):
            if relative == self.stage:
                path = card.path(relative)
                path.write_bytes(path.read_bytes()[:3000])

        with self.after('store', truncate):
            self.phase('install', expect=1)
        self.assertIn('expected', self.receipt('deployment.json')['error'])
        self.assertEqual(self.tree()[OLD_NRO], sha(self.OLD))

    def test_install_without_preflight(self):
        tree = self.tree()
        self.phase('install', expect=1)
        self.assertEqual(self.receipt('deployment.json')['state'], 'failed')
        self.assertEqual(self.tree(), tree)

    def test_changed_size_since_preflight(self):
        self.preflighted()
        (self.card / PLUGINS / 'libnro_hook.nro').write_bytes(b'changed size')
        tree = self.tree()
        self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertIn('Changed since preflight', receipt['error'])
        self.assertEqual(self.tree(), tree)
        self.assertEqual(self.card_writes(receipt['commands']), [])

    def test_new_file_in_exefs_since_preflight(self):
        self.preflighted()
        (self.card / EXEFS / 'surprise').write_bytes(b'x')
        tree = self.tree()
        self.phase('install', expect=1)
        self.assertIn('listing changed', self.receipt('deployment.json')['error'])
        self.assertEqual(self.tree(), tree)

    def test_old_nro_content_changed_same_size(self):
        self.preflighted()
        (self.card / OLD_NRO).write_bytes(nro('another-fork', len(self.OLD)))
        tree = self.tree()
        self.phase('install', expect=1)
        self.assertIn('Changed since preflight', self.receipt('deployment.json')['error'])
        self.assertEqual(self.tree(), tree)

    def test_watched_tree_changed_since_preflight(self):
        self.preflighted()
        (self.card / FORK / 'save_states.json').write_bytes(b'{"player":[{"held_item":null},{}]}\n')
        tree = self.tree()
        self.phase('install', expect=1)
        self.assertIn('watched tree changed since preflight', self.receipt('deployment.json')['error'])
        self.assertEqual(self.tree(), tree)
        (self.card / FORK / 'save_states.json').write_bytes(b'{"player":[{"held_item":null}]}\n')

    def test_new_file_in_watched_tree_since_preflight(self):
        self.preflighted()
        (self.card / FORK / 'surprise.txt').write_bytes(b'x')
        self.phase('install', expect=1)
        self.assertIn('only after', self.receipt('deployment.json')['error'])

    def test_new_nro_appeared_since_preflight(self):
        self.preflighted()
        (self.card / NEW_NRO).write_bytes(b'x')
        tree = self.tree()
        self.phase('install', expect=1)
        self.assertIn('a card listing changed since preflight', self.receipt('deployment.json')['error'])
        self.assertEqual(self.tree(), tree)

    def test_added_file_appeared_since_preflight(self):
        self.preflighted()
        (self.card / ROOMS).mkdir()
        (self.card / DEVELOPER).write_bytes(b'developer_page=off\n')
        tree = self.tree()
        self.phase('install', expect=1)
        self.assertIn('never overwrites', self.receipt('deployment.json')['error'])
        self.assertEqual(self.tree(), tree)

    def test_card_read_only_is_refused_before_any_write(self):
        self.preflighted()
        real = os.statvfs

        def statvfs(path):
            result = real(path)
            return type('S', (), {'f_bavail': result.f_bavail, 'f_frsize': result.f_frsize, 'f_flag': result.f_flag | os.ST_RDONLY})()

        tree = self.tree()
        with mock.patch('os.statvfs', statvfs):
            self.phase('install', expect=1)
        self.assertIn('read-only', self.receipt('deployment.json')['error'])
        self.assertEqual(self.tree(), tree)

    def test_approval_or_local_copy_changed_since_preflight(self):
        self.preflighted()
        self.approval['data'] = 'ultimate/Other'
        self.write_approval()
        tree = self.tree()
        self.phase('install', expect=1)
        self.assertIn('approval changed since preflight', self.receipt('deployment.json')['error'])
        self.assertEqual(self.tree(), tree)

    def test_local_copy_of_old_nro_changed(self):
        self.preflighted()
        copy = Path(self.receipt('preflight.json')['before'][OLD_NRO]['local'])
        copy.write_bytes(b'tampered')
        tree = self.tree()
        self.phase('install', expect=1)
        self.assertIn('local copy of the old NRO changed', self.receipt('deployment.json')['error'])
        self.assertEqual(self.tree(), tree)

    def test_install_needs_authorization_but_preflight_does_not(self):
        self.approval['operator_swap_authorized'] = False
        self.write_approval()
        self.phase('preflight')
        self.ops.clear()
        tree = self.tree()
        code, _, err = self.main('install')
        self.assertEqual(code, 1)
        self.assertIn('operator_swap_authorized', err)
        self.assertEqual(self.ops, [], 'no card access before the authorization check')
        self.assertFalse((self.dir / 'deployment.json').exists())
        self.assertEqual(self.tree(), tree)

    def test_lock_refuses_a_second_writer(self):
        with open(self.dir / '.transaction.lock', 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, _, err = self.main('preflight')
        self.assertEqual(code, 1)
        self.assertIn('another transaction holds the lock', err)
        self.assertEqual(self.ops, [])


class TestPreflightRefusals(Base):
    """A preflight that fails writes nothing and blocks the install."""

    def failed_preflight(self, fragment):
        tree, dirs = self.tree(), self.dirs()
        self.phase('preflight', expect=1)
        receipt = self.receipt('preflight.json')
        self.assertEqual(receipt['state'], 'failed')
        self.assertIn(fragment, receipt['error'])
        self.assertEqual(self.card_writes(receipt['commands']), [])
        self.assertEqual(self.tree(), tree)
        self.assertEqual(self.dirs(), dirs)
        self.phase('install', expect=1)
        self.assertEqual(self.tree(), tree)

    def test_old_nro_hash_mismatch(self):
        (self.card / OLD_NRO).write_bytes(nro('somebody-elses-build', len(self.OLD)))
        self.failed_preflight('Unexpected installed build')

    def test_old_nro_not_installed(self):
        (self.card / OLD_NRO).unlink()
        self.failed_preflight('nothing to remove')

    def test_new_nro_already_installed(self):
        (self.card / NEW_NRO).write_bytes(self.NEW)
        self.failed_preflight('already on the card')

    def test_archive_exists(self):
        (self.card / self.archive).write_bytes(b'x')
        self.failed_preflight('archive or stage exists')

    def test_stage_exists(self):
        (self.card / self.stage).write_bytes(b'x')
        self.failed_preflight('archive or stage exists')

    def test_added_file_exists(self):
        (self.card / ROOMS).mkdir()
        (self.card / DEVELOPER).write_bytes(b'x')
        self.failed_preflight('never overwrites')

    def test_a_dangling_symlink_at_an_added_path_counts_as_existing(self):
        (self.card / ROOMS).mkdir()
        (self.card / DEVELOPER).symlink_to('nowhere')
        self.failed_preflight('never overwrites')

    def test_plugin_folder_holds_more_than_nros(self):
        (self.card / PLUGINS / 'readme.txt').write_bytes(b'x')
        self.failed_preflight('more than NROs')

    def test_exefs_holds_a_folder(self):
        (self.card / EXEFS / 'sub').mkdir()
        self.failed_preflight('more than files')

    def test_not_enough_room(self):
        real = os.statvfs

        def statvfs(path):
            result = real(path)
            return type('S', (), {'f_bavail': 100, 'f_frsize': 4096, 'f_flag': result.f_flag})()

        with mock.patch('os.statvfs', statvfs):
            self.failed_preflight('bytes free')

    def test_room_for_exactly_the_writes_plus_the_reserve_passes(self):
        needed = len(self.NEW) * 2 + len(self.DEV) + 2 * 4096
        self.approval['min_free_bytes'] = 0
        self.write_approval()
        real = os.statvfs

        def statvfs(path):
            result = real(path)
            return type('S', (), {'f_bavail': needed // 4096 + 1, 'f_frsize': 4096, 'f_flag': result.f_flag})()

        with mock.patch('os.statvfs', statvfs):
            self.phase('preflight')

    def test_watched_tree_missing(self):
        shutil.rmtree(self.card / FORK)
        self.approval['data'] = 'ultimate/Elsewhere'
        self.write_approval()
        self.failed_preflight('not on the card')

    def test_the_card_listing_failing_is_a_failed_preflight(self):
        shutil.rmtree(self.card / TITLE / 'romfs')
        self.failed_preflight('')

    def test_not_a_mount_point_is_refused_before_any_card_operation(self):
        module = load_module()
        card = module.Card(module.Journal(self.tmp / 'j.json', {}), [], self.card)
        with self.assertRaises(module.Refused):
            card.check_mount()


class TestFailuresAfterTheRemoval(Base):
    """A mismatch or interrupt once the old NRO is gone undoes the swap, in reverse."""

    def assert_original_plus_archive(self, before, dirs, stage_left=False):
        after = self.tree()
        self.assertEqual(self.changed(before, after), {self.archive} | ({self.stage} if stage_left else set()),
                         'only the archive (and, before the rename, the stage) is left behind')
        self.assertEqual(after[OLD_NRO], sha(self.OLD), 'the old NRO is back on the card')
        self.assertNotIn(NEW_NRO, after)
        self.assertNotIn(DEVELOPER, after)
        self.assertFalse((self.card / ROOMS).exists(), 'the data folder this run made is gone')
        self.assertFalse(list(self.card.rglob('*.part')))
        self.assertEqual(self.dirs(), dirs)

    def test_new_nro_readback_mismatch_rolls_back(self):
        self.preflighted()
        before, dirs = self.tree(), self.dirs()
        with self.after('rename', lambda card, source, destination: self.flip(destination)):
            out, _ = self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'rolled_back_verified')
        self.assertIn('readback does not match', receipt['error'])
        self.assertIn('rolling_back', out)
        self.assertNotIn('unmount or eject', out, 'a failed install must not tell the user to disconnect')
        self.assert_original_plus_archive(before, dirs)
        self.assertEqual(receipt['rollback_readback_after_failure']['sha256'], sha(self.OLD))

    def test_rollback_order_is_the_reverse_of_the_swap(self):
        self.preflighted()
        with self.after('rename', lambda card, source, destination: self.flip(destination)):
            self.phase('install', expect=1)
        writes = self.card_writes(self.receipt('deployment.json')['commands'])
        start = writes.index(('remove-on-card', DEVELOPER))
        self.assertEqual(writes[start:], [('remove-on-card', DEVELOPER), ('rmdir-on-card', ROOMS),
                                          ('remove-on-card', NEW_NRO), ('copy-to-card', OLD_NRO)])

    def test_data_file_readback_mismatch_rolls_back(self):
        self.preflighted()
        before, dirs = self.tree(), self.dirs()
        with self.after('store', lambda card, local, relative: self.flip(relative) if relative == DEVELOPER else None):
            self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'rolled_back_verified')
        self.assertIn(DEVELOPER, receipt['error'])
        self.assert_original_plus_archive(before, dirs)

    def test_another_writer_changes_a_plugin_during_the_install(self):
        self.preflighted()
        plugin = self.card / PLUGINS / 'libparam_hook.nro'
        original = plugin.read_bytes()
        with self.after('rename', lambda card, source, destination: self.flip(PLUGINS + '/libparam_hook.nro')):
            self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'rolled_back_verified')
        self.assertIn('libparam_hook.nro', receipt['error'])
        self.assertEqual(self.tree()[OLD_NRO], sha(self.OLD))
        self.assertNotEqual(plugin.read_bytes(), original, "the other writer's change is left alone")

    def test_watched_tree_changed_during_the_install(self):
        self.preflighted()
        with self.after('store', lambda card, local, relative: self.flip(FORK + '/save_states.json') if relative == DEVELOPER else None):
            self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'rolled_back_verified')
        self.assertIn('watched tree', receipt['error'])
        self.assertIn('changed: ' + FORK + '/save_states.json', receipt['error'])
        self.assertEqual(self.tree()[OLD_NRO], sha(self.OLD))
        self.assertFalse((self.card / DEVELOPER).exists())

    def test_file_added_to_the_watched_tree_during_the_install(self):
        self.preflighted()

        def add(card, local, relative):
            if relative == DEVELOPER:
                (self.card / FORK / 'macros' / 'late.json').write_bytes(b'{}')

        with self.after('store', add):
            self.phase('install', expect=1)
        self.assertEqual(self.receipt('deployment.json')['state'], 'rolled_back_verified')
        self.assertIn('only after', self.receipt('deployment.json')['error'])

    def test_the_archive_changing_during_the_install_is_caught(self):
        self.preflighted()
        before, dirs = self.tree(), self.dirs()
        with self.after('rename', lambda card, source, destination: self.flip(self.archive)):
            self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'rolled_back_verified')
        self.assertIn(self.archive, receipt['error'])
        self.assertEqual(self.tree()[OLD_NRO], sha(self.OLD))

    def test_a_plugin_another_writer_adds_is_reported_not_undone(self):
        self.preflighted()
        with self.after('rename', lambda card, source, destination: (self.card / PLUGINS / 'libextra.nro').write_bytes(nro('extra', 4000))):
            self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertIn('the plugin folder lists', receipt['error'])
        self.assertEqual(receipt['state'], 'rolled_back_verified', 'the swap is undone; the other writer is not this tool\'s to undo')
        self.assertEqual(receipt['plugin_names_changed_by_others'], ['libextra.nro'])
        self.assertTrue((self.card / PLUGINS / 'libextra.nro').exists())
        self.assertEqual(self.tree()[OLD_NRO], sha(self.OLD))
        self.assertNotIn(NEW_NRO, self.tree())

    def test_a_file_that_appears_at_an_added_path_is_never_overwritten_or_removed(self):
        self.preflighted()
        foreign = b'developer_page=off # somebody else\n'
        with self.after('mkdir_p', lambda card, relative: (self.card / DEVELOPER).write_bytes(foreign) if relative == ROOMS else None):
            self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'rolled_back_verified')
        self.assertIn('never overwrites', receipt['error'])
        self.assertEqual((self.card / DEVELOPER).read_bytes(), foreign, 'the other writer\'s file is untouched')
        self.assertEqual(receipt['rollback_left_alone'], [DEVELOPER])
        self.assertFalse(list(self.card.rglob('*.part')), 'the temporary this run made is gone')
        self.assertTrue((self.card / ROOMS).exists(), 'a folder that holds a foreign file is kept')
        self.assertEqual(self.tree()[OLD_NRO], sha(self.OLD))
        self.assertNotIn(NEW_NRO, self.tree())

    def test_a_restore_that_reads_back_wrong_is_recovery_required(self):
        self.preflighted()
        with self.after('rename', lambda card, source, destination: self.flip(destination)), \
                self.after('store', lambda card, local, relative, *rest: self.flip(relative) if relative == OLD_NRO else None):
            self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'recovery_required')
        self.assertIn('does not match the old build', receipt['rollback_error'])

    def test_the_old_nro_coming_back_is_caught(self):
        self.preflighted()
        with self.after('rename', lambda card, source, destination: (self.card / OLD_NRO).write_bytes(self.OLD)):
            self.phase('install', expect=1)
        self.assertEqual(self.receipt('deployment.json')['state'], 'rolled_back_verified')
        self.assertEqual(self.tree()[OLD_NRO], sha(self.OLD))
        self.assertNotIn(NEW_NRO, self.tree())

    def test_interrupt_right_after_the_rename_rolls_back(self):
        self.preflighted()
        before, dirs = self.tree(), self.dirs()

        def interrupt(card, source, destination):
            raise KeyboardInterrupt('signal 15')

        with self.after('rename', interrupt):
            self.phase('install', expect=1)
        self.assertEqual(self.receipt('deployment.json')['state'], 'rolled_back_verified')
        self.assert_original_plus_archive(before, dirs)

    def test_interrupt_between_the_removal_and_the_rename_rolls_back(self):
        self.preflighted()
        before, dirs = self.tree(), self.dirs()

        def interrupted(card, source, destination):
            raise KeyboardInterrupt('signal 2')

        with mock.patch.object(self.module.Card, 'rename', interrupted):
            self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'rolled_back_verified')
        self.assertNotIn(OLD_NRO, {e['args'][-1] for e in receipt['commands'] if e['op'] == 'rename-on-card'})
        self.assert_original_plus_archive(before, dirs, stage_left=True)
        self.assertTrue((self.card / self.stage).exists(), 'the stage stays, with the archive, as evidence')

    def test_interrupt_while_placing_the_second_data_file(self):
        extra = b'second\n'
        (self.dir / 'data-files' / ROOMS / 'second.ini').write_bytes(extra)
        self.approval['add']['files'].append({'path': ROOMS + '/second.ini', 'sha256': sha(extra)})
        self.write_approval()
        self.preflighted()
        before, dirs = self.tree(), self.dirs()
        original = self.module.Card.store

        def interrupting(card, local, relative, overwrite=False):
            if relative == ROOMS + '/second.ini':
                raise KeyboardInterrupt('signal 15')
            return original(card, local, relative, overwrite)

        with mock.patch.object(self.module.Card, 'store', interrupting):
            self.phase('install', expect=1)
        self.assertEqual(self.receipt('deployment.json')['state'], 'rolled_back_verified')
        self.assert_original_plus_archive(before, dirs)

    def test_error_during_the_removal_leaves_the_old_nro_and_is_undone(self):
        self.preflighted()

        def failing(card, relative):
            raise OSError('card went away')

        with mock.patch.object(self.module.Card, 'unlink', failing):
            self.phase('install', expect=1)
        self.assertEqual(self.receipt('deployment.json')['state'], 'rolled_back_verified')
        self.assertEqual(self.tree()[OLD_NRO], sha(self.OLD))

    def test_rollback_that_cannot_write_is_recovery_required(self):
        self.preflighted()
        original = self.module.Card.store

        def refusing(card, local, relative, overwrite=False):
            if relative == OLD_NRO:
                raise OSError('card went away')
            return original(card, local, relative, overwrite)

        with self.after('rename', lambda card, source, destination: self.flip(destination)), \
                mock.patch.object(self.module.Card, 'store', refusing):
            out, _ = self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'recovery_required')
        self.assertIn('card went away', receipt['rollback_error'])
        self.assertEqual(sha((self.card / self.archive).read_bytes()), sha(self.OLD), 'the on-card archive is intact')
        self.assertEqual(sha(Path(receipt['before'][OLD_NRO]['local']).read_bytes()), sha(self.OLD), 'the local copy is intact')
        self.assertNotIn('unmount or eject', out)

    def test_rollback_does_not_overwrite_a_foreign_file_at_the_old_path(self):
        self.preflighted()

        def interrupt(card, source, destination):
            (self.card / OLD_NRO).write_bytes(b'somebody else wrote this')
            raise KeyboardInterrupt('signal 15')

        with mock.patch.object(self.module.Card, 'rename', interrupt):
            self.phase('install', expect=1)
        receipt = self.receipt('deployment.json')
        self.assertEqual(receipt['state'], 'recovery_required')
        self.assertIn('not overwriting', receipt['rollback_error'])
        self.assertEqual((self.card / OLD_NRO).read_bytes(), b'somebody else wrote this')

    def test_rollback_keeps_a_data_folder_that_already_existed(self):
        (self.card / ROOMS).mkdir()
        (self.card / ROOMS / 'settings.json').write_bytes(b'{}')
        self.preflighted()
        with self.after('rename', lambda card, source, destination: self.flip(destination)):
            self.phase('install', expect=1)
        self.assertEqual(self.receipt('deployment.json')['state'], 'rolled_back_verified')
        self.assertEqual((self.card / ROOMS / 'settings.json').read_bytes(), b'{}')
        self.assertFalse((self.card / DEVELOPER).exists())


class TestVerify(Base):
    def installed(self):
        self.phase('preflight')
        self.phase('install')

    def test_verify_notices_a_plugin_changed_after_the_install(self):
        self.installed()
        self.flip(PLUGINS + '/libarcropolis.nro')
        self.phase('verify', expect=1)
        self.assertEqual(self.receipt('verify.json')['state'], 'failed')

    def test_verify_notices_the_new_nro_or_a_data_file_changed(self):
        self.installed()
        self.flip(NEW_NRO)
        self.phase('verify', expect=1)
        self.assertIn(NEW_NRO, self.receipt('verify.json')['error'])

    def test_verify_notices_a_watched_file_changed(self):
        self.installed()
        self.flip(FORK + '/replay_captures/7265.smd')
        self.phase('verify', expect=1)
        self.assertIn('watched tree', self.receipt('verify.json')['error'])

    def test_verify_notices_the_old_nro_back(self):
        self.installed()
        (self.card / OLD_NRO).write_bytes(self.OLD)
        self.phase('verify', expect=1)
        self.assertEqual(self.receipt('verify.json')['state'], 'failed')

    def test_verify_notices_a_remount(self):
        self.installed()
        ids = iter([41, 42])
        with mock.patch.object(self.module.Card, 'mount_id', lambda card: next(ids)):
            self.phase('verify', expect=1)
        self.assertIn('remounted', self.receipt('verify.json')['error'])

    def test_verify_needs_a_verified_install(self):
        self.phase('preflight')
        self.phase('verify', expect=1)
        self.assertEqual(self.receipt('verify.json')['state'], 'failed')


class TestRefusalsBeforeCardAccess(Base):
    """Bad approvals and bad new files are refused before the first card operation."""

    def refused(self, fragment, mode='preflight'):
        self.ops.clear()
        tree = self.tree()
        code, _, err = self.main(mode)
        self.assertEqual(code, 1, err)
        self.assertIn('refused before any card access', err)
        self.assertIn(fragment, err)
        self.assertEqual(self.ops, [], 'no card operation')
        self.assertFalse((self.dir / RECEIPTS[mode]).exists())
        self.assertEqual(self.tree(), tree)

    def set(self, path, value):
        node = self.approval
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        self.write_approval()

    def test_names(self):
        for old, new in (('libtraining_modpack.nro', 'libtraining_modpack.nro'), ('../x.nro', 'libtraining_rooms.nro'),
                         ('libtraining_modpack.nro', 'Rooms.nro'), ('libtraining_modpack.nro', 'libtraining_rooms.nso'),
                         ('libtraining_modpack.nro', 'lib/rooms.nro')):
            with self.subTest(old=old, new=new):
                self.approval['remove']['name'], self.approval['add']['name'] = old, new
                self.write_approval()
                self.refused('name' if old != new else 'same name')

    def test_hashes(self):
        for old, new in (('ABC', sha(self.NEW)), (sha(self.OLD), 'def'), (sha(self.OLD), sha(self.OLD)), (None, sha(self.NEW))):
            with self.subTest(old=old, new=new):
                self.approval['remove']['sha256'], self.approval['add']['sha256'] = old, new
                self.write_approval()
                self.refused('sha256')

    def test_data_folders(self):
        for data in ('ultimate/../x', TITLE + '/exefs', 'ultimate', 'ultimate/a/b', '/ultimate/x', 'ultimate/x y', None, ['ultimate/x']):
            with self.subTest(data=data):
                self.set(['data'], data)
                self.refused('data')

    def test_added_file_paths(self):
        for path in ('ultimate/x/../y', 'ultimate/TrainingRooms/sub/x.ini', 'atmosphere/x/y', 'ultimate/TrainingRooms/.hidden',
                     'ultimate/TrainingRooms/..', '/ultimate/x/y', 'ultimate/TrainingRooms', None):
            with self.subTest(path=path):
                self.set(['add', 'files'], [{'path': path, 'sha256': sha(self.DEV)}])
                self.refused('added file')

    def test_the_import_marker_is_never_placed(self):
        for path in (ROOMS + '/migration.json', ROOMS + '/Migration.JSON', 'ultimate/Other/migration.json'):
            with self.subTest(path=path):
                (self.dir / 'data-files' / Path(path).parent).mkdir(parents=True, exist_ok=True)
                (self.dir / 'data-files' / path).write_bytes(self.DEV)
                self.set(['add', 'files'], [{'path': path, 'sha256': sha(self.DEV)}])
                self.refused('must never be placed')

    def test_added_file_inside_the_archive_folder_or_a_watched_tree(self):
        (self.dir / 'data-files' / FORK).mkdir(parents=True, exist_ok=True)
        (self.dir / 'data-files' / FORK / 'x.ini').write_bytes(self.DEV)
        self.set(['add', 'files'], [{'path': FORK + '/x.ini', 'sha256': sha(self.DEV)}])
        self.refused('inside the archive folder')
        self.set(['data'], 'ultimate/Other')
        self.refused('inside a watched tree')

    def test_added_file_list_shapes(self):
        self.set(['add', 'files'], [{'path': DEVELOPER, 'sha256': sha(self.DEV)}] * 2)
        self.refused('twice')
        self.set(['add', 'files'], 'developer.ini')
        self.refused('add.files')
        self.set(['add', 'files'], [{'path': DEVELOPER, 'sha256': 'XYZ'}])
        self.refused('sha256')

    def test_data_file_must_be_there_match_and_be_small(self):
        (self.dir / 'data-files' / DEVELOPER).write_bytes(b'other bytes')
        self.refused('not the approved file')
        (self.dir / 'data-files' / DEVELOPER).unlink()
        self.refused('missing')
        (self.dir / 'data-files' / DEVELOPER).write_bytes(self.DEV)
        with mock.patch.object(self.module, 'MAX_DATA_FILE', 4):
            self.refused('larger than')

    def test_extra_protected_and_watch(self):
        for extra in (['ultimate/x/../y'], ['bootloader/hekate_ipl.ini'], ['ultimate/x/.hidden'], PROTECTED[:1],
                      ['ultimate/a/b', 'ultimate/a/b'], 'ultimate/x/y', ['atmosphere/x/y'], [FORK + '/save_states.json']):
            with self.subTest(extra=extra):
                self.set(['extra_protected'], extra)
                self.refused('extra_protected' if extra != [FORK + '/save_states.json'] else 'watched tree')
        self.set(['extra_protected'], [])
        for watch in (['ultimate/x/y'], ['ultimate'], FORK, [FORK, FORK], ['atmosphere']):
            with self.subTest(watch=watch):
                self.set(['watch'], watch)
                self.refused('watch')

    def test_min_free_bytes(self):
        for value in (-1, True, '64', 1.5):
            with self.subTest(value=value):
                self.set(['min_free_bytes'], value)
                self.refused('min_free_bytes')

    def test_new_nro_must_match_the_approval_and_be_an_nro(self):
        (self.dir / 'libtraining_rooms.nro').write_bytes(nro('other', 7000))
        self.refused('not the approved new build')
        bad = blob('not-an-nro', 7000)
        (self.dir / 'libtraining_rooms.nro').write_bytes(bad)
        self.set(['add', 'sha256'], sha(bad))
        self.refused('not an NRO')
        (self.dir / 'libtraining_rooms.nro').unlink()
        self.refused('missing')

    def test_missing_order_mount_and_removal(self):
        self.approval['operator_order'] = ' '
        self.write_approval()
        self.refused('operator_order')
        self.approval['operator_order'] = 'x'
        for mount in (None, 'card', ''):
            with self.subTest(mount=mount):
                self.set(['mount'], mount)
                self.refused('mount')
        self.set(['mount'], str(self.card))
        del self.approval['remove']
        self.write_approval()
        self.refused('remove')

    def test_entry_point_refuses_without_touching_a_card(self):
        """The real `python3 -I` entry point: a plain directory is not a mount point, so nothing is read or written."""
        run = lambda *args: subprocess.run([sys.executable, '-I', str(SCRIPT), *args], capture_output=True, text=True)
        self.assertEqual(run().returncode, 2)
        self.assertEqual(run('swap', str(self.dir)).returncode, 2)
        self.assertEqual(run('preflight', str(self.tmp / 'missing')).returncode, 2)
        before = self.tree()
        done = run('preflight', str(self.dir))
        self.assertEqual(done.returncode, 1)
        self.assertIn('is not a mount point', done.stderr)
        self.assertEqual(self.receipt('preflight.json')['state'], 'failed')
        self.assertEqual(self.tree(), before)
        self.approval['mount'] = 'relative'
        self.write_approval()
        done = run('install', str(self.dir))
        self.assertEqual(done.returncode, 1)
        self.assertIn('refused before any card access', done.stderr)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ['.transaction.lock', 'data-files', 'libtraining_rooms.nro', 'operator-approval.json', 'preflight.json'])


class TestToolRules(unittest.TestCase):
    """The playbook rule that a script never mounts, unmounts or ejects the card."""

    def test_no_card_commands_in_tools_switch(self):
        spec = importlib.util.spec_from_file_location('compliance', HERE.parents[1] / 'check' / 'compliance.py')
        compliance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compliance)
        tools = HERE.parent
        self.assertEqual(compliance.card_commands(sorted(tools.rglob('*')), tools.parent), [])

    def test_the_script_runs_no_command_but_findmnt(self):
        text = SCRIPT.read_text()
        self.assertEqual(sorted(set(re.findall(r"'/usr/bin/[a-z]+'", text))), ["'/usr/bin/findmnt'"])


if __name__ == '__main__':
    unittest.main(verbosity=2)
