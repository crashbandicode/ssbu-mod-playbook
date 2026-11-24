import fcntl
import json
import re
import socket
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import routes
import state_gate


STOCK = json.loads((Path(__file__).parent / 'test-data' / 'bridge-on-stock.json').read_text())
BOOT = ('379690490 plugin=ssbu-hw-bridge/0.1.0 h1=installed h2=installed h3=installed indicator=hooked '
        'native_ui=installed chord=deferred toggle=off reason=boot_default fixture=closed deluxe=absent '
        'heap_register=1 guards=0x7fffff tr_export=available server=started result=installed '
        'heap_allocations=9 heap_install=9')
READY = '379729902 [Bridge] chord ready'
LISTENING = '379738760 [Bridge] listening port=6979 rebinds=0'
ON = '379738765 [Bridge] toggle phase=on gen=1 reason=pad_chord source=pad_chord fixture=closed indicator=on'
FAIL_BOOT = ('382550829 plugin=ssbu-hw-bridge/0.1.0 h1=installed h2=installed h3=installed indicator=hooked '
             'native_ui=installed chord=deferred toggle=off reason=boot_default fixture=closed deluxe=absent '
             'heap_register=1 guards=0x7fffff tr_export=available server=started result=installed '
             'heap_allocations=9 heap_install=9')
FAIL_READY = '382593020 [Bridge] chord ready'
LISTEN_FAILED = '382602979 [Bridge] listen failed port=6979 call=bind errno=98 retry_ms=250'
FAIL_ON = '382602983 [Bridge] toggle phase=on gen=1 reason=pad_chord source=pad_chord fixture=closed indicator=on'
HELLO = ('[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=0.2.0-rc.15 port=6979 guards=0x7fffff '
         'boot=0x46024760bd126949 uptime_ms=379738869 reconnects=0 event_latest=2 state_api=1 toggle_gen=1 '
         'toggle_reason=pad_chord refusals=0 last_refusal=none frame=5 wall=379738870')
INSTALL = BOOT + ' frame=0 wall=0'
BACKUP = '[Bridge] req backup result=ok files=27 bytes=82117 frame=87 wall=150922588'
STUB_FIXTURE = Path(__file__).parent / 'fixtures' / 'bridge-on' / 'stub-on-yoga-71158421'
STUB_EVENTS = (STUB_FIXTURE / 'events.log').read_text().splitlines()
STUB_CAPTURE = (STUB_FIXTURE / 'bridge.log').read_text().splitlines(keepends=True)
STUB_PREAMBLE = ''.join(STUB_CAPTURE[:6])
STUB_UI = ''.join(STUB_CAPTURE[6:])
STUB_BOOT = '0xa1e80deccd2a9a15'
FOREIGN_BOOT = '0x1111111111111111'


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
        self.stub = False
        self.status = {'running': True, 'loading_complete': True}
        self.listener = 'LISTEN users:(("eden",pid=712,fd=42))'
        self.stub_samples = []
        self.stub_default = STUB_UI
        self.bridge_requests = []
        self.ready_at = None

    def call(self, name, args=None):
        self.calls.append((name, args))
        if name == 'trace.arm':
            row = dict(STOCK, wall_time_ns=str(self.now_ns()))
            self.trace.write_text(json.dumps(row) + '\n')
            return {'id': '3'}
        if name == 'settings.get':
            return {'value': 'true' if self.stub else 'false'}
        if name == 'status':
            return self.status
        return {}

    def now_ns(self):
        return 1795382723341752108 + int(self.seconds * 1_000_000_000)

    def sleep(self, amount):
        self.seconds += amount
        if self.stub and self.ready_at is not None and self.seconds >= self.ready_at:
            if STUB_EVENTS[1] not in self.events.read_text():
                with self.events.open('a') as stream:
                    stream.write(STUB_EVENTS[1] + '\n')
        if self.seconds >= 3.1 and len([c for c in self.calls if c[0] == 'input.button']) >= 4:
            listening, on = STUB_EVENTS[2:] if self.stub else (LISTENING, ON)
            if on not in self.events.read_text():
                with self.events.open('a') as stream:
                    stream.write(listening + '\n' + on + '\n')

    def spawn(self, argv, **kwargs):
        self.popen.append((argv, kwargs))
        run = self.out / 'bridge-run'
        (run / 'bridge.log').write_text(STUB_PREAMBLE if self.stub else
                                       '[Adapter] bridge connected boot=0x46024760bd126949\n'
                                       + HELLO + '\n' + INSTALL + '\n' + self.backup + '\n')
        return SimpleNamespace(pid=4321)

    def configure_stub(self):
        self.stub = True
        del self.instance['trace_jsonl']
        self.events.write_text('\n'.join(STUB_EVENTS[:2]) + '\n')
        self.journal = self.root / 'control.jsonl'
        self.instance['control_log'] = str(self.journal)
        self.journal.write_text(''.join(json.dumps(row) + '\n' for row in (
            {'event': 'request', 'method': 'game.load', 'time': 'load-time'},
            {'event': 'request', 'method': 'status', 'time': 'status-time'},
            {'event': 'request', 'method': 'settings.get', 'time': 'settings-time'},
            {'event': 'request', 'method': 'modules.list', 'time': 'modules-time'},
        )))
        (self.root / 'run.json').write_text(json.dumps({'owned_pids': [{'pid': 712}]}))

    def spool(self, ctx, *requests):
        self.assertIs(ctx, self.ctx)
        self.assertEqual(requests, ('providers', 'state ssbu.ui'))
        self.bridge_requests.append(requests)
        sample = self.stub_samples.pop(0) if self.stub_samples else self.stub_default
        with (self.out / 'bridge-run' / 'bridge.log').open('a') as stream:
            stream.write(sample)

    def sample(self, *, active='yes', boot=STUB_BOOT, screen='title'):
        text = STUB_UI.replace(STUB_BOOT, boot)
        text = text.replace('observation.active=yes', f'observation.active={active}')
        text = text.replace('screen=title', f'screen={screen}')
        lines = text.splitlines()
        payload = [state_gate.SUFFIX.sub('', line).split(' ', 2)[2]
                   for line in lines if re.search(r'\[Bridge\] state\.\d+ ', line)]
        crc = zlib.crc32('\n'.join(payload).encode())
        text = re.sub(r'crc=[0-9a-f]{8}', f'crc={crc:08x}', text)
        block = state_gate.latest(text, 'ssbu.ui')
        self.assertIsNotNone(block)
        self.assertEqual((block['boot'], block['values']['observation.active'], block['values']['screen']),
                         (boot, active, screen))
        return text

    def invoke(self, **kwargs):
        with patch('time.monotonic', side_effect=lambda: self.seconds), \
             patch('time.monotonic_ns', side_effect=lambda: int(self.seconds * 1_000_000_000)), \
             patch('time.time_ns', side_effect=self.now_ns), \
             patch('time.time', side_effect=lambda: self.now_ns() / 1_000_000_000), \
             patch('time.sleep', side_effect=self.sleep), \
             patch('subprocess.Popen', side_effect=self.spawn), \
             patch.object(routes, '_bridge_on_listener_owner', return_value=self.listener, create=True), \
             patch.object(routes, 'bridge_v2', side_effect=self.spool):
            return routes.bridge_on(self.ctx, **kwargs)

    def test_bridge_on_uses_real_witness_and_detached_capture(self):
        receipt = self.invoke(port=6979)
        self.assertEqual([call[0] for call in self.calls].count('trace.arm'), 1)
        self.assertEqual([call[1]['state'] for call in self.calls if call[0] == 'input.button'],
                         ['press'] * 4 + ['release'] * 4)
        self.assertEqual(receipt['chord']['event'], ON)
        self.assertEqual(receipt['listening'], LISTENING)
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
        self.events.write_text(BOOT + '\n' + READY + '\n' + LISTENING + '\n' + ON + '\n')
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
        self.assertEqual(receipt['listening'], LISTENING)
        self.assertEqual(self.popen, [])
        self.assertFalse(any(name == 'input.button' for name, _ in self.calls))

    def test_bridge_on_requires_trace_or_stub_on(self):
        del self.instance['trace_jsonl']
        with self.assertRaisesRegex(RuntimeError, r'needs a trace run.*or a stub-on run'):
            self.invoke(port=6979)
        self.assertEqual(self.calls, [('settings.get', {'key': 'core.use_gdbstub'})])
        self.assertEqual(self.popen, [])

    def test_bridge_on_stub_on_reads_the_title_from_the_bridge_and_takes_no_picture(self):
        self.configure_stub()
        receipt = self.invoke(port=6980)
        self.assertEqual(receipt['witness'], 'stub-on')
        self.assertEqual((receipt['title']['screen'], receipt['title']['matched']), ('title', 'prompt'))
        self.assertEqual(receipt['title']['boot'], STUB_BOOT)
        self.assertEqual(receipt['chord']['pre_chord']['journal']['inputs_after_load'], 0)
        self.assertEqual(receipt['chord']['pre_chord']['journal']['requests_after_load'], 3)
        self.assertEqual(receipt['chord']['event'], STUB_EVENTS[3])
        self.assertEqual(len([args for method, args in self.calls
                              if method == 'input.button' and args['state'] == 'press']), 4)
        self.assertFalse(any(method in ('shot', 'screenshot') for method, _ in self.calls))
        self.assertEqual(self.bridge_requests, [('providers', 'state ssbu.ui')])
        self.assertEqual(json.loads((self.row / 'routes-bridge-on.json').read_text()), receipt)

    def test_bridge_on_stub_on_refuses_input_since_game_load(self):
        self.configure_stub()
        with self.journal.open('a') as stream:
            stream.write(json.dumps({'event': 'request', 'method': 'input.button', 'time': 'input-time'}) + '\n')
        with self.assertRaisesRegex(RuntimeError, r'input was sent since game.load \(input.button at input-time\)'):
            self.invoke(port=6980)
        self.assertFalse(any(method == 'input.button' for method, _ in self.calls))
        self.assertEqual(self.popen, [])

    def test_bridge_on_stub_on_refuses_without_this_boots_control_journal(self):
        self.configure_stub()
        for contents in (None, json.dumps({'event': 'request', 'method': 'status', 'time': 'now'}) + '\n'):
            with self.subTest(contents=contents):
                if contents is None:
                    self.journal.unlink(missing_ok=True)
                else:
                    self.journal.write_text(contents)
                with self.assertRaisesRegex(RuntimeError, r'no control journal with this boot.s game.load'):
                    self.invoke(port=6980)
                self.assertFalse(any(method == 'input.button' for method, _ in self.calls))
                self.assertEqual(self.popen, [])

    def test_bridge_on_stub_on_waits_for_chord_ready_instead_of_one_shot(self):
        self.configure_stub()
        self.events.write_text(STUB_EVENTS[0] + '\n')
        self.ready_at = .3
        receipt = self.invoke(port=6980)
        self.assertEqual(receipt['chord']['pre_chord']['chord_ready'], STUB_EVENTS[1])
        self.assertGreaterEqual(self.seconds, 3.4)

    def test_bridge_on_stub_on_fails_when_the_first_active_screen_is_not_the_title(self):
        self.configure_stub()
        self.stub_default = self.sample(screen='main')
        with self.assertRaisesRegex(RuntimeError, r'first screen it reports is main/title.press_any, not the Title'):
            self.invoke(port=6980)

    def test_bridge_on_stub_on_skips_inactive_and_foreign_boot_observations(self):
        self.configure_stub()
        self.stub_samples = [self.sample(active='no'), self.sample(boot=FOREIGN_BOOT), STUB_UI]
        receipt = self.invoke(port=6980)
        self.assertEqual((receipt['title']['boot'], receipt['title']['matched']), (STUB_BOOT, 'prompt'))
        self.assertEqual(len(self.bridge_requests), 3)

    def test_bridge_on_stub_on_refuses_a_listener_owned_by_another_pid(self):
        self.configure_stub()
        self.listener = 'LISTEN users:(("other",pid=999,fd=5))'
        with self.assertRaisesRegex(RuntimeError, r'port 6980 is not bound by the owned Eden \(pid 712\)'):
            self.invoke(port=6980)
        self.assertEqual(self.popen, [])

    def test_bridge_on_stub_on_refuses_a_corrupt_control_journal(self):
        self.configure_stub()
        with self.journal.open('a') as stream:
            stream.write('{not-json}\n')
        with self.assertRaisesRegex(RuntimeError, r'no control journal with this boot.s game.load'):
            self.invoke(port=6980)
        self.assertFalse(any(method == 'input.button' for method, _ in self.calls))

    def test_bridge_on_stub_on_refuses_macro_play_but_allows_schema_and_reset(self):
        self.configure_stub()
        with self.journal.open('a') as stream:
            for method in ('input.schema', 'input.reset', 'macro.play'):
                stream.write(json.dumps({'event': 'request', 'method': method, 'time': 't'}) + '\n')
        with self.assertRaisesRegex(RuntimeError, r'input was sent since game.load \(macro.play at t\)'):
            self.invoke(port=6980)
        self.assertFalse(any(method == 'input.button' for method, _ in self.calls))

    def test_bridge_on_stub_on_times_out_without_an_active_observation(self):
        self.configure_stub()
        self.stub_default = self.sample(active='no')
        with self.assertRaisesRegex(RuntimeError, r'no active ssbu.ui observation of this boot within 10 s'):
            self.invoke(port=6980)

    def test_bridge_on_stub_on_readiness_timeout_names_false_conditions(self):
        self.configure_stub()
        self.events.write_text(STUB_EVENTS[0] + '\n')
        self.status = {'running': False, 'loading_complete': False}
        with self.assertRaises(RuntimeError) as caught:
            self.invoke(port=6980)
        for condition in ('chord_ready', 'running', 'loading_complete'):
            self.assertIn(condition, str(caught.exception))
        self.assertFalse(any(method == 'input.button' for method, _ in self.calls))
        self.assertEqual(self.popen, [])

    def test_bridge_on_stub_on_refuses_without_owned_run_json(self):
        self.configure_stub()
        (self.root / 'run.json').unlink()
        with self.assertRaisesRegex(RuntimeError, r'run\.json'):
            self.invoke(port=6980)
        self.assertEqual(self.popen, [])

    def test_bridge_on_stub_on_reports_missing_ss(self):
        with patch('subprocess.check_output', side_effect=FileNotFoundError('ss')):
            with self.assertRaisesRegex(RuntimeError, r'ss'):
                routes._bridge_on_listener_owner(6980)

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

    def test_bridge_on_refuses_real_bind_failure_before_starting_capture(self):
        self.events.write_text('\n'.join((FAIL_BOOT, FAIL_READY, LISTEN_FAILED, FAIL_ON)) + '\n')
        with self.assertRaises(RuntimeError) as error:
            self.invoke(port=6979)
        self.assertEqual(str(error.exception),
                         'bridge_on: this Eden\'s bridge is not listening on port 6979; '
                         'expected "[Bridge] listening port=6979" in its own events log, saw: '
                         + LISTEN_FAILED + '. Another process holds the port: give the run its own free port '
                         '(port.txt) and boot again. No capture was started.')
        self.assertEqual(self.popen, [])
        self.assertFalse((self.out / 'bridge-run' / 'capture.json').exists())
        self.assertNotIn('bridge_log', self.ctx.runner.instances['offline'])

    def test_bridge_on_refuses_listening_on_another_port_before_reuse(self):
        other = '382602979 [Bridge] listening port=6978 rebinds=0'
        self.events.write_text('\n'.join((FAIL_BOOT, FAIL_READY, other, FAIL_ON)) + '\n')
        run = self.out / 'bridge-run'
        run.mkdir()
        (run / 'capture.json').write_text(json.dumps({'pid': 4312, 'port': 6979}))
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError) as error:
                self.invoke(port=6979)
        self.assertIn(other, str(error.exception))
        self.assertEqual(self.popen, [])
        self.assertNotIn('bridge_log', self.ctx.runner.instances['offline'])

    def test_bridge_on_waits_for_own_listener(self):
        self.events.write_text(BOOT + '\n' + READY + '\n' + ON + '\n')
        sleep = self.sleep

        def publish(amount):
            sleep(amount)
            if self.seconds >= .1 and LISTENING not in self.events.read_text():
                with self.events.open('a') as stream:
                    stream.write(LISTENING + '\n')

        with patch.object(self, 'sleep', side_effect=publish):
            receipt = self.invoke(port=6979)
        self.assertEqual(receipt['listening'], LISTENING)
        self.assertGreaterEqual(self.seconds, .1)


class FreePortTests(unittest.TestCase):
    def pair(self):
        for _ in range(100):
            first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            first.bind(('127.0.0.1', 0))
            port = first.getsockname()[1]
            second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if port == 65535:
                    raise OSError('no next port')
                second.bind(('127.0.0.1', port + 1))
            except OSError:
                first.close()
                second.close()
                continue
            first.listen()
            second.listen()
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            return port, second
        self.fail('could not find two adjacent local test ports')

    def test_free_port_skips_occupied_listener(self):
        port, second = self.pair()
        second.close()
        self.assertEqual(routes.free_port(first=port, last=port + 1, host='127.0.0.1'), port + 1)

    def test_free_port_refuses_fully_occupied_range(self):
        port, _ = self.pair()
        with self.assertRaisesRegex(RuntimeError, 'no free.*port'):
            routes.free_port(first=port, last=port + 1, host='127.0.0.1')


if __name__ == '__main__':
    unittest.main()
