#!/usr/bin/env python3
"""Offline tests for routes.py and routes_online.py: a fake Eden and fake plugin logs, no emulator.

Run: python3 -I test_routes.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import routes  # noqa: E402
import routes_online  # noqa: E402
import runner  # noqa: E402
import test_native_picker as picker_fixtures  # noqa: E402


class BoundedControlTests(unittest.TestCase):
    def blocked_call(self):
        release, entered, finished = threading.Event(), threading.Event(), threading.Event()
        threads = []

        def call(method):
            threads.append(threading.current_thread())
            entered.set()
            try:
                release.wait(5)
                return {'state': 'finished'}
            finally:
                finished.set()

        def cleanup():
            release.set()
            if entered.is_set():
                self.assertTrue(finished.wait(1), 'control-call thread did not exit')
                for thread in threads:
                    thread.join(1)
                    self.assertFalse(thread.is_alive(), 'control-call thread remains alive')

        self.addCleanup(cleanup)
        return types.SimpleNamespace(call=call)

    def test_bounded_call_returns_within_its_cap_when_the_control_call_hangs(self):
        ctx = self.blocked_call()
        started = time.monotonic()
        answered, value = routes._bounded_call(ctx, 'macro.status', 0.3)
        self.assertFalse(answered)
        self.assertIsNone(value)
        self.assertLess(time.monotonic() - started, 0.6)

    def test_bounded_call_returns_answers_and_exceptions(self):
        self.assertEqual(routes._bounded_call(types.SimpleNamespace(call=lambda method: {'state': 'idle'}),
                                              'macro.status', .3), (True, {'state': 'idle'}))
        error = RuntimeError('control offline')

        def fail(method):
            raise error

        answered, value = routes._bounded_call(types.SimpleNamespace(call=fail), 'macro.status', .3)
        self.assertFalse(answered)
        self.assertIs(value, error)

    def test_eden_macro_busy_reports_an_unanswered_status_as_busy(self):
        ctx = self.blocked_call()
        started = time.monotonic()
        reason = routes._eden_macro_busy(ctx, 0.25)
        self.assertIn('macro.status not answered within', reason)
        self.assertLess(time.monotonic() - started, 0.6)


class MacroDispatchTests(unittest.TestCase):
    def test_play_marks_a_refused_macro_play_as_rejected(self):
        for message in ('input_busy', 'macro.play busy'):
            with self.subTest(message=message):
                ctx = types.SimpleNamespace(call=mock.Mock(side_effect=RuntimeError(message)),
                                            runner=types.SimpleNamespace(wait_macro=mock.Mock()), on='host',
                                            log=mock.Mock())
                with mock.patch.object(routes, 'macro_file', return_value=(Path('/macro'), 'sha', 3)):
                    with self.assertRaises(routes.closed_loop.DispatchRejected) as caught:
                        routes.play(ctx, 'lobby-a')
                self.assertEqual(str(caught.exception), message)
                ctx.runner.wait_macro.assert_not_called()

    def test_play_marks_a_failure_after_acceptance_as_delivery_unknown(self):
        ctx = types.SimpleNamespace(call=mock.Mock(return_value={}),
                                    runner=types.SimpleNamespace(
                                        wait_macro=mock.Mock(side_effect=RuntimeError('input.reset input_busy'))),
                                    on='host', log=mock.Mock())
        with mock.patch.object(routes, 'macro_file', return_value=(Path('/macro'), 'sha', 3)):
            with self.assertRaises(routes.closed_loop.DeliveryUnknown) as caught:
                routes.play(ctx, 'lobby-a')
        self.assertIn('macro lobby-a was accepted; input.reset input_busy', str(caught.exception))
        ctx.call.assert_called_once_with('macro.play', {'path': '/macro'})
        ctx.log.assert_not_called()

    def test_an_unrelated_macro_play_error_is_not_a_dispatch_rejection(self):
        error = RuntimeError('macro.play timed out')
        ctx = types.SimpleNamespace(call=mock.Mock(side_effect=error),
                                    runner=types.SimpleNamespace(wait_macro=mock.Mock()), on='host', log=mock.Mock())
        with mock.patch.object(routes, 'macro_file', return_value=(Path('/macro'), 'sha', 3)):
            with self.assertRaises(RuntimeError) as caught:
                routes.play(ctx, 'lobby-a')
        self.assertIs(caught.exception, error)
        ctx.runner.wait_macro.assert_not_called()


class LogCursorTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / 'practice-diagnostics.log'
        self.path.write_text('old 1\nold 2\n')

    def test_only_new_lines(self):
        cursor = routes.LogCursor(self.path)
        with self.path.open('a') as f:
            f.write('new 1\nnew')
        self.assertEqual(cursor.read(), ['new 1'])
        with self.path.open('a') as f:
            f.write(' 2\n')
        self.assertEqual(cursor.read(), ['new 2'])

    @unittest.skipIf(os.name == 'nt', 'Windows uses the unheld cursor; a held handle blocks the rename')
    def test_rotation_drains_old_file_then_reads_new(self):
        cursor = routes.LogCursor(self.path, rotates=True)
        with self.path.open('a') as f:
            f.write('before rename\n')
        os.replace(self.path, self.dir / 'practice-diagnostics.previous.log')
        with (self.dir / 'practice-diagnostics.previous.log').open('a') as f:
            f.write('late write to old\n')
        self.path.write_text('first of new\n')
        self.assertEqual(cursor.read(), ['before rename', 'late write to old', 'first of new'])
        self.assertEqual(cursor.rotations, 1)
        with self.path.open('a') as f:
            f.write('second of new\n')
        self.assertEqual(cursor.read(), ['second of new'])

    def test_unheld_mode_for_windows(self):
        cursor = routes.LogCursor(self.path, hold=False, rotates=True)
        self.assertIsNone(cursor.handle)
        with self.path.open('a') as f:
            f.write('a\n')
        self.assertEqual(cursor.read(), ['a'])
        os.replace(self.path, self.dir / 'practice-diagnostics.previous.log')  # works: no handle is held
        self.path.write_text('new\n')
        self.assertEqual(cursor.read(), ['new'])
        self.assertEqual(cursor.rotations, 1)
        self.path.write_text('t\n')  # truncated below the cursor's offset
        self.assertEqual(cursor.read(), ['t'])

    def test_truncation_reads_from_start(self):
        cursor = routes.LogCursor(self.path, rotates=True)
        self.path.write_text('x\n')
        self.assertEqual(cursor.read(), ['x'])

    def test_missing_file_then_created(self):
        missing = self.dir / 'later.log'
        cursor = routes.LogCursor(missing)
        self.assertEqual(cursor.read(), [])
        missing.write_text('hello\n')
        self.assertEqual(cursor.read(), ['hello'])

    def test_wait_reports_lines_seen(self):
        cursor = routes.LogCursor(self.path)
        with self.path.open('a') as f:
            f.write('nothing here\n')
        with self.assertRaises(RuntimeError) as raised:
            cursor.wait('never', 0.2)
        self.assertIn('nothing here', str(raised.exception))

    # A held handle blocks the rename on Windows, so only the unheld cursor is replaced there.
    MODES = (True, False) if os.name != 'nt' else (False,)

    def _replace_with(self, text):
        """Put another file at the path (a new inode), as a rotation or a rewrite through a temporary file does."""
        fresh = self.dir / 'replacement.tmp'
        fresh.write_text(text)
        os.replace(fresh, self.path)

    def test_an_append_between_the_path_check_and_the_read_is_not_a_truncation(self):
        # The A1 trace boot of 2026-11-24: the capture appended while the cursor read, the cursor took the larger
        # read position for a shrunk file and returned the log from its first line again, eleven times.
        for hold in self.MODES:
            with self.subTest(hold=hold):
                self.path.write_text('old 1\nold 2\n')
                cursor = routes.LogCursor(self.path, hold=hold)
                real_stat, appended = os.stat, []

                def stat_then_append(path, *args, **kwargs):
                    result = real_stat(path, *args, **kwargs)
                    if not appended and isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(self.path):
                        appended.append(True)
                        with open(self.path, 'a') as f:
                            f.write('written during the read\n')
                    return result

                with mock.patch.object(routes.os, 'stat', stat_then_append):
                    lines = cursor.read()
                if not appended:  # this mode reads without asking the path first
                    with open(self.path, 'a') as f:
                        f.write('written during the read\n')
                lines += cursor.read()
                self.assertEqual(lines, ['written during the read'])
                self.assertEqual(cursor.rotations, 0)
                cursor.close()

    def test_a_writer_appending_during_fast_polls_gives_every_line_once_in_order(self):
        import threading
        wanted = [f'[Bridge] ui screen=stage_select ready=yes ev={n} frame={n}' for n in range(4000)]
        for hold in self.MODES:
            with self.subTest(hold=hold):
                self.path.write_text('old 1\nold 2\n')
                cursor = routes.LogCursor(self.path, hold=hold)

                def write():
                    with open(self.path, 'ab', buffering=0) as f:
                        for line in wanted:
                            f.write(line.encode() + b'\n')

                writer = threading.Thread(target=write)
                writer.start()
                got = []
                while writer.is_alive():
                    got += cursor.read()
                writer.join()
                got += cursor.read()
                self.assertEqual(len(got), len(wanted))
                self.assertEqual(got, wanted)
                self.assertEqual(cursor.rotations, 0)
                cursor.close()

    def test_wait_ignores_an_old_matching_line_in_a_replaced_log(self):
        old = 'screen=stage_select ready=yes frame=1013\nscreen=css.smash ready=yes frame=1751\n'
        for hold in self.MODES:
            with self.subTest(hold=hold):
                self.path.write_text(old)
                cursor = routes.LogCursor(self.path, hold=hold)  # the wait begins here: both lines are old
                self._replace_with(old)
                with self.assertRaises(RuntimeError):
                    cursor.wait('screen=stage_select ready=yes', 0.3)
                self.assertEqual(cursor.rotations, 1)
                with self.path.open('a') as f:
                    f.write('screen=stage_select ready=yes frame=2900\n')
                found = cursor.wait('screen=stage_select ready=yes', 1)
                self.assertEqual(found.string, 'screen=stage_select ready=yes frame=2900')
                cursor.close()

    def test_a_replaced_log_that_kept_the_read_lines_gives_only_the_new_ones(self):
        for hold in self.MODES:
            with self.subTest(hold=hold):
                self.path.write_text('old 1\nold 2\n')
                cursor = routes.LogCursor(self.path, hold=hold)
                with self.path.open('a') as f:
                    f.write('read 1\nread 2\n')
                self.assertEqual(cursor.read(), ['read 1', 'read 2'])
                self._replace_with('old 1\nold 2\nread 1\nread 2\nnew 1\n')
                self.assertEqual(cursor.read(), ['new 1'])
                self.assertEqual(cursor.rotations, 1)
                cursor.mark()
                self._replace_with('old 1\nold 2\nread 1\nread 2\nnew 1\n')
                self.assertEqual(cursor.read(), [])
                self.assertEqual(cursor.rotations, 2)
                cursor.close()

    def test_a_long_replaced_log_continues_after_the_lines_already_read(self):
        long = ''.join(f'[Bridge] ui screen=stage_select ready=yes ev={n} frame={1000 + n}\n' for n in range(400))
        self.assertGreater(len(long), 3 * 4096)
        for hold in self.MODES:
            with self.subTest(hold=hold):
                self.path.write_text(long)
                cursor = routes.LogCursor(self.path, hold=hold)
                self._replace_with(long + 'after the replacement\n')
                self.assertEqual(cursor.read(), ['after the replacement'])
                self.assertEqual(cursor.rotations, 1)
                cursor.close()

    STAGE_READY = ('[Bridge] ui screen=stage_select ctx=local_wireless ready=yes gen=4 owner=native dialog=none '
                   'picker=4.2.0 raw=0:4,0,0:4:2:0 ev=692 frame=1013')
    CSS_READY = ('[Bridge] ui screen=css.smash ctx=local_wireless ready=yes gen=5 owner=native dialog=none '
                 'picker=2.2.0 raw=0:2,0,0:2:2:0 ev=2600 frame=1967')

    def _boot_log(self):
        """A log like the A1 trace boot's up to its first CSS line: Stage Select ready early, then many frames."""
        frames = [f'[Bridge] ui screen=stage_select ctx=local_wireless ready=yes gen=4 owner=native dialog=none '
                  f'picker=4.{n}.0 raw=0:4,0,0:4:{n}:0 ev={692 + 2 * n} frame={1013 + n}' for n in range(1, 120)]
        text = '\n'.join([self.STAGE_READY] + frames + [self.CSS_READY]) + '\n'
        self.assertGreater(len(text), 2 * 4096)
        return text

    def test_wait_rejects_an_old_ready_line_in_a_replaced_log_it_cannot_place(self):
        # rv-fusion-g4, 2026-11-24 02:58Z: the replacement holds an old Stage Select ready line and a later CSS line,
        # but not the last text the cursor read. Its age is unknown, so it must not satisfy a wait or a read.
        wanted = r'screen=stage_select .*ready=yes'
        for hold in self.MODES:
            for in_place in (False, True):
                with self.subTest(hold=hold, in_place=in_place):
                    self.path.write_text(self._boot_log())
                    cursor = routes.LogCursor(self.path, hold=hold)
                    cursor.mark()
                    stale = self.STAGE_READY + '\n' + self.CSS_READY + '\n'
                    if in_place:
                        self.path.write_text(stale)  # the same file, cut short and written again
                    else:
                        self._replace_with(stale)
                    self.assertEqual(cursor.read(), [])  # what the trace tap does
                    with self.assertRaises(RuntimeError) as raised:
                        cursor.wait(wanted, 0.3)
                    self.assertIn('2 lines of a replaced file were not accepted', str(raised.exception))
                    self.assertEqual((cursor.rotations, cursor.unqualified, cursor.backlog), (1, 2, []))
                    # The positive control: a ready line written after the replacement is new.
                    later = self.STAGE_READY.replace('ev=692 frame=1013', 'ev=9000 frame=5000')
                    with self.path.open('a') as f:
                        f.write(later + '\n')
                    self.assertEqual(cursor.wait(wanted, 1).string, later)
                    self.assertEqual((cursor.rotations, cursor.unqualified), (1, 2))
                    cursor.close()

    def test_a_line_cut_off_in_text_of_unknown_age_is_not_returned_when_it_is_finished(self):
        for hold in self.MODES:
            with self.subTest(hold=hold):
                self.path.write_text(self._boot_log())
                cursor = routes.LogCursor(self.path, hold=hold)
                self._replace_with(self.CSS_READY + '\n[Bridge] ui screen=stage_select ctx=local_wireless ready=y')
                self.assertEqual(cursor.read(), [])
                with self.path.open('a') as f:
                    f.write('es gen=4\nwritten after\n')
                self.assertEqual(cursor.read(), ['written after'])
                self.assertEqual(cursor.unqualified, 2)
                cursor.close()

    def test_a_cursor_started_at_the_end_does_not_return_a_line_already_begun(self):
        for hold in self.MODES:
            with self.subTest(hold=hold):
                self.path.write_text('old 1\nscreen=stage_select ready=y')
                cursor = routes.LogCursor(self.path, hold=hold)
                with self.path.open('a') as f:
                    f.write('es frame=1013\nnew 1\n')
                self.assertEqual(cursor.read(), ['new 1'])
                cursor.mark()
                with self.path.open('a') as f:
                    f.write('new 2\n')
                self.assertEqual(cursor.read(), ['new 2'])
                cursor.close()

    def test_a_log_emptied_and_then_written_gives_the_new_lines(self):
        for hold in self.MODES:
            with self.subTest(hold=hold):
                self.path.write_text(self._boot_log())
                cursor = routes.LogCursor(self.path, hold=hold)
                with self.path.open('r+') as f:
                    f.truncate(0)
                self.assertEqual(cursor.read(), [])
                with self.path.open('a') as f:
                    f.write('first after the cut\n')
                self.assertEqual(cursor.read(), ['first after the cut'])
                self.assertEqual((cursor.rotations, cursor.unqualified), (1, 0))
                cursor.close()

    def test_a_rewrite_in_place_with_other_lines(self):
        for hold in self.MODES:
            for rotates in (False, True):
                with self.subTest(hold=hold, rotates=rotates):
                    self.path.write_text('old 1\nold 2\n')
                    cursor = routes.LogCursor(self.path, hold=hold, rotates=rotates)
                    inode = os.stat(self.path).st_ino
                    with self.path.open('r+') as f:  # the same file, emptied and written again: not smaller than before
                        f.truncate(0)
                        f.write('fresh 1\nfresh 2 is longer than the old file\n')
                    self.assertEqual(os.stat(self.path).st_ino, inode)
                    fresh = ['fresh 1', 'fresh 2 is longer than the old file']
                    self.assertEqual(cursor.read(), fresh if rotates else [])
                    self.assertEqual((cursor.rotations, cursor.unqualified), (1, 0 if rotates else 2))
                    with self.path.open('a') as f:
                        f.write('fresh 3\n')
                    self.assertEqual(cursor.read(), ['fresh 3'])
                    self.assertEqual(cursor.rotations, 1)
                    cursor.close()

    def test_a_partial_line_of_a_replaced_log_is_not_joined_to_the_new_file(self):
        for hold in self.MODES:
            for rotates in (False, True):
                with self.subTest(hold=hold, rotates=rotates):
                    self.path.write_text('old 1\n')
                    cursor = routes.LogCursor(self.path, hold=hold, rotates=rotates)
                    with self.path.open('a') as f:
                        f.write('cut off')
                    self.assertEqual(cursor.read(), [])
                    self._replace_with('other file\n')
                    self.assertEqual(cursor.read(), ['cut off', 'other file'] if rotates else [])
                    self.assertEqual(cursor.unqualified, 0 if rotates else 2)
                    with self.path.open('a') as f:
                        f.write('appended\n')
                    self.assertEqual(cursor.read(), ['appended'])
                    cursor.close()

    def test_lines_drained_from_a_file_that_was_replaced_are_not_returned(self):
        # rv-fusion-g4, 2026-11-24 03:10Z: a ready line appended to the old file between polls, then another file
        # at the path that the cursor cannot place. The old file's line belongs to a generation that ended.
        wanted = r'screen=stage_select .*ready=yes'
        for hold in self.MODES:
            with self.subTest(hold=hold):
                self.path.write_text(self._boot_log())
                cursor = routes.LogCursor(self.path, hold=hold)
                with self.path.open('a') as f:
                    f.write(self.STAGE_READY.replace('ev=692', 'ev=3000') + '\n')
                self._replace_with(self.CSS_READY + '\n')
                self.assertEqual(cursor.read(), [])
                self.assertEqual(cursor.rotations, 1)
                self.assertEqual(cursor.unqualified, 2 if hold else 1)  # the unheld cursor never saw the old line
                with self.assertRaises(RuntimeError):
                    cursor.wait(wanted, 0.3)
                later = self.STAGE_READY.replace('ev=692 frame=1013', 'ev=9000 frame=5000')
                with self.path.open('a') as f:
                    f.write(later + '\n')
                self.assertEqual(cursor.wait(wanted, 1).string, later)
                cursor.close()

    def test_a_partial_line_read_before_a_replacement_is_not_joined_to_what_follows(self):
        for hold in self.MODES:
            with self.subTest(hold=hold):
                self.path.write_text(self._boot_log())
                cursor = routes.LogCursor(self.path, hold=hold)
                with self.path.open('a') as f:
                    f.write('[Bridge] ui screen=stage_select ctx=local_wireless ready=y')
                self.assertEqual(cursor.read(), [])
                self._replace_with(self.CSS_READY + '\n')
                self.assertEqual(cursor.read(), [])
                with self.path.open('a') as f:
                    f.write('es gen=4 appended\n')
                self.assertEqual(cursor.read(), ['es gen=4 appended'])
                cursor.close()

    def _ctx(self, **files):
        return FakeCtx(FakeRunner({key: str(value) for key, value in files.items()}, None), self.dir)

    def test_only_a_plugins_own_log_is_read_as_a_rotating_log(self):
        other = self.dir / 'tile-cursor.log'
        other.write_text('tile\n')
        eden = self._ctx(diag_log=self.path, tile_log=other, bridge_log=self.dir / 'bridge.log')
        self.assertTrue(routes._log_cursor(eden, 'diag_log').rotates)
        self.assertTrue(routes._log_cursor(eden, 'diag').rotates)
        self.assertTrue(routes._log_cursor(eden, 'tile_log').rotates)
        self.assertFalse(routes._log_cursor(eden, 'bridge_log').rotates)
        # A Switch instance names the bridge capture's log as its diag log (switch_hw: diag_log = bridge_log).
        switch = self._ctx(diag_log=self.path, bridge_log=self.path, console_log=other, tile_log=other)
        self.assertFalse(routes._log_cursor(switch, 'diag_log').rotates)
        self.assertFalse(routes._log_cursor(switch, 'bridge_log').rotates)
        self.assertFalse(routes._log_cursor(switch, 'tile_log').rotates)  # the same file as its console log
        with self.assertRaises(KeyError):
            routes._log_cursor(self._ctx(bridge_log=self.path), 'diag_log')

    def test_a_chord_on_a_switch_does_not_accept_an_old_line_of_a_replaced_bridge_log(self):
        saved = '[TrainingRooms] state saved slot=1 entry=1 (file queued) frame=900'
        ctx = self._ctx(diag_log=self.path, bridge_log=self.path)
        for name, options, old in (('tr-save', {}, saved),
                                   ('tr-save', {'source': 'bridge_log', 'expect': r'screen=stage_select .*ready=yes'},
                                    self.STAGE_READY)):
            with self.subTest(options=options):
                self.path.write_text(self._boot_log() + old + '\n')

                def replaces(_ctx, _name):  # the chord is played; the log is replaced and gains nothing
                    self._replace_with(old + '\n' + self.CSS_READY + '\n')
                    return 'sha'

                with mock.patch.object(routes, 'play', replaces), self.assertRaises(RuntimeError):
                    routes.chord(ctx, name, cap=0.3, **options)
                new = old.replace('frame=', 'frame=77')

                def answers(_ctx, _name):
                    with self.path.open('a') as f:
                        f.write(new + '\n')
                    return 'sha'

                with mock.patch.object(routes, 'play', answers):
                    self.assertIn(routes.chord(ctx, name, cap=1, **options)['line'], new)

    def test_a_short_log_cut_back_to_lines_already_read_gives_nothing(self):
        for hold in self.MODES:
            for replace in (True, False):
                with self.subTest(hold=hold, replace=replace):
                    self.path.write_text('old 1\nold 2\n')
                    cursor = routes.LogCursor(self.path, hold=hold)
                    if replace:
                        self._replace_with('old 1\n')
                    else:
                        with self.path.open('r+') as f:
                            f.truncate(len('old 1\n'))
                    self.assertEqual(cursor.read(), [])
                    self.assertEqual((cursor.rotations, cursor.unqualified), (1, 0))
                    with self.path.open('a') as f:
                        f.write('new 1\n')
                    self.assertEqual(cursor.read(), ['new 1'])
                    cursor.close()

    def test_a_rotating_log_cursor_reads_a_new_file_that_repeats_old_lines(self):
        # The limit of rotates=True: the cursor keeps the last 4096 bytes it read, not the whole file.
        lines = self._boot_log().splitlines()
        for hold in self.MODES:
            with self.subTest(hold=hold):
                self.path.write_text('\n'.join(lines) + '\n')
                cursor = routes.LogCursor(self.path, hold=hold, rotates=True)
                self._replace_with('\n'.join(lines[:3]) + '\n')
                self.assertEqual(cursor.read(), lines[:3])
                self.assertEqual((cursor.rotations, cursor.unqualified), (1, 0))
                cursor.close()


class FakeTileGame:
    """A tile grid with the tile cursor's log lines. widths: tiles per row."""

    def __init__(self, log, widths, screen='sss', start=0, holding=True):
        self.log, self.widths, self.screen, self.at, self.holding = log, widths, screen, start, holding
        self.rows = []
        n = 0
        for w in widths:
            self.rows.append(list(range(n, n + w)))
            n += w

    def where(self, tile):
        for r, row in enumerate(self.rows):
            if tile in row:
                return r, row.index(tile)
        raise KeyError(tile)

    def step(self, direction):
        if not self.holding:
            return
        r, c = self.where(self.at)
        target = None
        if direction == 'left' and c > 0:
            target = self.rows[r][c - 1]
        elif direction == 'right' and c + 1 < len(self.rows[r]):
            target = self.rows[r][c + 1]
        elif direction == 'up' and r > 0:
            target = self.rows[r - 1][min(c, len(self.rows[r - 1]) - 1)]
        elif direction == 'down' and r + 1 < len(self.rows):
            target = self.rows[r + 1][min(c, len(self.rows[r + 1]) - 1)]
        with self.log.open('a') as f:
            f.write(f'1 step screen={self.screen} dir={direction} from={self.at} to={target if target is not None else "none"} x=0.0 y=0.0\n')
            if target is not None:
                self.at = target
                hovered = target if self.screen == 'css' else 'none'
                f.write(f'2 settled screen={self.screen} at={target} hovered={hovered} x=0.0 y=0.0\n')


class FakeRunner:
    def __init__(self, instance, game):
        self.instances = {'s': instance}
        self.plan = {'gates': {}}
        self.game = game
        self.played = []

    def call(self, on, method, params=None, job_cap=15):
        if method == 'macro.play':
            name = Path(params['path']).name.split('.')[0]
            self.played.append(name)
            if name.startswith('tile-'):
                self.game.step(name.split('-', 1)[1])
        return {}

    def wait_macro(self, on, cap):
        return None

    def log(self, record):
        pass

    def check_stop(self):
        pass


class FakeCtx:
    def __init__(self, runner_obj, row_dir):
        self.runner, self.on = runner_obj, 's'
        self.instance = runner_obj.instances['s']
        self.row_dir = row_dir

    def call(self, method, params=None, job_cap=15):
        return self.runner.call(self.on, method, params, job_cap)

    def log(self, record):
        pass

    def check_stop(self):
        pass

    def shot(self, name):
        return f'{name}.png'


class HostNetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.out = self.run / 'out'
        self.row = self.out / 'r00'
        self.row.mkdir(parents=True)
        self.config = self.run / 'config.json'
        self.config.write_text(json.dumps({'network_interface': 'wifi0'}))
        self.values = {}
        self.calls = []
        self.running = False
        self.wrong_readback = False
        self.ldn_error = None
        self.ctx = types.SimpleNamespace(runner=types.SimpleNamespace(out=self.out),
                                         row_dir=self.row, call=self.call)

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == 'status':
            return {'running': self.running}
        if method == 'settings.set':
            self.values[params['key']] = params['value']
        if method == 'settings.get':
            if params['key'] == 'core.ldn_mitm' and self.ldn_error:
                raise RuntimeError(self.ldn_error)
            if params['key'] == 'core.ldn_mitm' and params['key'] not in self.values:
                return {'value': True}
            value = self.values[params['key']]
            return {'value': 'wrong' if self.wrong_readback else value}
        return {}

    def test_reads_base_and_sets_and_reads_network_settings_without_saving(self):
        result = routes.host_network(self.ctx)
        self.assertEqual(result, {
            'block': 'host_network', 'interface': 'wifi0', 'lan_online': False, 'ldn_mitm': False,
            'readback': {'core.network_interface': 'wifi0', 'core.lan_online': False, 'core.ldn_mitm': False}})
        self.assertEqual(json.loads((self.row / 'routes-host-network.json').read_text()), result)
        self.assertEqual([name for name, _ in self.calls],
                         ['status', 'settings.set', 'settings.get', 'settings.set', 'settings.get',
                          'settings.get', 'settings.set', 'settings.get'])

    def test_argument_overrides_config_and_lan_online(self):
        result = routes.host_network(self.ctx, interface='ethernet0', lan_online=True)
        self.assertEqual(result['interface'], 'ethernet0')
        self.assertEqual(result['readback'], {
            'core.network_interface': 'ethernet0', 'core.lan_online': True, 'core.ldn_mitm': False})

    def test_absent_ldn_mitm_is_recorded_but_other_errors_propagate(self):
        self.ldn_error = "settings.get on a: {'ok': False, 'error': {'code': 'unknown_setting'}}"
        result = routes.host_network(self.ctx)
        self.assertEqual(result['readback']['core.ldn_mitm'], 'absent')
        self.assertNotIn('core.ldn_mitm', self.values)
        self.ldn_error = 'settings.get on a: connection lost'
        with self.assertRaisesRegex(RuntimeError, 'connection lost'):
            routes.host_network(self.ctx)

    def test_explicit_ldn_mitm_is_set_and_read_back(self):
        result = routes.host_network(self.ctx, ldn_mitm=True)
        self.assertTrue(result['ldn_mitm'])
        self.assertTrue(result['readback']['core.ldn_mitm'])
        self.assertTrue(self.values['core.ldn_mitm'])

    def test_rejects_loopback_missing_interface_and_running_game_before_setting(self):
        for interface, running, expected in (
            ('lo', False, 'host_network: Eden never lists loopback, so "lo" means no network and no local wireless room; name a real interface'),
            (None, False, 'host_network: no interface: pass "interface" or use a base that names "network_interface"'),
            ('wifi0', True, 'host_network runs before game.load: core.network_interface is not changeable at run time'),
        ):
            with self.subTest(interface=interface, running=running):
                self.config.write_text('{}')
                self.running = running
                self.calls.clear()
                with self.assertRaises(RuntimeError) as caught:
                    routes.host_network(self.ctx, interface=interface)
                self.assertEqual(str(caught.exception), expected)
                self.assertFalse(any(name == 'settings.set' for name, _ in self.calls))

    def test_readback_mismatch_refuses(self):
        self.wrong_readback = True
        with self.assertRaisesRegex(RuntimeError, 'setting core.network_interface reads'):
            routes.host_network(self.ctx)


class LoadGameNetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.out = self.run / 'out'
        self.row = self.out / 'r00'
        self.row.mkdir(parents=True)
        self.config = self.run / 'config.json'
        self.calls = []
        self.live = 'wifi0'
        self.ctx = types.SimpleNamespace(
            runner=types.SimpleNamespace(out=self.out), row_dir=self.row, instance={'game': '/fake/game.xci'},
            call=self.call, check_stop=lambda: None)

    def call(self, method, params=None, job_cap=15):
        self.calls.append((method, params))
        if method == 'settings.get':
            return {'value': self.live}
        if method == 'status':
            return {'running': True, 'loading_complete': True, 'shaders_building': False, 'game_fps': 60}
        return {}

    def test_different_live_interface_refuses_before_game_load(self):
        self.config.write_text(json.dumps({'network_check': {'interface': 'wifi0', 'no_lobby': False}}))
        self.live = 'lo'
        with self.assertRaises(RuntimeError) as caught:
            routes.load_game(self.ctx)
        self.assertEqual(str(caught.exception),
                         'load_game: core.network_interface is "lo" but this run\'s checked interface is "wifi0"; '
                         'a step changed it after host_network. No game was loaded.')
        self.assertEqual([name for name, _ in self.calls], ['settings.get'])

    def test_matching_interface_is_checked_and_recorded_before_load(self):
        self.config.write_text(json.dumps({'network_check': {'interface': 'wifi0', 'no_lobby': False}}))
        result = routes.load_game(self.ctx)
        self.assertEqual([name for name, _ in self.calls], ['settings.get', 'game.load', 'status'])
        self.assertEqual(result['network_interface'], 'wifi0')
        self.assertEqual(json.loads((self.row / 'routes-load.json').read_text()), result)

    def test_no_config_or_no_lobby_loads_without_network_check(self):
        for config in (None, {}, {'network_check': {'interface': 'wifi0', 'no_lobby': True}}):
            with self.subTest(config=config):
                self.calls.clear()
                if config is None:
                    self.config.unlink(missing_ok=True)
                else:
                    self.config.write_text(json.dumps(config))
                result = routes.load_game(self.ctx)
                self.assertEqual([name for name, _ in self.calls], ['game.load', 'status'])
                self.assertNotIn('network_interface', result)


class FrontendRoomTests(unittest.TestCase):
    DYNAMIC_ROOM = object()
    # p00-rooms.json in phase23/1f-save-hold/ldn-pair/rc15-ldn-xps-2/out.
    HOST_ACTION = {'id': 'o130', 'class': 'QAction', 'name': 'action_Start_Room', 'text': '&Create Room'}
    HOST_DIALOG = {'id': 'o217', 'class': 'QLineEdit', 'name': 'username', 'text': None}
    HOST_LEAVE = {'id': 'o272', 'class': 'QPushButton', 'name': 'disconnect', 'text': 'Leave Room'}
    PEER_ACTION = {'id': 'o53', 'class': 'QAction', 'name': 'action_Connect_To_Room',
                   'text': '&Direct Connect to Room'}
    PEER_DIALOG = {'id': 'o220', 'class': 'QLineEdit', 'name': 'nickname', 'text': None}
    ROOM = {'id': 'o259', 'class': 'ClientRoomWindow', 'name': 'ClientRoom', 'text': None,
            # xps-1/out/c2-match-end/a-GATE-FAILED-trace-sched-tree.json, 2 changed to 1.
            'title': 'TRB3Waiting - Super Smash Bros. Ultimate (1/8 members) - connected'}
    # tss-lobby-17060a5e-1: final-L2-live-widgets.json.log, captured before any recovery.
    MESSAGE = {'id': 'o259', 'class': 'QMessageBox', 'name': '', 'text': None, 'title': 'Error',
               'visible': True, 'enabled': True, 'parent': '', 'window': 'o259',
               'properties': {'text': {'type': 'QString', 'value':
                                       'Room name is not valid. Must be 4 to 20 alphanumeric characters.',
                                       'writable': True}}}
    OPEN_HOST = {'id': 'o244', 'class': 'QLineEdit', 'name': 'username', 'text': None, 'title': '',
                 'visible': True, 'enabled': True, 'parent': 'o214', 'window': 'o235'}
    OK = {'id': 'o261', 'class': 'QPushButton', 'name': '', 'text': 'OK', 'title': '', 'visible': True,
          'enabled': True, 'parent': 'o262', 'window': 'o259'}
    # tss-lobby-17060a5e-2: final-L4-live-widgets.json.log / final-L4-live-host-fields.json.log.
    RUNNING_WARNING = {'id': 'o277', 'class': 'QMessageBox', 'name': '',
                       'title': 'Game already running', 'visible': True, 'enabled': True, 'parent': '',
                       'window': 'o277', 'properties': {'text': {'type': 'QString', 'value':
                           'Joining a room when the game is already running is discouraged and can cause '
                           'the room feature not to work correctly.\nProceed anyway?', 'writable': True}}}
    WARNING_OK = {'id': 'o276', 'class': 'QPushButton', 'name': '', 'text': 'OK', 'title': '', 'visible': True,
                  'enabled': True, 'parent': 'o278', 'window': 'o277'}
    WARNING_CANCEL = {'id': 'o280', 'class': 'QPushButton', 'name': '', 'text': 'Cancel', 'title': '', 'visible': True,
                      'enabled': True, 'parent': 'o278', 'window': 'o277'}
    RUNNING_HOST = [
        {'id': 'o250', 'class': 'QLineEdit', 'name': 'username', 'visible': True, 'enabled': True,
         'window': 'o240', 'properties': {'text': {'type': 'QString', 'value': 'TRHost', 'writable': True}}},
        {'id': 'o251', 'class': 'QLineEdit', 'name': 'port', 'visible': True, 'enabled': True,
         'window': 'o240', 'properties': {'text': {'type': 'QString', 'value': '24872', 'writable': True}}},
        {'id': 'o268', 'class': 'QLineEdit', 'name': 'room_name', 'visible': True, 'enabled': True,
         'window': 'o240', 'properties': {'text': {'type': 'QString', 'value': 'TR24872', 'writable': True}}},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.out = self.run / 'out'
        self.row = self.out / 'r00'
        self.row.mkdir(parents=True)
        (self.run / 'config.json').write_text(json.dumps({'label': 'proof'}))
        self.actions = []
        self.running = False
        self.nodes = [self.HOST_ACTION, self.PEER_ACTION]
        self.room_node = self.DYNAMIC_ROOM
        self.room_after_dismiss = None
        self.host_room_name = None
        self.modal_after_host = False
        self.warning_after_host = False
        self.repeat_warning = False
        self.queued_warning = -1
        self.runner = types.SimpleNamespace(out=self.out, ui=self.ui, ui_matches=runner.Runner.ui_matches)
        self.ctx = types.SimpleNamespace(runner=self.runner, on='a', row_dir=self.row,
                                         call=self.call, check_stop=lambda: None)
        self.real_udp_listeners = routes.udp_listeners
        patcher = mock.patch.object(routes, 'udp_listeners', return_value=None)
        self.listeners = patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, method, params=None, job_cap=15):
        if method == 'status':
            return {'running': self.running}
        if method == 'ui.tree':
            if self.queued_warning == 0:
                self.nodes.extend([self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL])
                self.queued_warning = -1
            elif self.queued_warning > 0:
                self.queued_warning -= 1
            return self.nodes
        raise AssertionError(method)

    def ui(self, on, step):
        self.actions.append((on, step))
        target = step.get('target', {})
        if step['ui'] == 'trigger':
            self.nodes.append(self.HOST_DIALOG if target['name'] == 'action_Start_Room' else self.PEER_DIALOG)
        elif step['ui'] == 'set' and target.get('name') == 'room_name':
            self.host_room_name = step['value']
        elif step['ui'] == 'click' and target.get('id') == self.OK['id']:
            self.nodes = [node for node in self.nodes if node not in (self.MESSAGE, self.OK)]
            if self.room_after_dismiss is not None:
                self.nodes.append(self.room_after_dismiss)
        elif step['ui'] == 'click' and target.get('id') == self.WARNING_OK['id']:
            self.nodes = [node for node in self.nodes if node not in
                          (self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL)]
            if self.repeat_warning:
                self.queued_warning = 1
            else:
                self.nodes.append(self.HOST_LEAVE)
                if self.room_node is not None:
                    self.nodes.append(self.connected_room())
        elif step['ui'] == 'click' and target.get('name') in ('host', 'connect'):
            if self.modal_after_host:
                self.nodes.extend([self.MESSAGE, self.OK])
            elif self.warning_after_host:
                self.nodes.extend([self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL])
            else:
                self.nodes.append(self.HOST_LEAVE)
                if self.room_node is not None:
                    self.nodes.append(self.connected_room())

    def connected_room(self):
        if self.room_node is not self.DYNAMIC_ROOM:
            return self.room_node
        fields = [node for node in self.nodes if node.get('class') == 'QLineEdit' and node.get('name') == 'room_name']
        dialog_name = routes._frontend_text(fields[0]) if len(fields) == 1 else None
        room_file = self.out / 'frontend-room.json'
        saved = json.loads(room_file.read_text()) if room_file.exists() else {}
        name = self.host_room_name or dialog_name or saved.get('room_name') or 'TRB3Waiting'
        return {**self.ROOM, 'title': self.ROOM['title'].replace('TRB3Waiting - ', f'{name} - ', 1)}

    def test_host_replays_real_sequence_and_saves_room_handoff(self):
        with mock.patch.object(routes, 'free_udp_port', return_value=24872):
            receipt = routes.frontend_room(self.ctx)
        target = lambda cls, name: {'class': cls, 'name': name}
        self.assertEqual(self.actions, [('a', step) for step in [
            {'ui': 'trigger', 'target': target('QAction', 'action_Start_Room')},
            {'ui': 'set', 'target': target('QLineEdit', 'username'), 'property': 'text', 'value': 'TRHost'},
            {'ui': 'set', 'target': target('QLineEdit', 'room_name'), 'property': 'text', 'value': 'TR24872'},
            {'ui': 'set', 'target': target('QLineEdit', 'port'), 'property': 'text', 'value': '24872'},
            {'ui': 'set', 'target': target('QLineEdit', 'port'), 'property': 'modified', 'value': True},
            {'ui': 'set', 'target': target('QComboBox', 'host_type'), 'property': 'currentIndex', 'value': 1},
            {'ui': 'click', 'target': target('QPushButton', 'host')}]])
        self.assertEqual(receipt['port'], 24872)
        self.assertEqual(receipt['room_name'], 'TR24872')
        self.assertFalse(receipt['game_running'])
        self.assertEqual(receipt['member_count'], 1)
        self.assertEqual(receipt['window_title'], 'TR24872 - Super Smash Bros. Ultimate (1/8 members) - connected')
        self.assertTrue(receipt['room_matched'])
        self.assertEqual(receipt['evidence']['host_action'], self.HOST_ACTION)
        self.assertEqual(receipt['evidence']['host_dialog'], self.HOST_DIALOG)
        self.assertEqual(receipt['evidence']['frontend_leave'], self.HOST_LEAVE)
        self.assertEqual(json.loads((self.row / 'routes-frontend-room.json').read_text()), receipt)
        self.assertEqual(json.loads((self.out / 'frontend-room.json').read_text()),
                         {'port': 24872, 'port_verified': None, 'room_name': 'TR24872', 'instance': 'a'})

    def test_host_marks_port_before_click_and_verifies_own_udp_listener(self):
        self.ctx.instance = {'pid': 111}
        self.listeners.side_effect = [[], [{'port': 24873, 'address': '0.0.0.0', 'comm': 'Eden', 'pid': 111}]]
        result = routes.frontend_room(self.ctx, port=24873)
        steps = [step for _, step in self.actions]
        text = next(i for i, step in enumerate(steps) if step.get('property') == 'text'
                    and step['target'].get('name') == 'port')
        modified = next(i for i, step in enumerate(steps) if step.get('property') == 'modified')
        host = next(i for i, step in enumerate(steps) if step['ui'] == 'click'
                    and step['target'].get('name') == 'host')
        self.assertLess(text, modified)
        self.assertLess(modified, host)
        self.assertEqual(steps[modified]['value'], True)
        self.assertTrue(result['port_marked_edited'])
        self.assertTrue(result['port_verified'])
        self.assertEqual(result['bound_ports'], [24873])
        handoff = json.loads((self.out / 'frontend-room.json').read_text())
        self.assertEqual((handoff['port'], handoff['port_verified']), (24873, True))

    def test_explicit_port_already_bound_stops_before_dialog_actions(self):
        for holder, expected in (({'port': 24873, 'address': '::', 'comm': 'eden', 'pid': 111}, 'eden pid 111'),
                                 ({'port': 24873, 'address': '*', 'comm': None, 'pid': None},
                                  'an unknown process')):
            with self.subTest(holder=holder):
                self.listeners.return_value = [holder]
                with self.assertRaises(RuntimeError) as caught:
                    routes.frontend_room(self.ctx, port=24873)
                self.assertEqual(str(caught.exception), f'frontend_room: UDP port 24873 is already in use on this '
                                 f'host by {expected}; pass another port or omit it')
                self.assertEqual(self.actions, [])
        self.nodes = [self.MESSAGE, self.OK]
        with self.assertRaisesRegex(RuntimeError, 'UDP port 24873 is already in use'):
            routes.frontend_room(self.ctx, port=24873)
        self.assertEqual(self.actions, [])

    def test_refused_modified_property_stops_nondefault_but_allows_default(self):
        def refuse(on, step):
            if step.get('property') == 'modified':
                self.actions.append((on, step))
                raise RuntimeError('property is read-only')
            return self.ui(on, step)
        self.runner.ui = refuse
        with self.assertRaises(RuntimeError) as caught:
            routes.frontend_room(self.ctx, port=24873)
        self.assertEqual(str(caught.exception),
                         'frontend_room: this Eden refused to mark the port field as edited '
                         '(property is read-only); it would host on its default port 24872, not 24873')
        self.assertFalse(any(step['ui'] == 'click' and step['target'].get('name') == 'host'
                             for _, step in self.actions))
        self.actions.clear()
        self.nodes = [self.HOST_ACTION, self.PEER_ACTION]
        result = routes.frontend_room(self.ctx, port=24872)
        self.assertFalse(result['port_marked_edited'])
        self.assertIsNone(result['port_verified'])
        self.assertTrue(any(step['ui'] == 'click' and step['target'].get('name') == 'host'
                            for _, step in self.actions))

    def test_port_edit_refusal_never_bypasses_stop(self):
        stopped = []
        def refuse(on, step):
            if step.get('property') == 'modified':
                stopped.append(True)
                raise runner.StopRun('STOP')
            return self.ui(on, step)
        self.runner.ui = refuse
        def stop():
            if stopped:
                raise runner.StopRun('STOP')
        self.ctx.check_stop = stop
        with self.assertRaises(runner.StopRun):
            routes.frontend_room(self.ctx, port=24872)
        self.assertFalse(any(step['ui'] == 'click' and step['target'].get('name') == 'host'
                             for _, step in self.actions))

    def test_create_failure_reports_requested_default_or_no_holder(self):
        failure = 'Creating a room failed. Please retry. Restarting Eden might be necessary.'
        self.MESSAGE = {**self.MESSAGE, 'properties': {'text': {'value': failure}}}
        self.modal_after_host = True
        for holders, suffix in (
            ([{'port': 24872, 'address': '0.0.0.0', 'comm': 'eden', 'pid': 111}],
             "; port 24872 (Eden's default) is held by eden pid 111"),
            ([{'port': 24873, 'address': '::', 'comm': 'other', 'pid': 222}],
             '; port 24873 is held by other pid 222'),
            ([], '; no holder of 24873 or 24872 found'),
        ):
            with self.subTest(holders=holders):
                self.actions.clear()
                self.nodes = [self.HOST_ACTION, self.PEER_ACTION]
                self.listeners.side_effect = [[], holders]
                with self.assertRaises(RuntimeError) as caught:
                    routes.frontend_room(self.ctx, port=24873, cap=.1)
                self.assertEqual(str(caught.exception),
                                 'frontend_room: Eden could not open the room on UDP port 24873' + suffix)
                self.assertFalse((self.out / 'frontend-room.json').exists())

    def test_connected_wrong_instance_port_stops_without_handoff(self):
        self.ctx.instance = {'pid': 111}
        self.listeners.side_effect = [[], [
            {'port': 24872, 'address': '0.0.0.0', 'comm': 'eden', 'pid': 111},
            {'port': 24873, 'address': '0.0.0.0', 'comm': 'eden', 'pid': 222}]]
        with self.assertRaises(RuntimeError) as caught:
            routes.frontend_room(self.ctx, port=24873)
        self.assertEqual(str(caught.exception),
                         'frontend_room: the room is connected but Eden listens on UDP [24872], not the requested 24873')
        self.assertFalse((self.out / 'frontend-room.json').exists())

    def test_no_ss_and_process_name_fallback(self):
        result = routes.frontend_room(self.ctx, port=24873)
        self.assertIsNone(result['port_verified'])
        self.assertIsNone(result['bound_ports'])
        self.assertTrue(result['port_marked_edited'])
        self.assertIsNone(json.loads((self.out / 'frontend-room.json').read_text())['port_verified'])
        self.nodes = [self.HOST_ACTION, self.PEER_ACTION]
        self.listeners.side_effect = [[], [{'port': 24873, 'address': '*', 'comm': 'EDEN-qt', 'pid': 222}]]
        result = routes.frontend_room(self.ctx, port=24873)
        self.assertTrue(result['port_verified'])
        self.assertEqual(result['bound_ports'], [24873])

    def test_automatically_selected_port_is_not_prechecked(self):
        self.listeners.return_value = [{'port': 24873, 'address': '*', 'comm': 'eden', 'pid': 111}]
        with mock.patch.object(routes, 'free_udp_port', return_value=24873):
            result = routes.frontend_room(self.ctx)
        self.assertTrue(result['port_verified'])
        self.assertEqual(self.listeners.call_count, 1)

    def test_reused_room_has_unverified_port_and_no_new_ui(self):
        self.nodes = [self.ROOM]
        result = routes.frontend_room(self.ctx, room_name='TRB3Waiting', port=24872)
        self.assertTrue(result['already_connected'])
        self.assertIsNone(result['port_verified'])
        self.assertEqual(self.actions, [])
        self.listeners.assert_not_called()

    def test_instance_pids_read_unit_from_sibling_run_receipt(self):
        self.ctx.instance = {'pid': 42}
        self.assertEqual(routes._instance_pids(self.ctx), {42})
        self.ctx.instance = {'socket': str(self.run / 'control.sock')}
        (self.run / 'run.json').write_text(json.dumps({'unit': 'eden-life-a.service',
                                                      'owned_pids': [{'pid': 999}]}))
        responses = [types.SimpleNamespace(returncode=0, stdout='111\n'),
                     types.SimpleNamespace(returncode=0, stdout='')]
        with mock.patch.object(routes.subprocess, 'run', side_effect=responses) as systemctl:
            self.assertEqual(routes._instance_pids(self.ctx), {111})
        self.assertEqual(systemctl.call_args_list[0].args[0],
                         ['systemctl', '--user', 'show', '-p', 'MainPID', '--value', 'eden-life-a.service'])
        with mock.patch.object(routes.subprocess, 'run', side_effect=OSError('unavailable')):
            self.assertIsNone(routes._instance_pids(self.ctx))
        responses = [types.SimpleNamespace(returncode=0, stdout='111\n'),
                     types.SimpleNamespace(returncode=0, stdout='/user.slice/eden-life-a.service\n')]
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == '/sys/fs/cgroup/user.slice/eden-life-a.service/cgroup.procs':
                return '111\n333\n'
            return original_read(path, *args, **kwargs)
        with mock.patch.object(routes.subprocess, 'run', side_effect=responses), \
                mock.patch.object(Path, 'read_text', read), mock.patch.object(Path, 'glob', return_value=[]):
            self.assertEqual(routes._instance_pids(self.ctx), {111, 333})

    def test_udp_listeners_parses_ipv4_ipv6_wildcard_and_multiple_users(self):
        output = ('UNCONN 0 0 0.0.0.0:24872 0.0.0.0:* users:(("eden",pid=111,fd=3))\n'
                  'UNCONN 0 0 [::]:24873 [::]:* users:(("Eden",pid=222,fd=4))\n'
                  'UNCONN 0 0 *:5353 *:* users:(("one",pid=12,fd=5),("two",pid=13,fd=8))\n'
                  'UNCONN 0 0 127.0.0.1:1234 0.0.0.0:*\n'
                  'garbage\nUNCONN 0 0 malformed:* *:*\n')
        invoke = mock.Mock(return_value=types.SimpleNamespace(returncode=0, stdout=output))
        self.assertEqual(self.real_udp_listeners(invoke), [
            {'port': 24872, 'address': '0.0.0.0', 'comm': 'eden', 'pid': 111},
            {'port': 24873, 'address': '::', 'comm': 'Eden', 'pid': 222},
            {'port': 5353, 'address': '*', 'comm': 'one', 'pid': 12},
            {'port': 5353, 'address': '*', 'comm': 'two', 'pid': 13},
            {'port': 1234, 'address': '127.0.0.1', 'comm': None, 'pid': None}])
        invoke.assert_called_once_with(['ss', '-H', '-lunp'], capture_output=True, text=True, timeout=5)
        self.assertIsNone(self.real_udp_listeners(mock.Mock(side_effect=FileNotFoundError())))
        self.assertIsNone(self.real_udp_listeners(mock.Mock(return_value=types.SimpleNamespace(returncode=1))))

    def test_join_replays_peer_sequence_and_reads_host_port(self):
        (self.out / 'frontend-room.json').write_text(json.dumps({'port': 24872, 'room_name': 'TR-proof', 'instance': 'a'}))
        self.ctx.on = 'b'
        receipt = routes.frontend_room(self.ctx, role='join', nickname='Guest', ip='127.0.0.1')
        target = lambda cls, name: {'class': cls, 'name': name}
        self.assertEqual(self.actions, [('b', step) for step in [
            {'ui': 'trigger', 'target': target('QAction', 'action_Connect_To_Room')},
            {'ui': 'set', 'target': target('QLineEdit', 'nickname'), 'property': 'text', 'value': 'Guest'},
            {'ui': 'set', 'target': target('QLineEdit', 'ip'), 'property': 'text', 'value': '127.0.0.1'},
            {'ui': 'set', 'target': target('QLineEdit', 'port'), 'property': 'text', 'value': '24872'},
            {'ui': 'click', 'target': target('QPushButton', 'connect')}]])
        self.assertEqual(receipt['port'], 24872)
        self.assertEqual(receipt['evidence']['peer_action'], self.PEER_ACTION)
        self.assertEqual(receipt['evidence']['peer_dialog'], self.PEER_DIALOG)

    def test_no_connected_window_and_insufficient_members_fail_with_observed_titles(self):
        self.room_node = None
        with self.assertRaisesRegex(RuntimeError, 'at least 1 member\\(s\\); saw: no room window'):
            routes.frontend_room(self.ctx, port=24872, cap=.01)
        self.nodes = [self.HOST_ACTION, self.PEER_ACTION]
        self.room_node = self.ROOM
        with self.assertRaisesRegex(RuntimeError, r'at least 2 member\(s\); saw: .*1/8 members'):
            routes.frontend_room(self.ctx, port=24872, members=2, cap=.01)
        self.nodes = [self.HOST_ACTION, self.PEER_ACTION]
        self.room_node = {**self.ROOM, 'title': self.ROOM['title'].replace(' - connected', ' - disconnected')}
        with self.assertRaisesRegex(RuntimeError, r'at least 1 member\(s\); saw: .*disconnected'):
            routes.frontend_room(self.ctx, port=24872, cap=.01)

    def test_explicit_join_port_and_nested_two_member_title(self):
        title = self.ROOM['title'].replace('(1/8 members)', '(2/8 members)')
        self.room_node = {key: value for key, value in self.ROOM.items() if key != 'title'}
        self.room_node['properties'] = {'windowTitle': {'value': title}}
        result = routes.frontend_room(self.ctx, role='join', port=24890, members=2)
        self.assertEqual((result['nickname'], result['port'], result['member_count'], result['window_title']),
                         ('TRGuest', 24890, 2, title))

    def test_running_game_is_allowed_and_recorded(self):
        self.running = True
        result = routes.frontend_room(self.ctx, port=24872)
        self.assertTrue(result['game_running'])
        result = routes.frontend_room(self.ctx, port=24872)
        self.assertTrue(result['already_connected'])
        self.assertTrue(result['game_running'])

    def test_join_without_port_and_unknown_role_refuse_before_ui(self):
        with self.assertRaisesRegex(RuntimeError, 'frontend_room:.*port'):
            routes.frontend_room(self.ctx, role='join')
        with self.assertRaisesRegex(RuntimeError, 'frontend_room:.*role'):
            routes.frontend_room(self.ctx, role='invalid', port=24872)
        self.assertEqual(self.actions, [])

    def test_udp_selection_skips_a_bound_port(self):
        first = routes.free_udp_port(first=24872, last=24960)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserved:
            reserved.bind(('0.0.0.0', first))
            chosen = routes.free_udp_port(first=first, last=24971)
        self.assertGreater(chosen, first)

    def test_names_default_from_port_and_invalid_values_refuse_before_ui(self):
        (self.run / 'config.json').write_text(json.dumps({'label': 'tss-lobby-17060a5e-1'}))
        with mock.patch.object(routes, 'free_udp_port', return_value=24872):
            result = routes.frontend_room(self.ctx)
        self.assertEqual(result['room_name'], 'TR24872')
        for field, value in (('room_name', 'TR-tss-lobby-17060a5e-1'), ('room_name', ''),
                             ('room_name', 'a' * 21), ('nickname', ''), ('nickname', 'Bob'),
                             ('nickname', 'åUser'), ('nickname', 'bad/name')):
            with self.subTest(field=field, value=value):
                self.actions.clear()
                with self.assertRaises(RuntimeError) as caught:
                    routes.frontend_room(self.ctx, port=24872, **{field: value})
                self.assertEqual(str(caught.exception), f'frontend_room: {field} "{value}" is not valid for Eden: '
                                 '4 to 20 characters of letters, digits, ".", "_", " " or "-" '
                                 '(yuzu/multiplayer/validation.h)')
                self.assertEqual(self.actions, [])

    def test_names_allow_full_boundaries_and_eden_punctuation(self):
        result = routes.frontend_room(self.ctx, port=24872, nickname='A._-', room_name='A._ -123456789012345')
        self.assertEqual(result['nickname'], 'A._-')
        self.assertEqual(result['room_name'], 'A._ -123456789012345')

    def test_message_during_wait_aborts_with_real_text(self):
        self.modal_after_host = True
        with self.assertRaises(RuntimeError) as caught:
            routes.frontend_room(self.ctx, port=24872, cap=.05)
        self.assertIn('frontend_room: Eden showed "Room name is not valid. Must be 4 to 20 alphanumeric characters."',
                      str(caught.exception))
        self.assertIn('while waiting for', str(caught.exception))

    def test_running_warning_after_host_is_confirmed_once_and_saved(self):
        self.running = self.warning_after_host = True
        result = routes.frontend_room(self.ctx, port=24872, cap=.2)
        self.assertEqual(result['confirmed_running_game'], self.RUNNING_WARNING['properties']['text']['value'])
        clicks = [step for _, step in self.actions if step['ui'] == 'click']
        self.assertEqual(sum(step['target'].get('id') == 'o276' for step in clicks), 1)
        self.assertFalse(any(step['target'].get('id') == 'o280' for step in clicks))
        self.assertEqual(json.loads((self.row / 'routes-frontend-room.json').read_text()), result)

    def test_running_warning_without_running_game_is_an_error(self):
        self.warning_after_host = True
        with self.assertRaisesRegex(RuntimeError, 'Eden showed .*game is already running'):
            routes.frontend_room(self.ctx, port=24872, cap=.1)
        self.assertFalse(any(step['target'].get('id') in ('o276', 'o280') for _, step in self.actions))

    def test_join_confirms_running_warning_after_connect(self):
        self.running = self.warning_after_host = True
        result = routes.frontend_room(self.ctx, role='join', port=24872, cap=.2)
        self.assertEqual(result['confirmed_running_game'], self.RUNNING_WARNING['properties']['text']['value'])
        self.assertEqual(sum(step.get('target', {}).get('id') == 'o276' for _, step in self.actions), 1)
        self.assertFalse(any(step.get('target', {}).get('id') == 'o280' for _, step in self.actions))

    def test_existing_warning_resumes_only_connected_wait_with_actual_host_fields(self):
        self.running = True
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *self.RUNNING_HOST]
        with mock.patch.object(routes, 'free_udp_port', side_effect=AssertionError('unexpected allocation')):
            result = routes.frontend_room(self.ctx, cap=.2)
        self.assertEqual(result['confirmed_running_game'], self.RUNNING_WARNING['properties']['text']['value'])
        self.assertEqual((result['port'], result['room_name'], result['nickname']), (24872, 'TR24872', 'TRHost'))
        self.assertEqual(len(self.actions), 1)
        self.assertEqual(self.actions[0][1]['ui'], 'click')
        self.assertEqual(self.actions[0][1]['target']['id'], 'o276')
        self.assertEqual(json.loads((self.out / 'frontend-room.json').read_text()),
                         {'port': 24872, 'port_verified': None, 'room_name': 'TR24872', 'instance': 'a'})

    def test_existing_warning_without_running_game_is_an_error(self):
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *self.RUNNING_HOST]
        with self.assertRaisesRegex(RuntimeError, 'Eden showed .*game is already running'):
            routes.frontend_room(self.ctx, port=24872, cap=.1)
        self.assertEqual(self.actions, [])

    def test_existing_warning_with_connected_window_is_confirmed_without_other_ui(self):
        self.running = True
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *self.RUNNING_HOST, self.ROOM]
        result = routes.frontend_room(self.ctx, cap=.2)
        self.assertEqual(result['confirmed_running_game'], self.RUNNING_WARNING['properties']['text']['value'])
        self.assertEqual([step['target'].get('id') for _, step in self.actions], ['o276'])

    def test_join_existing_warning_reads_the_pending_peer_dialog(self):
        self.running = True
        fields = [
            {'class': 'QLineEdit', 'name': name, 'id': 'peer-' + name, 'window': 'peer-window',
             'visible': True, 'properties': {'text': {'value': value}}}
            for name, value in (('nickname', 'PeerName'), ('port', '24902'), ('ip', '192.0.2.1'))
        ]
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *fields]
        result = routes.frontend_room(self.ctx, role='join', cap=.2)
        self.assertEqual((result['nickname'], result['port'], result['ip']), ('PeerName', 24902, '192.0.2.1'))
        self.assertEqual([step['target'].get('id') for _, step in self.actions], ['o276'])

    def test_existing_warning_without_dialog_fields_cannot_invent_host_room(self):
        self.running = True
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL]
        with mock.patch.object(routes, 'free_udp_port', side_effect=AssertionError('unexpected allocation')):
            with self.assertRaisesRegex(RuntimeError, 'cannot read the room name of the open Create Room dialog'):
                routes.frontend_room(self.ctx, cap=.2)
        self.assertEqual(self.actions, [])
        self.assertFalse((self.out / 'frontend-room.json').exists())

    def test_repeated_running_warning_is_an_error_without_a_second_click(self):
        self.running = self.warning_after_host = self.repeat_warning = True
        with self.assertRaisesRegex(RuntimeError, 'frontend_room:.*(again|second|repeated)'):
            routes.frontend_room(self.ctx, port=24872, cap=.2)
        self.assertEqual(sum(step.get('target', {}).get('id') == 'o276' for _, step in self.actions), 1)

    def test_warning_repeated_after_entry_confirmation_is_an_error(self):
        self.running = self.repeat_warning = True
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *self.RUNNING_HOST]
        with self.assertRaisesRegex(RuntimeError, 'frontend_room:.*(again|second|repeated)'):
            routes.frontend_room(self.ctx, port=24872, cap=.2)
        self.assertEqual([step['target'].get('id') for _, step in self.actions], ['o276'])

    def test_existing_warning_without_room_reports_connected_timeout(self):
        self.running = True
        self.room_node = None
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *self.RUNNING_HOST]
        with self.assertRaisesRegex(RuntimeError, r'at least 1 member\(s\); saw: no room window'):
            routes.frontend_room(self.ctx, port=24872, cap=.02)
        self.assertEqual([step['target'].get('id') for _, step in self.actions], ['o276'])

    def test_existing_warning_with_ambiguous_ok_is_never_clicked(self):
        self.running = True
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, {**self.WARNING_OK, 'id': 'o999'},
                      self.WARNING_CANCEL, *self.RUNNING_HOST]
        with self.assertRaisesRegex(RuntimeError, 'frontend_room:.*OK'):
            routes.frontend_room(self.ctx, port=24872, cap=.1)
        self.assertEqual(self.actions, [])

    def test_captured_modal_is_dismissed_and_host_dialog_reused(self):
        self.nodes = [self.MESSAGE, self.OK, self.OPEN_HOST]
        result = routes.frontend_room(self.ctx, port=24872)
        self.assertEqual(result['dismissed'], self.MESSAGE['properties']['text']['value'])
        self.assertTrue(result['reused_dialog'])
        self.assertEqual(self.actions[0], ('a', {'ui': 'click',
                                                'target': {'class': 'QPushButton', 'id': 'o261', 'text': '^OK$'}}))
        self.assertFalse(any(step['ui'] == 'trigger' for _, step in self.actions))
        self.assertTrue(any(step['ui'] == 'set' and step['target']['name'] == 'room_name'
                            and step['value'] == 'TR24872' for _, step in self.actions))
        self.assertEqual(result['member_count'], 1)

    def test_modal_without_matching_ok_fails_without_clicking_unrelated_button(self):
        self.nodes = [self.MESSAGE, {**self.OK, 'window': 'other'}, self.OPEN_HOST]
        with self.assertRaisesRegex(RuntimeError, 'frontend_room:.*OK'):
            routes.frontend_room(self.ctx, port=24872)
        self.assertEqual(self.actions, [])

    def test_invalid_name_does_not_dismiss_existing_modal(self):
        self.nodes = [self.MESSAGE, self.OK, self.OPEN_HOST]
        with self.assertRaisesRegex(RuntimeError, 'frontend_room: room_name .* not valid'):
            routes.frontend_room(self.ctx, port=24872, room_name='bad/name')
        self.assertEqual(self.actions, [])

    def test_join_reuses_open_dialog(self):
        self.nodes = [self.PEER_DIALOG]
        result = routes.frontend_room(self.ctx, role='join', port=24872)
        self.assertTrue(result['reused_dialog'])
        self.assertFalse(any(step['ui'] == 'trigger' for _, step in self.actions))
        self.assertEqual(result['member_count'], 1)

    def test_already_connected_returns_without_ui_or_port_allocation(self):
        self.nodes = [self.ROOM]
        with mock.patch.object(routes, 'free_udp_port', side_effect=AssertionError('allocated port')):
            result = routes.frontend_room(self.ctx, room_name='TRB3Waiting')
        self.assertTrue(result['already_connected'])
        self.assertEqual((result['window_title'], result['member_count']), (self.ROOM['title'], 1))
        self.assertFalse(result['game_running'])
        self.assertEqual(self.actions, [])
        self.assertEqual(json.loads((self.row / 'routes-frontend-room.json').read_text()), result)

    def test_already_connected_even_with_a_modal_has_no_ui_action(self):
        self.nodes = [{**self.ROOM, 'id': 'o300'}, self.MESSAGE, self.OK]
        result = routes.frontend_room(self.ctx, port=24872, room_name='TRB3Waiting')
        self.assertTrue(result['already_connected'])
        self.assertEqual(self.actions, [])

    def test_connected_room_with_too_few_members_is_not_accepted(self):
        self.nodes = [self.HOST_ACTION, self.PEER_ACTION, self.ROOM]
        self.room_node = {**self.ROOM, 'id': 'o400',
                          'title': self.ROOM['title'].replace('TRB3Waiting - ', 'TR24872 - ')
                          .replace('(1/8 members)', '(2/8 members)')}
        result = routes.frontend_room(self.ctx, port=24872, members=2)
        self.assertFalse(result['already_connected'])
        self.assertEqual(result['member_count'], 2)
        self.assertTrue(any(step['ui'] == 'trigger' for _, step in self.actions))

    def test_reuse_rejects_another_room_without_action_or_receipt(self):
        self.nodes = [self.ROOM]
        with self.assertRaisesRegex(RuntimeError, r'another room: expected "WantedRoom", saw "TRB3Waiting - '):
            routes.frontend_room(self.ctx, port=24872, room_name='WantedRoom')
        self.assertEqual(self.actions, [])
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_reuse_accepts_requested_matching_room(self):
        self.nodes = [self.ROOM]
        result = routes.frontend_room(self.ctx, room_name='TRB3Waiting')
        self.assertTrue(result['already_connected'])
        self.assertEqual(result['room_name'], 'TRB3Waiting')
        self.assertTrue(result['room_matched'])
        self.assertEqual(result['window_title'], self.ROOM['title'])
        self.assertEqual(self.actions, [])

    def test_reuse_uses_saved_name_and_port(self):
        self.nodes = [self.ROOM]
        (self.out / 'frontend-room.json').write_text(json.dumps(
            {'port': 24872, 'room_name': 'TRB3Waiting', 'instance': 'a'}))
        result = routes.frontend_room(self.ctx)
        self.assertTrue(result['already_connected'])
        self.assertEqual((result['room_name'], result['port']), ('TRB3Waiting', 24872))
        self.assertTrue(result['room_matched'])
        self.assertEqual(self.actions, [])

    def test_reuse_rejects_wrong_saved_name(self):
        self.nodes = [self.ROOM]
        (self.out / 'frontend-room.json').write_text(json.dumps(
            {'port': 24872, 'room_name': 'TR24872', 'instance': 'a'}))
        with self.assertRaisesRegex(RuntimeError, r'another room: expected "TR24872", saw "TRB3Waiting - '):
            routes.frontend_room(self.ctx)
        self.assertEqual(self.actions, [])
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_reuse_requires_requested_or_saved_name(self):
        self.nodes = [self.ROOM]
        with self.assertRaisesRegex(RuntimeError, r'no room_name was given and this run has no frontend-room.json'):
            routes.frontend_room(self.ctx, port=24872)
        self.assertEqual(self.actions, [])
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_reuse_does_not_accept_room_name_prefix(self):
        self.nodes = [self.ROOM]
        with self.assertRaisesRegex(RuntimeError, r'another room: expected "TRB3", saw "TRB3Waiting - '):
            routes.frontend_room(self.ctx, port=24872, room_name='TRB3')
        self.assertEqual(self.actions, [])

    def test_fresh_host_rejects_wrong_connected_room_without_saving(self):
        self.room_node = self.ROOM
        with self.assertRaisesRegex(RuntimeError, r'for room "TR24872" with at least 1 member\(s\); saw: .*TRB3Waiting'):
            routes.frontend_room(self.ctx, port=24872, cap=.05)
        self.assertFalse((self.out / 'frontend-room.json').exists())
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_fresh_join_rejects_room_different_from_saved_name(self):
        (self.out / 'frontend-room.json').write_text(json.dumps(
            {'port': 24872, 'room_name': 'TR-proof', 'instance': 'a'}))
        self.room_node = self.ROOM
        with self.assertRaisesRegex(RuntimeError, r'for room "TR-proof" with at least 1 member\(s\); saw: .*TRB3Waiting'):
            routes.frontend_room(self.ctx, role='join', cap=.05)
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_fresh_join_accepts_matching_saved_name(self):
        (self.out / 'frontend-room.json').write_text(json.dumps(
            {'port': 24872, 'room_name': 'TR-proof', 'instance': 'a'}))
        self.room_node = {**self.ROOM, 'title': self.ROOM['title'].replace('TRB3Waiting - ', 'TR-proof - ')}
        result = routes.frontend_room(self.ctx, role='join', cap=.05)
        self.assertEqual(result['room_name'], 'TR-proof')
        self.assertTrue(result['room_matched'])

    def test_fresh_join_without_known_name_accepts_connected_window(self):
        result = routes.frontend_room(self.ctx, role='join', port=24872, cap=.05)
        self.assertIsNone(result['room_name'])
        self.assertIsNone(result['room_matched'])
        self.assertEqual(result['window_title'], self.ROOM['title'])

    def test_existing_running_warning_rejects_another_connected_room(self):
        self.running = True
        self.room_node = self.ROOM
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *self.RUNNING_HOST]
        with self.assertRaisesRegex(RuntimeError, r'for room "TR24872" with at least 1 member\(s\); saw: .*TRB3Waiting'):
            routes.frontend_room(self.ctx, cap=.05)
        self.assertEqual([step['target'].get('id') for _, step in self.actions], ['o276'])
        self.assertFalse((self.out / 'frontend-room.json').exists())

    def test_existing_running_warning_accepts_dialog_matching_room(self):
        self.running = True
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *self.RUNNING_HOST]
        result = routes.frontend_room(self.ctx, cap=.05)
        self.assertEqual(result['room_name'], 'TR24872')
        self.assertEqual(result['window_title'], 'TR24872 - Super Smash Bros. Ultimate (1/8 members) - connected')
        self.assertTrue(result['room_matched'])

    def test_warning_host_falls_back_to_requested_or_saved_room_name(self):
        self.running = True
        fields = [n for n in self.RUNNING_HOST if n['name'] != 'room_name']
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *fields]
        self.room_node = {**self.ROOM, 'title': self.ROOM['title'].replace('TRB3Waiting - ', 'WantedRoom - ')}
        result = routes.frontend_room(self.ctx, room_name='WantedRoom', cap=.05)
        self.assertEqual(result['room_name'], 'WantedRoom')
        self.assertTrue(result['room_matched'])

        self.actions.clear()
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *fields]
        (self.out / 'frontend-room.json').write_text(json.dumps(
            {'port': 24872, 'room_name': 'WantedRoom', 'instance': 'a'}))
        result = routes.frontend_room(self.ctx, cap=.05)
        self.assertEqual(result['room_name'], 'WantedRoom')
        self.assertTrue(result['room_matched'])

    def test_reuse_after_modal_refuses_mismatch(self):
        self.nodes = [self.MESSAGE, self.OK]
        self.room_after_dismiss = self.ROOM
        (self.out / 'frontend-room.json').write_text(json.dumps(
            {'port': 24872, 'room_name': 'TR24872', 'instance': 'a'}))
        with mock.patch.object(routes, 'free_udp_port', return_value=24999):
            with self.assertRaisesRegex(RuntimeError, r'another room: expected "TR24872", saw "TRB3Waiting - '):
                routes.frontend_room(self.ctx)
        self.assertEqual([step['target'].get('id') for _, step in self.actions], ['o261'])
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_reuse_after_modal_accepts_saved_name_and_port(self):
        self.nodes = [self.MESSAGE, self.OK]
        self.room_after_dismiss = self.ROOM
        (self.out / 'frontend-room.json').write_text(json.dumps(
            {'port': 24872, 'room_name': 'TRB3Waiting', 'instance': 'a'}))
        with mock.patch.object(routes, 'free_udp_port', return_value=24999):
            result = routes.frontend_room(self.ctx)
        self.assertTrue(result['already_connected'])
        self.assertEqual((result['room_name'], result['port']), ('TRB3Waiting', 24872))
        self.assertTrue(result['room_matched'])
        self.assertEqual([step['target'].get('id') for _, step in self.actions], ['o261'])

    def test_existing_warning_rejects_requested_name_different_from_dialog(self):
        self.running = True
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *self.RUNNING_HOST]
        with self.assertRaisesRegex(
                RuntimeError, r'open Create Room dialog is for room "TR24872".*not the requested room "WantedRoom"'):
            routes.frontend_room(self.ctx, room_name='WantedRoom', cap=.05)
        self.assertEqual(self.actions, [])
        self.assertFalse((self.out / 'frontend-room.json').exists())
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_existing_warning_accepts_requested_matching_name(self):
        self.running = True
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *self.RUNNING_HOST]
        result = routes.frontend_room(self.ctx, room_name='TR24872', cap=.05)
        self.assertEqual(result['room_name'], 'TR24872')
        self.assertTrue(result['room_matched'])
        self.assertEqual([step['target'].get('id') for _, step in self.actions], ['o276'])

    def test_existing_warning_accepts_requested_matching_port(self):
        self.running = True
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *self.RUNNING_HOST]
        result = routes.frontend_room(self.ctx, port=24872, cap=.05)
        self.assertEqual((result['room_name'], result['port']), ('TR24872', 24872))
        self.assertTrue(result['room_matched'])

    def test_existing_warning_rejects_requested_host_port_mismatch(self):
        self.running = True
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *self.RUNNING_HOST]
        with self.assertRaisesRegex(
                RuntimeError, r'for room "TR24872" on port 24872, not the requested room "None" on port 24999'):
            routes.frontend_room(self.ctx, port=24999, cap=.05)
        self.assertEqual(self.actions, [])
        self.assertFalse((self.out / 'frontend-room.json').exists())
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_existing_warning_rejects_requested_join_port_mismatch(self):
        self.running = True
        fields = [
            {'class': 'QLineEdit', 'name': name, 'id': 'peer-' + name, 'window': 'peer-window',
             'visible': True, 'properties': {'text': {'value': value}}}
            for name, value in (('nickname', 'PeerName'), ('port', '24902'), ('ip', '192.0.2.1'))
        ]
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *fields]
        with self.assertRaisesRegex(
                RuntimeError, r'the open Direct Connect dialog is on port 24902, not the requested port 24999'):
            routes.frontend_room(self.ctx, role='join', port=24999, cap=.05)
        self.assertEqual(self.actions, [])
        self.assertFalse((self.out / 'frontend-room.json').exists())
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_existing_warning_with_unreadable_dialog_values_uses_requested_name(self):
        self.running = True
        fields = [node for node in self.RUNNING_HOST if node['name'] not in ('room_name', 'port')]
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *fields]
        self.room_node = {**self.ROOM, 'title': self.ROOM['title'].replace('TRB3Waiting - ', 'WantedRoom - ')}
        result = routes.frontend_room(self.ctx, port=24872, room_name='WantedRoom', cap=.05)
        self.assertEqual(result['room_name'], 'WantedRoom')
        self.assertTrue(result['room_matched'])

    def test_reuse_rejects_ambiguous_longer_room_name_without_receipt(self):
        title = 'WantedRoom - Other - Super Smash Bros. Ultimate (1/8 members) - connected'
        self.nodes = [{**self.ROOM, 'title': title}]
        with self.assertRaisesRegex(RuntimeError, r'another room: expected "WantedRoom".*ambiguous') as caught:
            routes.frontend_room(self.ctx, room_name='WantedRoom', cap=.05)
        self.assertIn('; the title has more than one " - ", so its room name is ambiguous',
                      str(caught.exception))
        self.assertEqual(self.actions, [])
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_reuse_reports_ambiguity_without_prefix_and_with_overlapping_separators(self):
        titles = ('A - B - X (1/8 members) - connected', 'A - - B (1/8 members) - connected')
        for title in titles:
            with self.subTest(title=title):
                self.nodes = [{**self.ROOM, 'title': title}]
                with self.assertRaisesRegex(RuntimeError, r'another room: expected "WrongRoom".*ambiguous') as caught:
                    routes.frontend_room(self.ctx, room_name='WrongRoom', cap=.05)
                self.assertIn('; the title has more than one " - ", so its room name is ambiguous',
                              str(caught.exception))
                self.assertEqual(self.actions, [])
                self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_reuse_rejects_long_room_name_before_tree(self):
        title = 'WantedRoom - Other - Super Smash Bros. Ultimate (1/8 members) - connected'
        self.nodes = [{**self.ROOM, 'title': title}]
        calls = []
        def call(method, *args):
            calls.append(method)
            return self.call(method, *args)
        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, r'room_name "WantedRoom - Other" contains " - "'):
            routes.frontend_room(self.ctx, room_name='WantedRoom - Other', cap=.05)
        self.assertEqual(calls, ['status'])
        self.assertEqual(self.actions, [])
        self.assertFalse((self.out / 'frontend-room.json').exists())
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_fresh_host_rejects_ambiguous_longer_room_without_saving(self):
        title = 'TR24872 - Other - Super Smash Bros. Ultimate (1/8 members) - connected'
        self.room_node = {**self.ROOM, 'title': title}
        with self.assertRaisesRegex(RuntimeError, r'for room "TR24872"'):
            routes.frontend_room(self.ctx, port=24872, cap=.05)
        self.assertFalse((self.out / 'frontend-room.json').exists())
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_fresh_host_rejects_long_room_name_before_tree(self):
        title = 'TR24872 - Other - Super Smash Bros. Ultimate (1/8 members) - connected'
        self.room_node = {**self.ROOM, 'title': title}
        calls = []
        def call(method, *args):
            calls.append(method)
            return self.call(method, *args)
        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, r'room_name "TR24872 - Other" contains " - "'):
            routes.frontend_room(self.ctx, port=24872, room_name='TR24872 - Other', cap=.05)
        self.assertEqual(calls, ['status'])
        self.assertEqual(self.actions, [])
        self.assertFalse((self.out / 'frontend-room.json').exists())
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_host_and_join_reject_room_name_ending_space_hyphen_before_tree(self):
        calls = []
        def call(method, *args):
            calls.append(method)
            return self.call(method, *args)
        self.ctx.call = call
        for role in ('host', 'join'):
            with self.subTest(role=role):
                calls.clear()
                with self.assertRaisesRegex(RuntimeError, r'room_name "Room -" contains " - "') as caught:
                    routes.frontend_room(self.ctx, role=role, port=24872, room_name='Room -', cap=.05)
                self.assertEqual(str(caught.exception), 'frontend_room: room_name "Room -" contains " - ", so '
                                 'Eden\'s room window title ("<room> - <game> (n/m members) - connected") could '
                                 'not confirm it; use a name without " - "')
                self.assertEqual(calls, ['status'])
                self.assertEqual(self.actions, [])
                self.assertFalse((self.out / 'frontend-room.json').exists())
                self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_saved_unconfirmable_room_name_refused_before_tree(self):
        room_file = self.out / 'frontend-room.json'
        data = {'port': 24872, 'room_name': 'A - B', 'instance': 'a'}
        room_file.write_text(json.dumps(data))
        calls = []
        def call(method, *args):
            calls.append(method)
            return self.call(method, *args)
        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, r'room_name "A - B" contains " - "'):
            routes.frontend_room(self.ctx, cap=.05)
        self.assertEqual(calls, ['status'])
        self.assertEqual(self.actions, [])
        self.assertEqual(json.loads(room_file.read_text()), data)
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_dialog_unconfirmable_room_name_refused_before_warning_click(self):
        self.running = True
        fields = [
            {**node, 'properties': {'text': {'value': 'A - B'}}} if node['name'] == 'room_name' else node
            for node in self.RUNNING_HOST
        ]
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *fields]
        with self.assertRaisesRegex(RuntimeError, r'room_name "A - B" contains " - "'):
            routes.frontend_room(self.ctx, cap=.05)
        self.assertEqual(self.actions, [])
        self.assertFalse((self.out / 'frontend-room.json').exists())
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_dialog_invalid_name_keeps_character_validation_before_ambiguity(self):
        self.running = True
        fields = [
            {**node, 'properties': {'text': {'value': 'A - B!'}}} if node['name'] == 'room_name' else node
            for node in self.RUNNING_HOST
        ]
        self.nodes = [self.RUNNING_WARNING, self.WARNING_OK, self.WARNING_CANCEL, *fields]
        with self.assertRaisesRegex(RuntimeError, r'room_name "A - B!" is not valid for Eden'):
            routes.frontend_room(self.ctx, cap=.05)
        self.assertEqual(self.actions, [])
        self.assertFalse((self.out / 'frontend-room.json').exists())
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_join_rejects_unconfirmable_requested_name_before_tree(self):
        calls = []
        def call(method, *args):
            calls.append(method)
            return self.call(method, *args)
        self.ctx.call = call
        with self.assertRaisesRegex(RuntimeError, r'room_name "A - B" contains " - "'):
            routes.frontend_room(self.ctx, role='join', port=24872, room_name='A - B', cap=.05)
        self.assertEqual(calls, ['status'])
        self.assertEqual(self.actions, [])
        self.assertFalse((self.row / 'routes-frontend-room.json').exists())

    def test_room_title_matches_only_complete_unambiguous_room(self):
        cases = [
            ('TRB3Waiting - Super Smash Bros. Ultimate (1/8 members) - connected', 'TRB3Waiting', True),
            ('TRB3Waiting - Super Smash Bros. Ultimate (1/8 members) - connected', 'TRB3', False),
            ('TR-proof - Super Smash Bros. Ultimate (2/8 members) - connected', 'TR-proof', True),
            ('WantedRoom - Other - Super Smash Bros. Ultimate (1/8 members) - connected', 'WantedRoom', False),
            ('WantedRoom - Other - Super Smash Bros. Ultimate (1/8 members) - connected', 'WantedRoom - Other', False),
            ('A - B - X (1/8 members) - connected', 'A - B', False),
            ('A - B - X (1/8 members) - connected', 'A', False),
            ('A - - B (1/8 members) - connected', 'A', False),
            ('A - - B (1/8 members) - connected', 'A -', False),
            ('WantedRoom - Super Smash Bros. Ultimate', 'WantedRoom', False),
            ('WantedRoom -  (1/8 members) - connected', 'WantedRoom', False),
        ]
        for title, room_name, expected in cases:
            with self.subTest(title=title, room_name=room_name):
                self.assertEqual(routes._frontend_room_matches(title, room_name), expected)


class BridgeAliveTests(unittest.TestCase):
    BOOT = '0x901afdfc49eabde3'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / 'bridge.log'
        self.log.write_text('[Adapter] reconnect same_boot=1\n[Adapter] reconnect same_boot=1\n')
        self.ctx = types.SimpleNamespace(instance={'bridge_log': str(self.log)}, on='a',
                                         runner=types.SimpleNamespace(), row_dir=self.root,
                                         check_stop=lambda: None)
        self.requests = []
        self.orphan_end = False
        self.supply = [self.wire('ssbu.ui', 5, 200, 1000), self.wire('ssbu.ui', 6, 201, 1010),
                       self.wire('tr.menu', 1, 202, 1020), self.wire('tr.menu', 1, 203, 1030)]

    @classmethod
    def wire(cls, provider, rev, frame, wall):
        schema = provider + '/1'
        # tr.menu keys and framing follow rc15-intermediate-next/runtime-17060a5e/xps/bridge.log:4077.
        payload = ([f'schema={schema}', f'rev={rev}', f'boot={cls.BOOT}', f'observation={rev}',
                    'observation.active=yes', 'focus=main_menu', 'screen=main_menu'] if provider == 'ssbu.ui' else
                   [f'schema={schema}', 'ack=0', 'ack.result="handled"', 'focus=/-/0/row', 'open=0'])
        suffix = f' frame={frame} wall={wall}\n'
        begin = (f'[Bridge] state begin name={provider} schema={schema} rev={rev} boot={cls.BOOT} '
                 f'lines={len(payload)} filtered=0' + suffix)
        body = ''.join(f'[Bridge] state.{i} {line}' + suffix for i, line in enumerate(payload))
        crc = zlib.crc32('\n'.join(payload).encode())
        return begin + body + f'[Bridge] state end name={provider} rev={rev} crc={crc:08x}' + suffix

    def spool(self, ctx, *requests):
        self.assertEqual(ctx, self.ctx)
        self.requests.append(requests)
        if self.supply:
            with self.log.open('a') as f:
                f.write(f'[Bridge] req providers result=ok count=2 boot={self.BOOT}\n')
                if self.orphan_end:
                    f.write('[Bridge] state end name=ssbu.ui rev=4 crc=00000000 frame=199 wall=990\n')
                f.write(self.supply.pop(0))

    def test_fenced_replies_allow_equal_menu_revision_and_save_reply_clocks(self):
        with mock.patch.object(routes, 'bridge_v2', self.spool):
            result = routes.bridge_alive(self.ctx, cap=2)
        self.assertEqual(result['block'], 'bridge_alive')
        self.assertEqual(result['ssbu.ui'], {'rev': [5, 6], 'screen': 'main_menu'})
        self.assertEqual(result['tr.menu'], {'rev': [1, 1], 'frame': [202, 203], 'wall': [1020, 1030]})
        self.assertEqual(result['reconnects'], 2)
        self.assertEqual(self.requests, [('providers', 'state ssbu.ui')] * 2 +
                         [('providers', 'state tr.menu')] * 2)
        self.assertEqual(json.loads((self.root / 'routes-bridge-alive.json').read_text()), result)

    def test_orphan_end_before_reply_is_ignored(self):
        self.orphan_end = True
        with mock.patch.object(routes, 'bridge_v2', self.spool):
            result = routes.bridge_alive(self.ctx, cap=2)
        self.assertEqual(result['tr.menu']['rev'], [1, 1])

    def test_missing_second_menu_reply_names_provider_and_cap(self):
        self.supply.pop()
        with mock.patch.object(routes, 'bridge_v2', self.spool):
            with self.assertRaisesRegex(RuntimeError, r'bridge_alive: tr\.menu.*0\.1'):
                routes.bridge_alive(self.ctx, cap=.1)

    def test_missing_newer_ui_sample_names_provider_and_cap(self):
        self.supply = [self.wire('ssbu.ui', 5, 200, 1000), self.wire('ssbu.ui', 5, 201, 1010)]
        with mock.patch.object(routes, 'bridge_v2', self.spool):
            with self.assertRaisesRegex(RuntimeError, r'bridge_alive: ssbu\.ui.*0\.1'):
                routes.bridge_alive(self.ctx, cap=.1)

    def test_wrong_boot_on_second_menu_reply_is_refused(self):
        self.supply[3] = self.supply[3].replace(self.BOOT, '0x0000000000000002')
        with mock.patch.object(routes, 'bridge_v2', self.spool):
            with self.assertRaisesRegex(RuntimeError, r'bridge_alive: tr\.menu.*0\.1'):
                routes.bridge_alive(self.ctx, cap=.1)


class NativeStateTests(unittest.TestCase):
    """routes.native_state / wait_native / frame_advancing over a bridge capture's bridge.log."""

    def ctx(self):
        import types
        run = Path(tempfile.mkdtemp())
        (run / 'bridge').mkdir()
        log = run / 'bridge.log'
        log.write_text('[Bridge] stage id=3 frame=100 wall=1\n[Bridge] roster entry=0 kind=0 nana=false frame=100 wall=1\n')
        return types.SimpleNamespace(instance={'bridge_log': str(log)}, check_stop=lambda: None), log

    def test_native_state_reads_the_bridge_log(self):
        ctx, _ = self.ctx()
        state = routes.native_state(ctx)
        self.assertEqual((state['stage'], state['frame'], state['roster'][0]['kind']), (3, 100, 0))

    def test_frame_advancing_waits_for_the_counter(self):
        import threading
        ctx, log = self.ctx()

        def run():
            for frame in range(110, 200, 10):
                time.sleep(0.05)
                with log.open('a') as f:
                    f.write(f'[Bridge] item entry=0 held=false kind=-1 frame={frame} wall=2\n')

        worker = threading.Thread(target=run)
        worker.start()
        state = routes.frame_advancing(ctx, frames=50, cap=5)
        worker.join()
        self.assertGreaterEqual(state['frame'], 150)

    def test_wait_native_reports_the_last_state_on_timeout(self):
        ctx, _ = self.ctx()
        with self.assertRaisesRegex(RuntimeError, 'frame=100 stage=3'):
            routes.wait_native(ctx, lambda s: s.get('stage') == 8, cap=0.3, every=0.05)


class TileWalkerTests(unittest.TestCase):
    def walker(self, widths, start, screen='sss'):
        folder = Path(tempfile.mkdtemp())
        log = folder / 'events.log'
        log.write_text('')
        game = FakeTileGame(log, widths, screen, start)
        ctx = FakeCtx(FakeRunner({'tile_log': str(log)}, game), folder)
        return routes.TileWalker(ctx, screen), game, ctx

    def test_walks_to_target_by_rows_read_from_the_log(self):
        walker, game, ctx = self.walker([11] * 10 + [6], start=0)
        out = walker.walk_to(4)
        self.assertEqual(out['at'], 4)
        self.assertEqual(game.at, 4)
        walker, game, ctx = self.walker([11] * 10 + [6], start=37)
        self.assertEqual(walker.walk_to(57)['at'], 57)
        self.assertEqual(game.at, 57)

    def test_last_short_row(self):
        walker, game, ctx = self.walker([13] * 6 + [9], start=0, screen='css')
        self.assertEqual(walker.walk_to(85)['at'], 85)

    def test_plain_presses_are_never_sent(self):
        walker, game, ctx = self.walker([11] * 3, start=5)
        walker.walk_to(14)
        self.assertTrue(all(name.startswith('tile-') for name in ctx.runner.played))

    def test_settled_mismatch_fails(self):
        walker, game, ctx = self.walker([11] * 3, start=0)
        log = Path(ctx.instance['tile_log'])

        def lying(direction):
            with log.open('a') as f:
                f.write(f'1 step screen=sss dir={direction} from=0 to=1 x=0.0 y=0.0\n'
                        '3 settled screen=sss at=99 hovered=none\n')
        game.step = lying
        with self.assertRaises(RuntimeError):
            walker.step('right')


class StageTransitionTests(unittest.TestCase):
    def test_generic_stage_finish_rejects_a_different_destination_context(self):
        pending = picker_fixtures.recorded_smash_ui(847)
        pending.values['screen.context'] = 'training'
        io, prepared = picker_fixtures.stage_confirmation_replay(
            'smash', pending, picker_fixtures.recorded_smash_ui(870))
        ctx = FakeCtx(FakeRunner({}, None), Path(tempfile.mkdtemp()))
        with self.assertRaisesRegex(RuntimeError, 'unexpected pending css.smash observation'):
            routes._finish_native_pick(ctx, io, 'sss', prepared, {'id': 308, 'name': 'Training'},
                                       'css.smash', .1)
        self.assertEqual(io.actions, ['tap-a1'])

    def test_transition_deadline_changes_only_the_css_wait(self):
        from unittest.mock import Mock, patch
        ctx = Mock()
        for cap in (60, 180):
            with patch.multiple(routes, BridgePickerIO=Mock(), _tile_topology=Mock(),
                                _finish_native_pick=Mock(), gate=Mock()), \
                    patch.object(routes.native_picker, 'hover') as hover:
                if cap == 60:
                    routes.pick_stage(ctx, "Battlefield")
                else:
                    routes.pick_stage(ctx, "Battlefield", transition_cap=cap)
                io = routes.BridgePickerIO.return_value
                hover.assert_called_once_with(
                    io, 'sss', routes._native_target(routes.stage_entry('Battlefield'), 'normal'),
                    routes._tile_topology.return_value, expected_context='training')
                routes._finish_native_pick.assert_called_once_with(
                    ctx, io, 'sss', hover.return_value, routes.stage_entry('Battlefield'), 'css.training', cap)
                routes.gate.assert_not_called()


class TableTests(unittest.TestCase):
    def test_tables_come_from_trainingrooms(self):
        t = routes.tables()
        self.assertEqual(routes.fighter_entry('Mario')['tile'], 0)
        self.assertEqual(routes.fighter_entry('Lucas')['kind'], 0x28)
        self.assertEqual(routes.fighter_entry('0x28')['tile'], 39)
        self.assertEqual(routes.fighter_entry('Steve')['tile'], 78)
        self.assertEqual(routes.stage_entry('Battlefield')['tile'], 1)
        self.assertEqual(routes.stage_entry('final destination')['id'], 3)
        self.assertEqual(routes.stage_entry('Kongo Jungle')['tile'], 6)
        self.assertTrue(routes.stage_entry('Kongo Jungle').get('proven'))
        self.assertIn('commit', t['source'])
        with self.assertRaises(KeyError):
            routes.fighter_entry('Pokemon Trainer')

    def test_macro_index_hashes_match(self):
        for name in routes.routes_index()['macros']:
            path, sha, cap = routes.macro_file(name)
            self.assertTrue(path.is_file(), name)

    def test_chord_macros_press_in_one_frame(self):
        reset = json.loads(routes.macro_file('reset-lra')[0].read_text())
        self.assertIn(1 | 64 | 128, [e['buttons'] for e in reset['events']])
        tile = json.loads(routes.macro_file('tile-right')[0].read_text())
        self.assertEqual([e['buttons'] for e in tile['events']], [16, 16 | 16384, 0])


class RecipeTests(unittest.TestCase):
    def test_combo_in_one_frame_with_min_hold(self):
        import recipe_macro
        doc = recipe_macro.compile_recipe('open', [{'buttons': 'b+up', 'hold': 10, 'gap': 0.1}])
        self.assertEqual([(e['frame'], e['buttons']) for e in doc['events']], [(0, 8194), (2, 0)])
        self.assertEqual(doc['duration_frames'], 2 + 6)
        doc = recipe_macro.compile_recipe('x', [{'buttons': 'plus', 'hold': 100}, {'stick': [0, -1, 250], 'gap': 0}])
        self.assertEqual([(e['frame'], e['buttons'], e['sticks'][1]) for e in doc['events']],
                         [(0, 1024, 0), (6, 0, 0), (42, 0, -1.0), (57, 0, 0)])
        with self.assertRaises(ValueError):
            recipe_macro.compile_recipe('h', [{'buttons': 'home'}])

    def test_plan_compiles_to_a_ready_dry_run(self):
        import recipe_macro
        folder = Path(tempfile.mkdtemp())
        plan = {'game_id': '01006A800016E000', 'start': 's',
                'gates': {'g': {'kind': 'log', 'source': 'console_log', 'pattern': 'x', 'cap': 1}},
                'segments': {'menu': {'recipe': [{'buttons': 'b+up', 'hold': 100}]}},
                'rows': [{'id': 'r', 'start': 's', 'end': 's', 'steps': [{'gate': 'g'}, {'play': 'menu'}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        self.assertEqual(recipe_macro.compile_plan(folder / 'rows.json'), ['menu'])
        report = runner.dry_run(folder / 'rows.json')
        self.assertEqual(report['result'], 'READY', report)
        self.assertEqual(recipe_macro.compile_plan(folder / 'rows.json'), [])  # already compiled


# vendored hardware stills (fixtures/training-ink/PROVENANCE.md): never read from the artifacts tree, which is pruned
INK_FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'training-ink'


class TrainingInkGateTests(unittest.TestCase):
    """The hw Training gate on another stage: the plate is translucent, the text is not (run-1 S-B07, 19:34Z)."""

    def test_peachs_castle_passes_and_other_screens_fail(self):
        from runner import Runner
        spec = routes._switch_spec('training')
        compare = Runner.compare_region  # a staticmethod
        positives = [INK_FIXTURES / 'pos-other-stage-plate.png', INK_FIXTURES / 'pos-t01-closed.png']
        negatives = [INK_FIXTURES / n for n in ('neg-t01-rec-reset-row.png', 'neg-up-still.png', 'neg-css.png',
                                                'neg-tr-menu.png')]
        for png in positives:
            self.assertLessEqual(routes.ink_compare(png, spec), spec['ink_limit'], png)
        for png in negatives:
            self.assertGreater(routes.ink_compare(png, spec), 0.6, png)
        diff, note = routes.match_frame(positives[0], spec, compare)
        self.assertEqual(diff, 0.0)
        self.assertIn('ink_mismatch', note)
        for png in negatives:
            self.assertGreater(routes.match_frame(png, spec, compare)[0], spec['tolerance'], png)


class SwitchHwTests(unittest.TestCase):
    BRIDGE_LINES = [
        '[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=v1 port=6970 guards=0x7f frame=0 wall=10',
        '[TrainingRooms] cpu_ids FIGHTER_STATUS_KIND_WAIT=0 FIGHTER_STATUS_KIND_CAPTURE_PULLED=63 frame=1 wall=11',
        '[Bridge] stage id=3 frame=182 wall=12',
        '[Bridge] roster entry=0 kind=0 nana=false frame=182 wall=12',
        '[Bridge] roster entry=1 kind=84 nana=false frame=182 wall=12',
        '[Bridge] item entry=0 held=true kind=429 frame=651 wall=13',
        '[Bridge] speed fps=59.94 ratio=0.9990 frames=599 ms=9994 frame=700 wall=14',
        '[TrainingRooms] state pos entry=0 kind=0 x=-35.999996 y=0.010259508 lr=1 dmg=0 status=0 situation=0 frame=701 wall=15',
        '[TrainingRooms] state pos entry=1 kind=84 x=36 y=0.010259508 lr=-1 dmg=12.5 status=63 situation=2 frame=701 wall=15',
    ]

    def test_bridge_state_parses_every_native_read(self):
        import switch_hw
        state = switch_hw.parse_bridge_state(self.BRIDGE_LINES)
        self.assertEqual((state['frame'], state['wall_ms']), (701, 15))
        self.assertEqual(state['stage'], 3)
        self.assertEqual(state['roster'], {0: {'kind': 0, 'nana': False}, 1: {'kind': 84, 'nana': False}})
        self.assertEqual(state['items'], {0: {'held': True, 'kind': 429}})
        self.assertEqual(state['speed'], {'fps': 59.94, 'ratio': 0.999, 'frames': 599, 'ms': 9994})
        self.assertEqual(state['pos'][1], {'kind': 84, 'x': 36.0, 'y': 0.010259508, 'lr': -1.0, 'dmg': 12.5, 'status': 63,
                                           'situation': 2, 'frame': 701})
        self.assertEqual(state['status_ids'], {'FIGHTER_STATUS_KIND_WAIT': 0, 'FIGHTER_STATUS_KIND_CAPTURE_PULLED': 63})

    V2_LINES = [
        '[Bridge] caps proto=2 peek chain frame scene fighters input inject watch inject_net input_net tr=none frame=5 wall=1',
        '[Bridge] scene mode=0x04040000 melee=0x20 init=1 ldn=none class=training frame=40 wall=2',
        '[Bridge] fighter entry=0 kind=0 x=-36 y=0.01 lr=1 dmg=0 status=0 situation=0 stock=-1 dead=0 item=-1 frame=41 wall=2',
        '[Bridge] fighter entry=1 kind=84 x=36 y=0.01 lr=-1 dmg=3.5 status=11 situation=0 stock=-1 dead=0 item=-1 frame=41 wall=2',
        '[Bridge] input slot=0 src=final buttons=0x4 lx=0 ly=0 rx=0 ry=0 frame=42 wall=3',
        '[Bridge] input slot=0 src=net buttons=0x0 lx=-120 ly=0 rx=0 ry=0 frame=42 wall=3',
        '[Bridge] watch addr=main+0x53050f0 len=4 val=0x4040000 left=3 frame=43 wall=3',
        '[Bridge] fighter entry=1 gone frame=44 wall=4',
        '[Bridge] scene mode=0x02010000 melee=0x0d init=1 ldn=station_connected class=local_wireless tr_frame=7 frame=50 wall=5',
    ]

    def test_bridge_state_parses_the_v2_lines(self):
        import switch_hw
        state = switch_hw.parse_bridge_state(self.V2_LINES)
        self.assertEqual(state['caps'], {'proto': 2, 'tr': 'none', 'features': [
            'peek', 'chain', 'frame', 'scene', 'fighters', 'input', 'inject', 'watch', 'inject_net', 'input_net']})
        self.assertEqual(state['scene'], {'mode': 0x02010000, 'melee': 0x0d, 'init': True, 'ldn': 'station_connected',
                                          'class': 'local_wireless', 'frame': 50})
        self.assertEqual(sorted(state['fighters']), [0], 'entry 1 went away')
        self.assertEqual(state['fighters'][0], {'kind': 0, 'x': -36.0, 'y': 0.01, 'lr': 1.0, 'dmg': 0.0, 'status': 0,
                                                'situation': 0, 'stock': -1, 'dead': 0, 'item': -1, 'frame': 41})
        self.assertEqual(state['inputs']['final'][0], {'buttons': 4, 'lx': 0, 'ly': 0, 'rx': 0, 'ry': 0, 'frame': 42})
        self.assertEqual(state['inputs']['net'][0]['lx'], -120)
        self.assertEqual(state['watch'], {'addr': 'main+0x53050f0', 'len': 4, 'val': 0x4040000, 'left': 3, 'frame': 43})

    def test_v2_requests_go_through_their_own_spool_files_and_other_verbs_are_dropped(self):
        import time
        import switch_hw
        run = Path(tempfile.mkdtemp())
        spool = run / 'bridge'
        switch_hw.bridge_v2(run, 'scene', 'inject 0 AAAA')
        time.sleep(0.002)
        (spool / 'bridge-v2-x.req').write_bytes(b'inject_start 0 at=next\nrestore\n\nwatch main+0x53050f0 4 count=5\n')
        lines = switch_hw.spool_requests(spool)
        self.assertEqual(lines, [b'scene', b'inject 0 AAAA', b'inject_start 0 at=next', b'watch main+0x53050f0 4 count=5'])
        self.assertEqual(list(spool.iterdir()), [])

    def test_v2_spool_forwards_bridge_toggle_verbs_but_not_restore(self):
        import switch_hw

        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp)
            (spool / 'bridge-v2-toggle.req').write_bytes(b'bridge status\nbridge off\nrestore\n')
            self.assertEqual(switch_hw.spool_requests(spool), [b'bridge status', b'bridge off'])

    def test_file_capture_mirrors_the_stream_and_hands_requests_over_whole(self):
        import threading
        import switch_hw
        with tempfile.TemporaryDirectory() as tmp:
            run, sd = Path(tmp) / 'run', Path(tmp) / 'sd'
            sd.mkdir()
            stream = sd / 'stream.log'
            stream.write_bytes(b'[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 frame=0 wall=1\n[Bridge] scene mo')
            stop = threading.Event()
            worker = threading.Thread(target=switch_hw.capture_bridge_file, args=(run, sd, Path(tmp) / 'console.log'),
                                      kwargs={'stop': stop.is_set, 'poll_s': 0.01})
            worker.start()
            try:
                for _ in range(300):  # the opening requests arrive as one whole file
                    if (sd / 'requests.txt').exists():
                        break
                    time.sleep(0.01)
                self.assertEqual((sd / 'requests.txt').read_bytes(), b'backup\nfile settings.json\n')
                (sd / 'requests.txt').unlink()  # the bridge takes it
                switch_hw.bridge_v2(run, 'caps')
                with stream.open('ab') as f:
                    f.write(b'de=0x04040000 melee=0x20 init=1 ldn=none class=training frame=3 wall=2\n')
                for _ in range(300):
                    if (sd / 'requests.txt').exists() and 'class=training' in (run / 'bridge.log').read_text():
                        break
                    time.sleep(0.01)
                self.assertEqual((sd / 'requests.txt').read_bytes(), b'caps\n')
            finally:
                stop.set()
                worker.join()
            lines = (run / 'bridge.log').read_text().splitlines()
            self.assertEqual(lines[0], '[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 frame=0 wall=1')
            self.assertEqual(lines[1], '[Bridge] scene mode=0x04040000 melee=0x20 init=1 ldn=none class=training frame=3 wall=2')

    def test_wait_scene_asks_fresh_and_waits_for_the_class(self):
        import threading
        import switch_hw
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'bridge').mkdir()
            log = run / 'bridge.log'
            log.write_text(self.V2_LINES[1] + '\n')

            def bridge():
                for _ in range(200):
                    if any(p.read_bytes() == b'scene' for p in (run / 'bridge').glob('bridge-v2*.req')):
                        for p in (run / 'bridge').glob('bridge-v2*.req'):
                            p.unlink()
                        with log.open('a') as f:
                            f.write(self.V2_LINES[-1] + '\n')
                        return
                    time.sleep(0.02)

            worker = threading.Thread(target=bridge)
            worker.start()
            scene = switch_hw.wait_scene(run, 'local_wireless', cap=5)
            worker.join()
            self.assertEqual(scene['mode'], 0x02010000)
            with self.assertRaisesRegex(RuntimeError, 'scene class training not reached'):
                switch_hw.wait_scene(run, 'training', cap=0.5)

    def test_bridge_state_newest_line_wins(self):
        import switch_hw
        state = switch_hw.parse_bridge_state(self.BRIDGE_LINES + ['[Bridge] stage id=8 frame=900 wall=20',
                                                                  '[Bridge] item entry=0 held=false kind=-1 frame=901 wall=21'])
        self.assertEqual((state['stage'], state['items'][0]['held'], state['frame']), (8, False, 901))

    def test_bridge_state_fresh_reads_wait_for_their_answers(self):
        import tempfile
        import threading
        import switch_hw
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'bridge').mkdir()
            log = run / 'bridge.log'
            log.write_text(self.BRIDGE_LINES[0] + '\n')

            def bridge():  # the capture: a request file in the spool becomes the bridge's answer in bridge.log
                for _ in range(100):
                    if (run / 'bridge' / 'diag-pos.req').exists() and (run / 'bridge' / 'bridge-speed.req').exists():
                        with log.open('a') as f:
                            f.write('[Bridge] req pos result=ok entries=0x3 frame=700 wall=14\n')
                            f.write('\n'.join(self.BRIDGE_LINES[6:]) + '\n')
                        return
                    time.sleep(0.02)

            worker = threading.Thread(target=bridge)
            worker.start()
            state = switch_hw.bridge_state(run, fresh=['pos', 'speed'], cap=5)
            worker.join()
            self.assertEqual(sorted(state['pos']), [0, 1])
            self.assertEqual(state['speed']['fps'], 59.94)
            with self.assertRaisesRegex(RuntimeError, r"no answer for \['roster'\]"):
                switch_hw.bridge_state(run, fresh=['roster'], cap=0.3)

    def test_the_bridges_own_status_names_fill_status_ids_without_tr(self):
        import switch_hw
        lines = ['[Bridge] status_names FIGHTER_STATUS_KIND_NONE=-1 FIGHTER_STATUS_KIND_WAIT=0 frame=5 wall=1',
                 '[Bridge] status_names FIGHTER_STATUS_KIND_JUMP_SQUAT=10 FIGHTER_STATUS_KIND_JUMP=11 frame=5 wall=1',
                 '[Bridge] req status_names result=ok count=4 frame=5 wall=1']
        state = switch_hw.parse_bridge_state(lines)
        self.assertEqual(state['status_ids'], {'FIGHTER_STATUS_KIND_NONE': -1, 'FIGHTER_STATUS_KIND_WAIT': 0,
                                               'FIGHTER_STATUS_KIND_JUMP_SQUAT': 10, 'FIGHTER_STATUS_KIND_JUMP': 11})
        self.assertIn('status_names', switch_hw.V2_VERBS)
        self.assertEqual(switch_hw.FRESH['status_names'][2], b'status_names')

    def test_bridge_bodies_read_the_v2_fighters_in_any_battle_and_tr_pos_only_without_them(self):
        import threading
        import switch_hw
        for caps, want in ((self.V2_LINES[0], 'fighters'), (self.BRIDGE_LINES[0], 'pos')):
            with tempfile.TemporaryDirectory() as tmp:
                run = Path(tmp)
                (run / 'bridge').mkdir()
                log = run / 'bridge.log'
                log.write_text(caps + '\n')
                asked = []

                def bridge():  # the capture: a spool file becomes the bridge's (or TR's) answer in bridge.log
                    for _ in range(300):
                        if not (run / 'bridge').exists():
                            return
                        for p in (run / 'bridge').iterdir():
                            asked.append(p.name if p.name == 'diag-pos.req' else p.read_bytes().decode())
                            p.unlink()
                            with log.open('a') as f:
                                if asked[-1] == 'fighters':
                                    f.write('\n'.join(self.V2_LINES[2:4]) + '\n[Bridge] fighters entries=0x3 count=2 '
                                            'frame=41 wall=2\n')
                                    return
                                if asked[-1] == 'diag-pos.req':
                                    f.write('[Bridge] req pos result=ok entries=0x3 frame=700 wall=14\n'
                                            + '\n'.join(self.BRIDGE_LINES[7:]) + '\n')
                                    return
                                # a slice 2 bridge has no `caps`: no answer
                        time.sleep(0.02)

                worker = threading.Thread(target=bridge)
                worker.start()
                state = switch_hw.bridge_bodies(run, cap=1.0)
                worker.join()
                self.assertEqual(asked, ['fighters'] if want == 'fighters' else ['caps', 'diag-pos.req'])
                self.assertEqual(state['pos'], state[want])
                self.assertEqual(state['pos'][1]['dmg'], 3.5 if want == 'fighters' else 12.5)
                self.assertEqual(state['bodies_from'], want)

    def test_external_log_needs_a_connection_line(self):
        import switch_hw
        folder = Path(tempfile.mkdtemp())
        mine, theirs, dead = folder / 'a.log', folder / 'b.log', folder / 'c.log'
        mine.write_text('[capture] connected 192.168.0.224:6969 2026-11-16T19:20:09Z\nTCP Socket Connected.\n')
        theirs.write_text('### 2026-11-16T19:20:09.266Z skyline_tcplog start host=192.168.0.224:6969\n'
                          '### 2026-11-16T19:20:09.269Z connected (#1)\nTCP Socket Connected.\n')
        dead.write_text('### 2026-11-16T19:20:09.266Z skyline_tcplog start host=192.168.0.224:6969\n')
        self.assertTrue(switch_hw.external_log_connected(mine).startswith('[capture] connected'))
        self.assertIn('connected (#1)', switch_hw.external_log_connected(theirs))
        self.assertIsNone(switch_hw.external_log_connected(dead))
        self.assertIsNone(switch_hw.external_log_connected(folder / 'missing.log'))

    def test_line_writer_keeps_lines_whole(self):
        import io
        import switch_hw
        buf = io.BytesIO()
        w = switch_hw.LineWriter(buf)
        w.feed(b'plugin=training_rooms/0.2.0 game=13.0.5 res')
        self.assertEqual(buf.getvalue(), b'')
        w.feed(b'ult=installed\r\nnext')
        self.assertEqual(buf.getvalue(), b'plugin=training_rooms/0.2.0 game=13.0.5 result=installed\n')
        w.close()
        self.assertTrue(buf.getvalue().endswith(b'next\n'))

    def test_capture_reconnects_and_stops(self):
        import switch_hw
        chunks = [[b'a\nb', b'c\n'], [b'd\n']]
        calls = []

        class Sock:
            def __init__(self, data):
                self.data = list(data)

            def settimeout(self, t):
                pass

            def recv(self, n):
                return self.data.pop(0) if self.data else b''

            def close(self):
                pass

        def connect(addr, timeout):
            calls.append(addr)
            if not chunks:
                raise OSError('refused')
            return Sock(chunks.pop(0))
        path = Path(tempfile.mkdtemp()) / 'skyline.log'
        switch_hw.capture_log(path, 'h', 1, stop=lambda: len(calls) >= 3, connect=connect, retry_s=0)
        lines = path.read_text().splitlines()
        self.assertEqual([l for l in lines if not l.startswith('[capture]')], ['a', 'bc', 'd'])
        self.assertEqual(sum(l.startswith(switch_hw.CONNECTED + ' h:1 ') for l in lines), 2)  # up's live line

    def test_capture_drops_a_silent_connection_and_reconnects(self):
        import switch_hw
        calls, now = [], [0.0]

        class Silent:
            def settimeout(self, t):
                pass

            def recv(self, n):
                now[0] += 50.0
                raise switch_hw.socket.timeout()

            def close(self):
                pass

        def connect(addr, timeout):
            calls.append(addr)
            return Silent()
        path = Path(tempfile.mkdtemp()) / 'skyline.log'
        switch_hw.capture_log(path, 'h', 1, stop=lambda: len(calls) >= 3, connect=connect, retry_s=0, idle_s=120,
                              clock=lambda: now[0])
        self.assertEqual(path.read_text().count('idle 120 s: reconnecting'), 2)  # half-open: not forever

    def test_capture_keeps_a_quiet_connection_by_default(self):
        """Skyline takes one client per boot: the default never drops a quiet live connection (session-2 17:53Z)."""
        import switch_hw
        calls, now = [], [0.0]

        class Quiet:
            def settimeout(self, t):
                pass

            def setsockopt(self, *a):
                pass

            def recv(self, n):
                now[0] += 50.0
                raise switch_hw.socket.timeout()

            def close(self):
                pass

        def connect(addr, timeout):
            calls.append(addr)
            return Quiet()
        path = Path(tempfile.mkdtemp()) / 'skyline.log'
        switch_hw.capture_log(path, 'h', 1, stop=lambda: now[0] > 1000, connect=connect, retry_s=0,
                              clock=lambda: now[0])
        self.assertEqual(len(calls), 1)
        self.assertNotIn('reconnecting', path.read_text())

    def test_up_refuses_when_obs_cannot_split_per_row(self):
        import contextlib
        import io
        import switch_hw
        folder = Path(tempfile.mkdtemp())
        split = {'ok': False, 'why': 'simple_file_output.split_file is None: manual file splitting is off'}
        calls = []

        class Client:
            def request(self, method, params=None):
                calls.append(method)
                if method == 'ui.screenshot':
                    Path(params['path']).write_bytes(b'png')
                if method == 'recording.split_check':
                    return {'ok': True, 'result': dict(split)}
                return {'ok': True, 'result': {'version': 'x'}}

            def close(self):
                pass
        saved = (switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen)
        switch_hw.CLAIM = folder / 'claim'
        switch_hw.holders = lambda device: []
        switch_hw._client = lambda device: Client()
        live = [True]

        def popen(args, **k):  # the capture: its connected line arrives (or, with live off, nothing does)
            if live[0]:
                with open(args[4], 'a') as log:
                    log.write('[capture] connected h:1 now\n')
            return type('P', (), {'pid': 4242, 'terminate': lambda self: None})()
        switch_hw.subprocess.Popen = popen
        try:
            with self.assertRaises(SystemExit) as cm:
                switch_hw.up(folder / 'run', '/dev/null', 'h:1', 'test')
            self.assertIn('split-enable', str(cm.exception))
            self.assertFalse((folder / 'claim').exists())  # released on the refusal
            self.assertNotIn('SetOutputSettings', calls)    # read-only: up never changes OBS
            with contextlib.redirect_stdout(io.StringIO()):
                switch_hw.up(folder / 'run2', '/dev/null', 'h:1', 'test', allow_no_split=True)
            journal = json.loads((folder / 'run2' / 'journal.json').read_text())
            self.assertFalse(journal['obs_split']['ok'])
            (folder / 'claim').unlink()
            split['ok'], split['why'] = True, None
            with contextlib.redirect_stdout(io.StringIO()):
                switch_hw.up(folder / 'run3', '/dev/null', 'h:1', 'test')
            journal = json.loads((folder / 'run3' / 'journal.json').read_text())
            self.assertTrue(journal['obs_split']['ok'])
            self.assertEqual(journal['log_live'], '[capture] connected h:1 now')
            (folder / 'claim').unlink()
            live[0] = False  # no Skyline line within the cap: refused, claim released
            with self.assertRaises(SystemExit) as cm:
                switch_hw.up(folder / 'run4', '/dev/null', 'h:1', 'test', log_cap_s=0.5)
            self.assertIn('no Skyline log line', str(cm.exception))
            self.assertFalse((folder / 'claim').exists())
        finally:
            switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen = saved

    def test_bridge_spool_turns_eden_request_files_into_bridge_requests(self):
        import base64
        import time
        import switch_hw
        spool = Path(tempfile.mkdtemp())
        for name, data in [('diag-settings-next.json', b'{"menu":{}}'), ('diag-pos.req', b''), ('diag-item.req', b' 7\n'),
                           ('bridge-file.req', b'save_states.json\nlibrary/a.json\n'), ('diag-save.req', b''),
                           ('diag-load.req', b''), ('settings.json', b'{}')]:
            (spool / name).write_bytes(data)
            time.sleep(0.002)
        lines = switch_hw.spool_requests(spool)
        self.assertEqual(lines, [b'set ' + base64.b64encode(b'{"menu":{}}'), b'pos', b'item 7', b'file save_states.json',
                                 b'file library/a.json', b'save', b'load'])
        self.assertEqual(sorted(p.name for p in spool.iterdir()), ['settings.json'])  # requests consumed, the mirror kept
        self.assertEqual(switch_hw.spool_requests(spool / 'missing'), [])

    def test_bridge_mirror_writes_settings_and_keeps_lines_whole(self):
        import base64
        import hashlib
        import io
        import switch_hw
        run = Path(tempfile.mkdtemp())
        blog, clog = io.BytesIO(), io.BytesIO()
        mirror = switch_hw.BridgeMirror(run, [blog, clog])
        body = b'{"menu": {"input_delay": [3]}}' * 20
        for seq, start in enumerate(range(0, len(body), 384)):
            mirror.line(b'[Bridge] file_data name=settings.json seq=%d b64=' % seq + base64.b64encode(body[start:start + 384]))
        digest = hashlib.sha256(body).hexdigest().encode()
        mirror.line(b'[Bridge] file_end name=settings.json bytes=%d sha256=%s result=ok frame=9 wall=10\r' % (len(body), digest))
        self.assertEqual((run / 'bridge' / 'settings.json').read_bytes(), body)
        self.assertEqual((run / 'bridge' / 'files' / 'settings.json').read_bytes(), body)
        mirror.line(b'[Bridge] file_end name=x.json bytes=1 sha256=' + b'0' * 64 + b' result=ok')
        mirror.line(b'[Bridge] file_data name=cut.json seq=0 b64=Zm9vYm')  # a chunk cut short
        mirror.line(b'[Bridge] file_end name=cut.json bytes=4 sha256=' + hashlib.sha256(b'foob').hexdigest().encode() + b' result=ok')
        self.assertFalse((run / 'bridge' / 'files' / 'cut.json').exists())
        self.assertFalse((run / 'bridge' / 'files' / 'x.json').exists())
        mirror.line(b'[TrainingRooms] settings.json written by the writer thread frame=12 wall=13')
        mirror.line(b'[Ui] press A page=3 cursor=0 depth=1 screen=Page row=None recording= frame=14 wall=15')
        self.assertEqual(mirror.wanted, [b'file settings.json'])
        text = blog.getvalue().decode()
        self.assertEqual(text, clog.getvalue().decode())
        self.assertNotIn('file_data', text)
        self.assertIn(' mirrored=true\n', text)
        self.assertIn('x.json bytes=1 sha256=' + '0' * 64 + ' result=ok mirrored=false\n', text)
        self.assertTrue(text.endswith('recording= frame=14 wall=15\n'))

    def test_capture_bridge_sends_hello_and_spool_then_reconnects(self):
        import switch_hw
        run = Path(tempfile.mkdtemp())
        console = run / 'skyline.log'
        (run / 'bridge').mkdir()
        (run / 'bridge' / 'diag-pos.req').write_bytes(b'')
        sent, calls = [], []
        chunks = [[b'[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 frame=0 wall=1\n[TrainingRooms] state pos entry=0 kind=0 ',
                   b'x=1 y=2 lr=1 dmg=0 status=0 situation=0 frame=5 wall=6\n']]

        class Sock:
            def __init__(self, data):
                self.data = list(data)

            def settimeout(self, t):
                pass

            def sendall(self, b):
                sent.append(b)

            def recv(self, n):
                return self.data.pop(0) if self.data else b''

            def close(self):
                pass

        def connect(addr, timeout):
            calls.append(addr)
            if not chunks:
                raise OSError('refused')
            return Sock(chunks.pop(0))
        switch_hw.capture_bridge(run, 'h', 6970, console, stop=lambda: len(calls) >= 2, connect=connect, retry_s=0)
        self.assertEqual(sent[0], b'backup\nfile settings.json\n')
        self.assertIn(b'pos\n', sent)
        log = (run / 'bridge.log').read_text()
        self.assertIn('[TrainingRooms] state pos entry=0 kind=0 x=1 y=2 lr=1 dmg=0 status=0 situation=0 frame=5 wall=6\n', log)
        self.assertEqual(console.read_text(), log)
        self.assertEqual(json.loads((run / 'bridge.requests.jsonl').read_text().splitlines()[0])['request'], 'pos')
        import fcntl
        with (run / 'bridge.lock').open('w') as held:  # a second capture of the same run is refused
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(SystemExit):
                switch_hw.capture_bridge(run, 'h', 6970, console, stop=lambda: True, connect=connect, retry_s=0)

    def test_read_state_runs_unchanged_on_a_bridge_instance(self):
        import threading
        run = Path(tempfile.mkdtemp())
        (run / 'bridge').mkdir()
        log = run / 'bridge.log'
        log.write_text('[TrainingRooms] state pos entry=0 kind=9 x=0 y=0 lr=1 dmg=0 status=0 situation=0 frame=1 wall=1\n')
        instance = {'socket': 'usb:///dev/ttyACM0', 'diag_log': str(log), 'settings': str(run / 'bridge' / 'settings.json')}
        ctx = type('Ctx', (), {'instance': instance, 'check_stop': lambda self: None})()

        def bridge():
            for _ in range(100):
                if (run / 'bridge' / 'diag-pos.req').exists():
                    with log.open('a') as f:
                        f.write('[TrainingRooms] state pos entry=1 kind=66 x=-65.37304 y=0 lr=-1 dmg=12.5 status=27 situation=2 frame=8 wall=9\n')
                        f.write('[TrainingRooms] state pos entry=0 kind=0 x=6.5219345 y=0 lr=1 dmg=0 status=0 situation=0 frame=8 wall=9\n')
                    return
                threading.Event().wait(0.02)
        t = threading.Thread(target=bridge)
        t.start()
        bodies = routes.read_state(ctx, cap=3)
        t.join()
        self.assertEqual(bodies[0]['kind'], 0)  # the stale entry-0 line before the request is not used
        self.assertEqual((bodies[1]['kind'], bodies[1]['x'], bodies[1]['lr'], bodies[1]['status']), (66, -65.37304, -1.0, 27))

    def test_up_with_a_bridge_gives_the_instance_eden_keys(self):
        import contextlib
        import io
        import switch_hw
        folder = Path(tempfile.mkdtemp())
        popen = []

        class Client:
            def request(self, method, params=None):
                if method == 'ui.screenshot':
                    Path(params['path']).write_bytes(b'png')
                return {'ok': True, 'result': {'version': 'x', 'ok': True}}

            def close(self):
                pass
        saved = (switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen)
        switch_hw.CLAIM = folder / 'claim'
        switch_hw.holders = lambda device: []
        switch_hw._client = lambda device: Client()
        def fake_popen(args, **k):  # the Skyline capture writes its connected line (up waits for a live log line)
            popen.append(args)
            if 'capture-log' in args:
                with open(args[args.index('capture-log') + 1], 'a') as log:
                    log.write('[capture] connected h:1 now\n')
            return type('P', (), {'pid': 4242 + len(popen), 'terminate': lambda self: None})()
        switch_hw.subprocess.Popen = fake_popen
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                switch_hw.up(folder / 'run', '/dev/null', 'h:1', 'test', bridge_host='192.168.0.224:6970')
            sw = json.loads((folder / 'run' / 'instances.json').read_text())['sw']
            self.assertEqual(sw['diag_log'], str(folder / 'run' / 'bridge.log'))
            self.assertEqual(Path(sw['settings']).parent, folder / 'run' / 'bridge')
            self.assertTrue(Path(sw['bridge_files']).is_dir())
            self.assertIn('capture-bridge', popen[1])
            self.assertEqual(popen[1][popen[1].index('--console') + 1], sw['console_log'])
            self.assertEqual(json.loads((folder / 'run' / 'journal.json').read_text())['bridge_pid'], 4244)
        finally:
            switch_hw.CLAIM, switch_hw.holders, switch_hw._client, switch_hw.subprocess.Popen = saved

    def test_an_unrestored_backup_is_restored_before_a_new_one(self):
        import io
        import switch_hw
        mirror = switch_hw.BridgeMirror(Path(tempfile.mkdtemp()), [io.BytesIO()])
        mirror.line(b'[Bridge] req backup result=refused reason=backup_open frame=0 wall=1')
        self.assertEqual(mirror.wanted, [b'restore', b'backup'])
        mirror.line(b'[Bridge] req backup result=refused reason=backup_open frame=0 wall=2')
        self.assertEqual(mirror.wanted, [b'restore', b'backup'], 'once per connection, never a loop')

    def test_down_restores_the_fixture_and_keeps_a_receipt(self):
        import switch_hw
        run = Path(tempfile.mkdtemp())
        sent = []

        class Sock:
            def __init__(self):
                self.data = [b'[Bridge] hello x\n[Bridge] req restore result=ok files=3 written=1 removed=0 frame=1 wall=2\n']

            def settimeout(self, t):
                pass

            def sendall(self, b):
                sent.append(b)

            def recv(self, n):
                return self.data.pop(0) if self.data else b''

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        receipt = switch_hw.restore_fixture(run, {'bridge_host': 'h:6970'}, connect=lambda a, timeout: Sock())
        self.assertTrue(receipt['ok'])
        self.assertEqual(sent, [b'restore\n'])
        self.assertTrue(json.loads((run / 'bridge-restore.json').read_text())['ok'])
        failed = switch_hw.bridge_request('h', 1, 'restore', cap=0.5, connect=lambda a, timeout: (_ for _ in ()).throw(OSError()))
        self.assertIsNone(failed)

        def replying(line):
            sock = Sock()
            sock.data = [b'[Bridge] hello x\n' + line + b'\n']
            return lambda a, timeout: sock
        # switch-hw-sol-3 14:11Z: after an explicit successful restore the backup is gone, so Down's restore replies
        # no_backup; that is nothing outstanding, not a dirty fixture. A pending restore is still dirty.
        closed = switch_hw.restore_fixture(run, {'bridge_host': 'h:6970'},
                                           connect=replying(b'[Bridge] req restore result=refused reason=no_backup frame=9 wall=9'))
        self.assertEqual((closed['ok'], closed['state']), (True, 'no_open_backup'))
        pending = switch_hw.restore_fixture(run, {'bridge_host': 'h:6970'}, connect=replying(
            b'[Bridge] req restore result=pending reason=live_settings_refused_scene backup=kept frame=9 wall=9'))
        self.assertEqual((pending['ok'], pending['state']), (False, 'incomplete'))

    def test_a_failed_restore_keeps_the_claim_and_fails_loudly(self):
        from unittest import mock
        import switch_hw
        run = Path(tempfile.mkdtemp())
        claim = run / 'claim.json'
        claim.write_text(json.dumps({'run': str(run)}))
        (run / 'journal.json').write_text(json.dumps({'device': 'x', 'claim': str(claim), 'bridge_host': 'h:6970'}))
        with mock.patch.object(switch_hw, '_client', side_effect=OSError('no pad')), \
                mock.patch.object(switch_hw, 'restore_fixture', return_value={'ok': False, 'restore': None}), \
                mock.patch.object(switch_hw.time, 'sleep'):
            with self.assertRaises(SystemExit):
                switch_hw.down(run)
        self.assertTrue(claim.is_file(), 'the console stays claimed')
        self.assertIn('fixture restore incomplete', json.loads(claim.read_text())['dirty'])
        self.assertFalse(json.loads((run / 'journal.json').read_text())['down_result']['released'])
        with mock.patch.object(switch_hw, '_client', side_effect=OSError('no pad')), \
                mock.patch.object(switch_hw, 'restore_fixture', return_value={'ok': True, 'restore': 'ok'}), \
                mock.patch.object(switch_hw.time, 'sleep'):
            switch_hw.down(run)
        self.assertFalse(claim.is_file(), 'a verified restore releases it')

    def test_factory_routes_usb_to_switch_remote(self):
        folder = Path(tempfile.mkdtemp())
        (folder / 'switch_remote.py').write_text('def make_client(s):\n    return ("fake", s)\n')
        old = os.environ.get('SWITCH_REMOTE_DIR')
        os.environ['SWITCH_REMOTE_DIR'] = str(folder)
        sys.modules.pop('switch_remote', None)
        try:
            self.assertEqual(runner.default_client_factory()('usb:///dev/ttyACM0'), ('fake', 'usb:///dev/ttyACM0'))
        finally:
            sys.modules.pop('switch_remote', None)
            if old is None:
                os.environ.pop('SWITCH_REMOTE_DIR', None)
            else:
                os.environ['SWITCH_REMOTE_DIR'] = old


class SwitchGateTests(unittest.TestCase):
    def test_recorder_evidence_stills(self):
        from PIL import Image
        folder = Path(tempfile.mkdtemp())
        spec = routes._switch_spec('tr-menu')
        frame = Image.new('RGB', (1280, 720))
        frame.paste(Image.open(spec['reference']).convert('RGB'), tuple(spec['roi'][:2]))
        good, blank = folder / 'good.png', folder / 'blank.png'
        frame.save(good)
        Image.new('RGB', (1280, 720)).save(blank)
        shots = []

        class R:
            plan = {'gates': {}}

            @staticmethod
            def compare_region(*a):
                return runner.Runner.compare_region(*a)

        ctx = FakeCtx.__new__(FakeCtx)
        ctx.runner, ctx.on, ctx.row_dir = R(), 'sw', folder
        ctx.instance = {'socket': 'usb:///dev/ttyACM0'}
        counter = []

        def shot(name):  # a fresh file per still, as runner.shot makes (the gate deletes failed stills)
            counter.append(1)
            path = folder / f'shot-{len(counter)}.png'
            path.write_bytes(Path(shots.pop(0)).read_bytes())
            return str(path)
        ctx.shot = shot
        self.assertTrue(routes.on_switch(ctx))
        def evidence(cap):
            return routes._frame_gate(ctx, 'tr-menu-evidence', spec, cap)

        shots[:] = [str(blank), str(good)]
        passed = evidence(5)
        self.assertEqual(Path(passed).read_bytes(), good.read_bytes())
        shots[:] = [str(blank)] * 50
        with self.assertRaises(RuntimeError):
            evidence(0.5)
        # The console's auto-dim (about 1/3 the luminance) still passes; a dark unrelated frame doesn't.
        dim = folder / 'dim.png'
        frame.point(lambda v: int(v * 0.33)).save(dim)
        shots[:] = [str(dim)]
        self.assertEqual(Path(evidence(1)).read_bytes(), dim.read_bytes())
        gray = folder / 'gray.png'
        Image.new('RGB', (1280, 720), (20, 20, 20)).save(gray)
        shots[:] = [str(gray)] * 50
        with self.assertRaises(RuntimeError):
            evidence(0.5)


class SwitchDiscriminationTests(unittest.TestCase):
    """Every real-Switch gate passes its own screen, normal and dimmed (the console's ~1/3 auto-dim), and fails
    every other gate's screen content placed at its ROI, normal and dimmed: HOME vs main vs TR menu vs HUD and the
    rest. Built from the committed references only (no game stills in the repo)."""

    @staticmethod
    def frame_with(spec, content, factor):
        from PIL import Image
        frame = Image.new('RGB', (1280, 720))
        x0, y0, x1, y1 = spec['roi']
        frame.paste(content.resize((x1 - x0, y1 - y0)), (x0, y0))
        return frame.point(lambda v: int(v * factor)) if factor != 1 else frame

    def test_every_gate_only_matches_its_own_screen(self):
        from PIL import Image
        folder = Path(tempfile.mkdtemp())
        gates = sorted(routes.SWITCH_REFS)
        self.assertIn('home', gates)
        refs = {g: Image.open(routes._switch_spec(g)['reference']).convert('RGB') for g in gates}
        wrong = []
        for g in gates:
            spec = routes._switch_spec(g)
            for factor in (1, 0.33, 0.35):
                own = folder / f'{g}-own-{factor}.png'
                self.frame_with(spec, refs[g], factor).save(own)
                diff, note = routes.match_frame(own, spec, runner.Runner.compare_region)
                self.assertLessEqual(diff, spec['tolerance'], f'{g} own x{factor}: {diff} {note}')
                for h in gates:
                    if h == g:
                        continue
                    other = folder / f'{g}-as-{h}-{factor}.png'
                    self.frame_with(spec, refs[h], factor).save(other)
                    diff, _ = routes.match_frame(other, spec, runner.Runner.compare_region)
                    if diff <= spec['tolerance']:
                        wrong.append((g, h, factor, round(diff, 1)))
        self.assertEqual(wrong, [], 'a gate passed another screen')


class PreloadNoticeTests(unittest.TestCase):
    """TR's preloader notice (switch-hw-session-1 T-01, 15:03Z: an R lost at "Fighters / Loading 4/7"): the
    classifier and the preload_clear gate, on frames built from the committed notice references only."""

    BOX = (310, 290, 970, 387)

    @staticmethod
    def ref(name):
        from PIL import Image
        entry = json.loads((routes.ROUTES / 'refs' / 'hw-refs.json').read_text())[name]
        return Image.open(routes.ROUTES / 'refs' / f'{name}.png').convert('RGB'), entry['roi']

    def frame(self, box=True, header=True, word='Loading', dx=0, dim=1.0, menu=False):
        from PIL import Image, ImageDraw
        im = Image.new('RGB', (1280, 720), (13, 13, 13) if menu else (212, 212, 212))
        draw = ImageDraw.Draw(im)
        if not menu:
            for x in range(0, 1280, 48):  # the Training stage grid
                draw.line([(x, 0), (x, 720)], fill=(150, 150, 150), width=2)
            for y in range(0, 720, 48):
                draw.line([(0, y), (1280, y)], fill=(150, 150, 150), width=2)
        else:
            draw.text((60, 140), 'Reactions (4)   Mash   0 of 30', fill=(230, 230, 230))
        if box:
            draw.rectangle(self.BOX, fill=(13, 13, 13))
            if header:
                head, roi = self.ref('hw-notice-fighters')
                im.paste(head, tuple(roi[:2]))
            if word:
                img, roi = self.ref('hw-notice-loading')
                if word == 'Load':  # "Loaded n/m" and "All loaded" share it: blank "ing" (from column 37)
                    img = img.copy()
                    ImageDraw.Draw(img).rectangle((37, 0, img.size[0], img.size[1]), fill=(13, 13, 13))
                elif word == 'Loaded':  # "Load" + an "e"-like (the "o") + the "d", glyph-aligned
                    base = img.copy()
                    img = base.copy()
                    ImageDraw.Draw(img).rectangle((37, 0, img.size[0], img.size[1]), fill=(13, 13, 13))
                    img.paste(base.crop((9, 0, 19, base.size[1])), (38, 0))
                    img.paste(base.crop((28, 0, 37, base.size[1])), (49, 0))
                elif word == 'other':  # some other second line ("Ready 7/7", "Swap P1 once to load")
                    img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                im.paste(img, (roi[0] + dx, roi[1]))
        if dim != 1.0:
            im = im.point(lambda v: int(v * dim))
        return im

    def save(self, im, folder, name):
        path = Path(folder) / f'{name}.png'
        im.save(path)
        return path

    def test_classifier(self):
        folder = tempfile.mkdtemp()
        cases = {
            'loading': dict(), 'loading-right': dict(dx=24), 'loading-left': dict(dx=-30),
            'loading-dim': dict(dim=0.33),
            'final-load': dict(word='Load'), 'final-loaded': dict(word='Loaded'), 'final-other': dict(word='other'),
            'final-blank': dict(word=None), 'final-dim': dict(word='Load', dim=0.35),
            'other-notice': dict(header=False, word='other'), 'stage': dict(box=False),
            'stage-dim': dict(box=False, dim=0.33), 'menu': dict(box=False, menu=True),
            'menu-with-box-pixels': dict(menu=True)}
        want = {'loading': 'loading', 'loading-right': 'loading', 'loading-left': 'loading', 'loading-dim': 'loading',
                'final-load': 'fighters', 'final-loaded': 'fighters', 'final-other': 'fighters',
                'final-blank': 'fighters', 'final-dim': 'fighters', 'other-notice': 'other', 'stage': None,
                'stage-dim': None, 'menu': None, 'menu-with-box-pixels': None}
        got = {name: routes.preload_notice(self.save(self.frame(**kw), folder, name)) for name, kw in cases.items()}
        self.assertEqual(got, want)

    def test_word_margin(self):
        """The "Loading" word stays far from its look-alikes: the limit (0.3) sits between them."""
        word, _ = self.ref('hw-notice-loading')
        ref = routes._ink(word)
        self.assertEqual(routes.ink_mismatch(ref, ref), 0.0)
        for kw in (dict(word='Load'), dict(word='Loaded')):
            im = self.frame(**kw)
            _, roi = self.ref('hw-notice-loading')
            self.assertGreater(routes.ink_mismatch(routes._ink(im.crop(tuple(roi))), ref), 0.4, kw)

    def ctx_with_frames(self, frames, on_look=None):
        out = Path(tempfile.mkdtemp())
        log = out / 'skyline.log'
        log.write_text('[NRO hook] Loaded richter. BindFlag: Lazy\n')
        fake = FakeRunner({'socket': 'usb:///dev/ttyACM0', 'console_log': str(log)}, None)
        fake.out = str(out)
        row = out / 'S-BOOT'
        row.mkdir()
        ctx = FakeCtx(fake, row)
        seq = list(frames)
        looks = []

        def shot(name):
            kw = seq.pop(0) if len(seq) > 1 else seq[0]
            looks.append(kw)
            if on_look:
                on_look(len(looks), log)
            return self.save(self.frame(**kw), row, f'{name}-{len(looks)}')
        ctx.shot = shot
        return ctx, log, looks

    def test_gate_waits_through_gaps_for_the_final_notice(self):
        none, loading, final = dict(box=False), dict(), dict(word='Load')
        # HUD before the preloader starts, Loading, a gap between steps, Loading again, the final notice.
        ctx, log, looks = self.ctx_with_frames([none, loading, none, none, loading, final])
        result = routes.preload_clear(ctx, cap=30, appear=60)
        self.assertEqual(result['preload'], 'final notice')
        self.assertEqual(len(looks), 6)
        self.assertEqual([k for _, k in result['looks']], [None, 'loading', None, 'loading', 'fighters'])
        record = json.loads((Path(ctx.runner.out) / 'routes-preload.json').read_text())
        self.assertEqual(record['size'], log.stat().st_size)
        # Same scene: passes at once, no look.
        again = routes.preload_clear(ctx, cap=30, appear=60)
        self.assertEqual(again['preload'], 'done earlier')
        self.assertEqual(len(looks), 6)
        # Unloads (helpers given back) don't re-arm it; a new load (new scene, fighter change) does.
        with log.open('a') as f:
            f.write('[NRO hook] Unloaded lucas.\n')
        self.assertEqual(routes.preload_clear(ctx, cap=30, appear=60)['preload'], 'done earlier')
        with log.open('a') as f:
            f.write('[NRO hook] Loaded mario. BindFlag: Lazy\n')
        self.assertEqual(routes.preload_clear(ctx, cap=30, appear=60)['preload'], 'final notice')
        self.assertEqual(len(looks), 7)

    def test_no_notice_needs_the_appear_window_and_a_quiet_log(self):
        def loads(n, log):
            if n <= 4:
                with log.open('a') as f:
                    f.write(f'[NRO hook] Loaded fighter{n}. BindFlag: Lazy\n')
        ctx, _, looks = self.ctx_with_frames([dict(box=False)], on_look=loads)
        result = routes.preload_clear(ctx, cap=30, appear=1.0)
        self.assertEqual(result['preload'], 'no notice')
        self.assertGreaterEqual(len(looks), 5)  # not before the loads stopped
        self.assertGreaterEqual(result['seconds'], 1.0)

    def test_a_standing_other_notice_is_no_preload(self):
        ctx, _, _ = self.ctx_with_frames([dict(header=False, word='other')])
        self.assertEqual(routes.preload_clear(ctx, cap=30, appear=0.6)['preload'], 'no notice')

    def test_gate_fails_while_loading_never_ends(self):
        ctx, _, _ = self.ctx_with_frames([dict()])
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            routes.preload_clear(ctx, cap=1, appear=60)

    def test_refs_are_pinned(self):
        for name in ('hw-notice-fighters', 'hw-notice-loading'):
            self.assertIsNotNone(routes._notice_ref(name)[0])


class SessionRecordingTests(unittest.TestCase):
    """plan "session_recording": one continuous recording, marks around every executed row (failed ones too), a
    mark per shot, the stop in run()'s finally; a backend without it runs on without one."""

    class Client:
        def __init__(self, supports=True):
            self.supports, self.calls, self.rec_ms = supports, [], 0

        def request(self, method, params=None):
            self.calls.append(method)
            if method.startswith('recording.session'):
                if not self.supports:
                    return {'ok': False, 'error': {'code': 'unknown_method'}}
                self.rec_ms += 1000
                return {'ok': True, 'result': {'rec_ms': self.rec_ms, 'active': True, 'wall': 1.0,
                                               'path': '/rec.mp4' if method.endswith('stop') else None}}
            if method == 'ui.tree':
                return {'ok': True, 'result': [{'id': 'w', 'class': 'GRenderWindow'}]}
            if method == 'ui.screenshot':
                Path(params['path']).write_bytes(b'png')
            return {'ok': True, 'result': {}}

    def run_plan(self, client, **extra):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'gates': {'never': {'kind': 'log', 'source': 'console_log', 'pattern': 'never', 'cap': 0.3}},
                'session_recording': {'marks': ['row-start', 'row-end', 'shot'], 'chapters': True},
                'rows': [{'id': 'a', 'start': 's', 'end': 's', 'steps': [{'shot': 'one'}]},
                         {'id': 'b', 'start': 's', 'end': 's', 'steps': [{'gate': 'never'}]}], **extra}
        (folder / 'rows.json').write_text(json.dumps(plan))
        log = folder / 'console.log'
        log.write_text('')
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'x', 'console_log': str(log)}}, folder / 'out', 15,
                          client_factory=lambda s: client)
        try:
            r.run()
        except BaseException:
            pass
        return folder / 'out'

    def test_marks_and_stop_even_after_a_failure(self):
        client = self.Client()
        out = self.run_plan(client)
        events = [json.loads(l)['event'] for l in (out / 'recording.jsonl').read_text().splitlines()]
        self.assertEqual(events, ['start', 'row-start', 'shot', 'row-end', 'row-start', 'row-end', 'stop'])
        self.assertEqual(json.loads((out / 'b.json').read_text())['status'], 'FAILED')
        self.assertEqual(len(json.loads((out / 'b.json').read_text())['rec_ms']), 2)
        self.assertEqual(client.calls.count('recording.session_start'), 1)
        self.assertEqual(client.calls[-1] if client.calls[-1].startswith('recording') else
                         [c for c in client.calls if c.startswith('recording')][-1], 'recording.session_stop')

    def test_hardware_gif_is_marked_not_recorded(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'session_recording': {'marks': ['row-start', 'row-end']},
                'rows': [{'id': 'g', 'start': 's', 'end': 's',
                          'steps': [{'gif': {'name': 'clip', 'preset': 'evidence', 'steps': [{'shot': 'mid'}]}}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        client = self.Client()
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'usb:///dev/ttyACM0'}}, folder / 'out', 15,
                          client_factory=lambda s: client)
        r.run()
        events = [json.loads(l) for l in (folder / 'out' / 'recording.jsonl').read_text().splitlines()]
        self.assertEqual([e['event'] for e in events], ['start', 'row-start', 'gif-start', 'gif-end', 'row-end', 'stop'])
        self.assertIn('"gif":"clip"', events[2]['label'])
        self.assertNotIn('recording.start', client.calls)
        self.assertNotIn('recording.stop', client.calls)
        receipt = json.loads((folder / 'out' / 'g.json').read_text())
        self.assertEqual(receipt['status'], 'PASS')
        self.assertIn('deferred', receipt['results'][0]['gif'])

    def test_hardware_gif_without_session_is_refused(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'rows': [{'id': 'g', 'start': 's', 'end': 's',
                                        'steps': [{'gif': {'name': 'clip', 'preset': 'evidence', 'steps': []}}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'usb:///dev/ttyACM0'}}, folder / 'out', 15,
                          client_factory=lambda s: self.Client())
        try:
            r.run()
        except BaseException:
            pass
        receipt = json.loads((folder / 'out' / 'g.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertIn('session recording', receipt['error'])

    def test_backend_without_it_runs_on(self):
        client = self.Client(supports=False)
        out = self.run_plan(client)
        self.assertFalse((out / 'recording.jsonl').exists())
        self.assertEqual(json.loads((out / 'a.json').read_text())['status'], 'PASS')

    class SplitClient:
        """recording.session_split like switch-remote's OBS client: a new file per acknowledged split."""

        def __init__(self, folder, ack=lambda label: True, stopped_at=(), refuse_start=False):
            self.folder, self.ack, self.calls, self.n, self.rec_ms, self.cur = folder, ack, [], 0, 0, None
            # OBS is found stopped at these row starts (1 = the first executed row); refuse_start: it won't restart
            self.stopped_at, self.refuse_start, self.ensures = set(stopped_at), refuse_start, 0

        def new_file(self):
            self.n += 1
            path = self.folder / f'rec-{self.n}.mp4'
            path.write_bytes(b'mp4' * self.n)
            return str(path)

        def request(self, method, params=None):
            self.calls.append(method)
            params = params or {}
            if method == 'recording.session_start':
                self.cur = self.new_file()
                return {'ok': True, 'result': {'started_wall': 1.0, 'already_active': False, 'path': self.cur}}
            if method == 'recording.session_ensure':
                self.ensures += 1
                if self.ensures not in self.stopped_at:
                    return {'ok': True, 'result': {'active_before': True, 'restarted': False, 'path': self.cur}}
                if self.refuse_start:
                    return {'ok': False, 'error': {'code': 'capture_failed', 'message': 'obs: obs_500'}}
                self.cur, self.rec_ms = self.new_file(), 0
                return {'ok': True, 'result': {'active_before': False, 'restarted': True, 'path': self.cur,
                                               'started_wall': 1.0, 'rec_ms': 0}}
            if method == 'recording.session_split':
                self.rec_ms += 1000
                base = {'active': True, 'rec_ms': self.rec_ms, 'wall': 1.0, 'closed': [], 'late_paths': []}
                if not self.ack(params['label']):
                    return {'ok': True, 'result': {**base, 'split_ok': False, 'error': 'split_unacknowledged'}}
                prev, self.cur = self.cur, self.new_file()
                return {'ok': True, 'result': {**base, 'split_ok': True, 'new_path': self.cur, 'prev_path': prev,
                                               'file_rec_ms': self.rec_ms + 40,
                                               'closed': [{'path': prev, 'finalized': True}]}}
            if method == 'recording.session_mark':
                self.rec_ms += 1000
                return {'ok': True, 'result': {'active': True, 'rec_ms': self.rec_ms, 'wall': 1.0}}
            if method == 'recording.session_stop':
                return {'ok': True, 'result': {'path': self.cur, 'finalized': True, 'stopped_wall': 2.0,
                                               'late_paths': []}}
            if method == 'ui.tree':
                return {'ok': True, 'result': [{'id': 'w', 'class': 'GRenderWindow'}]}
            if method == 'ui.screenshot':
                Path(params['path']).write_bytes(b'png')
            return {'ok': True, 'result': {}}

    def run_split_plan(self, rows, ack=lambda label: True, **client_options):
        folder = Path(tempfile.mkdtemp())
        client = self.SplitClient(folder, ack, **client_options)
        plan = {'start': 's', 'gates': {'never': {'kind': 'log', 'source': 'console_log', 'pattern': 'never', 'cap': 0.3}},
                'session_recording': {'marks': ['row-start', 'row-end', 'shot'], 'per_row_files': True},
                'rows': [{'id': rid, 'start': 's', 'end': 's', 'steps': steps} for rid, steps in rows]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        log = folder / 'console.log'
        log.write_text('')
        r = runner.Runner(folder / 'rows.json', {'sw': {'socket': 'x', 'console_log': str(log)}}, folder / 'out', 15,
                          client_factory=lambda s: client)
        try:
            r.run()
        except BaseException:
            pass
        out = folder / 'out'
        events = [json.loads(l) for l in (out / 'recording.jsonl').read_text().splitlines()]
        return folder, out, events, client

    def test_per_row_files_split_at_each_row_start(self):
        folder, out, events, client = self.run_split_plan([('a', [{'shot': 'one'}]), ('b', [])])
        self.assertEqual([(e['event'], e.get('row')) for e in events],
                         [('start', None), ('row-start', 'a'), ('clip', None), ('shot', 'a'), ('row-end', 'a'),
                          ('row-start', 'b'), ('clip', 'a'), ('row-end', 'b'), ('stop', None), ('clip', 'b')])
        self.assertEqual(client.calls.count('recording.session_split'), 2)
        self.assertEqual(client.calls.count('recording.session_mark'), 3)  # shot, row-end x2 stay plain marks
        start_a = events[1]
        self.assertTrue(start_a['split']['ok'])
        self.assertEqual((start_a['rec_ms'], start_a['split']['requested_rec_ms']), (1040, 1000))
        pre = events[2]
        self.assertEqual((pre['path'], pre['clip']), (str(folder / 'rec-1.mp4'), None))  # before any row: no clip
        for row, n in (('a', 2), ('b', 3)):
            receipt = json.loads((out / f'{row}.json').read_text())
            self.assertEqual(receipt['clip'], str(out / row / 'clip.mp4'))
            self.assertEqual(receipt['clip_source'], str(folder / f'rec-{n}.mp4'))
            self.assertTrue(receipt['clip_finalized'])
            # a hard link: the same file, never a move (the original recording stays)
            self.assertTrue((folder / f'rec-{n}.mp4').is_file())
            self.assertTrue((out / row / 'clip.mp4').samefile(folder / f'rec-{n}.mp4'))
        self.assertEqual(events[-1]['clip'], str(out / 'b' / 'clip.mp4'))
        self.assertEqual(client.calls.count('recording.session_start'), 1)
        self.assertNotIn('recording.start', client.calls)
        self.assertNotIn('recording.stop', client.calls)

    def test_unacknowledged_split_fails_loudly_and_the_row_has_no_clip(self):
        folder, out, events, client = self.run_split_plan([('a', []), ('b', []), ('c', [])],
                                                           ack=lambda label: not label.startswith('b:'))
        start_b = [e for e in events if e['event'] == 'row-start' and e['row'] == 'b'][0]
        self.assertEqual((start_b['split']['ok'], start_b['split']['error']), (False, 'split_unacknowledged'))
        clips = {e['row']: e for e in events if e['event'] == 'clip'}
        self.assertNotIn('b', clips)
        self.assertNotIn('clip', json.loads((out / 'b.json').read_text()))
        # a's file went on through b: a's clip says so
        self.assertEqual(clips['a']['rows'], ['a', 'b'])
        self.assertEqual(json.loads((out / 'a.json').read_text())['clip_spans_rows'], ['a', 'b'])
        self.assertEqual(clips['c']['path'], str(folder / 'rec-3.mp4'))
        actions = (out / 'actions.jsonl').read_text()
        self.assertIn('"session_split": "failed"', actions)
        # never a Start/Stop substitute mid-session
        self.assertEqual((client.calls.count('recording.session_start'), client.calls.count('recording.session_stop')),
                         (1, 1))

    def test_failed_rows_get_their_clip_and_the_stop_closes_the_last(self):
        # b fails and the run stops there (rows after it never run): the stop closes b's file
        folder, out, events, client = self.run_split_plan([('a', []), ('b', [{'gate': 'never'}]), ('c', [])])
        self.assertFalse((out / 'c.json').exists())
        receipt = json.loads((out / 'b.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED')
        self.assertEqual(receipt['clip'], str(out / 'b' / 'clip.mp4'))
        self.assertTrue((out / 'b' / 'clip.mp4').samefile(folder / 'rec-3.mp4'))
        self.assertEqual([e['event'] for e in events[-2:]], ['stop', 'clip'])
        self.assertEqual(events[-1]['row'], 'b')
        self.assertEqual(json.loads((out / 'a.json').read_text())['clip'], str(out / 'a' / 'clip.mp4'))

    def test_stopped_recording_is_restarted_before_the_row_and_is_its_clip(self):
        folder, out, events, client = self.run_split_plan([('a', []), ('b', [{'shot': 'x'}])], stopped_at=[2])
        self.assertEqual([(e['event'], e.get('row')) for e in events],
                         [('start', None), ('row-start', 'a'), ('clip', None), ('row-end', 'a'),
                          ('recording-restarted', 'b'), ('clip', 'a'), ('row-start', 'b'), ('shot', 'b'),
                          ('row-end', 'b'), ('stop', None), ('clip', 'b')])
        restarted = events[4]
        self.assertEqual((restarted['ok'], restarted['path']), (True, str(folder / 'rec-3.mp4')))
        self.assertTrue(events[6]['split']['restarted'])
        self.assertEqual(client.calls.count('recording.session_split'), 1)  # b needed no split
        # ensure came before b's first input (its shot)
        self.assertLess(len(client.calls) - client.calls[::-1].index('recording.session_ensure'),
                        len(client.calls) - client.calls[::-1].index('ui.screenshot'))
        b = json.loads((out / 'b.json').read_text())
        self.assertEqual((b['status'], b['clip_source']), ('PASS', str(folder / 'rec-3.mp4')))
        self.assertEqual(json.loads((out / 'a.json').read_text())['clip_source'], str(folder / 'rec-2.mp4'))

    def test_a_recording_that_cannot_restart_stops_the_run_before_the_row(self):
        folder, out, events, client = self.run_split_plan([('a', []), ('b', [{'shot': 'x'}]), ('c', [])],
                                                           stopped_at=[2], refuse_start=True)
        b = json.loads((out / 'b.json').read_text())
        self.assertEqual((b['status'], b['not_executed'], b['results']), ('FAILED', True, []))
        self.assertIn('could not be restarted', b['error'])
        self.assertFalse((out / 'c.json').exists())
        self.assertNotIn('ui.screenshot', client.calls)  # b's shot never ran
        failed = [e for e in events if e['event'] == 'recording-restarted']
        self.assertEqual((failed[0]['row'], failed[0]['ok']), ('b', False))

    def test_dry_run_validates_per_row_files(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'session_recording': {'marks': ['shot'], 'per_row_files': 'yes', 'split_cap_s': 0},
                'rows': [{'id': 'a', 'start': 's', 'end': 's', 'steps': [{'shot': 'x'}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        errors = runner.dry_run(folder / 'rows.json')['errors']
        self.assertTrue(any('per_row_files must be' in e for e in errors), errors)
        self.assertTrue(any('split_cap_s' in e for e in errors), errors)
        plan['session_recording'] = {'marks': ['shot'], 'per_row_files': True}
        (folder / 'rows.json').write_text(json.dumps(plan))
        errors = runner.dry_run(folder / 'rows.json')['errors']
        self.assertTrue(any('keep it in marks' in e for e in errors), errors)

    def test_dry_run_validates_it(self):
        folder = Path(tempfile.mkdtemp())
        plan = {'start': 's', 'session_recording': {'marks': ['bogus'], 'log': '../x'},
                'rows': [{'id': 'a', 'start': 's', 'end': 's', 'steps': [{'shot': 'x'}]}]}
        (folder / 'rows.json').write_text(json.dumps(plan))
        errors = runner.dry_run(folder / 'rows.json')['errors']
        self.assertTrue(any('marks' in e for e in errors), errors)
        self.assertTrue(any('log' in e for e in errors), errors)


class TrainingReturnTests(unittest.TestCase):
    class IO:
        def __init__(self, token):
            self.token = token
            self.screen = 0
            self.revision = 0
            self.actions = []
            self.connection = 1
            self.boot = '0x0000000000000001'

        def read(self, provider, cap):
            import test_native_picker as fixtures

            self.revision += 1
            if provider == routes.native_picker.TILE:
                if self.screen == 0:
                    return fixtures.tile(self.revision, seat='0', token=self.token)
                if self.screen == 1:
                    return fixtures.snapshot(provider, self.revision, {
                        'screen': 'sss', 'focus': 'sss.grid.normal', 'layout': 'sss.grid',
                        'grid': 'normal', 'generation': '1',
                    })
                return fixtures.snapshot(provider, self.revision, {
                    'screen': 'unknown', 'focus': 'unknown', 'observation.active': '0',
                })
            states = [
                {**routes.TRAINING_CSS, 'focus': 'css.training'},
                {**routes.TRAINING_SSS, 'focus': 'stage_select'},
                routes.GAMES_TRAINING,
                routes._main_tab('smash'),
            ]
            return fixtures.native(self.revision, **states[self.screen])

        def arm(self, provider, cap):
            pass

        def send(self, macro):
            self.actions.append(macro)
            if self.screen == 0 and self.token == 'placed':
                self.token = 'held'
            else:
                self.screen += 1

        def finish(self):
            pass

    def execute(self, io):
        from itertools import count
        from unittest.mock import patch

        clock = count(step=.01)
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
                patch.object(routes.time, 'monotonic', side_effect=lambda: next(clock)), \
                patch.object(routes, '_save', side_effect=lambda _ctx, _name, data: data):
            return routes.back_to_main(object())

    def run_return(self, token):
        io = self.IO(token)
        return io, self.execute(io)

    def test_placed_training_token_returns_before_leaving_css(self):
        io, result = self.run_return('placed')
        self.assertEqual(io.actions, ['leave-css', 'leave-css', 'tap-b1', 'tap-b1'])
        self.assertEqual(result['looks'][0], 'css-token-back')

    def test_held_training_token_needs_only_one_css_return(self):
        io, result = self.run_return('held')
        self.assertEqual(io.actions, ['leave-css', 'tap-b1', 'tap-b1'])
        self.assertEqual(result['looks'], ['css', 'sss', 'games_and_more'])

    def test_reconnected_tile_cannot_authorize_token_back(self):
        from unittest.mock import patch

        io = self.IO('placed')
        read = io.read

        def reconnect_on_tile(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE:
                io.connection = 2
                return routes.native_picker.Sample(sample.block, 2)
            return sample

        io.read = reconnect_on_tile
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
                self.assertRaisesRegex(RuntimeError, 'bridge connection changed'):
            routes.back_to_main(object())
        self.assertEqual(io.actions, [])

    def test_foreign_sss_tile_cannot_authorize_css_token_back(self):
        io = self.IO('placed')
        read = io.read

        def foreign_tile(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE:
                sample.values.update(screen='sss', controller='7', seat='7')
            return sample

        io.read = foreign_tile
        with self.assertRaisesRegex(RuntimeError, 'css: no qualified active tile layout'):
            self.execute(io)
        self.assertEqual(io.actions, [])

    def test_missing_local_token_after_b_is_not_success(self):
        io = self.IO('placed')
        read = io.read

        def missing_after_b(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE and io.actions:
                sample.values.pop('local.token')
            return sample

        io.read = missing_after_b
        with self.assertRaisesRegex(RuntimeError, 'local medal changed while taking it back'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_changed_placed_target_after_b_is_rejected(self):
        io = self.IO('placed')
        io.send = io.actions.append
        read = io.read

        def changed_target(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE and io.actions:
                sample.values['placed.name'] = 'Other'
            return sample

        io.read = changed_target
        with self.assertRaisesRegex(RuntimeError, 'local medal changed while taking it back'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_unowned_held_token_rejects_input(self):
        io = self.IO('held')
        read = io.read

        def wrong_carried(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE:
                sample.values['carried'] = '7'
            return sample

        io.read = wrong_carried
        with self.assertRaisesRegex(RuntimeError, 'local token ownership'):
            self.execute(io)
        self.assertEqual(io.actions, [])

    def test_inactive_tile_can_fence_direct_return_to_games_and_more(self):
        io = self.IO('placed')
        send = io.send

        def return_directly(macro):
            if not io.actions:
                io.actions.append(macro)
                io.screen = 2
            else:
                send(macro)

        io.send = return_directly
        result = self.execute(io)
        self.assertEqual(result['looks'], ['css', 'games_and_more'])
        self.assertEqual(io.actions, ['leave-css', 'tap-b1'])

    def test_sss_tile_then_foreign_main_dialog_is_rejected(self):
        io = self.IO('placed')
        send, read = io.send, io.read

        def return_to_stage(macro):
            send(macro)
            if len(io.actions) == 1:
                io.screen = 1

        def foreign_main(provider, cap):
            sample = read(provider, cap)
            if io.actions and provider == routes.native_picker.UI:
                sample.values.update(**routes._main_tab('smash'))
                sample.values.update({'input.owner': 'foreign', 'dialog': 'confirmation'})
            return sample

        io.send, io.read = return_to_stage, foreign_main
        with self.assertRaisesRegex(RuntimeError, 'unexpected Training return focus'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_pre_input_placement_change_rejects_b(self):
        io = self.IO('placed')
        read = io.read
        tile_reads = 0

        def changing_tile(provider, cap):
            nonlocal tile_reads
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE:
                tile_reads += 1
                if tile_reads == 2:
                    sample.values['placed.at'] = '8'
            return sample

        io.read = changing_tile
        with self.assertRaisesRegex(RuntimeError, 'local medal placement changed before B'):
            self.execute(io)
        self.assertEqual(io.actions, [])

    def test_changed_placed_identity_before_b_sends_nothing(self):
        for field, changed in (('placed.kind', '999'), ('placed.name', 'Other'), ('placed.form', 'other')):
            with self.subTest(field=field):
                io = self.IO('placed')
                read = io.read
                tile_reads = 0

                def changed_identity(provider, cap):
                    nonlocal tile_reads
                    sample = read(provider, cap)
                    if provider == routes.native_picker.TILE:
                        tile_reads += 1
                        if tile_reads == 2:
                            sample.values[field] = changed
                    return sample

                io.read = changed_identity
                with self.assertRaisesRegex(RuntimeError, 'local medal placement changed before B'):
                    self.execute(io)
                self.assertEqual(io.actions, [])

    def test_held_tile_then_direct_return_is_accepted(self):
        io = self.IO('placed')
        read = io.read

        def held_then_games(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE and io.actions and io.screen == 0:
                self.assertEqual(sample.values['local.token'], 'held')
                io.screen = 2
            return sample

        io.read = held_then_games
        result = self.execute(io)
        self.assertEqual(result['looks'], ['css', 'games_and_more'])
        self.assertEqual(io.actions, ['leave-css', 'tap-b1'])

    def test_held_then_foreign_main_before_games_is_rejected(self):
        import test_native_picker as fixtures

        io = self.IO('placed')
        read = io.read
        post_input_ui = 0

        def foreign_then_games(provider, cap):
            nonlocal post_input_ui
            sample = read(provider, cap)
            if provider == routes.native_picker.UI and io.actions:
                post_input_ui += 1
                if post_input_ui == 1:
                    io.screen = 2
                    return fixtures.native(io.revision, **{
                        **routes._main_tab('smash'), 'input.owner': 'foreign', 'dialog': 'confirmation',
                    })
            return sample

        io.read = foreign_then_games
        with self.assertRaisesRegex(RuntimeError, 'unexpected Training return focus'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_held_then_newer_sss_before_older_games_is_rejected(self):
        import test_native_picker as fixtures

        io = self.IO('placed')
        read = io.read
        post_input_ui = 0

        def sss_then_older_games(provider, cap):
            nonlocal post_input_ui
            sample = read(provider, cap)
            if provider == routes.native_picker.UI and io.actions:
                post_input_ui += 1
                if post_input_ui == 1:
                    io.screen = 2
                    return fixtures.native(900, **{**routes.TRAINING_SSS, 'focus': 'stage_select'})
            return sample

        io.read = sss_then_older_games
        with self.assertRaisesRegex(RuntimeError, 'ssbu.ui: no newer active observation'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_held_then_loading_and_settling_games_is_accepted(self):
        import test_native_picker as fixtures

        io = self.IO('placed')
        read = io.read
        post_input_ui = 0

        def loading_then_games(provider, cap):
            nonlocal post_input_ui
            sample = read(provider, cap)
            if provider == routes.native_picker.UI and io.actions:
                post_input_ui += 1
                if post_input_ui == 1:
                    io.screen = 2
                    return fixtures.loading(io.revision)
                if post_input_ui == 2:
                    return fixtures.native(io.revision, **routes._GAMES_TRAINING_SETTLING)
            return sample

        io.read = loading_then_games
        result = self.execute(io)
        self.assertEqual(result['looks'], ['css', 'games_and_more'])
        self.assertEqual(io.actions, ['leave-css', 'tap-b1'])

    def test_held_tile_then_foreign_main_dialog_is_rejected(self):
        io = self.IO('placed')
        read = io.read

        def held_then_foreign_main(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE and io.actions and io.screen == 0:
                self.assertEqual(sample.values['local.token'], 'held')
                io.screen = 3
            elif provider == routes.native_picker.UI and io.actions:
                sample.values.update({'input.owner': 'foreign', 'dialog': 'confirmation'})
            return sample

        io.read = held_then_foreign_main
        with self.assertRaisesRegex(RuntimeError, 'unexpected Training return focus'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_token_back_does_not_send_after_source_deadline(self):
        from unittest.mock import patch

        io = self.IO('placed')
        read = io.read
        now, tile_reads = [0], [0]

        def late_second_tile(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.TILE:
                tile_reads[0] += 1
                if tile_reads[0] == 2:
                    now[0] = 10
            return sample

        io.read = late_second_tile
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
                patch.object(routes.time, 'monotonic', side_effect=lambda: now[0]), \
                self.assertRaisesRegex(RuntimeError, 'Training return source deadline expired before leave-css'):
            routes.back_to_main(object())
        self.assertEqual(io.actions, [])

    def test_tile_step_during_token_back_is_rejected(self):
        for field, value in (('ack', '1'), ('applied', '1'), ('ack', 'unknown'), ('applied', 'unknown')):
            with self.subTest(field=field, value=value):
                io = self.IO('placed')
                read = io.read

                def stepped_tile(provider, cap):
                    sample = read(provider, cap)
                    if provider == routes.native_picker.TILE and io.actions:
                        sample.values[field] = value
                    return sample

                io.read = stepped_tile
                with self.assertRaisesRegex(RuntimeError, 'a tile step arrived while taking back the local token'):
                    self.execute(io)
                self.assertEqual(io.actions, ['leave-css'])

    def test_css_token_back_requires_a_fresh_qualified_css_ui(self):
        io = self.IO('placed')
        read = io.read

        def changed_owner(provider, cap):
            sample = read(provider, cap)
            if provider == routes.native_picker.UI and io.actions:
                sample.values['dialog'] = 'confirmation'
            return sample

        io.read = changed_owner
        with self.assertRaisesRegex(RuntimeError, 'unexpected Training return focus'):
            self.execute(io)
        self.assertEqual(io.actions, ['leave-css'])

    def test_token_return_can_already_leave_css(self):
        from unittest.mock import patch

        io = self.IO('placed')
        send = io.send

        def send_and_leave(macro):
            send(macro)
            if len(io.actions) == 1:
                io.screen = 1

        io.send = send_and_leave
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
                patch.object(routes, '_save', side_effect=lambda _ctx, _name, data: data):
            result = routes.back_to_main(object())
        self.assertEqual(io.actions, ['leave-css', 'tap-b1', 'tap-b1'])
        self.assertEqual(result['looks'], ['css', 'sss', 'games_and_more'])

    def test_placed_token_timeout_does_not_claim_a_return(self):
        from itertools import count
        from unittest.mock import patch

        io = self.IO('placed')
        io.send = io.actions.append
        clock = count(step=.25)
        with patch.object(routes, 'BridgePickerIO', return_value=io), \
                patch.object(routes.time, 'monotonic', side_effect=lambda: next(clock)), \
                self.assertRaisesRegex(RuntimeError, 'ssbu.ui: no newer qualified Training return scene transition'):
            routes.back_to_main(object())
        self.assertEqual(io.actions, ['leave-css'])


class WirelessRoomTests(unittest.TestCase):
    class IO:
        def __init__(self):
            self.phase = 'main'
            self.actions = []
            self.events = []
            self.main_screen = 'main_menu'
            self.main_owner = 'native'
            self.room_list_missing = False
            self.stuck_in_list = False
            self.room_focus_id = 'ldn.create_room'
            self.room_focus_index = '0'
            self.room_focus_after_send = None
            self.lost_create_sends = 0
            self.form_script = []
            self.form_screen = 'unknown'
            self.confirm_form_reads = 0
            self.confirm_form_only_first = False
            self.confirm_reads = 0
            self.confirm_sent_at = None
            self.confirm_read_delay_after_cap = 0
            self.stage_context_unknown_reads = 0
            self.unpublished_confirms = 0
            self.confirm_errors_before_unknown = 0
            self.confirm_unknown_before_errors = 0
            self.unpublished_reads = 0
            self.ui_fault_at = None
            self.ui_fault_kind = 'error'
            self.form_reads = 0
            self.tile_reads = 0
            self.css_ui_reads = 0
            self.tile_script = []
            self.tile_read_error = False
            self.wrong_confirm_screen = False
            self.direct_css = False
            self.stuck_stage = False
            self.lost_stage_sends = 0
            self.stage_return_rooms_after_send = False
            self.stage_load_seconds = 0
            self.stage_load_until = None
            self.stage_reaction_seconds = 0
            self.stage_reaction_until = None
            self.stage_script = []
            self.stage_errors_after_send = False
            self.stage_tile_unsettled_reads = 0
            self.stage_token = 'held'
            self.stage_p1_token = 'held'
            self.stage_since = None
            self.stage_ready_after = 0
            self.stage_tile_delay = 0
            self.stage_tile_delay_kind = 'error'
            self.stage_send_times = []
            self.stage_retry_error_at = None
            self.stage_retry_error_done = False
            self.ui_fatal_after_stage = None
            self.ui_fatal_before_send = None
            self.fatal_after_stage_tile_error = False
            self.stage_tile_error_done = False
            self.ready_no_phase = None
            self.read_caps = []
            self.stage_tile_after_send = None
            self.fail_stage_dispatch = False
            self.busy_macro = None
            self.busy_left = 0
            self.pending = None
            self.css_tile_delay = 0
            self.css_since = None
            self.clock = lambda: 0
            self.stuck_tile = False
            self.place_on_chord = False
            self.stuck_placement = False
            self.waiting_role = 'host'
            self.never_waiting = False
            self.ready_load_seconds = 0
            self.ready_load_until = None
            self.lost_ready_sends = 0
            self.ready_send_times = []
            self.confirm_count = 0
            self.stage_count = 0
            self.role_reads = 0
            self.host_after = 0
            self.revision = 0
            self.tile = {
                'screen': 'css', 'at': 'unknown', 'hovered': 'unknown', 'local.token': 'unknown',
                'p1.token': 'held', 'p1.coin': 'held', 'p1.coin.at': 'unknown',
                'p1.placed.at': 'unknown', 'p1.placed.name': 'unknown', 'phase': 'resting',
            }

        def read(self, provider, cap):
            self.read_caps.append((provider, cap))
            self.revision += 1
            if provider == 'tile.cursor':
                if self.phase in ('stage', 'stage_reacting'):
                    if (self.fatal_after_stage_tile_error and self.stage_count
                            and not self.stage_tile_error_done):
                        self.stage_tile_error_done = True
                        self.events.append(('read_error', provider, 'no fenced bridge reply for state tile.cursor'))
                        raise RuntimeError('no fenced bridge reply for state tile.cursor')
                    if self.stage_since is not None and self.clock() - self.stage_since < self.stage_tile_delay:
                        if self.stage_tile_delay_kind == 'error':
                            self.events.append(('read_error', provider, 'tile not published'))
                            raise RuntimeError('tile not published')
                        values = {'screen': 'unknown'}
                        self.events.append(('read', provider, dict(values)))
                        block = {'name': provider, 'schema': provider + '/1', 'filtered': False,
                                 'rev': self.revision, 'values': values}
                        return types.SimpleNamespace(block=block, values=values, connection=1)
                    settled = '0' if self.stage_tile_unsettled_reads else '1'
                    self.stage_tile_unsettled_reads = max(0, self.stage_tile_unsettled_reads - 1)
                    at = self.stage_tile_after_send if self.stage_count and self.stage_tile_after_send else '0'
                    values = {'screen': 'sss', 'settled': settled, 'at': at, 'hovered': at,
                              'local.token': self.stage_token, 'p1.token': self.stage_p1_token}
                    block = {'name': provider, 'schema': provider + '/1', 'filtered': False,
                             'rev': self.revision, 'boot': '0xe54e9b821337ecd1', 'values': values}
                    self.events.append(('read', provider, dict(values)))
                    return types.SimpleNamespace(block=block, values=values, connection=1)
                self.tile_reads += 1
                if (self.tile_read_error or
                        (self.css_since is not None and self.clock() - self.css_since < self.css_tile_delay)):
                    error = 'no fenced bridge reply for state tile.cursor'
                    self.events.append(('read_error', provider, error))
                    raise RuntimeError(error)
                if self.tile_script:
                    self.tile.update(self.tile_script.pop(0))
                values = dict(self.tile)
            else:
                if self.ui_fatal_before_send and not self.actions:
                    self.events.append(('read_error', provider, self.ui_fatal_before_send))
                    raise RuntimeError(self.ui_fatal_before_send)
                if self.phase == 'stage' and self.stage_since is None:
                    self.stage_since = self.clock()
                if (self.phase == 'stage' and self.stage_count and self.ui_fatal_after_stage
                        and (not self.fatal_after_stage_tile_error or self.stage_tile_error_done)):
                    self.events.append(('read_error', provider, self.ui_fatal_after_stage))
                    raise RuntimeError(self.ui_fatal_after_stage)
                if (self.phase == 'stage' and self.stage_count == 1 and self.stage_retry_error_at is not None
                        and not self.stage_retry_error_done and self.clock() - self.stage_send_times[0] >= 5):
                    self.stage_retry_error_done = True
                    routes.time.sleep(max(0, self.stage_send_times[0] + self.stage_retry_error_at - self.clock()))
                    self.events.append(('read_error', provider, 'no fenced bridge reply for state ssbu.ui'))
                    raise RuntimeError('no fenced bridge reply for state ssbu.ui')
                if self.phase == 'stage_reacting' and self.clock() >= self.stage_reaction_until:
                    self.phase = 'loading'
                    self.stage_load_until = self.clock() + 1
                if self.phase == 'loading' and self.clock() >= self.stage_load_until:
                    self.phase = 'css'
                if self.phase == 'ready_loading' and self.clock() >= self.ready_load_until:
                    self.phase = 'waiting'
                if self.phase == 'css' and self.css_since is None:
                    self.css_since = self.clock()
                if self.phase == 'form':
                    self.form_reads += 1
                if self.phase == 'unpublished' and self.confirm_count == 1:
                    self.unpublished_reads += 1
                if self.phase == 'css':
                    self.css_ui_reads += 1
                chord_sent = 'tile-up' in self.actions or 'lobby-first-tile' in self.actions
                fault = (
                    (self.ui_fault_at == 'form_settle' and self.phase == 'form' and self.form_reads >= 4)
                    or (self.ui_fault_at == 'confirm' and self.phase == 'unpublished' and self.confirm_count == 1)
                    or (self.ui_fault_at == 'stage_retry' and self.phase == 'stage' and self.stage_count >= 1)
                    or (self.ui_fault_at == 'chord' and self.phase == 'css' and self.css_ui_reads >= 2
                        and not chord_sent and self.tile['p1.token'] != 'placed')
                    or (self.ui_fault_at == 'placement' and self.phase == 'css' and chord_sent
                        and self.tile['p1.token'] != 'placed')
                    or (self.ui_fault_at == 'ready' and self.phase == 'css' and self.tile['p1.token'] == 'placed')
                )
                mixed_error = (self.phase == 'unpublished' and self.confirm_count == 1
                               and (self.unpublished_reads <= self.confirm_errors_before_unknown
                                    or (self.confirm_unknown_before_errors
                                        and self.unpublished_reads > self.confirm_unknown_before_errors)))
                if mixed_error or (fault and self.ui_fault_kind == 'error'):
                    error = 'no fenced bridge reply for state ssbu.ui'
                    self.events.append(('read_error', provider, error))
                    raise RuntimeError(error)
                scripted_stage = None
                if self.phase == 'stage' and self.stage_count:
                    if self.stage_errors_after_send:
                        scripted_stage = 'error'
                    elif self.stage_script:
                        scripted_stage = self.stage_script.pop(0)
                    if scripted_stage == 'error':
                        error = 'no fenced bridge reply for state ssbu.ui'
                        self.events.append(('read_error', provider, error))
                        raise RuntimeError(error)
                if self.phase == 'confirm_form':
                    if (self.confirm_read_delay_after_cap and self.confirm_count == 1
                            and self.clock() - self.confirm_sent_at >= 20):
                        routes.time.sleep(self.confirm_read_delay_after_cap)
                    if self.confirm_reads >= self.confirm_form_reads:
                        self.phase = 'css' if self.direct_css else 'stage'
                    else:
                        self.confirm_reads += 1
                screen = {
                    'main': self.main_screen, 'rooms': 'ldn.room_list', 'form': self.form_screen,
                    'confirm_form': 'ldn.create',
                    'unpublished': 'unknown', 'wrong': 'ldn.room_list', 'stage': 'stage_select',
                    'css': 'css.smash', 'waiting': 'ldn.waiting', 'not_waiting': 'css.smash', 'loading': 'unknown',
                    'ready_loading': 'css.smash',
                    'stage_reacting': 'stage_select',
                }[self.phase]
                if scripted_stage is not None:
                    screen = scripted_stage
                if self.phase == 'form' and self.form_script:
                    screen = self.form_script.pop(0)
                role = 'unknown'
                if self.phase == 'waiting':
                    self.role_reads += 1
                    role = 'unknown' if self.role_reads <= self.host_after else self.waiting_role
                context = 'local_wireless' if screen in ('stage_select', 'css.smash') else 'unknown'
                if self.phase == 'stage' and self.stage_context_unknown_reads:
                    self.stage_context_unknown_reads -= 1
                    context = 'unknown'
                values = {
                    'screen': screen, 'screen.context': context,
                    'input.owner': self.main_owner if self.phase == 'main' else 'unknown',
                    'observation.active': 'no' if screen == 'unknown' else 'yes',
                    'ldn.role': role, 'ldn.members': 'unknown',
                }
                if screen == 'ldn.room_list':
                    values.update({'focus.id': self.room_focus_id, 'focus.index': self.room_focus_index})
                if self.phase == self.ready_no_phase:
                    values['input.ready'] = 'no'
                elif self.phase == 'stage' and self.stage_ready_after:
                    values['input.ready'] = 'yes' if self.clock() - self.stage_since >= self.stage_ready_after else 'no'
                if fault and self.ui_fault_kind == 'missing':
                    values.pop('screen')
            block = {'name': provider, 'schema': provider + '/1', 'filtered': False, 'rev': self.revision,
                     'frame': self.revision * 10, 'wall': self.revision * 100,
                     'boot': '0xe54e9b821337ecd1', 'values': values}
            self.events.append(('read', provider, dict(values)))
            return types.SimpleNamespace(block=block, values=values, connection=1)

        def send(self, macro):
            self.events.append(('send', macro))
            self.actions.append(macro)
            self.pending = macro
            if macro == self.busy_macro and self.busy_left:
                self.busy_left -= 1
                raise routes.closed_loop.DispatchRejected('macro.play busy')
            if macro == 'lobby-a' and self.phase == 'stage' and self.fail_stage_dispatch:
                raise RuntimeError('no reply to macro.play')
            if macro == 'lobby-open-room-list' and self.phase == 'main':
                if not self.room_list_missing:
                    self.phase = 'rooms'
            elif macro == 'tap-a1' and self.phase == 'rooms':
                if self.lost_create_sends:
                    self.lost_create_sends -= 1
                    if self.room_focus_after_send is not None:
                        self.room_focus_id, self.room_focus_index = self.room_focus_after_send
                elif not self.stuck_in_list:
                    self.phase = 'form'
            elif macro == 'tap-plus1' and self.phase in ('form', 'confirm_form', 'unpublished', 'wrong'):
                self.confirm_count += 1
                if self.confirm_count == 1:
                    self.confirm_sent_at = self.clock()
                if self.wrong_confirm_screen:
                    self.phase = 'wrong'
                elif self.confirm_count <= self.unpublished_confirms:
                    self.phase = 'unpublished'
                elif self.confirm_form_reads and (not self.confirm_form_only_first or self.confirm_count == 1):
                    self.phase = 'confirm_form'
                    self.confirm_reads = 0
                else:
                    self.phase = 'css' if self.direct_css else 'stage'
            elif macro in ('tap-a1', 'lobby-a') and self.phase == 'stage':
                self.stage_count += 1
                self.stage_send_times.append(self.clock())
                if self.stage_return_rooms_after_send:
                    self.phase = 'rooms'
                elif self.lost_stage_sends:
                    self.lost_stage_sends -= 1
                elif not self.stuck_stage and self.stage_reaction_seconds:
                    self.phase = 'stage_reacting'
                    self.stage_reaction_until = self.clock() + self.stage_reaction_seconds
                elif not self.stuck_stage and self.stage_load_seconds:
                    self.phase = 'loading'
                    self.stage_load_until = self.clock() + self.stage_load_seconds
                elif not self.stuck_stage and not self.stage_errors_after_send:
                    self.phase = 'css'
            elif macro in ('tap-a1', 'lobby-a') and self.phase == 'stage_reacting':
                self.stage_count += 1
            elif macro in ('tile-up', 'lobby-first-tile') and self.phase == 'css':
                if not self.stuck_tile:
                    self.tile.update({'at': '66', 'hovered': '66', 'phase': 'settled'})
                if self.place_on_chord:
                    self.tile.update({'p1.token': 'placed', 'p1.placed.at': '66', 'p1.placed.name': 'Simon'})
            elif macro in ('tap-a1', 'lobby-a') and self.phase == 'css':
                if not self.stuck_placement:
                    self.tile.update({'at': '45', 'hovered': 'unknown', 'p1.token': 'placed',
                                      'p1.coin': 'placed', 'p1.coin.at': '66',
                                      'p1.placed.at': '66', 'p1.placed.name': 'Simon',
                                      'phase': 'resting'})
            elif macro in ('tap-plus1', 'lobby-ready') and self.phase in ('css', 'ready_loading', 'not_waiting'):
                self.ready_send_times.append(self.clock())
                if self.lost_ready_sends:
                    self.lost_ready_sends -= 1
                elif self.never_waiting:
                    self.phase = 'not_waiting'
                elif self.ready_load_seconds:
                    self.phase = 'ready_loading'
                    self.ready_load_until = self.clock() + self.ready_load_seconds
                else:
                    self.phase = 'waiting'
            else:
                raise AssertionError(f'unexpected input {macro!r} on {self.phase!r}')

        def finish(self):
            self.events.append(('finish', self.pending))
            self.pending = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.row = Path(self.temp.name)
        self.ctx = types.SimpleNamespace(row_dir=self.row, check_stop=lambda: None, call=self.call)
        self.io = self.IO()
        self.now = 0.0
        self.status_script = None
        self.macro_status_calls = []

    def call(self, method):
        if method == 'macro.status':
            self.macro_status_calls.append((self.now, self.io.phase))
            result = self.status_script() if self.status_script else {'state': 'finished'}
            if isinstance(result, Exception):
                raise result
            return result
        return {}

    def run_room(self, *, unpublished_create=None):
        def sleep(seconds):
            self.now += seconds

        self.io.clock = lambda: self.now
        with mock.patch.object(routes, 'BridgePickerIO', return_value=self.io), \
                mock.patch.object(routes.time, 'monotonic', side_effect=lambda: self.now), \
                mock.patch.object(routes.time, 'sleep', side_effect=sleep):
            options = {} if unpublished_create is None else {'unpublished_create': unpublished_create}
            return routes.wireless_room(self.ctx, ready_cap=3, **options)

    def blocked_control(self, blocked_method):
        release, entered, finished = threading.Event(), threading.Event(), threading.Event()
        calls, threads = [], []

        def call(method):
            calls.append(method)
            if method == blocked_method:
                threads.append(threading.current_thread())
                entered.set()
                try:
                    release.wait(5)
                    return {'state': 'finished'}
                finally:
                    finished.set()
            return {'state': 'playing'} if method == 'macro.status' else {}

        def cleanup():
            release.set()
            if entered.is_set():
                self.assertTrue(finished.wait(1), 'control-call thread did not exit')
                for thread in threads:
                    thread.join(1)
                    self.assertFalse(thread.is_alive(), 'control-call thread remains alive')

        self.addCleanup(cleanup)
        return call, calls

    def test_a_hanging_macro_status_stops_the_route_within_the_idle_bound(self):
        self.io.phase = 'stage'
        self.io.clock = time.monotonic
        self.ctx.call, _ = self.blocked_control('macro.status')
        started = time.monotonic()
        with mock.patch.object(routes, 'BridgePickerIO', return_value=self.io):
            with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
                routes.wireless_room(self.ctx, unpublished_create='allow', idle_cap=0.6)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertEqual(self.io.actions, [])
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertIn('macro.status not answered within', stop['input_channel'])

    def test_the_stall_diagnostic_status_call_cannot_delay_a_stop(self):
        self.io.phase = 'stage'
        self.io.clock = time.monotonic
        self.ctx.call, calls = self.blocked_control('status')
        started = time.monotonic()
        with mock.patch.object(routes, 'BridgePickerIO', return_value=self.io):
            with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
                routes.wireless_room(self.ctx, unpublished_create='allow', idle_cap=1.2)
        self.assertLess(time.monotonic() - started, 1.8)
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertNotIn('status', calls)
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual(stop['stalls'][0]['kind'], 'idle-wait')
        self.assertIsNone(stop['stalls'][0]['eden_frame_ms'])
        self.assertIsNone(stop['stalls'][0]['eden_fps'])

    def test_the_route_declares_its_revalidation_cost(self):
        self.io.phase = 'stage'
        with mock.patch.object(routes.closed_loop, 'run', wraps=routes.closed_loop.run) as engine:
            self.run_room(unpublished_create='allow')
        ui_caps = [cap for provider, cap in self.io.read_caps if provider == 'ssbu.ui']
        tile_caps = [cap for provider, cap in self.io.read_caps if provider == 'tile.cursor']
        self.assertTrue(ui_caps and tile_caps)
        self.assertGreaterEqual(engine.call_args.kwargs['revalidate_cap'], max(ui_caps) + max(tile_caps))

    def test_a_resend_stop_does_not_request_diagnostic_status(self):
        self.io.phase = 'stage'
        self.io.stuck_stage = True
        blocking, _ = self.blocked_control('status')
        status_counts = []

        def call(method):
            if method == 'macro.status':
                return {'state': 'finished'}
            if method == 'status':
                status_counts.append(self.io.stage_count)
                if self.io.stage_count >= 3:
                    return blocking(method)
                return {'game_fps': 50}
            return {}

        self.ctx.call = call
        started = time.perf_counter()
        with self.assertRaises(routes.closed_loop.RouteStopped):
            self.run_room(unpublished_create='allow')
        self.assertLess(time.perf_counter() - started, .6)
        self.assertEqual(self.io.stage_count, 3)
        self.assertEqual(status_counts, [1, 2])
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertIsNone(stop['stalls'][-1]['eden_frame_ms'])
        self.assertNotIn('_skip_diagnostic', stop['stalls'][-1])

    def test_a_successful_resend_bounds_the_diagnostic_status_call(self):
        self.io.phase = 'stage'
        self.io.lost_stage_sends = 1
        blocking, calls = self.blocked_control('status')

        def call(method):
            return {'state': 'finished'} if method == 'macro.status' else blocking(method)

        self.ctx.call = call
        started = time.perf_counter()
        result = self.run_room(unpublished_create='allow')
        self.assertLess(time.perf_counter() - started, 1.6)
        self.assertEqual(calls, ['status'])
        self.assertEqual(result['result'], 'PASS with 1 stall')
        self.assertIsNone(result['stalls'][0]['eden_fps'])
        self.assertIsNone(result['stalls'][0]['eden_frame_ms'])

    def test_an_unpublished_create_form_is_refused_by_default(self):
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room()
        self.assertEqual(caught.exception.step, 'open-create')
        self.assertTrue(caught.exception.reason.startswith(
            'unsupported reader: the Create form is not published as ldn.create'))
        self.assertNotIn('tap-plus1', self.io.actions)
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual(stop['unsupported_reader'], 'ldn.create')

    def test_a_published_create_form_is_confirmed_in_refuse_mode(self):
        self.io.form_screen = 'ldn.create'
        result = self.run_room(unpublished_create='refuse')
        self.assertEqual(result['create_witness'], 'ldn.create')
        self.assertEqual(self.io.actions.count('tap-plus1'), 1)
        self.assertEqual(result['waiting']['ldn.role'], 'host')

    def test_allow_mode_records_that_the_form_had_no_witness(self):
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['create_form'], 'unpublished')
        self.assertEqual(result['create_witness'], 'none: unpublished Create form allowed by the caller')

    def test_an_invalid_unpublished_create_value_is_rejected_before_any_read(self):
        with self.assertRaises(RuntimeError):
            self.run_room(unpublished_create='invalid')
        self.assertEqual(self.io.events, [])

    def test_full_room_route_and_saved_receipt(self):
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'lobby-a',
                                           'lobby-first-tile', 'lobby-a', 'lobby-ready'])
        self.assertEqual(self.io.actions.count('tap-a1'), 1)
        self.assertEqual(result['block'], 'wireless_room')
        self.assertEqual(result['confirm_presses'], 1)
        self.assertEqual(result['stage_presses'], 1)
        self.assertEqual(result['placed_by'], 'chord + A')
        self.assertEqual(result['chord_fence'], {'screen': 'css', 'p1.token': 'held', 'local.token': 'unknown',
                                                  'at': 'unknown', 'hovered': 'unknown'})
        self.assertEqual(result['tile']['p1.placed.name'], 'Simon')
        self.assertEqual(result['waiting']['screen'], 'ldn.waiting')
        self.assertEqual(result['waiting']['ldn.role'], 'host')
        self.assertEqual(result['ldn.members'], 'unknown')
        screens = result['screens']
        expected = ['main_menu', 'ldn.room_list', 'stage_select', 'css.smash', 'ldn.waiting']
        self.assertEqual([s for s in screens if s in expected], expected)
        self.assertEqual(json.loads((self.row / 'routes-wireless-room.json').read_text()), result)
        required = ['main_menu', 'ldn.room_list', 'unknown', 'stage_select',
                    'css.smash', 'css.smash', 'css.smash']
        reads = []
        for event in self.io.events:
            if event[0] == 'read' and event[1] == 'ssbu.ui':
                reads.append(event[2]['screen'])
            elif event[0] == 'send':
                self.assertTrue(reads, f'no fresh ssbu.ui read before {event}')
                self.assertEqual(reads[-1], required.pop(0))
                reads.clear()
        self.assertEqual(required, [])

    def test_direct_css_skips_stage_a(self):
        self.io.direct_css = True
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['stage_presses'], 0)
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1',
                                           'lobby-first-tile', 'lobby-a', 'lobby-ready'])

    def test_main_screen_and_owner_gate_before_any_input(self):
        for screen, owner in (('unknown', 'native'), ('main_menu', 'unknown'), ('main_menu', 'foreign')):
            with self.subTest(screen=screen, owner=owner):
                self.io.main_screen, self.io.main_owner = screen, owner
                with self.assertRaisesRegex(RuntimeError, 'main_menu'):
                    self.run_room(unpublished_create='allow')
                self.assertEqual(self.io.actions, [])

    def test_room_list_timeout_names_expected_and_seen(self):
        self.io.room_list_missing = True
        with self.assertRaisesRegex(RuntimeError, 'ldn.room_list.*main_menu'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list'] * 3)

    def test_create_form_never_leaves_list_sends_no_confirm(self):
        self.io.stuck_in_list = True
        with self.assertRaisesRegex(RuntimeError, 'leave.*ldn.room_list'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-a1', 'tap-a1'])

    def test_form_requires_three_consecutive_samples_away_from_list(self):
        self.io.form_script = ['unknown', 'unknown', 'ldn.room_list', 'unknown', 'unknown', 'unknown']
        self.run_room(unpublished_create='allow')
        first_a = next(i for i, e in enumerate(self.io.events) if e == ('send', 'tap-a1'))
        confirm = next(i for i, e in enumerate(self.io.events) if e == ('send', 'tap-plus1'))
        screens = [e[2]['screen'] for e in self.io.events[first_a + 1:confirm]
                   if e[0] == 'read' and e[1] == 'ssbu.ui']
        self.assertEqual(screens[:6], ['unknown', 'unknown', 'ldn.room_list',
                                       'unknown', 'unknown', 'unknown'])

    def test_published_create_form_before_and_after_confirm_succeeds(self):
        self.io.form_screen = 'ldn.create'
        self.io.confirm_form_reads = 1
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['create_form'], 'ldn.create')
        self.assertEqual(result['confirm_presses'], 1)
        confirm = next(i for i, e in enumerate(self.io.events) if e == ('send', 'tap-plus1'))
        after = self.io.events[confirm + 1:]
        self.assertTrue(any(e[0] == 'read' and e[1] == 'ssbu.ui'
                            and e[2]['screen'] == 'ldn.create' and e[2]['observation.active'] == 'yes'
                            for e in after))

    def test_unpublished_create_form_is_named_in_receipt(self):
        self.assertEqual(self.run_room(unpublished_create='allow')['create_form'], 'unpublished')

    def test_published_create_form_stuck_retries_once_then_fails(self):
        self.io.form_screen = 'ldn.create'
        self.io.confirm_form_reads = 1000
        with self.assertRaisesRegex(RuntimeError, 'stage_select|css.smash'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'tap-plus1'])

    def test_wrong_published_screen_after_a_refuses_without_confirm(self):
        self.io.form_screen = 'main_menu'
        with self.assertRaisesRegex(RuntimeError,
                                    r'expected the Create form \(ldn\.create, or unpublished\) after A.*main_menu'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1'])

    def test_stage_context_can_arrive_after_stage_screen(self):
        self.io.stage_context_unknown_reads = 1
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['confirm_presses'], 1)
        stages = [e[2]['screen.context'] for e in self.io.events
                  if e[0] == 'read' and e[1] == 'ssbu.ui' and e[2]['screen'] == 'stage_select']
        self.assertEqual(stages[:2], ['unknown', 'local_wireless'])

    def test_stage_wrong_context_at_cap_fails_without_confirm_retry(self):
        self.io.stage_context_unknown_reads = 1000
        with self.assertRaisesRegex(RuntimeError, r'stage_select.*unknown'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1'])

    def test_unpublished_confirm_retries_once_then_succeeds(self):
        self.io.unpublished_confirms = 1
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['confirm_presses'], 2)
        self.assertEqual(self.io.actions.count('tap-plus1'), 2)
        self.assertEqual(self.io.actions.count('lobby-ready'), 1)

    def test_confirm_read_errors_never_authorize_a_second_press(self):
        self.io.unpublished_confirms = 1
        self.io.ui_fault_at = 'confirm'
        with self.assertRaisesRegex(RuntimeError, 'no ssbu.ui reply after the Create confirm.*'
                                    'no screen witness.*no fenced bridge reply for state ssbu.ui'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1'])
        self.assertFalse((self.row / 'routes-wireless-room.json').exists())

    def test_confirm_missing_screen_never_authorizes_a_second_press(self):
        self.io.unpublished_confirms = 1
        self.io.ui_fault_at = 'confirm'
        self.io.ui_fault_kind = 'missing'
        with self.assertRaisesRegex(RuntimeError, 'no ssbu.ui reply after the Create confirm.*no screen witness'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1'])
        self.assertFalse((self.row / 'routes-wireless-room.json').exists())

    def test_last_error_at_cap_after_unknowns_never_authorizes_retry(self):
        self.io.unpublished_confirms = 1
        self.io.confirm_unknown_before_errors = 4
        with self.assertRaisesRegex(RuntimeError, 'no ssbu.ui reply after the Create confirm.*no screen witness'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1'])
        self.assertFalse((self.row / 'routes-wireless-room.json').exists())

    def test_errors_then_last_complete_unknown_at_cap_allow_one_retry(self):
        self.io.unpublished_confirms = 1
        self.io.confirm_errors_before_unknown = 4
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['confirm_presses'], 2)
        pluses = [i for i, event in enumerate(self.io.events) if event == ('send', 'tap-plus1')]
        between = self.io.events[pluses[0] + 1:pluses[1]]
        self.assertTrue(any(event[0] == 'read_error' for event in between))
        reads = [event for event in between if event[0] == 'read' and event[1] == 'ssbu.ui']
        self.assertEqual(reads[-1][2]['screen'], 'unknown')

    def test_complete_published_create_at_cap_allows_one_retry(self):
        self.io.form_screen = 'ldn.create'
        self.io.confirm_form_reads = 1000
        self.io.confirm_form_only_first = True
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['create_form'], 'ldn.create')
        self.assertEqual(result['confirm_presses'], 2)
        self.assertEqual(self.io.actions.count('tap-plus1'), 2)
        self.assertEqual(self.io.actions.count('lobby-ready'), 1)

    def test_form_settle_read_error_names_the_error_and_sends_no_confirm(self):
        self.io.ui_fault_at = 'form_settle'
        with self.assertRaisesRegex(RuntimeError, 'Create form.*no fenced bridge reply for state ssbu.ui') as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1'])
        self.assertGreater(next(step for step in caught.exception.receipt['steps']
                                if step['name'] == 'open-create')['unanswered'], 0)

    def test_screen_read_error_or_missing_screen_refuses_other_inputs(self):
        prefix = ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'lobby-a']
        for gate, expected, message in (
                ('stage_retry', prefix, 'expected css.smash'),
                ('chord', prefix, 'before tile-up'),
                ('placement', prefix + ['lobby-first-tile'], 'before placement'),
                ('ready', prefix + ['lobby-first-tile', 'lobby-a'], 'before Ready')):
            for kind in ('error', 'missing'):
                with self.subTest(gate=gate, kind=kind):
                    self.io = self.IO()
                    self.io.ui_fault_at = gate
                    self.io.ui_fault_kind = kind
                    self.io.stuck_stage = gate == 'stage_retry'
                    with self.assertRaisesRegex(RuntimeError, message):
                        self.run_room(unpublished_create='allow')
                    self.assertEqual(self.io.actions, expected)
                    self.assertFalse((self.row / 'routes-wireless-room.json').exists())

    def test_unpublished_after_second_confirm_fails_without_third(self):
        self.io.unpublished_confirms = 3
        with self.assertRaisesRegex(RuntimeError, 'stage_select|css.smash'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'tap-plus1'])

    def test_other_published_screen_never_retries_confirm(self):
        self.io.wrong_confirm_screen = True
        with self.assertRaisesRegex(RuntimeError, 'stage_select|css.smash'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1'])

    def test_stage_still_selected_after_a_retries_twice(self):
        self.io.stuck_stage = True
        with self.assertRaisesRegex(RuntimeError, 'stage-a.*3 sends'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 3)
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1',
                                           'lobby-a', 'lobby-a', 'lobby-a'])

    def test_css_entry_waits_for_held_token_immediately_before_chord(self):
        unknown = {key: 'unknown' for key in self.io.tile}
        css_unknown = {**unknown, 'screen': 'css'}
        held = {**css_unknown, 'p1.token': 'held', 'p1.coin': 'held', 'phase': 'resting'}
        self.io.tile_script = [dict(unknown), dict(unknown), dict(unknown), css_unknown, held]
        result = self.run_room(unpublished_create='allow')
        chord = next(i for i, event in enumerate(self.io.events)
                     if event[0] == 'send' and event[1] in ('tile-up', 'lobby-first-tile'))
        witness = self.io.events[chord - 1]
        self.assertEqual(witness[:2], ('read', 'tile.cursor'))
        self.assertEqual((witness[2]['screen'], witness[2]['p1.token']), ('css', 'held'))
        self.assertEqual(self.io.events[chord], ('send', 'lobby-first-tile'))
        self.assertEqual(self.io.actions.count('lobby-first-tile'), 1)
        prior = [e[2] for e in self.io.events[:chord] if e[:2] == ('read', 'tile.cursor')]
        self.assertTrue(any(v['screen'] == 'unknown' for v in prior))
        self.assertTrue(any(v['screen'] == 'css' and v['p1.token'] == 'unknown' for v in prior))
        self.assertEqual(result['chord_fence'], {key: witness[2][key]
                                                  for key in ('screen', 'p1.token', 'local.token', 'at', 'hovered')})

    def test_no_held_token_before_cap_never_sends_chord_or_placement(self):
        for screen in ('unknown', 'css'):
            with self.subTest(screen=screen):
                self.io = self.IO()
                self.io.tile.update({'screen': screen, 'p1.token': 'unknown', 'local.token': 'unknown'})
                with self.assertRaisesRegex(RuntimeError,
                                            r'expected tile.cursor screen css with p1.token held before the chord'):
                    self.run_room(unpublished_create='allow')
                self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'lobby-a'])
                self.assertFalse((self.row / 'routes-wireless-room.json').exists())

    def test_tile_read_errors_before_cap_never_authorize_chord(self):
        self.io.tile_read_error = True
        with self.assertRaisesRegex(RuntimeError,
                                    r'expected tile.cursor screen css with p1.token held before the chord.*'
                                    r'no fenced bridge reply for state tile.cursor'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'lobby-a'])
        self.assertFalse((self.row / 'routes-wireless-room.json').exists())

    def test_local_held_without_p1_held_never_authorizes_chord(self):
        self.io.tile.update({'p1.token': 'unknown', 'local.token': 'held'})
        with self.assertRaisesRegex(RuntimeError,
                                    r'expected tile.cursor screen css with p1.token held before the chord'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'lobby-a'])
        self.assertFalse((self.row / 'routes-wireless-room.json').exists())

    def test_local_held_without_p1_held_on_tile_never_authorizes_placement(self):
        self.io.tile.update({'p1.token': 'unknown', 'local.token': 'held', 'at': '42', 'hovered': '42'})
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'css-hand')
        self.assertIn('not ready', caught.exception.reason)
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'lobby-a'])
        self.assertEqual(self.io.actions.count('lobby-a'), 1)
        self.assertFalse((self.row / 'routes-wireless-room.json').exists())

    def test_local_held_waits_until_p1_held_before_chord(self):
        self.io.tile_script = [
            {'p1.token': 'unknown', 'local.token': 'held'},
            {'p1.token': 'held', 'local.token': 'held'},
        ]
        result = self.run_room(unpublished_create='allow')
        chord = self.io.events.index(('send', 'lobby-first-tile'))
        witness = self.io.events[chord - 1]
        self.assertEqual(witness[:2], ('read', 'tile.cursor'))
        self.assertEqual((witness[2]['p1.token'], witness[2]['local.token']), ('held', 'held'))
        self.assertEqual(result['chord_fence']['p1.token'], 'held')
        self.assertEqual(self.io.actions.count('lobby-first-tile'), 1)

    def test_local_placed_with_p1_unknown_still_skips_placement(self):
        self.io.tile.update({'p1.token': 'unknown', 'local.token': 'placed'})
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['placed_by'], 'already placed')
        self.assertIsNone(result['chord_fence'])
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'lobby-a', 'lobby-ready'])

    def test_already_hovered_tile_waits_until_token_is_held(self):
        self.io.tile_script = [
            {'screen': 'css', 'at': '42', 'hovered': '42', 'p1.token': 'unknown', 'local.token': 'unknown'},
            {'screen': 'css', 'at': '42', 'hovered': '42', 'p1.token': 'held'},
        ]
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['placed_by'], 'A')
        self.assertIsNone(result['chord_fence'])
        self.assertNotIn('lobby-first-tile', self.io.actions)
        self.assertEqual(self.io.actions.count('lobby-a'), 2)

    def test_token_placed_during_entry_skips_chord_and_placement(self):
        self.io.tile_script = [
            {'screen': 'unknown', 'p1.token': 'unknown', 'local.token': 'unknown'},
            {'screen': 'css', 'p1.token': 'placed', 'p1.placed.at': '66', 'p1.placed.name': 'Simon'},
        ]
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['placed_by'], 'already placed')
        self.assertIsNone(result['chord_fence'])
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'lobby-a', 'lobby-ready'])

    def test_unknown_equals_unknown_is_not_a_tile_and_never_sends_a(self):
        self.io.stuck_tile = True
        with self.assertRaisesRegex(RuntimeError, 'expected the hand on a tile'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'lobby-a',
                                           'lobby-first-tile', 'lobby-first-tile', 'lobby-first-tile'])

    def test_existing_p1_or_local_placement_needs_no_placement_input(self):
        for field in ('p1.token', 'local.token'):
            with self.subTest(field=field):
                self.io = self.IO()
                self.io.tile[field] = 'placed'
                result = self.run_room(unpublished_create='allow')
                self.assertEqual(result['placed_by'], 'already placed')
                self.assertIsNone(result['chord_fence'])
                self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1',
                                                   'lobby-a', 'lobby-ready'])

    def test_unconfirmed_a_reports_last_tile(self):
        self.io.stuck_placement = True
        with self.assertRaisesRegex(RuntimeError, 'placed.*66'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions[-4:], ['lobby-first-tile', 'lobby-a', 'lobby-a', 'lobby-a'])
        self.assertEqual(self.io.actions.count('tap-plus1'), 1)

    def test_token_placed_after_chord_without_a_does_not_claim_already_placed(self):
        self.io.place_on_chord = True
        result = self.run_room(unpublished_create='allow')
        self.assertEqual((result['placed_by'], result['chord_sent']), ('chord', True))
        self.assertEqual(result['chord_fence']['p1.token'], 'held')
        self.assertEqual(self.io.actions,
                         ['lobby-open-room-list', 'tap-a1', 'tap-plus1', 'lobby-a', 'lobby-first-tile', 'lobby-ready'])

    def test_waiting_role_must_be_host(self):
        for role in ('unknown', 'guest'):
            with self.subTest(role=role):
                self.io = self.IO()
                self.io.waiting_role = role
                with self.assertRaisesRegex(RuntimeError, 'expected ldn.role host'):
                    self.run_room(unpublished_create='allow')
                self.assertEqual(self.io.actions[-1], 'lobby-ready')

    def test_waiting_role_can_arrive_after_waiting_screen(self):
        self.io.host_after = 3
        self.assertEqual(self.run_room(unpublished_create='allow')['waiting']['ldn.role'], 'host')

    def test_ready_is_not_pressed_again_while_the_room_loads(self):
        self.io.ready_load_seconds = 25
        result = self.run_room(unpublished_create='allow')
        self.assertGreaterEqual(self.now - self.io.ready_send_times[0], 25)
        self.assertEqual(self.io.actions.count('lobby-ready'), 1)
        self.assertEqual(next(s for s in result['steps'] if s['name'] == 'ready')['sends'], 1)
        self.assertEqual(result['waiting']['ldn.role'], 'host')

    def test_a_lost_ready_is_never_pressed_twice(self):
        self.io.lost_ready_sends = 1
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'ready')
        self.assertIn('no transition within cap', caught.exception.reason)
        self.assertEqual(self.io.actions.count('lobby-ready'), 1)
        self.assertGreaterEqual(self.now - self.io.ready_send_times[0], 45)
        self.assertEqual(self.io.pending, 'lobby-ready')
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual(stop['sends'], 1)
        self.assertEqual(stop['result'], 'STOPPED')
        self.assertEqual(stop['stalls'], [])

    def test_waiting_timeout_reports_screens_seen(self):
        self.io.never_waiting = True
        with self.assertRaisesRegex(RuntimeError, 'ldn.waiting.*css.smash'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions[-1], 'lobby-ready')

    def test_already_hovering_numeric_tile_skips_chord_but_places(self):
        self.io.tile.update({'at': '42', 'hovered': '42', 'phase': 'settled'})
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['placed_by'], 'A')
        self.assertFalse(result['chord_sent'])
        self.assertIsNone(result['chord_fence'])
        self.assertNotIn('lobby-first-tile', self.io.actions)
        self.assertEqual(self.io.actions, ['lobby-open-room-list', 'tap-a1', 'tap-plus1',
                                           'lobby-a', 'lobby-a', 'lobby-ready'])

    def test_a_lost_stage_a_is_sent_again(self):
        self.io.lost_stage_sends = 1
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 2)
        self.assertEqual(result['stage_presses'], 2)
        self.assertEqual(next(s for s in result['steps'] if s['name'] == 'stage-a')['sends'], 2)

    def test_a_step_sent_while_eden_reports_a_macro_playing_waits(self):
        self.io.phase = 'stage'
        self.status_script = lambda: {'state': 'playing' if self.now - self.io.stage_since < 3 else 'finished'}
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 1)
        self.assertGreaterEqual(self.io.stage_send_times[0] - self.io.stage_since, 3)
        self.assertEqual(result['result'], 'PASS with 1 stall')
        event, = result['stalls']
        self.assertEqual((event['kind'], event['step'], event['why']), ('idle-wait', 'stage-a', 'macro playing'))
        record = next(item for item in result['steps'] if item['name'] == 'stage-a')
        self.assertEqual(record['idle_waits'], 1)
        self.assertGreaterEqual(record['idle_wait_ms'], 3000)
        rows = [json.loads(line) for line in (self.row / 'stall-events.jsonl').read_text().splitlines()]
        self.assertEqual(rows, [{**event, 'block': 'wireless_room'}])

    def test_a_recording_macro_also_blocks_the_step_until_idle(self):
        self.io.phase = 'stage'
        self.status_script = lambda: {'state': 'recording' if self.now - self.io.stage_since < 1.2 else 'finished'}
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 1)
        self.assertGreaterEqual(self.io.stage_send_times[0] - self.io.stage_since, 1.2)
        self.assertEqual(result['result'], 'PASS with 1 stall')
        self.assertEqual(result['stalls'][0]['why'], 'macro recording')

    def test_a_macro_that_never_finishes_stops_on_the_step_with_the_receipt(self):
        self.io.phase = 'stage'
        self.status_script = lambda: {'state': 'playing'}
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertIn('input channel busy', caught.exception.reason)
        self.assertEqual(self.io.actions, [])
        self.assertIsNone(self.io.pending)
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual(stop['input_channel'], 'macro playing')
        self.assertEqual(stop['result'], 'STOPPED with 1 stall')

    def test_an_unavailable_macro_status_sends_nothing(self):
        for reply in (RuntimeError('offline'), None):
            with self.subTest(reply=reply):
                self.io = self.IO()
                self.io.phase = 'stage'
                self.now = 0.0
                self.status_script = lambda: reply
                with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
                    self.run_room(unpublished_create='allow')
                self.assertEqual(caught.exception.step, 'stage-a')
                self.assertEqual(self.io.actions, [])
                stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
                self.assertIn('macro.status not understood:', stop['input_channel'])

    def test_an_empty_macro_status_never_releases_input(self):
        self.io.phase = 'stage'
        self.status_script = lambda: {}
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertEqual(self.io.actions, [])
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertIn('macro.status not understood:', stop['input_channel'])

    def test_an_unknown_macro_state_never_releases_input(self):
        self.io.phase = 'stage'
        self.status_script = lambda: {'state': 'unknown'}
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertEqual(self.io.actions, [])
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertIn('macro.status not understood:', stop['input_channel'])
        self.assertIn('unknown', stop['input_channel'])

    def test_idle_and_finished_without_an_error_release_input(self):
        for reply in ({'state': 'idle'}, {'state': 'finished'}, {'state': 'idle', 'error': None},
                      {'state': 'finished', 'error': ''}):
            with self.subTest(reply=reply):
                self.io = self.IO()
                self.io.phase = 'stage'
                self.now = 0
                self.status_script = lambda: reply
                result = self.run_room(unpublished_create='allow')
                self.assertEqual(self.io.stage_count, 1)
                self.assertEqual(result['result'], 'PASS')

    def test_a_finished_status_with_an_error_does_not_release_input(self):
        self.io.phase = 'stage'
        self.status_script = lambda: {'state': 'finished', 'error': 'late failure'}
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertEqual(self.io.actions, [])
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertIn('macro.status not understood:', stop['input_channel'])
        self.assertIn('late failure', stop['input_channel'])

    def test_a_macro_error_state_is_not_an_idle_channel(self):
        self.io.phase = 'stage'
        self.status_script = lambda: {'state': 'error', 'error': ''}
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertEqual(self.io.actions, [])
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertIn('macro.status not understood:', stop['input_channel'])
        self.assertIn("'error'", stop['input_channel'])

    def test_countdown_and_branch_playing_are_busy(self):
        for state in ('countdown', 'branch_playing'):
            with self.subTest(state=state):
                self.io = self.IO()
                self.io.phase = 'stage'
                self.now = 0
                self.status_script = lambda: {'state': state}
                with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
                    self.run_room(unpublished_create='allow')
                self.assertEqual(caught.exception.step, 'stage-a')
                self.assertEqual(self.io.actions, [])
                stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
                self.assertEqual(stop['input_channel'], f'macro {state}')

    def test_sustained_macro_contention_stops_within_the_bound(self):
        self.io.phase = 'stage'
        calls = []

        def status():
            calls.append(True)
            state = 'playing' if self.now < 18 and len(calls) % 2 else 'finished'
            return {'state': state}

        self.status_script = status
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertGreaterEqual(self.now, 10.5)
        self.assertLessEqual(self.now, 10.81)
        self.assertIn('not enough of the bound left to re-read', caught.exception.reason)
        self.assertEqual(self.io.actions, [])
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual(stop['input_channel'], 'macro playing')
        event, = stop['stalls']
        self.assertEqual(event['kind'], 'idle-wait')
        self.assertGreater(event['waits'], 1)

    def test_an_input_reset_busy_error_after_the_macro_was_accepted_stops_without_a_second_a(self):
        self.io.phase = 'stage'
        original_send = self.io.send
        self.ctx.on = 'host'
        self.ctx.runner = types.SimpleNamespace(
            wait_macro=mock.Mock(side_effect=RuntimeError('input.reset input_busy')))
        self.ctx.log = mock.Mock()

        def call(method, params=None):
            if method == 'macro.status':
                return {'state': 'finished'}
            if method == 'macro.play':
                self.io.stage_count += 1
                return {}
            return {}

        def send(macro):
            if macro == 'lobby-a' and self.io.phase == 'stage':
                self.io.events.append(('send', macro))
                self.io.actions.append(macro)
                self.io.pending = macro
                return routes.play(self.ctx, macro)
            return original_send(macro)

        self.ctx.call = call
        self.io.send = send
        with mock.patch.object(routes, 'macro_file', return_value=(Path('/macro'), 'sha', 3)):
            with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
                self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertIn('dispatch failed, delivery unknown:', caught.exception.reason)
        self.assertIn('input.reset input_busy', caught.exception.reason)
        self.assertEqual(self.io.actions, ['lobby-a'])
        self.assertEqual(self.io.stage_count, 1)
        self.assertEqual(self.io.pending, 'lobby-a')
        self.assertFalse(any(event[0] == 'finish' for event in self.io.events))
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual((stop['sends'], stop['dispatch']), (1, 'unknown'))

    def test_a_macro_status_error_is_not_an_idle_channel(self):
        self.io.phase = 'stage'
        self.status_script = lambda: {'state': 'finished', 'error': 'decoder fault'}
        with self.assertRaises(routes.closed_loop.RouteStopped):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, [])
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertIn('macro.status not understood:', stop['input_channel'])
        self.assertIn('decoder fault', stop['input_channel'])

    def test_a_busy_macro_play_reply_is_not_a_blind_resend(self):
        self.io.phase = 'stage'
        self.io.place_on_chord = True
        self.io.busy_macro, self.io.busy_left = 'lobby-a', 1
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions.count('lobby-a'), 2)
        self.assertEqual(self.io.stage_count, 1)
        attempts = [i for i, event in enumerate(self.io.events) if event == ('send', 'lobby-a')]
        self.assertTrue(any(event[:2] == ('read', 'ssbu.ui') for event in
                            self.io.events[attempts[0] + 1:attempts[1]]))
        record = next(item for item in result['steps'] if item['name'] == 'stage-a')
        self.assertEqual((record['sends'], record['busy_rejections']), (1, 1))
        self.assertEqual(result['result'], 'PASS with 1 stall')
        self.assertEqual(result['stalls'][0]['kind'], 'busy-rejected')

    def test_a_busy_create_confirm_does_not_start_the_retry_cooldown(self):
        self.io.phase = 'form'
        self.io.form_screen = 'ldn.create'
        self.io.busy_macro, self.io.busy_left = 'tap-plus1', 1
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions.count('tap-plus1'), 2)
        self.assertLess(self.io.confirm_sent_at, 2)
        self.assertEqual(result['confirm_presses'], 1)
        record = next(item for item in result['steps'] if item['name'] == 'confirm-create')
        self.assertEqual((record['sends'], record['busy_rejections']), (1, 1))

    def test_a_busy_rejection_followed_by_a_long_wait_reports_both_stalls(self):
        self.io.phase = 'stage'
        self.io.busy_macro, self.io.busy_left = 'lobby-a', 1
        self.status_script = lambda: {'state': 'playing' if not self.io.busy_left
                                      and not self.io.stage_count and self.now < 2.1 else 'finished'}
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 1)
        self.assertEqual(result['result'], 'PASS with 2 stalls')
        self.assertEqual([event['kind'] for event in result['stalls']], ['busy-rejected', 'idle-wait'])
        rows = [json.loads(line) for line in (self.row / 'stall-events.jsonl').read_text().splitlines()]
        self.assertEqual(rows, [{**event, 'block': 'wireless_room'} for event in result['stalls']])

    def test_two_busy_dispatches_stop_with_both_stalls_in_the_receipt(self):
        self.io.phase = 'stage'
        self.io.busy_macro, self.io.busy_left = 'lobby-a', 2
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertEqual(self.io.actions, ['lobby-a', 'lobby-a'])
        self.assertEqual(self.io.stage_count, 0)
        self.assertIsNone(self.io.pending)
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual((stop['sends'], stop['input_channel'], stop['result']),
                         (0, 'macro.play busy', 'STOPPED with 2 stalls'))
        self.assertEqual([event['kind'] for event in stop['stalls']], ['busy-rejected', 'busy-rejected'])
        rows = [json.loads(line) for line in (self.row / 'stall-events.jsonl').read_text().splitlines()]
        self.assertEqual(rows, [{**event, 'block': 'wireless_room'} for event in stop['stalls']])

    def test_a_busy_create_press_is_rechecked_before_it_opens_the_form(self):
        self.io.phase = 'rooms'
        self.io.busy_macro, self.io.busy_left = 'tap-a1', 1
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions.count('tap-a1'), 2)
        self.assertEqual(result['create_witness'], 'none: unpublished Create form allowed by the caller')
        record = next(item for item in result['steps'] if item['name'] == 'open-create')
        self.assertEqual((record['sends'], record['busy_rejections']), (1, 1))

    def test_open_create_presses_only_on_the_create_row(self):
        self.io.phase = 'rooms'
        self.run_room(unpublished_create='allow')
        index = self.io.events.index(('send', 'tap-a1'))
        reads = [event[2] for event in self.io.events[:index] if event[:2] == ('read', 'ssbu.ui')]
        self.assertEqual((reads[-1]['focus.id'], reads[-1]['focus.index']), ('ldn.create_room', '0'))
        self.assertEqual(self.io.actions.count('tap-a1'), 1)

    def test_a_room_row_focus_gets_no_a(self):
        self.io.phase = 'rooms'
        self.io.room_focus_id = 'ldn.room.aabbccddeeff.' + 'a' * 32
        self.io.room_focus_index = '1'
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'open-create')
        self.assertIn('the Create row is not focused', caught.exception.reason)
        self.assertIn(self.io.room_focus_id, caught.exception.reason)
        self.assertIn('focus.index=1', caught.exception.reason)
        self.assertNotIn('tap-a1', self.io.actions)

    def test_an_unknown_room_list_focus_is_an_unsupported_reader_stop(self):
        self.io.phase = 'rooms'
        self.io.room_focus_id = self.io.room_focus_index = 'unknown'
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'open-create')
        self.assertIn('the Create row is not focused', caught.exception.reason)
        self.assertIn('focus.id=unknown, focus.index=unknown', caught.exception.reason)
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual(stop['unsupported_reader'], 'ldn.room_list focus')
        self.assertNotIn('tap-a1', self.io.actions)

    def test_a_lost_create_a_is_not_repeated_after_the_focus_moved_to_a_room(self):
        self.io.phase = 'rooms'
        self.io.lost_create_sends = 1
        self.io.room_focus_after_send = ('ldn.room.aabbccddeeff.' + 'b' * 32, '1')
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'open-create')
        self.assertIn('source target changed', caught.exception.reason)
        self.assertEqual(self.io.actions.count('tap-a1'), 1)
        self.assertEqual(self.io.pending, 'tap-a1')
        sent = self.io.events.index(('send', 'tap-a1'))
        self.assertFalse(any(event[0] == 'finish' for event in self.io.events[sent + 1:]))

    def test_a_lost_create_a_is_repeated_on_the_same_create_row(self):
        self.io.phase = 'rooms'
        self.io.lost_create_sends = 1
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions.count('tap-a1'), 2)
        self.assertEqual(result['result'], 'PASS with 1 stall')

    def test_a_bounce_to_an_unfocused_room_list_stops_on_open_create(self):
        self.io.phase = 'stage'
        self.io.stage_return_rooms_after_send = True
        self.io.room_focus_id = self.io.room_focus_index = 'unknown'
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'open-create')
        self.assertIn('the Create row is not focused', caught.exception.reason)
        self.assertEqual(self.io.actions, ['lobby-a'])
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual(stop['unsupported_reader'], 'ldn.room_list focus')

    def test_a_press_on_the_first_stage_select_read_is_refused_while_not_ready(self):
        self.io.phase = 'stage'
        self.io.stage_ready_after = 3
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 1)
        self.assertGreaterEqual(self.io.stage_send_times[0] - self.io.stage_since, 3)
        press = self.io.events.index(('send', 'lobby-a'))
        reads = [e[2] for e in self.io.events[:press] if e[:2] == ('read', 'ssbu.ui')]
        self.assertEqual(reads[0]['input.ready'], 'no')
        self.assertEqual(reads[-1]['input.ready'], 'yes')
        self.assertEqual(result['press_gate'], 'input.ready + tile.cursor')

    def test_a_stage_select_without_a_published_tile_cursor_gets_no_press(self):
        for kind in ('error', 'unknown'):
            with self.subTest(kind=kind):
                self.io = self.IO()
                self.io.phase = 'stage'
                self.io.stage_tile_delay = 5
                self.io.stage_tile_delay_kind = kind
                result = self.run_room(unpublished_create='allow')
                self.assertEqual(self.io.stage_count, 1)
                self.assertGreaterEqual(self.io.stage_send_times[0] - self.io.stage_since, 5)
                press = self.io.events.index(('send', 'lobby-a'))
                tiles = [e[2] for e in self.io.events[:press] if e[:2] == ('read', 'tile.cursor')]
                self.assertEqual((tiles[-1]['screen'], tiles[-1]['p1.token'], tiles[-1]['local.token']),
                                 ('sss', 'held', 'held'))
                self.assertEqual(result['press_gate'], 'tile.cursor (no input.ready published)')

    def test_stage_a_needs_both_tokens_held(self):
        for local, p1 in (('unknown', 'held'), ('held', 'unknown')):
            with self.subTest(local=local, p1=p1):
                self.io = self.IO()
                self.io.stage_token, self.io.stage_p1_token = local, p1
                with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
                    self.run_room(unpublished_create='allow')
                self.assertEqual(caught.exception.step, 'stage-a')
                self.assertIn('not ready', caught.exception.reason)
                self.assertNotIn('lobby-a', self.io.actions)

    def test_input_ready_no_blocks_every_step(self):
        cases = (('main', None, 'lobby-open-room-list'),
                 ('rooms', None, 'tap-a1'),
                 ('form', 'form', 'tap-plus1'),
                 ('stage', None, 'lobby-a'),
                 ('css', 'hand', 'lobby-first-tile'),
                 ('css', 'place', 'lobby-a'),
                 ('css', 'ready', 'lobby-ready'))
        for phase, variant, macro in cases:
            with self.subTest(phase=phase, variant=variant):
                self.io = self.IO()
                self.io.phase = self.io.ready_no_phase = phase
                if variant == 'form':
                    self.io.form_screen = 'ldn.create'
                elif variant == 'place':
                    self.io.tile.update({'at': '42', 'hovered': '42'})
                elif variant == 'ready':
                    self.io.tile['p1.token'] = 'placed'
                with self.assertRaises(routes.closed_loop.RouteStopped):
                    self.run_room(unpublished_create='allow')
                self.assertNotIn(macro, self.io.actions)
                stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
                self.assertEqual(stop['press_gate'], 'input.ready + tile.cursor')

    def test_the_last_read_of_a_wait_is_never_given_less_than_two_seconds(self):
        for slow in (False, True):
            with self.subTest(slow=slow):
                self.io = self.IO()
                self.io.stage_load_seconds = 40 if slow else 0
                self.run_room(unpublished_create='allow')
                self.assertTrue(self.io.read_caps)
                self.assertTrue(all(cap >= 2.0 for _, cap in self.io.read_caps), self.io.read_caps)

    def test_a_missing_reply_at_the_deadline_does_not_skip_the_retry(self):
        self.io.lost_stage_sends = 1
        self.io.stage_retry_error_at = 15
        result = self.run_room(unpublished_create='allow')
        self.assertTrue(self.io.stage_retry_error_done)
        self.assertEqual(self.io.stage_count, 2)
        self.assertLess(self.io.stage_send_times[1] - self.io.stage_send_times[0], 19)
        self.assertEqual(result['result'], 'PASS with 1 stall')
        stage = next(step for step in result['steps'] if step['name'] == 'stage-a')
        self.assertEqual(stage['unanswered'], 1)

    def test_the_create_confirm_retry_keeps_its_witnessed_deadline_behavior(self):
        self.io.form_screen = 'ldn.create'
        self.io.confirm_form_reads = 1000
        self.io.confirm_form_only_first = True
        self.io.confirm_read_delay_after_cap = .2
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions.count('tap-plus1'), 2)
        self.assertEqual(result['confirm_presses'], 2)
        self.assertEqual(result['waiting']['ldn.role'], 'host')

    def test_stage_a_needs_the_held_stage_token(self):
        for token in ('unknown', 'none', None):
            with self.subTest(token=token):
                self.io = self.IO()
                self.io.stage_token = token
                with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
                    self.run_room(unpublished_create='allow')
                self.assertEqual(caught.exception.step, 'stage-a')
                self.assertIn('not ready', caught.exception.reason)
                self.assertNotIn('lobby-a', self.io.actions)
                stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
                self.assertEqual(stop['sends'], 0)

    def test_a_lost_a_then_a_different_stage_tile_never_presses_again(self):
        self.io.lost_stage_sends = 1
        self.io.stage_tile_after_send = '1'
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertIn('source target changed after the send', caught.exception.reason)
        self.assertEqual(self.io.actions.count('lobby-a'), 1)
        self.assertEqual(self.io.pending, 'lobby-a')
        press = self.io.events.index(('send', 'lobby-a'))
        self.assertFalse(any(event[0] == 'finish' for event in self.io.events[press + 1:]))
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual((stop['step'], stop['sends']), ('stage-a', 1))
        self.assertEqual(stop['result'], 'STOPPED')

    def test_a_macro_play_error_writes_the_stop_receipt(self):
        self.io.fail_stage_dispatch = True
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertIn('dispatch failed, delivery unknown: no reply to macro.play', caught.exception.reason)
        self.assertEqual(self.io.actions.count('lobby-a'), 1)
        self.assertEqual(self.io.pending, 'lobby-a')
        press = self.io.events.index(('send', 'lobby-a'))
        self.assertFalse(any(event[0] == 'finish' for event in self.io.events[press + 1:]))
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual((stop['step'], stop['sends'], stop['dispatch']), ('stage-a', 1, 'unknown'))
        self.assertEqual(stop['last_ui']['screen'], 'stage_select')
        self.assertEqual((stop['last_tile']['screen'], stop['last_tile']['at']), ('sss', '0'))
        self.assertEqual(stop['result'], 'STOPPED')

    def test_a_bridge_continuity_error_writes_the_stop_receipt(self):
        messages = ('bridge connection or replay continuity changed while input was unconfirmed',
                    'native picker bridge boot changed',
                    'ssbu.ui: malformed explicit state reply',
                    'ssbu.ui: overlapping explicit state reply',
                    'native picker bridge log rotated or truncated')
        for message in messages:
            with self.subTest(message=message):
                self.io = self.IO()
                self.io.lost_stage_sends = 1
                self.io.ui_fatal_after_stage = message
                with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
                    self.run_room(unpublished_create='allow')
                self.assertEqual(caught.exception.step, 'stage-a')
                self.assertTrue(caught.exception.reason.startswith(
                    f'bridge continuity lost, nothing was sent after it: {message}'))
                self.assertEqual(self.io.actions.count('lobby-a'), 1)
                self.assertEqual(self.io.pending, 'lobby-a')
                press = self.io.events.index(('send', 'lobby-a'))
                self.assertEqual(self.io.events[press + 1:], [('read_error', 'ssbu.ui', message)])
                stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
                self.assertEqual(stop['continuity'], 'lost')
                self.assertEqual(stop['last_error'], message)
                self.assertEqual(stop['last_ui']['screen'], 'stage_select')
                self.assertEqual((stop['last_tile']['screen'], stop['last_tile']['at']), ('sss', '0'))
                self.assertEqual(next(step for step in stop['steps'] if step['name'] == 'stage-a')['continuity'],
                                 'lost')

    def test_a_continuity_error_before_the_first_read_writes_a_locate_stop(self):
        message = 'bridge connection or replay continuity changed during native UI read'
        self.io.ui_fatal_before_send = message
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'locate')
        self.assertEqual(self.io.actions, [])
        self.assertEqual(self.io.events, [('read_error', 'ssbu.ui', message)])
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual(stop['continuity'], 'lost')
        self.assertEqual(stop['last_error'], message)
        self.assertIsNone(stop['last_ui'])
        self.assertIsNone(stop['last_tile'])

    def test_a_continuity_stop_retains_the_last_answered_tile_after_a_tile_timeout(self):
        message = 'bridge connection or replay continuity changed while input was unconfirmed'
        self.io.lost_stage_sends = 1
        self.io.ui_fatal_after_stage = message
        self.io.fatal_after_stage_tile_error = True
        with self.assertRaises(routes.closed_loop.RouteStopped) as caught:
            self.run_room(unpublished_create='allow')
        self.assertEqual(caught.exception.step, 'stage-a')
        self.assertEqual(self.io.pending, 'lobby-a')
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual(stop['continuity'], 'lost')
        self.assertEqual(stop['last_error'], message)
        self.assertEqual((stop['last_tile']['screen'], stop['last_tile']['at']), ('sss', '0'))
        press = self.io.events.index(('send', 'lobby-a'))
        self.assertFalse(any(event[0] == 'finish' for event in self.io.events[press + 1:]))

    def test_a_resend_is_reported_as_pass_with_one_stall(self):
        log = self.row / 'bridge.log'
        log.write_text('previous\nlast non-empty line\n\n')
        self.ctx.instance = {'bridge_log': str(log)}
        self.ctx.call = lambda method: {'game_fps': 50} if method == 'status' else {'state': 'finished'}
        self.io.lost_stage_sends = 1
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['result'], 'PASS with 1 stall')
        self.assertEqual(len(result['stalls']), 1)
        event, = result['stalls']
        self.assertEqual((event['kind'], event['step'], event['send']), ('resend', 'stage-a', 1))
        self.assertEqual(event['target'], repr('0'))
        self.assertEqual(event['screen'], 'stage_select')
        self.assertEqual(event['tile']['at'], '0')
        self.assertGreaterEqual(event['seconds_on_source'], 6)
        self.assertEqual(event['frame'], event['rev'] * 10)
        self.assertEqual(event['wall'], event['rev'] * 100)
        self.assertEqual(event['last_bridge_line'], 'last non-empty line')
        self.assertEqual((event['eden_fps'], event['eden_frame_ms']), (50, 20))
        self.assertIn('utc', event)
        rows = [json.loads(line) for line in (self.row / 'stall-events.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {**event, 'block': 'wireless_room'})

    def test_the_plain_path_is_a_plain_pass_with_no_stall_file(self):
        result = self.run_room(unpublished_create='allow')
        self.assertEqual((result['result'], result['stalls']), ('PASS', []))
        self.assertFalse((self.row / 'stall-events.jsonl').exists())

    def test_a_stop_after_resends_lists_its_stalls(self):
        self.io.stuck_stage = True
        with self.assertRaises(routes.closed_loop.RouteStopped):
            self.run_room(unpublished_create='allow')
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual(stop['result'], 'STOPPED with 3 stalls')
        self.assertEqual([item['send'] for item in stop['stalls']], [1, 2, 3])
        rows = [json.loads(line) for line in (self.row / 'stall-events.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['block'] == 'wireless_room' for row in rows))

    def test_a_slow_stage_load_gets_one_press(self):
        self.io.stage_load_seconds = 40
        result = self.run_room(unpublished_create='allow')
        self.assertGreaterEqual(self.now, 40)
        self.assertEqual(self.io.stage_count, 1)
        self.assertEqual(result['stage_presses'], 1)
        self.assertEqual(result['waiting']['ldn.role'], 'host')

    def test_stage_select_still_shown_briefly_after_a_gets_one_press(self):
        self.io.stage_reaction_seconds = 3
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 1)
        self.assertEqual(result['stage_presses'], 1)
        press = self.io.events.index(('send', 'lobby-a'))
        screens = [e[2].get('screen') for e in self.io.events[press + 1:]
                   if e[:2] == ('read', 'ssbu.ui')]
        self.assertEqual(screens[0], 'stage_select')
        self.assertIn('unknown', screens)
        self.assertIn('css.smash', screens)

    def test_five_seconds_of_stage_select_after_a_gets_one_press(self):
        self.io.stage_reaction_seconds = 5
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 1)
        self.assertEqual(result['stage_presses'], 1)

    def test_the_flown_stall_sequence_recovers(self):
        self.io.lost_stage_sends = 1
        self.io.stage_script = ['stage_select', 'unknown', 'error']
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 2)
        self.assertEqual(result['stage_presses'], 2)
        self.assertIn('unknown', result['screens'])
        self.assertTrue(any(e[:2] == ('read_error', 'ssbu.ui') for e in self.io.events))

    def test_no_reply_until_the_stage_cap_never_presses_again(self):
        self.io.stage_errors_after_send = True
        with self.assertRaisesRegex(RuntimeError, "stage-a.*cap"):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 1)
        self.assertEqual(self.io.actions.count('lobby-a'), 1)
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual((stop['step'], stop['sends']), ('stage-a', 1))
        self.assertIn('screens', stop)
        self.assertEqual(stop['last_ui']['screen'], 'stage_select')
        self.assertIn('no fenced bridge reply', stop['last_error'])
        self.assertIn('last_tile', stop)

    def test_three_lost_stage_presses_stop_on_stage_a(self):
        self.io.stuck_stage = True
        with self.assertRaisesRegex(RuntimeError, 'stage-a.*3 sends'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 3)
        stop = json.loads((self.row / 'routes-wireless-room-stop.json').read_text())
        self.assertEqual((stop['step'], stop['sends']), ('stage-a', 3))

    def test_stage_a_waits_for_the_settled_stage_tile(self):
        self.io.stage_tile_unsettled_reads = 5
        self.run_room(unpublished_create='allow')
        press = self.io.events.index(('send', 'lobby-a'))
        stage_tiles = [e[2] for e in self.io.events[:press] if e[:2] == ('read', 'tile.cursor')]
        self.assertGreaterEqual(sum(tile['settled'] == '0' for tile in stage_tiles), 5)
        self.assertEqual(stage_tiles[-1]['settled'], '1')
        self.assertEqual(stage_tiles[-1]['at'], stage_tiles[-1]['hovered'])

    def test_stage_a_never_settled_stops_with_no_press(self):
        self.io.stage_tile_unsettled_reads = 1000
        with self.assertRaisesRegex(RuntimeError, 'stage-a.*not ready'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.stage_count, 0)
        self.assertNotIn('lobby-a', self.io.actions)

    def test_a_late_css_tile_cursor_is_waited_for(self):
        self.io.css_tile_delay = 20
        result = self.run_room(unpublished_create='allow')
        self.assertGreaterEqual(self.now - self.io.css_since, 20)
        self.assertEqual(result['placed_by'], 'chord + A')
        chord = self.io.events.index(('send', 'lobby-first-tile'))
        css_entry = next(i for i, event in enumerate(self.io.events)
                         if event[:2] == ('read', 'ssbu.ui') and event[2]['screen'] == 'css.smash')
        self.assertFalse(any(e[0] == 'send' for e in self.io.events[css_entry:chord]))
        self.assertTrue(any(e[:2] == ('read_error', 'tile.cursor') for e in self.io.events[:chord]))
        last_tile = [e for e in self.io.events[:chord] if e[:2] == ('read', 'tile.cursor')][-1]
        self.assertEqual((last_tile[2]['screen'], last_tile[2]['p1.token']), ('css', 'held'))

    def test_entry_from_the_room_list(self):
        self.io.phase = 'rooms'
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['entered_at'], 'open-create')
        self.assertEqual(self.io.actions, ['tap-a1', 'tap-plus1', 'lobby-a',
                                           'lobby-first-tile', 'lobby-a', 'lobby-ready'])

    def test_entry_from_stage_select(self):
        self.io.phase = 'stage'
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['entered_at'], 'stage-a')
        self.assertEqual(self.io.actions, ['lobby-a', 'lobby-first-tile', 'lobby-a', 'lobby-ready'])
        self.assertEqual(result['create_witness'], 'not observed: the route entered after the Create form')

    def test_entry_from_the_published_create_form(self):
        self.io.phase = 'form'
        self.io.form_screen = 'ldn.create'
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['entered_at'], 'confirm-create')
        self.assertEqual(result['create_form'], 'ldn.create')
        self.assertEqual(self.io.actions, ['tap-plus1', 'lobby-a', 'lobby-first-tile', 'lobby-a', 'lobby-ready'])

    def test_entry_from_css_with_the_token_held(self):
        self.io.phase = 'css'
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['entered_at'], 'css-hand')
        self.assertEqual(self.io.actions, ['lobby-first-tile', 'lobby-a', 'lobby-ready'])

    def test_entry_from_css_with_the_token_placed(self):
        self.io.phase = 'css'
        self.io.tile['p1.token'] = 'placed'
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['entered_at'], 'ready')
        self.assertEqual(self.io.actions, ['lobby-ready'])
        self.assertEqual(result['placed_by'], 'already placed')

    def test_entry_from_css_with_the_token_hovering(self):
        self.io.phase = 'css'
        self.io.tile.update({'at': '42', 'hovered': '42'})
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['entered_at'], 'place')
        self.assertEqual(self.io.actions, ['lobby-a', 'lobby-ready'])
        self.assertEqual(result['placed_by'], 'A')

    def test_entry_from_the_waiting_room_sends_nothing(self):
        self.io.phase = 'waiting'
        result = self.run_room(unpublished_create='allow')
        self.assertEqual(result['entered_at'], 'done')
        self.assertEqual(self.io.actions, [])

    def test_entry_from_an_unknown_screen_stops_on_locate(self):
        self.io.main_screen = 'unknown'
        with self.assertRaisesRegex(RuntimeError, 'wireless_room:.*locate.*unknown'):
            self.run_room(unpublished_create='allow')
        self.assertEqual(self.io.actions, [])


class SwitchBootTests(unittest.TestCase):
    def test_launched_switch_uses_the_same_title_gate_and_deadline(self):
        from unittest.mock import Mock, patch

        ctx = Mock(instance={'socket': 'usb:///dev/ttyACM0'})
        with patch.object(routes, 'to_main', return_value={'main': True}) as to_main, \
                patch.object(routes, '_save', side_effect=lambda _ctx, _name, result: result):
            self.assertEqual(routes.switch_boot(ctx, cap=5), {'main': True})
        to_main.assert_called_once_with(ctx, cap=5)
        ctx.call.assert_not_called()
        ctx.shot.assert_not_called()

    def test_system_applet_needs_owner_and_focus(self):
        from unittest.mock import Mock, patch

        ctx = Mock(instance={'socket': 'usb:///dev/ttyACM0'})
        with patch.object(routes, 'to_main', side_effect=RuntimeError('ssbu.ui unavailable')), \
                self.assertRaisesRegex(RuntimeError, 'system applets have no bridge owner or focus'):
            routes.switch_boot(ctx, cap=5)
        ctx.call.assert_not_called()
        ctx.shot.assert_not_called()


class ExpectTests(unittest.TestCase):
    def test_expectations_survive_a_new_runner_process(self):
        folder = Path(tempfile.mkdtemp())

        class R:
            out = folder
        first = FakeCtx.__new__(FakeCtx)
        first.runner = R()
        routes.remember(first, stage_id=3)
        routes.remember(first, p1_kind=0)
        second = FakeCtx.__new__(FakeCtx)
        second.runner = R()  # a retry: a new runner object, nothing in memory
        self.assertEqual(routes.expected(second), {'stage_id': 3, 'p1_kind': 0})


class SettledTests(unittest.TestCase):
    def body(self, x, status=0, situation=0):
        return {'x': x, 'y': 0.01, 'status': status, 'situation': situation, 'kind': 0, 'lr': 1.0, 'dmg': 0.0}

    def test_two_equal_grounded_reads(self):
        a = {0: self.body(-40.0), 1: self.body(40.0)}
        self.assertTrue(routes.settled(a, {0: self.body(-40.0), 1: self.body(40.0)}))
        self.assertFalse(routes.settled(None, a))
        self.assertFalse(routes.settled(a, {0: self.body(-39.0), 1: self.body(40.0)}))
        self.assertFalse(routes.settled(a, {0: self.body(-40.0, status=7), 1: self.body(40.0)}))
        self.assertFalse(routes.settled(a, {0: self.body(-40.0), 1: self.body(40.0, situation=2)}))


class LoadStepTests(unittest.TestCase):
    def test_shared_hook_names_resolve_next_to_the_runner(self):
        fn = runner.load_step(Path('/nonexistent'), 'routes:to_main')
        self.assertEqual(fn.__name__, 'to_main')
        fn = runner.load_step(Path('/nonexistent'), 'routes_online:lan_pair')
        self.assertEqual(fn.__name__, 'lan_pair')


class OnlineTests(unittest.TestCase):
    A1 = ('[auto] broker started title=01006A800016E000 lan=192.168.0.222 local_only=false\n'
          '[coord] leading epoch=137 tcp=192.168.0.222:35095\n'
          '[coord] member joined pid=1031977419 vip=10.253.109.237 members=1\n'
          '[auto] ready pid=1031977419 vip=10.253.109.237\n')
    B = ('[auto] broker started title=01006A800016E000 lan=192.168.0.166 local_only=false\n'
         '[auto] ready pid=1709175818 vip=10.253.175.95\n')

    def test_leader_then_pair(self):
        self.assertTrue(routes_online.leader_ok(self.A1, '192.168.0.222'))
        self.assertFalse(routes_online.leader_ok(self.A1, '192.168.0.1'))
        a2 = self.A1 + '[coord] member joined pid=1709175818 vip=10.253.175.95 members=2\n'
        self.assertFalse(routes_online.pair_joined(self.A1, self.B))
        self.assertTrue(routes_online.pair_joined(a2, self.B))

    def test_two_rooms_fail(self):
        b_leads = self.B + '[coord] leading epoch=9 tcp=192.168.0.166:1\n'
        a2 = self.A1 + '[coord] member joined pid=1709175818 vip=10.253.175.95 members=2\n'
        self.assertFalse(routes_online.pair_joined(a2, b_leads))
        self.assertFalse(routes_online.pair_joined(self.A1 + '[coord] member joined pid=5 members=2\n', self.B))


if __name__ == '__main__':
    unittest.main()
