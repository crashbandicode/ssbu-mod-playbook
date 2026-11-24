"""Offline checks for screenshot freshness and bridge frame provenance."""
import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path


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

    def test_game_frame_reads_the_last_frame_line_and_boot(self):
        path = self.root / 'bridge.log'
        path.write_text('[Bridge] boot=0xabc\n[Bridge] state.8 screen=one frame=618 wall=1513497634\n'
                        '[Bridge] state.8 screen=two frame=625 wall=1513497790\n')
        self.assertEqual(self.fresh.game_frame(path), {'boot': '0xabc', 'frame': 625, 'wall': 1513497790})

    def test_game_frame_is_none_without_a_log_or_a_frame_line(self):
        path = self.root / 'bridge.log'
        self.assertIsNone(self.fresh.game_frame(None))
        self.assertIsNone(self.fresh.game_frame(path))
        path.write_text('[Bridge] boot=0xabc\n[Bridge] no frame here\n')
        self.assertIsNone(self.fresh.game_frame(path))

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

    def test_every_still_is_appended_to_stills_jsonl(self):
        reply = {'backend': 'window', 'uniform': False, 'warning': 'sample warning'}
        one = self.record(self.frame(1000), reply=reply)
        two = self.record(self.frame(1200))
        rows = [json.loads(line) for line in (self.root / 'stills.jsonl').read_text().splitlines()]
        self.assertEqual(rows, [one, two])
        self.assertEqual(one['sha256'], hashlib.sha256(b'picture').hexdigest())
        self.assertEqual((one['bytes'], one['backend'], one['uniform'], one['warning']),
                         (7, 'window', False, 'sample warning'))
        self.assertEqual(one['path'], str(self.root / one['file']))
        self.assertTrue(one['utc'])

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


if __name__ == '__main__':
    unittest.main()
