#!/usr/bin/env python3
"""Offline tests for harvest.py with a temporary library and row receipts."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harvest  # noqa: E402


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.library = self.base / 'library'
        self.out = self.base / 'out'
        self.library.mkdir()
        self.out.mkdir()
        self.segment = self.base / 'recorded.edenmacro.json'
        self.segment.write_text('{"events": [1, 2]}\n')
        self.digest = hashlib.sha256(self.segment.read_bytes()).hexdigest()
        self.manifest = self.base / 'rows.json'
        self.manifest.write_text(json.dumps({
            'segments': {'s1': {'file': self.segment.name, 'sha256': self.digest}},
            'gates': {'g1': {'kind': 'status', 'cap': 1}},
            'rows': [{'id': 'r1', 'steps': [{'gate': 'g1'}, {'play': 's1'}]}],
        }))
        self.index = self.library / 'index.json'

    def index_with(self, segments, gates):
        self.index.write_text(json.dumps({'segments': segments, 'gates': gates}) + '\n')
        os.utime(self.index, ns=(1000000000000000000, 1000000000000000000))
        return self.index.read_bytes(), self.index.stat().st_mtime_ns

    def receipt(self, status):
        (self.out / 'r1.json').write_text(json.dumps({'status': status}))

    def test_existing_entries_leave_index_bytes_and_mtime_unchanged(self):
        before = self.index_with({'s1': {'sha256': self.digest}}, {'g1': {'kind': 'status'}})
        self.receipt('PASS')
        result = harvest.harvest(self.manifest, self.out, 'test', self.library)
        self.assertEqual(result, {'segments': [], 'gates': [], 'skipped': ['s1']})
        self.assertEqual((self.index.read_bytes(), self.index.stat().st_mtime_ns), before)

    def test_no_pass_receipt_leaves_index_unchanged(self):
        before = self.index_with({}, {})
        self.receipt('FAILED')
        result = harvest.harvest(self.manifest, self.out, 'test', self.library)
        self.assertEqual(result, {'segments': [], 'gates': [], 'skipped': []})
        self.assertEqual((self.index.read_bytes(), self.index.stat().st_mtime_ns), before)

    def test_new_segment_is_copied_and_indexed(self):
        before = self.index_with({}, {'g1': {'kind': 'status'}})
        self.receipt('PASS')
        result = harvest.harvest(self.manifest, self.out, 'test', self.library)
        self.assertEqual(result['segments'], ['s1'])
        entry = json.loads(self.index.read_text())['segments']['s1']
        self.assertEqual((self.library / entry['file']).read_bytes(), self.segment.read_bytes())
        self.assertEqual((entry['sha256'], entry['from_run'], entry['row']), (self.digest, 'test', 'r1'))
        self.assertNotEqual(self.index.read_bytes(), before[0])

    def test_new_gate_is_indexed_without_a_new_segment(self):
        before = self.index_with({'s1': {'sha256': self.digest}}, {})
        self.receipt('PASS')
        result = harvest.harvest(self.manifest, self.out, 'test', self.library)
        self.assertEqual(result['gates'], ['g1'])
        self.assertEqual(json.loads(self.index.read_text())['gates']['g1']['from_run'], 'test')
        self.assertNotEqual(self.index.read_bytes(), before[0])

    def test_dry_run_does_not_write_the_index_or_segment(self):
        before = self.index_with({}, {})
        self.receipt('PASS')
        result = harvest.harvest(self.manifest, self.out, 'test', self.library, dry=True)
        self.assertEqual((result['segments'], result['gates']), (['s1'], ['g1']))
        self.assertEqual((self.index.read_bytes(), self.index.stat().st_mtime_ns), before)
        self.assertFalse((self.library / 'segments/s1.edenmacro.json').exists())

    def test_pending_reference_is_not_harvested_even_if_its_legacy_hash_matches(self):
        reference = self.base / 'reference.png'
        reference.write_bytes(b'old reference')
        plan = json.loads(self.manifest.read_text())
        plan['gates']['g1'] = {'kind': 'image', 'reference': reference.name, 'acquire': True,
                               'sha256': hashlib.sha256(reference.read_bytes()).hexdigest(), 'cap': 1}
        self.manifest.write_text(json.dumps(plan))
        self.receipt('PASS')
        (self.base / 'reference.png.acquiring').touch()
        with self.assertRaisesRegex(RuntimeError, 'reference is not approved'):
            harvest.harvest(self.manifest, self.out, 'test', self.library)
        self.assertFalse(self.index.exists())

    def test_reserved_candidate_is_not_harvested_even_if_its_hash_matches(self):
        candidate = self.base / '.gate-candidate-leftover'
        candidate.write_bytes(b'unpublished')
        plan = json.loads(self.manifest.read_text())
        plan['gates']['g1'] = {'kind': 'image', 'reference': candidate.name,
                               'sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(), 'cap': 1}
        self.manifest.write_text(json.dumps(plan))
        self.receipt('PASS')
        with self.assertRaisesRegex(RuntimeError, 'reference is not approved'):
            harvest.harvest(self.manifest, self.out, 'test', self.library)
        self.assertFalse(self.index.exists())

    def test_reference_that_becomes_pending_during_read_is_not_harvested(self):
        reference = self.base / 'reference.png'
        reference.write_bytes(b'old reference')
        plan = json.loads(self.manifest.read_text())
        plan['gates']['g1'] = {'kind': 'image', 'reference': reference.name, 'acquire': True,
                               'sha256': hashlib.sha256(reference.read_bytes()).hexdigest(), 'cap': 1}
        self.manifest.write_text(json.dumps(plan))
        self.receipt('PASS')
        with mock.patch.object(harvest.runner, 'reference_approved', side_effect=[True, False]):
            with self.assertRaisesRegex(RuntimeError, 'reference is not approved'):
                harvest.harvest(self.manifest, self.out, 'test', self.library)
        self.assertFalse(self.index.exists())
        self.assertFalse((self.library / 'refs/g1.png').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
