"""compile_peak.py sums its command's whole process tree: a parent holding 40 MB and a child holding 60 MB at once
peak at no less than 100 MB, and the child alone is the largest single process."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHILD = 'import time; b = bytearray(60_000_000); b[::4096] = b"x" * len(b[::4096]); time.sleep(1.5)'
PARENT = ('import subprocess, sys, time; b = bytearray(40_000_000); b[::4096] = b"x" * len(b[::4096]); '
          'subprocess.run([sys.executable, "-c", %r])' % CHILD)


class CompilePeakTests(unittest.TestCase):
    def test_the_peak_sums_the_process_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'peak.json'
            done = subprocess.run([sys.executable, '-I', str(HERE / 'compile_peak.py'), str(out), '--',
                                   sys.executable, '-c', PARENT], capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            summary = json.loads(out.read_text())
        self.assertEqual(summary['exit'], 0)
        self.assertGreaterEqual(summary['peak_tree_rss'], 100_000_000)
        self.assertGreaterEqual(summary['peak_single_rss'], 60_000_000)
        self.assertLess(summary['peak_single_rss'], summary['peak_tree_rss'])
        self.assertGreater(summary['lowest_mem_available'], 0)


if __name__ == '__main__':
    unittest.main()
