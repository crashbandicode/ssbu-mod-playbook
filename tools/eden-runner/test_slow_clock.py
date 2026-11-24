"""Tests for the slow-clock inventory.

The probes use a 5 s deadline and a 10 s delay per read, so that their own plain run does not depend on the load
of the host: only a stall of 5 s between two statements would fail it."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


HARNESS = Path(__file__).with_name('slow_clock.py')


class SlowClockTests(unittest.TestCase):
    def inventory(self, source, helper=None):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            module = folder / 'test_fixture.py'
            module.write_text(textwrap.dedent(source), encoding='utf-8')
            if helper is not None:
                (folder / 'clock_helper.py').write_text(textwrap.dedent(helper), encoding='utf-8')
            result_path = folder / 'result.json'
            result = subprocess.run(
                [sys.executable, '-I', '-B', str(HARNESS), str(module), '--delay', '10', '--json', str(result_path)],
                capture_output=True, text=True,
            )
            return result.returncode, json.loads(result_path.read_text(encoding='utf-8'))

    def test_monotonic_deadline_depends_on_wall_time(self):
        code, report = self.inventory('''
            import time
            import unittest

            class Deadline(unittest.TestCase):
                def test_wait(self):
                    deadline = time.monotonic() + 5
                    self.assertLess(time.monotonic(), deadline)
        ''')
        self.assertEqual(code, 1)
        self.assertEqual([item['test'] for item in report['depends_on_wall_time']], ['test_fixture.Deadline.test_wait'])

    def test_default_bound_clock_depends_on_wall_time(self):
        code, report = self.inventory('''
            import time
            import unittest
            import clock_helper

            class Deadline(unittest.TestCase):
                def test_wait(self):
                    deadline = time.monotonic() + 5
                    self.assertTrue(clock_helper.wait(deadline))
        ''', '''
            import time

            def wait(cap, clock=time.monotonic):
                return clock() < cap
        ''')
        self.assertEqual(code, 1)
        self.assertEqual([item['test'] for item in report['depends_on_wall_time']], ['test_fixture.Deadline.test_wait'])
        self.assertIn('clock_helper.wait', report['default_bound_clocks_slowed'])

    def test_injected_fake_clock_does_not_depend_on_wall_time(self):
        code, report = self.inventory('''
            import unittest
            import clock_helper

            class Clock:
                def monotonic(self):
                    return 1000.0

            class Deadline(unittest.TestCase):
                def test_wait(self):
                    clock = Clock()
                    self.assertTrue(clock_helper.wait(clock.monotonic() + 5, clock.monotonic))
        ''', '''
            import time

            def wait(cap, clock=time.monotonic):
                return clock() < cap
        ''')
        self.assertEqual(code, 0)
        self.assertEqual(report['depends_on_wall_time'], [])
        self.assertEqual(report['failing_without_delay'], [])


if __name__ == '__main__':
    unittest.main()
