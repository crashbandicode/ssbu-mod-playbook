"""Offline checks for screenshot freshness and bridge frame provenance."""
import hashlib
import gzip
import importlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class StillFreshTests(unittest.TestCase):
    def setUp(self):
        self.fresh = importlib.import_module('still_fresh')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.fresh.Ledger(self.root)
        self.number = 0

    def frame(self, value, boot='0xabc'):
        return {'boot': boot, 'frame': value, 'wall': value * 17}

    def record(self, frame, data=b'picture', on='a', reply=None):
        self.number += 1
        path = self.root / f'picture-{self.number}.png'
        path.write_bytes(data)
        return self.ledger.record(on, path, reply, frame)

    def test_deferred_commit_does_not_publish_the_ledger_or_journal(self):
        path = self.root / 'deferred.png'
        path.write_bytes(b'deferred')
        captured = []
        record = self.ledger.record('a', path, None, self.frame(42),
                                    commit=lambda result, run: captured.append((result, run)))
        self.assertEqual(captured[0][0], record)
        self.assertEqual(captured[0][1]['sha256'], hashlib.sha256(b'deferred').hexdigest())
        self.assertEqual(self.ledger.runs, {})
        self.assertFalse((self.root / 'stills.jsonl').exists())

    def test_frame_source_reads_the_last_frame_line_and_boot(self):
        path = self.root / 'bridge.log'
        path.write_text('[Bridge] boot=0xabc\n[Bridge] state.8 screen=one frame=618 wall=1513497634\n'
                        '[Bridge] state.8 screen=two frame=625 wall=1513497790\n')
        self.assertEqual(self.fresh.FrameSource(path).read(), {'boot': '0xabc', 'frame': 625, 'wall': 1513497790})

    def test_frame_source_is_none_without_a_log_or_a_frame_line(self):
        path = self.root / 'bridge.log'
        self.assertIsNone(self.fresh.FrameSource(None).read())
        self.assertIsNone(self.fresh.FrameSource(path).read())
        path.write_text('[Bridge] boot=0xabc\n[Bridge] no frame here\n')
        self.assertIsNone(self.fresh.FrameSource(path).read())

    def test_initial_read_finds_a_boot_before_the_last_64k(self):
        path = self.root / 'bridge.log'
        path.write_bytes(b'[Bridge] boot=0xabc\n' + b'x' * (65 * 1024) +
                         b'\n[Bridge] state screen=sss frame=42 wall=99\n')
        self.assertEqual(self.fresh.FrameSource(path).read(), {'boot': '0xabc', 'frame': 42, 'wall': 99})

    def growing_trace(self):
        fixture = Path(__file__).parent / 'fixtures' / 'still-fresh' / 'a1-boot2-bridge.log.gz'
        lines = gzip.decompress(fixture.read_bytes()).splitlines(keepends=True)
        path = self.root / 'growing-bridge.log'
        path.write_bytes(b'')
        return lines, path, self.fresh.FrameSource(path)

    def append_until_frame(self, lines, path, offset, target):
        for index in range(offset, len(lines)):
            match = re.search(rb' frame=(\d+) wall=(\d+)\r?\n$', lines[index])
            if match and int(match[1]) >= target:
                with path.open('ab') as stream:
                    stream.writelines(lines[offset:index + 1])
                return index + 1, int(match[1]), int(match[2])
        self.fail(f'no retained frame at or after {target}')

    def test_the_retained_trace_gives_the_boot_at_every_frozen_frame(self):
        lines, path, source = self.growing_trace()
        offset = 0
        for expected in (1751, 1967, 2807, 4603):
            offset, frame, wall = self.append_until_frame(lines, path, offset, expected)
            self.assertEqual(frame, expected)
            self.assertEqual(source.read(), {'boot': '0x0d10511fffc85e43', 'frame': frame, 'wall': wall})

    def test_the_flown_freeze_is_refused_from_the_retained_trace(self):
        lines, path, source = self.growing_trace()
        offset, frames, records = 0, [], []
        for target in (1751, 1967, 2750, 2800, 2810, 3350, 3400, 3440):
            offset, frame, wall = self.append_until_frame(lines, path, offset, target)
            observed = source.read()
            self.assertEqual((observed['frame'], observed['wall']), (frame, wall))
            frames.append(frame)
            records.append(self.record(observed, data=b'prior image' if target == 1751 else b'frozen image'))
        self.assertEqual(frames, [1751, 1967, 2750, 2800, 2811, 3350, 3400, 3440])
        self.assertEqual([record['status'] for record in records], ['fresh', 'fresh'] + ['refused'] * 6)
        self.assertTrue(all(record['first_identical'] == records[1]['file'] for record in records[1:]))

    def test_a_replaced_log_does_not_inherit_the_old_boot(self):
        path = self.root / 'bridge.log'
        path.write_bytes(b'[Bridge] boot=0xabc frame=100 wall=1000\n')
        source = self.fresh.FrameSource(path)
        self.assertEqual(source.read()['boot'], '0xabc')
        replacement = self.root / 'new.log'
        replacement.write_bytes(b'[Bridge] padding=' + b'x' * 200 + b' frame=300 wall=3000\n')
        replacement.replace(path)
        frame = source.read()
        self.assertEqual(frame, {'boot': None, 'frame': 300, 'wall': 3000})
        self.assertEqual(self.record(frame)['status'], 'unverified')

    def test_a_log_replaced_during_a_read_is_reopened_without_the_old_boot(self):
        path = self.root / 'bridge.log'
        path.write_bytes(b'[Bridge] boot=0xabc frame=100 wall=1000\n')
        replacement = self.root / 'new.log'
        replacement.write_bytes(b'[Bridge] frame=300 wall=3000\n')
        original_stat = Path.stat
        replaced = []

        def replace_before_stat(target, *args, **kwargs):
            if target == path and not replaced:
                replacement.replace(path)
                replaced.append(True)
            return original_stat(target, *args, **kwargs)

        with mock.patch.object(Path, 'stat', autospec=True, side_effect=replace_before_stat):
            frame = self.fresh.FrameSource(path).read()
        self.assertTrue(replaced)
        self.assertEqual(frame, {'boot': None, 'frame': 300, 'wall': 3000})

    def test_a_truncated_log_restarts_from_its_start(self):
        path = self.root / 'bridge.log'
        path.write_bytes(b'[Bridge] boot=0xabc frame=100 wall=1000\n' + b'x' * 200 + b'\n')
        source = self.fresh.FrameSource(path)
        self.assertEqual(source.read()['boot'], '0xabc')
        path.write_bytes(b'[Bridge] frame=2 wall=20\n')
        self.assertEqual(source.read(), {'boot': None, 'frame': 2, 'wall': 20})

    def test_a_new_boot_token_switches_the_boot(self):
        path = self.root / 'bridge.log'
        path.write_bytes(b'[Bridge] boot=0xabc frame=100 wall=1000\n')
        source = self.fresh.FrameSource(path)
        self.assertEqual(source.read()['boot'], '0xabc')
        with path.open('ab') as stream:
            stream.write(b'[Bridge] boot=0xdef\n')
        self.assertIsNone(source.read())
        with path.open('ab') as stream:
            stream.write(b'[Bridge] frame=2 wall=20\n')
        self.assertEqual(source.read(), {'boot': '0xdef', 'frame': 2, 'wall': 20})

    def test_a_repeated_boot_token_preserves_the_latest_frame(self):
        path = self.root / 'bridge.log'
        path.write_bytes(b'[Bridge] boot=0xabc frame=100 wall=1000\n')
        source = self.fresh.FrameSource(path)
        self.assertEqual(source.read(), {'boot': '0xabc', 'frame': 100, 'wall': 1000})
        with path.open('ab') as stream:
            stream.write(b'[Bridge] boot=0xabc\n')
        self.assertEqual(source.read(), {'boot': '0xabc', 'frame': 100, 'wall': 1000})

    def test_an_incomplete_last_line_is_completed_by_the_next_read(self):
        path = self.root / 'bridge.log'
        path.write_bytes(b'[Bridge] boot=0xabc frame=12 wall=')
        source = self.fresh.FrameSource(path)
        self.assertIsNone(source.read())
        with path.open('ab') as stream:
            stream.write(b'345\n')
        self.assertEqual(source.read(), {'boot': '0xabc', 'frame': 12, 'wall': 345})

    def test_reads_are_incremental(self):
        path = self.root / 'bridge.log'
        initial = b'[Bridge] boot=0xabc frame=1 wall=10\n'
        appended = b'[Bridge] frame=2 wall=20\n'
        path.write_bytes(initial)
        source = self.fresh.FrameSource(path)
        self.assertEqual(source.read()['frame'], 1)
        self.assertEqual(source.offset, len(initial))
        with path.open('ab') as stream:
            stream.write(appended)
        original_open = Path.open
        observed = []

        class CountedStream:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, size=-1):
                data = self.stream.read(size)
                observed.append(data)
                return data

        def tracked_open(path, *args, **kwargs):
            return CountedStream(original_open(path, *args, **kwargs))

        with mock.patch.object(Path, 'open', autospec=True, side_effect=tracked_open):
            self.assertEqual(source.read()['frame'], 2)
        self.assertEqual(b''.join(observed), appended)
        self.assertEqual(source.offset, len(initial) + len(appended))

    def test_a_large_first_read_spans_multiple_chunks(self):
        path = self.root / 'bridge.log'
        path.write_bytes(b'[Bridge] boot=0xabc\n' + b'x' * (1024 * 1024 + 5) +
                         b'\n[Bridge] frame=100 wall=1000\n')
        source = self.fresh.FrameSource(path)
        self.assertEqual(source.read(), {'boot': '0xabc', 'frame': 100, 'wall': 1000})
        self.assertEqual(source.offset, path.stat().st_size)

    def test_missing_log_clears_a_previously_observed_boot(self):
        path = self.root / 'bridge.log'
        path.write_bytes(b'[Bridge] boot=0xabc frame=1 wall=10\n')
        source = self.fresh.FrameSource(path)
        self.assertEqual(source.read()['boot'], '0xabc')
        path.unlink()
        self.assertIsNone(source.read())
        path.write_bytes(b'[Bridge] frame=200 wall=2000\n')
        self.assertEqual(source.read(), {'boot': None, 'frame': 200, 'wall': 2000})

    def test_unreadable_log_clears_a_previously_observed_boot(self):
        path = self.root / 'bridge.log'
        path.write_bytes(b'[Bridge] boot=0xabc frame=1 wall=10\n')
        source = self.fresh.FrameSource(path)
        self.assertEqual(source.read()['boot'], '0xabc')
        with mock.patch.object(Path, 'open', side_effect=PermissionError('denied')):
            self.assertIsNone(source.read())
        path.write_bytes(b'[Bridge] padding=xxxxxxxxxxxxxxxx frame=200 wall=2000\n')
        self.assertEqual(source.read(), {'boot': None, 'frame': 200, 'wall': 2000})

    def test_a_changed_picture_is_fresh(self):
        first = self.record(self.frame(1000))
        second = self.record(self.frame(1400), data=b'changed picture')
        self.assertEqual((first['status'], second['status']), ('fresh', 'fresh'))
        self.assertEqual(second['first_identical'], second['file'])
        self.assertFalse(self.fresh.refused(second))

    def test_identical_bytes_across_180_game_frames_are_refused(self):
        first, same, refused = [self.record(self.frame(frame)) for frame in (1000, 1060, 1200)]
        self.assertEqual([first['status'], same['status'], refused['status']], ['fresh', 'same', 'refused'])
        self.assertEqual(refused['unchanged_frames'], 200)
        self.assertEqual(refused['first_identical'], first['file'])
        self.assertEqual(refused['reason'], f'unchanged across 200 game frames since {first["file"]}')
        self.assertTrue(self.fresh.refused(refused))

    def test_the_flown_freeze_is_refused(self):
        records = [self.record(self.frame(frame)) for frame in (1967, 2750, 2800, 2810, 3350, 3400, 3440)]
        self.assertEqual([record['status'] for record in records], ['fresh'] + ['refused'] * 6)
        self.assertEqual(records[-1]['unchanged_frames'], 1473)
        self.assertEqual(records[-1]['first_identical'], records[0]['file'])

    def test_identical_bytes_one_second_apart_are_not_refused(self):
        self.record(self.frame(1000))
        result = self.record(self.frame(1060))
        self.assertEqual(result['status'], 'same')
        self.assertEqual(result['unchanged_frames'], 60)
        self.assertFalse(self.fresh.refused(result))

    def test_a_new_boot_starts_a_new_run(self):
        self.record(self.frame(1000))
        new = self.record(self.frame(1200, boot='0xdef'))
        self.assertEqual(new['status'], 'fresh')
        self.assertEqual(new['first_identical'], new['file'])
        self.assertEqual(self.record(self.frame(1260, boot='0xdef'))['status'], 'same')

    def test_no_frame_source_is_unverified_and_never_refused(self):
        first = self.record(None)
        second = self.record(None)
        third = self.record(self.frame(1300))
        self.assertEqual([item['status'] for item in (first, second, third)], ['unverified'] * 3)
        self.assertEqual(first['reason'], 'no game frame source')
        self.assertTrue(all(not self.fresh.refused(item) for item in (first, second, third)))

    def test_a_frame_that_went_backwards_is_unverified(self):
        self.record(self.frame(1000))
        result = self.record(self.frame(999))
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['reason'], 'game frame went backwards')
        self.assertFalse(self.fresh.refused(result))

    def test_a_backwards_frame_is_unverified_even_when_the_image_changed(self):
        self.record(self.frame(1000))
        result = self.record(self.frame(999), data=b'changed picture')
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['reason'], 'game frame went backwards')
        self.assertFalse(self.fresh.refused(result))

    def test_every_still_is_appended_to_stills_jsonl(self):
        reply = {'backend': 'window', 'uniform': False, 'warning': 'sample warning'}
        one = self.record(self.frame(1000), reply=reply)
        two = self.record(self.frame(1200))
        rows = self.fresh.load(self.root / 'stills.jsonl')
        self.assertEqual(rows, [one, two])
        self.assertEqual(len((self.root / 'stills.jsonl').read_text().splitlines()), 2)
        self.assertEqual(one['sha256'], hashlib.sha256(b'picture').hexdigest())
        self.assertEqual((one['bytes'], one['backend'], one['uniform'], one['warning']),
                         (7, 'window', False, 'sample warning'))
        self.assertEqual(one['path'], str(self.root / one['file']))
        self.assertTrue(one['utc'])

    def test_loading_a_journal_skips_only_records_with_a_later_matching_retraction(self):
        first = self.record(self.frame(1000), data=b'first')
        second = self.record(self.frame(1001), data=b'second')
        replacement = {**first, 'frame': 1002}
        self.ledger.append({'retract': first['file'], 'sha256': first['sha256'],
                            'reason': 'written by a superseded gate check', 'utc': 'later'})
        self.ledger.append(replacement)
        self.assertEqual(self.fresh.load(self.root / 'stills.jsonl'), [second, replacement])

    def test_the_threshold_is_configurable(self):
        self.ledger = self.fresh.Ledger(self.root, frames=60)
        self.record(self.frame(1000))
        self.assertEqual(self.record(self.frame(1059))['status'], 'same')
        self.assertEqual(self.record(self.frame(1060))['status'], 'refused')

    def test_instances_have_separate_identical_runs(self):
        first_a = self.record(self.frame(1000), on='a')
        first_b = self.record(self.frame(1200), on='b')
        refused_a = self.record(self.frame(1200), on='a')
        self.assertEqual((first_a['status'], first_b['status'], refused_a['status']), ('fresh', 'fresh', 'refused'))
        self.assertEqual(refused_a['first_identical'], first_a['file'])

    def test_a_still_without_any_record_is_not_usable(self):
        for status in ('fresh', 'same', 'unverified'):
            with self.subTest(status=status):
                self.assertTrue(self.fresh.usable({'status': status}))
        for record in (None, {}, {'status': 'refused'}, {'status': 'error'}, {'status': 'unknown'}, 'fresh'):
            with self.subTest(record=record):
                self.assertFalse(self.fresh.usable(record))

    def test_an_unknown_boot_does_not_authorise_a_refusal(self):
        first = self.record(self.frame(1000, boot=None))
        second = self.record(self.frame(1300, boot=None))
        self.assertEqual((first['status'], second['status']), ('unverified', 'unverified'))
        self.assertEqual(second['reason'], 'no game boot source')
        self.assertFalse(self.fresh.refused(second))

    def test_a_known_frame_after_missing_provenance_starts_a_new_witness(self):
        self.record(None)
        first_known = self.record(self.frame(1000))
        later = self.record(self.frame(1180))
        self.assertEqual((first_known['status'], later['status']), ('unverified', 'refused'))
        self.assertEqual(later['first_identical'], first_known['file'])


if __name__ == '__main__':
    unittest.main()
