import fcntl
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import routes


STOCK = json.loads((Path(__file__).parent / 'test-data' / 'bridge-on-stock.json').read_text())
BOOT = ('379690490 plugin=ssbu-hw-bridge/0.1.0 h1=installed h2=installed h3=installed indicator=hooked '
        'native_ui=installed chord=deferred toggle=off reason=boot_default fixture=closed deluxe=absent '
        'heap_register=1 guards=0x7fffff tr_export=available server=started result=installed '
        'heap_allocations=9 heap_install=9')
READY = '379729902 [Bridge] chord ready'
ON = '379738765 [Bridge] toggle phase=on gen=1 reason=pad_chord source=pad_chord fixture=closed indicator=on'
HELLO = ('[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=0.2.0-rc.15 port=6979 guards=0x7fffff '
         'boot=0x46024760bd126949 uptime_ms=379738869 reconnects=0 event_latest=2 state_api=1 toggle_gen=1 '
         'toggle_reason=pad_chord refusals=0 last_refusal=none frame=5 wall=379738870')
INSTALL = BOOT + ' frame=0 wall=0'
BACKUP = '[Bridge] req backup result=ok files=27 bytes=82117 frame=87 wall=150922588'


class BridgeOnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / 'out'
        self.row = self.out / 'r00'
        self.row.mkdir(parents=True)
        self.trace = self.root / 'trace.jsonl'
        self.trace.touch()
        self.events = self.root / 'events.log'
        self.events.write_text(BOOT + '\n' + READY + '\n')
        self.instance = {'trace_jsonl': str(self.trace), 'bridge_events': str(self.events), 'profile': str(self.root)}
        self.ctx = SimpleNamespace(on='offline', row_dir=str(self.row), instance=self.instance,
                                   runner=SimpleNamespace(instances={'offline': self.instance}),
                                   check_stop=lambda: None, call=self.call)
        self.calls = []
        self.seconds = 0.0
        self.popen = []
        self.backup = BACKUP

    def call(self, name, args):
        self.calls.append((name, args))
        if name == 'trace.arm':
            row = dict(STOCK, wall_time_ns=str(self.now_ns()))
            self.trace.write_text(json.dumps(row) + '\n')
            return {'id': '3'}
        return {}

    def now_ns(self):
        return 1795382723341752108 + int(self.seconds * 1_000_000_000)

    def sleep(self, amount):
        self.seconds += amount
        if self.seconds >= 3.1 and len([c for c in self.calls if c[0] == 'input.button']) >= 4:
            if ON not in self.events.read_text():
                with self.events.open('a') as stream:
                    stream.write(ON + '\n')

    def spawn(self, argv, **kwargs):
        self.popen.append((argv, kwargs))
        run = self.out / 'bridge-run'
        (run / 'bridge.log').write_text('[Adapter] bridge connected boot=0x46024760bd126949\n'
                                       + HELLO + '\n' + INSTALL + '\n' + self.backup + '\n')
        return SimpleNamespace(pid=4321)

    def invoke(self, **kwargs):
        with patch('time.monotonic', side_effect=lambda: self.seconds), \
             patch('time.monotonic_ns', side_effect=lambda: int(self.seconds * 1_000_000_000)), \
             patch('time.time_ns', side_effect=self.now_ns), \
             patch('time.time', side_effect=lambda: self.now_ns() / 1_000_000_000), \
             patch('time.sleep', side_effect=self.sleep), \
             patch('subprocess.Popen', side_effect=self.spawn):
            return routes.bridge_on(self.ctx, **kwargs)

    def test_bridge_on_uses_real_witness_and_detached_capture(self):
        receipt = self.invoke(port=6979)
        self.assertEqual([call[0] for call in self.calls].count('trace.arm'), 1)
        self.assertEqual([call[1]['state'] for call in self.calls if call[0] == 'input.button'],
                         ['press'] * 4 + ['release'] * 4)
        self.assertEqual(receipt['chord']['event'], ON)
        self.assertEqual((receipt['hello'], receipt['install'], receipt['backup'], receipt['backup_open']),
                         (HELLO, INSTALL, BACKUP, True))
        self.assertFalse(receipt['reused'])
        run = self.out / 'bridge-run'
        self.assertEqual(receipt['capture'], {'pid': 4321, 'port': 6979, 'bridge_log': str(run / 'bridge.log')})
        argv, kwargs = self.popen[0]
        self.assertEqual(argv, [sys.executable, '-I', str(Path(routes.__file__).resolve().parent / 'switch_hw.py'),
                                'capture-bridge', str(run), '--bridge-host', '127.0.0.1:6979',
                                '--console', str(run / 'bridge-console.log')])
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(Path(kwargs['stderr'].name), run / 'capture.stderr.log')
        saved = json.loads((run / 'capture.json').read_text())
        self.assertEqual((saved['pid'], saved['port'], saved['instance'], saved['bridge_log']),
                         (4321, 6979, 'offline', str(run / 'bridge.log')))
        self.assertEqual(self.ctx.runner.instances['offline']['bridge_log'], str(run / 'bridge.log'))
        json.dumps(receipt)

    def test_bridge_on_reuses_locked_capture_and_skips_existing_on(self):
        self.events.write_text(BOOT + '\n' + READY + '\n' + ON + '\n')
        run = self.out / 'bridge-run'
        run.mkdir()
        (run / 'bridge.log').write_text(HELLO + '\n' + INSTALL + '\n' + BACKUP + '\n')
        (run / 'capture.json').write_text(json.dumps({'pid': 4312, 'port': 6979}))
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            receipt = self.invoke(port=6979)
        self.assertTrue(receipt['reused'])
        self.assertEqual(receipt['capture']['pid'], 4312)
        self.assertEqual(receipt['chord'], {'chord': 'already on', 'event': ON})
        self.assertEqual(self.popen, [])
        self.assertFalse(any(name == 'input.button' for name, _ in self.calls))

    def test_bridge_on_requires_trace(self):
        del self.instance['trace_jsonl']
        with self.assertRaisesRegex(RuntimeError, 'bridge_on needs a trace run'):
            self.invoke(port=6979)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.popen, [])

    def test_bridge_on_requires_port_argument_or_profile_file(self):
        with self.assertRaisesRegex(RuntimeError, r'port.*port.txt'):
            self.invoke()
        self.assertEqual(self.popen, [])

    def test_bridge_on_reads_profile_port_and_reports_refused_backup_without_failing(self):
        self.backup = '[Bridge] req backup result=refused reason=busy frame=87 wall=150922588'
        port = self.root / 'user/sdmc/ultimate/ssbu-hw-bridge/port.txt'
        port.parent.mkdir(parents=True)
        port.write_text('6979\n')
        receipt = self.invoke()
        self.assertEqual(receipt['capture']['port'], 6979)
        self.assertEqual(receipt['backup'], self.backup)
        self.assertFalse(receipt['backup_open'])

    def test_bridge_on_missing_backup_reply_is_recorded_without_failure(self):
        self.backup = ''
        receipt = self.invoke(port=6979)
        self.assertIsNone(receipt['backup'])
        self.assertFalse(receipt['backup_open'])
        self.assertGreaterEqual(self.seconds, 15)


if __name__ == '__main__':
    unittest.main()
