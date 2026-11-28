import copy
import fcntl
import json
import os
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
TRANSPORT = ('474261175 [Bridge] transport=file stream=sd:/ultimate/ssbu-hw-bridge/stream.log '
             'requests=sd:/ultimate/ssbu-hw-bridge/requests.txt')
LISTEN_EINVAL = '585593349 [Bridge] listen failed port=6979 call=listen errno=22 retry_ms=250'
MARKER = 'user/sdmc/ultimate/ssbu-hw-bridge/file-transport'
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
RUN1 = (Path(__file__).parent / 'fixtures' / 'bridge-on' / 'stub-on-yoga-e-run1' / 'bridge.log').read_text().splitlines(keepends=True)
RUN1_PREAMBLE = ''.join(RUN1[:3])
RUN1_OPENING = ''.join(RUN1[52:101])
RUN1_LAST_OPENING = ''.join(RUN1[101:150])
RUN1_PROMPT = ''.join(RUN1[150:199])
RUN1_BOOT = '0x1dc637526fb29a71'


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
        self.journal = self.root / 'control.jsonl'
        self.journal.write_text('')
        (self.root / 'run.json').write_text(json.dumps({'token': 'a' * 32, 'owned_pids': [{'pid': 712}]}))
        self.instance = {'trace_jsonl': str(self.trace), 'bridge_events': str(self.events), 'profile': str(self.root),
                         'control_log': str(self.journal)}
        self.ctx = SimpleNamespace(on='offline', row_dir=str(self.row), instance=self.instance,
                                   runner=SimpleNamespace(instances={'offline': self.instance}),
                                   check_stop=lambda: None, call=self.call, log=lambda _: None)
        self.ctx.runner.wait_macro = lambda *_: None
        self.calls = []
        self.seconds = 0.0
        self.popen = []
        self.backup = BACKUP
        self.stub = False
        self.lan_online = False
        self.owner_calls = []
        self.status = {'running': True, 'loading_complete': True}
        self.listener = 'LISTEN users:(("eden",pid=712,fd=42))'
        self.stub_samples = []
        self.stub_default = STUB_UI
        self.stub_preamble = STUB_PREAMBLE
        self.bridge_requests = []
        self.request_times = []
        self.ready_at = None
        self.chord_started = None
        self.toggle_enabled = True
        self.sleep_hook = None
        self.prompt_after_skip = False
        self.macro_error = None
        self.recorded_open_seen = 0

    def call(self, name, args=None):
        self.calls.append((name, args))
        if name == 'macro.status':
            return {'state': 'idle'}
        if name == 'macro.play' and self.macro_error:
            raise RuntimeError(self.macro_error)
        if name == 'input.button' and args['state'] == 'press' and self.chord_started is None:
            self.chord_started = self.seconds
        if name == 'trace.arm':
            row = dict(STOCK, wall_time_ns=str(self.now_ns()))
            self.trace.write_text(json.dumps(row) + '\n')
            return {'id': '3'}
        if name == 'settings.get':
            if args['key'] == 'core.lan_online':
                return {'value': 'true' if self.lan_online else 'false'}
            return {'value': 'true' if self.stub else 'false'}
        if name == 'status':
            return self.status
        return {}

    def now_ns(self):
        return 1795382723341752108 + int(self.seconds * 1_000_000_000)

    def sleep(self, amount):
        self.seconds += amount
        if self.sleep_hook is not None:
            self.sleep_hook(amount)
        if self.stub and self.ready_at is not None and self.seconds >= self.ready_at:
            if STUB_EVENTS[1] not in self.events.read_text():
                with self.events.open('a') as stream:
                    stream.write(STUB_EVENTS[1] + '\n')
        if (self.toggle_enabled and self.chord_started is not None and self.seconds - self.chord_started >= 3.1
                and len([c for c in self.calls if c[0] == 'input.button']) >= 4):
            listening, on = STUB_EVENTS[2:] if self.stub else (LISTENING, ON)
            if (Path(self.instance['profile']) / MARKER).exists():
                listening = TRANSPORT
            if on not in self.events.read_text():
                with self.events.open('a') as stream:
                    stream.write(listening + '\n' + on + '\n')

    def spawn(self, argv, **kwargs):
        self.popen.append((argv, kwargs))
        run = Path(argv[4])
        (run / 'bridge.log').write_text(self.stub_preamble if self.stub else
                                       '[Adapter] bridge connected boot=0x46024760bd126949\n'
                                       + HELLO + '\n' + INSTALL + '\n' + self.backup + '\n')
        return SimpleNamespace(pid=4321)

    def configure_stub(self):
        self.stub = True
        del self.instance['trace_jsonl']
        own_hello = HELLO.replace('0x46024760bd126949', STUB_BOOT).replace('port=6979', 'port=6980')
        self.events.write_text(own_hello + '\n' + '\n'.join(STUB_EVENTS[:2]) + '\n')
        self.journal = self.root / 'control.jsonl'
        self.instance['control_log'] = str(self.journal)
        self.journal.write_text(''.join(json.dumps(row) + '\n' for row in (
            {'event': 'request', 'method': 'game.load', 'time': 'load-time'},
            {'event': 'request', 'method': 'status', 'time': 'status-time'},
            {'event': 'request', 'method': 'settings.get', 'time': 'settings-time'},
            {'event': 'request', 'method': 'modules.list', 'time': 'modules-time'},
        )))
        (self.root / 'run.json').write_text(json.dumps({'token': 'a' * 32, 'owned_pids': [{'pid': 712}]}))

    def spool(self, ctx, *requests):
        self.assertIs(ctx, self.ctx)
        self.assertEqual(requests, ('providers', 'state ssbu.ui'))
        self.bridge_requests.append(requests)
        self.request_times.append(self.seconds)
        sample = self.stub_samples.pop(0) if self.stub_samples else self.stub_default
        if self.prompt_after_skip and any(method == 'macro.play' for method, _ in self.calls):
            sample = RUN1_PROMPT
        if sample == RUN1_OPENING:
            self.recorded_open_seen += 1
            if self.recorded_open_seen > 2:
                return
        with Path(self.instance['bridge_log']).open('a') as stream:
            stream.write(sample)

    def sample(self, *, active='yes', boot=STUB_BOOT, screen='title', fields=None):
        text = STUB_UI.replace(STUB_BOOT, boot)
        if boot == RUN1_BOOT:
            text = re.sub(r'^\[Bridge\] req providers result=.*$', RUN1[52].rstrip('\n'), text, flags=re.MULTILINE)
        text = text.replace('observation.active=yes', f'observation.active={active}')
        text = text.replace('screen=title', f'screen={screen}')
        for key, value in (fields or {}).items():
            pattern = r'(\[Bridge\] state\.\d+ ' + re.escape(key) + r'=).*?( frame=\d+ wall=\d+)$'
            text, count = re.subn(pattern, lambda m: m[1] + value + m[2], text, flags=re.MULTILINE)
            self.assertEqual(count, 1, key)
        lines = text.splitlines()
        payload = [state_gate.SUFFIX.sub('', line).split(' ', 2)[2]
                   for line in lines if re.search(r'\[Bridge\] state\.\d+ ', line)]
        crc = zlib.crc32('\n'.join(payload).encode())
        text = re.sub(r'crc=[0-9a-f]{8}', f'crc={crc:08x}', text)
        block = state_gate.latest(text, 'ssbu.ui')
        self.assertIsNotNone(block)
        self.assertEqual((block['boot'], block['values']['observation.active'], block['values']['screen']),
                         (boot, active, screen))
        for key, value in (fields or {}).items():
            self.assertEqual(block['values'][key], value)
        return text

    def opening(self):
        return self.sample(fields={'screen.context': 'opening', 'focus': 'title.opening_playback',
                                   'focus.id': 'opening_playback'})

    def title_without_prompt(self):
        return self.sample(fields={'focus': 'title', 'focus.source': 'unknown', 'focus.id': 'unknown',
                                   'focus.index': 'unknown', 'focus.label': 'unknown',
                                   'input.owner': 'unknown', 'dialog': 'unknown'})

    def slow_title_bridge(self, response):
        self.configure_stub()
        pending, overlaps = [], []

        def request(ctx, *requests):
            self.assertIs(ctx, self.ctx)
            self.assertEqual(requests, ('providers', 'state ssbu.ui'))
            self.bridge_requests.append(requests)
            self.request_times.append(self.seconds)
            if pending:
                overlaps.append(self.seconds)
            pending.append(self.seconds)

        def deliver(_):
            while pending and self.seconds - pending[0] >= .25 - 1e-9:
                pending.pop(0)
                with Path(self.instance['bridge_log']).open('a') as stream:
                    stream.write(response(bool(pending)))

        self.spool = request
        self.sleep_hook = deliver
        return overlaps

    def recorded_opening(self, *, prompt_after_skip=True):
        self.configure_stub()
        self.stub_preamble = RUN1_PREAMBLE
        self.stub_default = RUN1_OPENING
        self.prompt_after_skip = prompt_after_skip

    def macro_attempts(self):
        return [args for method, args in self.calls if method == 'macro.play']

    def assert_fresh_offsets(self, title, prefix, reply, index):
        boundary = len(prefix.encode())
        self.assertEqual(title['skip']['fresh'], {
            'request_bytes_before': boundary,
            'providers_reply_offset': boundary,
            'block_begin_offset': boundary + len(reply.splitlines(keepends=True)[0].encode()),
            'block_index': index,
        })

    def invoke(self, **kwargs):
        with patch('time.monotonic', side_effect=lambda: self.seconds), \
             patch('time.monotonic_ns', side_effect=lambda: int(self.seconds * 1_000_000_000)), \
             patch('time.time_ns', side_effect=self.now_ns), \
             patch('time.time', side_effect=lambda: self.now_ns() / 1_000_000_000), \
             patch('time.sleep', side_effect=self.sleep), \
             patch('subprocess.Popen', side_effect=self.spawn), \
             patch.object(routes, '_bridge_on_listener_owner', side_effect=self.owner, create=True), \
             patch.object(routes, 'bridge_v2', side_effect=self.spool):
            return routes.bridge_on(self.ctx, **kwargs)

    def owner(self, port):
        self.owner_calls.append(port)
        return [{'pid': int(pid), 'line': self.listener} for pid in re.findall(r'pid=(\d+)', self.listener)]

    def configure_lan(self):
        self.configure_stub()
        self.lan_online = True
        marker = Path(self.instance['profile']) / MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return marker

    def cold_stub_receipt(self):
        self.configure_stub()
        receipt = self.invoke(port=6980)
        path = self.row / 'routes-bridge-on.json'
        self.assertEqual(json.loads(path.read_text()), receipt)
        return receipt, path

    def stub_connection(self, boot=STUB_BOOT, *, reconnect=False, hello=None, install=True):
        if reconnect:
            note = f'[Adapter] reconnect same_boot=1 boot={boot} connects=2 cursor=0\n'
        else:
            note = f'[Adapter] bridge connected boot={boot}\n'
        hello = hello if hello is not None else STUB_CAPTURE[1].replace(STUB_BOOT, boot)
        return note + hello + (STUB_CAPTURE[2].replace(STUB_BOOT, boot) if install else '')

    def locked_capture(self, text):
        run = self.out / 'bridge-run'
        run.mkdir()
        (run / 'bridge.log').write_text(text)
        (run / 'capture.json').write_text(json.dumps({'pid': 4312, 'port': 6980, 'owner_pid': 712,
                                                     'instance': 'offline', 'run_token': 'a' * 32}))
        return run

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
        self.assertEqual(receipt['capture'], {'pid': 4321, 'port': 6979, 'bridge_log': str(run / 'bridge.log'),
                                              'owner_pid': 712, 'run_token': 'a' * 32})
        argv, kwargs = self.popen[0]
        self.assertEqual(argv, [sys.executable, '-I', str(Path(routes.__file__).resolve().parent / 'switch_hw.py'),
                                'capture-bridge', str(run), '--bridge-host', '127.0.0.1:6979',
                                '--owner-pid', '712', '--run-token', 'a' * 32,
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

    def test_default_greeting_timeout_is_still_sixty_seconds(self):
        self.configure_stub()
        self.stub_preamble = ''
        with self.assertRaisesRegex(RuntimeError, 'no bridge greeting within 60 s'):
            self.invoke(port=6980)

    def test_reused_socket_capture_must_match_this_run(self):
        self.cold_stub_receipt()
        run = self.out / 'bridge-run'
        path = run / 'capture.json'
        capture = json.loads(path.read_text())
        capture['run_token'] = 'b' * 32
        path.write_text(json.dumps(capture))
        with (run / 'bridge.lock').open('a+b') as lock:
            routes.portable_lock.lock_exclusive(lock, blocking=False)
            with self.assertRaisesRegex(RuntimeError, 'capture identity'):
                self.invoke(port=6980)

    def test_bridge_on_reuses_locked_capture_and_skips_existing_on(self):
        self.events.write_text(BOOT + '\n' + READY + '\n' + LISTENING + '\n' + ON + '\n')
        run = self.out / 'bridge-run'
        run.mkdir()
        (run / 'bridge.log').write_text(HELLO + '\n' + INSTALL + '\n' + BACKUP + '\n')
        (run / 'capture.json').write_text(json.dumps({'pid': 4312, 'port': 6979, 'owner_pid': 712,
                                                     'instance': 'offline', 'run_token': 'a' * 32}))
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            receipt = self.invoke(port=6979)
        self.assertTrue(receipt['reused'])
        self.assertEqual(receipt['capture']['pid'], 4312)
        self.assertEqual(receipt['chord']['chord'], 'already on')
        self.assertTrue(receipt['chord']['requalified'])
        self.assertTrue(receipt['chord']['stock'])
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

    def test_bridge_on_accepts_hello_from_connected_capture_when_events_have_no_hello(self):
        self.configure_stub()
        self.events.write_text('\n'.join(STUB_EVENTS[:2]) + '\n')
        receipt = self.invoke(port=6980)
        self.assertEqual(receipt['capture']['run_token'], 'a' * 32)
        self.assertEqual(receipt['hello'].split('boot=')[1].split()[0], STUB_BOOT)
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

    def test_bridge_on_waits_for_game_loading_complete_signal_before_pressing(self):
        self.configure_stub()
        self.status = {'running': True, 'loading_complete': False}
        self.ready_at = .2

        def loading_signal(_):
            if self.seconds >= 1.0:
                self.status = {'running': True, 'loading_complete': True}

        self.sleep_hook = loading_signal
        receipt = self.invoke(port=6980)
        self.assertGreaterEqual(self.chord_started, 1.0)
        self.assertTrue(receipt['chord']['pre_chord']['status']['loading_complete'])

    def test_bridge_on_trace_waits_for_game_loading_complete_signal_before_pressing(self):
        self.configure_stub()
        self.instance['trace_jsonl'] = str(self.trace)
        self.status = {'running': True, 'loading_complete': False}

        def loading_signal(_):
            if self.seconds >= 1.0:
                self.status = {'running': True, 'loading_complete': True}

        self.sleep_hook = loading_signal
        receipt = self.invoke(port=6980)
        self.assertGreaterEqual(self.chord_started, 1.0)
        self.assertEqual(receipt['chord']['status']['loading_complete'], True)

    def test_bridge_on_stub_on_fails_when_the_first_active_screen_is_not_the_title(self):
        self.configure_stub()
        self.stub_default = self.sample(screen='main')
        with self.assertRaisesRegex(RuntimeError, r'first screen it reports is main/title.press_any, not the Title'):
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])

    def test_bridge_on_stub_on_skips_inactive_and_foreign_boot_observations(self):
        self.configure_stub()
        self.stub_samples = [self.sample(active='no'), self.sample(boot=FOREIGN_BOOT), STUB_UI]
        receipt = self.invoke(port=6980)
        self.assertEqual((receipt['title']['boot'], receipt['title']['matched']), (STUB_BOOT, 'prompt'))
        self.assertEqual(len(self.bridge_requests), 3)

    def test_bridge_on_stub_on_already_on_checks_no_title_and_sends_no_chord(self):
        self.stub = True
        self.events.write_text('\n'.join(STUB_EVENTS[:2]) + '\n')
        first = self.invoke(port=6980)
        prior = self.row / 'routes-bridge-on.json'
        self.assertEqual(json.loads(prior.read_text()), first)
        self.assertEqual(first['witness'], 'trace')
        self.configure_stub()
        self.events.write_text('\n'.join(STUB_EVENTS) + '\n')
        self.journal.unlink()
        run = self.out / 'bridge-run'
        buttons = [call for call in self.calls if call[0] == 'input.button']
        with (run / 'bridge.lock').open('w') as lock, \
             patch.object(routes, '_bridge_on_stub_title', return_value={'unexpected': True}) as title_read:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            receipt = self.invoke(port=6980)
            with self.subTest(boot='foreign'):
                other = copy.deepcopy(first)
                other['hello'] = other['hello'].replace(STUB_BOOT, FOREIGN_BOOT)
                prior.write_text(json.dumps(other))
                with self.assertRaisesRegex(RuntimeError, f'from boot {FOREIGN_BOOT}.*greeting is boot {STUB_BOOT}'):
                    self.invoke(port=6980)
        title_read.assert_not_called()
        self.assertEqual(receipt['title'], {
            'checked': False, 'reason': 'bridge already On before this call',
            'qualified_by': {'receipt': str(prior), 'witness': 'trace', 'title': None, 'boot': STUB_BOOT}})
        self.assertEqual(receipt['chord']['chord'], 'already on')
        self.assertTrue(receipt['reused'])
        self.assertEqual(len(self.popen), 1)
        self.assertEqual(self.bridge_requests, [])
        self.assertEqual([call for call in self.calls if call[0] == 'input.button'], buttons)

    def test_bridge_on_stub_on_title_record_has_the_checked_blocks_frame(self):
        self.configure_stub()
        receipt = self.invoke(port=6980)
        title = receipt['title']
        self.assertEqual((title.get('frame'), title['rev'], title['matched']), (31, 4, 'prompt'))
        self.assertEqual(title.get('wall'), 474261702)
        self.assertIsInstance(title.get('frame'), int)
        self.assertIsInstance(title.get('wall'), int)
        self.assertEqual(receipt['chord']['pre_chord']['status'], self.status)
        self.assertEqual(receipt['chord']['pre_chord']['journal']['inputs_after_load'], 0)

    def test_stub_title_keeps_one_request_in_flight(self):
        overlaps = self.slow_title_bridge(lambda _: self.sample())
        title = self.invoke(port=6980)['title']
        self.assertEqual(title['matched'], 'prompt')
        self.assertEqual(overlaps, [])

    def test_stub_title_passes_on_a_slow_bridge(self):
        title_seen = []

        def response(queued):
            if queued:
                return self.sample(active='no', screen='unknown')
            if self.macro_attempts():
                return self.sample()
            if not title_seen:
                title_seen.append(True)
                return self.title_without_prompt()
            return self.opening()

        overlaps = self.slow_title_bridge(response)
        try:
            title = self.invoke(port=6980)['title']
        except RuntimeError as error:
            self.fail(f'slow bridge did not reach the prompt: {error}')
        self.assertEqual(overlaps, [])
        self.assertEqual([row['focus'] for row in title['not_yet']], ['title', 'title.opening_playback'])
        self.assertEqual(title['matched'], 'prompt')
        self.assertEqual(title['skip']['button'], 'plus')
        self.assertEqual(len(self.macro_attempts()), 1)

    def test_stub_title_asks_again_after_the_reply_bound(self):
        self.configure_stub()

        def request(ctx, *requests):
            self.assertIs(ctx, self.ctx)
            self.assertEqual(requests, ('providers', 'state ssbu.ui'))
            self.bridge_requests.append(requests)
            self.request_times.append(self.seconds)
            if len(self.request_times) == 2:
                with Path(self.instance['bridge_log']).open('a') as stream:
                    stream.write(self.sample())

        self.spool = request
        title = self.invoke(port=6980)['title']
        self.assertEqual(title['matched'], 'prompt')
        self.assertEqual(len(self.request_times), 2)
        self.assertGreaterEqual(self.request_times[1] - self.request_times[0], 2)
        self.assertLess(self.request_times[1] - self.request_times[0], 2.1)
        self.assertEqual(self.macro_attempts(), [])

    def test_bridge_on_stub_on_does_not_accept_the_title_without_its_prompt(self):
        self.configure_stub()
        self.stub_samples, self.stub_default = [self.title_without_prompt()], ''
        with self.assertRaisesRegex(RuntimeError, r'Title prompt was not observed within 10 s.*title/title.*rev=4 frame=31'):
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertFalse((self.row / 'routes-bridge-on.json').exists())

    def test_bridge_on_stub_on_does_not_accept_the_opening_playback(self):
        self.configure_stub()
        self.stub_samples, self.stub_default = [self.opening(), self.opening()], ''
        with self.assertRaisesRegex(RuntimeError, r'opening was skipped with one press on rev=4 frame=31.*within 10 s.*title/title.opening_playback'):
            self.invoke(port=6980)
        self.assertLess(self.seconds, 20)
        self.assertEqual(len(self.macro_attempts()), 1)
        self.assertFalse((self.row / 'routes-bridge-on.json').exists())

    def test_bridge_on_stub_on_waits_through_the_opening_for_the_prompt(self):
        self.configure_stub()
        self.stub_samples = [self.opening(), self.opening()] + [''] * 4 + [STUB_UI]
        self.stub_default = ''
        receipt = self.invoke(port=6980)
        self.assertEqual(receipt['title']['matched'], 'prompt')
        self.assertLess(receipt['title'].get('waited_s', 100), 10)
        self.assertEqual(len(self.macro_attempts()), 1)
        self.assertEqual(receipt['title'].get('not_yet'), [{
            'rev': 4, 'frame': 31, 'screen': 'title', 'screen.context': 'opening',
            'focus': 'title.opening_playback'}])

    def test_bridge_on_stub_on_resets_prompt_deadline_after_opening_handoff(self):
        self.configure_stub()
        self.stub_samples = [self.opening(), self.opening()] + [''] * 4 + [self.title_without_prompt()]
        self.stub_default = ''
        with self.assertRaisesRegex(RuntimeError, r'opening was skipped with one press.*within 10 s.*title/title'):
            self.invoke(port=6980)
        self.assertLess(self.seconds, 20)
        self.assertEqual(len(self.macro_attempts()), 1)

    def test_bridge_on_stub_on_skips_the_opening_with_one_press(self):
        """The lines are recorded; the prompt's order after the press is modelled."""
        self.recorded_opening()
        try:
            receipt = self.invoke(port=6980)
        except RuntimeError as error:
            self.fail(f'expected one skip and a prompt: {error}')
        self.assertEqual(len(self.macro_attempts()), 1)
        title = receipt['title']
        skip = title.get('skip')
        self.assertIsInstance(skip, dict)
        self.assertEqual(skip['button'], 'plus')
        self.assertEqual(skip['sent_on'], {'rev': 12, 'frame': 72, 'wall': 506017830,
                                          'screen': 'title', 'screen.context': 'opening',
                                          'focus': 'title.opening_playback'})
        self.assertTrue(skip['send'])
        self.assertLess(skip['prompt_after_s'], 10)
        self.assertEqual((title['matched'], title['frame'], title['boot']), ('prompt', 7860, RUN1_BOOT))

    def test_bridge_on_stub_on_never_presses_twice(self):
        self.recorded_opening(prompt_after_skip=False)
        with self.assertRaisesRegex(RuntimeError, r'opening was skipped with one press on rev=12 frame=72.*within 10 s'):
            self.invoke(port=6980)
        self.assertEqual(len(self.macro_attempts()), 1)

    def test_bridge_on_stub_on_stale_opening_and_silent_bridge_sends_nothing(self):
        self.recorded_opening(prompt_after_skip=False)
        self.stub_preamble = RUN1_PREAMBLE + RUN1_OPENING
        self.stub_default = ''
        with self.assertRaises(RuntimeError) as stopped:
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertRegex(str(stopped.exception), 'opening was seen but no fresh ssbu.ui reply confirmed it; nothing was sent')

    def test_bridge_on_stub_on_stale_opening_then_modelled_prompt_does_not_pass_on_a_stale_press(self):
        self.recorded_opening()
        self.stub_preamble = RUN1_PREAMBLE + RUN1_OPENING
        self.stub_default = ''
        try:
            self.invoke(port=6980)
        except RuntimeError as error:
            stopped = error
        else:
            self.fail('stale opening incorrectly sent a press and reached the modelled prompt')
        self.assertEqual(self.macro_attempts(), [])
        self.assertRegex(str(stopped), 'opening was seen but no fresh ssbu.ui reply confirmed it; nothing was sent')
        self.assertFalse((self.row / 'routes-bridge-on.json').exists())

    def test_bridge_on_stub_on_stale_opening_and_stale_passive_title_have_no_fresh_reply(self):
        self.recorded_opening(prompt_after_skip=False)
        passive = self.sample(boot=RUN1_BOOT, fields={
            'focus': 'title', 'focus.source': 'unknown', 'focus.id': 'unknown',
            'focus.index': 'unknown', 'focus.label': 'unknown',
            'input.owner': 'unknown', 'dialog': 'unknown'})
        self.stub_preamble = RUN1_PREAMBLE + RUN1_OPENING + passive
        self.stub_default = ''
        with self.assertRaisesRegex(RuntimeError, 'opening was seen but no fresh ssbu.ui reply confirmed it; nothing was sent'):
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])

    def test_bridge_on_stub_on_fresh_opening_reply_sends_once(self):
        self.recorded_opening()
        self.stub_samples, self.stub_default = [RUN1_OPENING, RUN1_LAST_OPENING], ''
        try:
            title = self.invoke(port=6980)['title']
        except RuntimeError as error:
            self.fail(f'expected rechecked opening and modelled prompt: {error}')
        self.assertEqual(len(self.macro_attempts()), 1)
        self.assertEqual((title['skip']['sent_on']['rev'], title['skip']['sent_on']['frame'],
                          title['skip']['sent_on']['wall']), (1193, 7849, 506147525))
        self.assert_fresh_offsets(title, RUN1_PREAMBLE + RUN1_OPENING, RUN1_LAST_OPENING, 1)
        self.assertEqual((title['matched'], title['frame']), ('prompt', 7860))

    def split_recheck(self, prompt_after_skip):
        self.recorded_opening(prompt_after_skip=prompt_after_skip)
        lines = RUN1_LAST_OPENING.splitlines(keepends=True)
        self.stub_samples = [RUN1_OPENING + ''.join(lines[:-1]), lines[-1]]
        self.stub_default = ''
        with self.assertRaises(RuntimeError) as stopped:
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertRegex(str(stopped.exception), 'opening was seen but no fresh ssbu.ui reply confirmed it; nothing was sent')
        self.assertFalse((self.row / 'routes-bridge-on.json').exists())

    def test_bridge_on_stub_on_reply_begun_before_the_recheck_sends_nothing(self):
        self.split_recheck(False)

    def test_bridge_on_stub_on_reply_begun_before_recheck_cannot_unlock_modelled_prompt(self):
        self.split_recheck(True)

    def test_bridge_on_stub_on_recheck_reply_without_its_providers_fence_sends_nothing(self):
        self.recorded_opening(prompt_after_skip=False)
        no_fence = ''.join(RUN1_LAST_OPENING.splitlines(keepends=True)[1:])
        self.stub_samples, self.stub_default = [RUN1_OPENING, no_fence], ''
        with self.assertRaises(RuntimeError) as stopped:
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertRegex(str(stopped.exception), 'opening was seen but no fresh ssbu.ui reply confirmed it; nothing was sent')

    def test_bridge_on_stub_on_provider_reply_begun_before_recheck_is_not_fresh(self):
        self.recorded_opening(prompt_after_skip=False)
        lines = RUN1_LAST_OPENING.splitlines(keepends=True)
        cut = len(lines[0]) // 2
        self.stub_samples = [RUN1_OPENING + lines[0][:cut], lines[0][cut:] + ''.join(lines[1:])]
        self.stub_default = ''
        with self.assertRaises(RuntimeError) as stopped:
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertRegex(str(stopped.exception), 'opening was seen but no fresh ssbu.ui reply confirmed it; nothing was sent')

    def test_bridge_on_stub_on_embedded_provider_text_is_not_a_reply(self):
        self.recorded_opening(prompt_after_skip=False)
        lines = RUN1_LAST_OPENING.splitlines(keepends=True)
        self.stub_samples = [RUN1_OPENING, '[Adapter] note=' + ''.join(lines)]
        self.stub_default = ''
        with self.assertRaises(RuntimeError) as stopped:
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertRegex(str(stopped.exception), 'opening was seen but no fresh ssbu.ui reply confirmed it; nothing was sent')

    def test_bridge_on_stub_on_first_opening_reply_without_provider_is_not_fresh(self):
        self.recorded_opening(prompt_after_skip=False)
        no_fence = ''.join(RUN1_OPENING.splitlines(keepends=True)[1:])
        self.stub_samples, self.stub_default = [no_fence], ''
        with self.assertRaisesRegex(RuntimeError, 'opening was seen but no fresh ssbu.ui reply confirmed it; nothing was sent'):
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertFalse(any(method == 'macro.status' for method, _ in self.calls))

    def test_bridge_on_stub_on_offsets_count_utf8_bytes(self):
        self.recorded_opening()
        self.stub_preamble = RUN1_PREAMBLE + '[Adapter] note=é\n'
        self.stub_samples, self.stub_default = [RUN1_OPENING, RUN1_LAST_OPENING], ''
        title = self.invoke(port=6980)['title']
        self.assertEqual(len(self.macro_attempts()), 1)
        self.assert_fresh_offsets(title, self.stub_preamble + RUN1_OPENING, RUN1_LAST_OPENING, 1)

    def test_bridge_on_stub_on_first_opening_begun_before_its_request_is_not_fresh(self):
        self.recorded_opening(prompt_after_skip=False)
        lines = RUN1_OPENING.splitlines(keepends=True)
        self.stub_preamble = RUN1_PREAMBLE + ''.join(lines[:-1])
        self.stub_samples, self.stub_default = [lines[-1]], ''
        with self.assertRaisesRegex(RuntimeError, 'opening was seen but no fresh ssbu.ui reply confirmed it; nothing was sent'):
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertFalse(any(method == 'macro.status' for method, _ in self.calls))

    def test_bridge_on_stub_on_opening_gone_at_the_recheck_sends_nothing(self):
        self.recorded_opening(prompt_after_skip=False)
        self.stub_samples, self.stub_default = [RUN1_OPENING, RUN1_PROMPT], ''
        try:
            title = self.invoke(port=6980)['title']
        except RuntimeError as error:
            self.fail(f'expected prompt without an opening press: {error}')
        self.assertEqual(self.macro_attempts(), [])
        self.assertEqual((title['matched'], title['frame'], title['skip']), ('prompt', 7860, None))

    def test_bridge_on_stub_on_other_screen_at_the_recheck_sends_nothing(self):
        self.recorded_opening(prompt_after_skip=False)
        main = self.sample(boot=RUN1_BOOT, screen='main_menu')
        self.stub_samples, self.stub_default = [RUN1_OPENING, main], ''
        with self.assertRaises(RuntimeError) as stopped:
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertRegex(str(stopped.exception), 'main_menu/title.press_any, not the Title')

    def test_bridge_on_stub_on_title_without_prompt_at_recheck_keeps_existing_timeout(self):
        self.recorded_opening(prompt_after_skip=False)
        passive = self.sample(boot=RUN1_BOOT, fields={
            'focus': 'title', 'focus.source': 'unknown', 'focus.id': 'unknown',
            'focus.index': 'unknown', 'focus.label': 'unknown',
            'input.owner': 'unknown', 'dialog': 'unknown'})
        self.stub_samples, self.stub_default = [RUN1_OPENING, passive], ''
        with self.assertRaisesRegex(RuntimeError, r'Title prompt was not observed within 10 s.*title/title'):
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])

    def test_bridge_on_stub_on_passive_title_without_provider_keeps_existing_timeout(self):
        self.recorded_opening(prompt_after_skip=False)
        passive = self.sample(boot=RUN1_BOOT, fields={
            'focus': 'title', 'focus.source': 'unknown', 'focus.id': 'unknown',
            'focus.index': 'unknown', 'focus.label': 'unknown',
            'input.owner': 'unknown', 'dialog': 'unknown'})
        passive = re.sub(r'^\[Bridge\] req providers result=.*\n', '', passive, flags=re.MULTILINE)
        self.stub_samples, self.stub_default = [RUN1_OPENING, passive], ''
        with self.assertRaisesRegex(RuntimeError, r'Title prompt was not observed within 10 s.*title/title'):
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])

    def test_bridge_on_stub_on_recheck_without_reply_sends_nothing(self):
        self.recorded_opening(prompt_after_skip=False)
        self.stub_samples, self.stub_default = [RUN1_OPENING], ''
        with self.assertRaises(RuntimeError) as stopped:
            self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertRegex(str(stopped.exception), 'opening was seen but no fresh ssbu.ui reply confirmed it; nothing was sent')
        self.assertAlmostEqual(self.seconds - self.request_times[0], 10)

    def test_bridge_on_stub_on_recheck_accepts_a_delayed_reply(self):
        self.recorded_opening()
        self.stub_samples, self.stub_default = [RUN1_OPENING, ''], ''
        recheck_started = []

        def deliver(_amount):
            if len(self.bridge_requests) >= 2:
                if not recheck_started:
                    recheck_started.append(self.seconds)
                elif self.seconds - recheck_started[0] >= .2:
                    with (self.out / 'bridge-run' / 'bridge.log').open('a') as stream:
                        stream.write(RUN1_LAST_OPENING)
                    self.sleep_hook = None

        self.sleep_hook = deliver
        title = self.invoke(port=6980)['title']
        self.assertEqual(len(self.macro_attempts()), 1)
        self.assertEqual(title['skip']['sent_on']['frame'], 7849)
        self.assert_fresh_offsets(title, RUN1_PREAMBLE + RUN1_OPENING, RUN1_LAST_OPENING, 1)

    def test_bridge_on_stub_on_retries_confirmation_after_a_silent_recheck(self):
        self.recorded_opening()
        self.stub_samples, self.stub_default = [RUN1_OPENING, '', RUN1_OPENING, RUN1_LAST_OPENING], ''
        title = self.invoke(port=6980)['title']
        self.assertEqual(len(self.macro_attempts()), 1)
        self.assertEqual(title['skip']['sent_on']['frame'], 7849)
        self.assert_fresh_offsets(title, RUN1_PREAMBLE + RUN1_OPENING * 2, RUN1_LAST_OPENING, 2)
        self.assertGreaterEqual(self.request_times[2] - self.request_times[1], 2)

    def test_bridge_on_stub_on_fresh_indices_include_preexisting_blocks(self):
        self.recorded_opening()
        self.stub_preamble = RUN1_PREAMBLE + RUN1_OPENING
        self.stub_samples, self.stub_default = [RUN1_OPENING, RUN1_LAST_OPENING], ''
        title = self.invoke(port=6980)['title']
        self.assertEqual(len(self.macro_attempts()), 1)
        self.assert_fresh_offsets(title, RUN1_PREAMBLE + RUN1_OPENING * 2, RUN1_LAST_OPENING, 2)

    def test_bridge_on_stub_on_preexisting_prompt_still_qualifies(self):
        self.recorded_opening(prompt_after_skip=False)
        self.stub_preamble = RUN1_PREAMBLE + RUN1_PROMPT
        self.stub_default = ''
        title = self.invoke(port=6980)['title']
        self.assertEqual((title['matched'], title['frame'], title['skip']), ('prompt', 7860, None))
        self.assertEqual(self.macro_attempts(), [])

    def test_bridge_on_stub_on_does_not_press_on_other_screens(self):
        self.configure_stub()
        receipt = self.invoke(port=6980)
        self.assertIsNone(receipt['title'].get('skip', 'missing'))
        self.assertEqual(self.macro_attempts(), [])

    def test_bridge_on_stub_on_never_presses_old_or_foreign_opening(self):
        self.recorded_opening(prompt_after_skip=False)
        self.stub_samples = [RUN1_OPENING + RUN1_PROMPT]
        receipt = self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertIsNone(receipt['title'].get('skip', 'missing'))

    def test_bridge_on_stub_on_never_presses_opening_on_foreign_boot(self):
        self.configure_stub()
        self.stub_samples = [self.sample(boot=FOREIGN_BOOT, fields={
            'screen.context': 'opening', 'focus': 'title.opening_playback', 'focus.id': 'opening_playback'}), STUB_UI]
        receipt = self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertIsNone(receipt['title'].get('skip', 'missing'))

    def test_bridge_on_stub_on_never_presses_prior_connection_opening(self):
        self.recorded_opening(prompt_after_skip=False)
        self.stub_preamble = self.stub_connection() + self.opening() + RUN1_PREAMBLE
        self.stub_default = RUN1_PROMPT
        receipt = self.invoke(port=6980)
        self.assertEqual(self.macro_attempts(), [])
        self.assertIsNone(receipt['title'].get('skip', 'missing'))

    def test_bridge_on_stub_on_skip_delivery_unknown_stops_without_resend(self):
        self.recorded_opening()
        self.macro_error = 'simulated delivery unknown'
        with self.assertRaisesRegex(RuntimeError, 'delivery unknown'):
            self.invoke(port=6980)
        self.assertEqual(len(self.macro_attempts()), 1)

    def test_bridge_on_stub_on_skip_guard_refuses_repeat_and_wrong_screen(self):
        blocks = list(routes._bridge_on_ui_blocks_since_hello(''.join(RUN1), RUN1[1].rstrip('\n'), 1))
        opening, prompt = blocks[1], blocks[-1]
        with self.assertRaisesRegex(RuntimeError, 'opening skip was already sent'):
            routes._bridge_on_skip_opening(self.ctx, opening, {'boot': RUN1_BOOT, 'sent': True})
        with self.assertRaisesRegex(RuntimeError, 'refusing the opening skip on title/title.press_any'):
            routes._bridge_on_skip_opening(self.ctx, prompt, {'boot': RUN1_BOOT, 'sent': False})
        for changed in ({'boot': FOREIGN_BOOT}, {'active': 'no'}):
            block = copy.deepcopy(opening)
            if 'boot' in changed:
                block['boot'] = changed['boot']
            else:
                block['values']['observation.active'] = changed['active']
            with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, 'refusing the opening skip'):
                routes._bridge_on_skip_opening(self.ctx, block, {'boot': RUN1_BOOT, 'sent': False})

    def test_bridge_on_stub_on_stops_at_deadline_even_if_macro_completion_is_slow(self):
        self.recorded_opening()
        self.ctx.runner.wait_macro = lambda *_: self.sleep(11)
        with self.assertRaisesRegex(RuntimeError, 'opening was skipped with one press.*within 10 s'):
            self.invoke(port=6980)
        self.assertEqual(len(self.macro_attempts()), 1)

    def test_bridge_on_stub_on_stops_if_screen_changes_after_the_skip(self):
        self.configure_stub()
        self.stub_samples, self.stub_default = [self.opening(), self.opening(), self.sample(screen='main')], ''
        with self.assertRaisesRegex(RuntimeError, r'first screen it reports is main/title.press_any, not the Title'):
            self.invoke(port=6980)
        self.assertEqual(len(self.macro_attempts()), 1)

    def test_bridge_on_stub_on_stops_if_connection_changes_after_the_skip(self):
        self.configure_stub()
        self.stub_samples, self.stub_default = [self.opening(), self.opening(), '[Adapter] connection idle\n'], ''
        with self.assertRaisesRegex(RuntimeError, 'bridge connection changed after the greeting'):
            self.invoke(port=6980)
        self.assertEqual(len(self.macro_attempts()), 1)

    def test_bridge_on_stub_on_accepts_prompt_arriving_while_checking_idle_without_press(self):
        self.recorded_opening()
        def idle_changes_screen(_ctx, _remaining):
            with (self.out / 'bridge-run' / 'bridge.log').open('a') as stream:
                stream.write(RUN1_PROMPT)
            return None
        with patch.object(routes, '_eden_macro_busy', side_effect=idle_changes_screen):
            try:
                receipt = self.invoke(port=6980)
            except RuntimeError as error:
                self.fail(f'expected already-arrived prompt without a press: {error}')
        self.assertEqual(self.macro_attempts(), [])
        self.assertIsNone(receipt['title']['skip'])
        self.assertEqual(receipt['title']['frame'], 7860)

    def test_bridge_on_stub_on_skip_follows_chord_without_earlier_input(self):
        self.recorded_opening()
        try:
            receipt = self.invoke(port=6980)
        except RuntimeError as error:
            self.fail(f'expected skip after chord: {error}')
        self.assertEqual(receipt['chord']['pre_chord']['journal']['inputs_after_load'], 0)
        inputs = [(method, args.get('button'), args.get('state')) for method, args in self.calls
                  if method in ('input.button', 'macro.play')]
        buttons = ('lstick', 'rstick', 'zl', 'zr')
        self.assertEqual(inputs, [('input.button', b, 'press') for b in buttons]
                         + [('input.button', b, 'release') for b in buttons] + [('macro.play', None, None)])
        names = [method for method, _ in self.calls]
        self.assertLess(names.index('macro.status'), names.index('macro.play'))

    def test_bridge_on_stub_on_already_on_without_control_journal_refuses(self):
        first, prior = self.cold_stub_receipt()
        self.journal.unlink()
        buttons = [call for call in self.calls if call[0] == 'input.button']
        requests = list(self.bridge_requests)
        run = self.out / 'bridge-run'
        cases = ('missing', 'corrupt', 'missing title', 'wrong event', 'reused trace',
                 'reused trace with stock', 'trace missing stock', 'trace empty stock',
                 'trace missing capture', 'wrong port')
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for label in cases:
                with self.subTest(case=label):
                    other = copy.deepcopy(first)
                    if label in ('wrong event', 'reused trace', 'reused trace with stock',
                                 'trace missing stock', 'trace empty stock', 'trace missing capture'):
                        other['witness'] = 'trace'
                        other['chord'] = {'stock': STOCK, 'event': STUB_EVENTS[3]}
                    if label == 'missing title':
                        other['title'] = {}
                    elif label == 'wrong event':
                        other['chord']['event'] = 'another event'
                    elif label in ('reused trace', 'reused trace with stock'):
                        other['chord']['chord'] = 'already on'
                        if label == 'reused trace':
                            del other['chord']['stock']
                    elif label == 'trace missing stock':
                        del other['chord']['stock']
                    elif label == 'trace empty stock':
                        other['chord']['stock'] = {}
                    elif label == 'trace missing capture':
                        del other['capture']
                    elif label == 'wrong port':
                        other['capture']['port'] = 6981
                    if label == 'missing':
                        prior.unlink(missing_ok=True)
                    elif label == 'corrupt':
                        prior.write_text('{not-json}')
                    else:
                        prior.write_text(json.dumps(other))
                    with self.assertRaisesRegex(RuntimeError, r'no control journal with this boot.s game.load'):
                        self.invoke(port=6980)
                    self.assertEqual(len(self.popen), 1)
                    self.assertEqual([call for call in self.calls if call[0] == 'input.button'], buttons)
                    self.assertEqual(self.bridge_requests, requests)

    def test_bridge_on_stub_on_already_on_links_the_qualified_receipt(self):
        first, prior = self.cold_stub_receipt()
        self.assertTrue(prior.is_file())
        self.assertEqual(first['capture']['port'], 6980)
        self.assertEqual(first['chord']['chord'], 'pad')
        requests = list(self.bridge_requests)
        presses = len([1 for method, args in self.calls if method == 'input.button' and args['state'] == 'press'])
        run = self.out / 'bridge-run'
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = self.invoke(port=6980)
        qualified = second['title'].get('qualified_by', {})
        self.assertEqual(qualified.get('receipt'), str(prior))
        self.assertEqual(qualified.get('title'), first['title'])
        self.assertEqual(qualified.get('boot'), STUB_BOOT)
        self.assertFalse(second['title']['checked'])
        self.assertEqual(self.bridge_requests, requests)
        self.assertEqual(json.loads(prior.read_text()), first)
        self.assertEqual(len([1 for method, args in self.calls
                              if method == 'input.button' and args['state'] == 'press']), presses)

    def test_bridge_on_same_boot_rerun_requalifies_an_unqualified_on_without_another_toggle(self):
        first, prior = self.cold_stub_receipt()
        prior.unlink()
        presses = len([1 for method, args in self.calls if method == 'input.button' and args['state'] == 'press'])
        run = self.out / 'bridge-run'
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = self.invoke(port=6980)
        self.assertTrue(second['chord']['requalified'])
        self.assertEqual(second['chord']['event'], first['chord']['event'])
        self.assertEqual(second['title']['matched'], 'prompt')
        self.assertEqual(second['title']['boot'], STUB_BOOT)
        self.assertEqual(len([1 for method, args in self.calls
                              if method == 'input.button' and args['state'] == 'press']), presses)

    def test_bridge_on_same_boot_rerun_waits_for_load_complete_before_requalifying(self):
        _, prior = self.cold_stub_receipt()
        prior.unlink()
        self.status = {'running': True, 'loading_complete': False}

        def loading_signal(_):
            if self.seconds >= 1.0:
                self.status = {'running': True, 'loading_complete': True}

        self.sleep_hook = loading_signal
        run = self.out / 'bridge-run'
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = self.invoke(port=6980)
        self.assertGreaterEqual(self.seconds, 1.0)
        self.assertTrue(second['chord']['requalified'])
        self.assertTrue(second['chord']['pre_chord']['status']['loading_complete'])
        self.assertEqual(second['title']['matched'], 'prompt')

    def test_bridge_on_stub_on_requalifies_when_prior_receipt_is_unqualified(self):
        first, prior = self.cold_stub_receipt()
        buttons = [call for call in self.calls if call[0] == 'input.button']
        requests = list(self.bridge_requests)
        run = self.out / 'bridge-run'
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for change in ('hello', 'title', 'both'):
                with self.subTest(change=change):
                    other = copy.deepcopy(first)
                    if change in ('hello', 'both'):
                        other['hello'] = other['hello'].replace(STUB_BOOT, FOREIGN_BOOT)
                    if change in ('title', 'both'):
                        other['title']['boot'] = FOREIGN_BOOT
                    prior.write_text(json.dumps(other))
                    for receipt in self.row.glob('routes-bridge-on*.json'):
                        if receipt != prior:
                            receipt.unlink()
                    if change == 'both':
                        with self.assertRaisesRegex(RuntimeError, f'from boot {FOREIGN_BOOT}.*greeting is boot {STUB_BOOT}'):
                            self.invoke(port=6980)
                    else:
                        second = self.invoke(port=6980)
                        self.assertTrue(second['chord']['requalified'])
                        self.assertEqual(second['title']['boot'], STUB_BOOT)
                    self.assertEqual([call for call in self.calls if call[0] == 'input.button'], buttons)
                    self.assertGreaterEqual(len(self.bridge_requests), len(requests))
                    self.assertEqual(len(self.popen), 1)

    def test_bridge_on_stub_on_receipt_qualification_rejects_invalid_observations(self):
        first, prior = self.cold_stub_receipt()
        buttons = [call for call in self.calls if call[0] == 'input.button']
        requests = list(self.bridge_requests)
        missing = object()
        cases = (
            ('frame missing', ('title', 'frame'), missing),
            ('wall missing', ('title', 'wall'), missing),
            ('frame string', ('title', 'frame'), '31'),
            ('wall string', ('title', 'wall'), '474261702'),
            ('rev string', ('title', 'rev'), '4'),
            ('frame bool', ('title', 'frame'), True),
            ('wall bool', ('title', 'wall'), False),
            ('rev bool', ('title', 'rev'), True),
            ('pre empty', ('chord', 'pre_chord'), {}),
            ('input sent', ('chord', 'pre_chord', 'journal', 'inputs_after_load'), 1),
            ('input bool', ('chord', 'pre_chord', 'journal', 'inputs_after_load'), False),
            ('load time empty', ('chord', 'pre_chord', 'journal', 'game_load_time'), ''),
            ('not running', ('chord', 'pre_chord', 'status', 'running'), False),
            ('not loaded', ('chord', 'pre_chord', 'status', 'loading_complete'), False),
            ('ready missing', ('chord', 'pre_chord', 'chord_ready'), missing),
            ('ready malformed', ('chord', 'pre_chord', 'chord_ready'), '1 [Bridge] chord ready extra'),
            ('install not boot default', ('chord', 'pre_chord', 'install'), 'not the install'),
            ('screen not title', ('title', 'screen'), 'main'),
            ('focus not prompt', ('title', 'focus'), 'title'),
            ('matched not prompt', ('title', 'matched'), 'opening'),
            ('inactive', ('title', 'observation.active'), 'no'),
            ('title boot missing', ('title', 'boot'), missing),
            ('hello missing', ('hello',), missing),
        )
        run = self.out / 'bridge-run'
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for label, path, value in cases:
                with self.subTest(case=label):
                    other = copy.deepcopy(first)
                    parent = other
                    for key in path[:-1]:
                        parent = parent[key]
                    if value is missing:
                        del parent[path[-1]]
                    else:
                        parent[path[-1]] = value
                    prior.write_text(json.dumps(other))
                    self.assertIsNone(routes._bridge_on_receipt_qualifies(other, STUB_EVENTS[3], 6980))
        self.assertEqual([call for call in self.calls if call[0] == 'input.button'], buttons)
        self.assertEqual(self.bridge_requests, requests)
        self.assertEqual(len(self.popen), 1)

    def test_bridge_on_stub_on_receipt_qualification_checks_observations(self):
        first, _ = self.cold_stub_receipt()
        qualify = getattr(routes, '_bridge_on_receipt_qualifies', lambda *_: 'missing helper')
        for label in ('frame missing', 'input bool'):
            with self.subTest(case=label):
                other = copy.deepcopy(first)
                if label == 'frame missing':
                    del other['title']['frame']
                else:
                    other['chord']['pre_chord']['journal']['inputs_after_load'] = False
                self.assertIsNone(qualify(other, STUB_EVENTS[3], 6980))
        self.assertEqual(qualify(first, STUB_EVENTS[3], 6980), STUB_BOOT)
        other = copy.deepcopy(first)
        other['hello'] = other['hello'].replace(STUB_BOOT, FOREIGN_BOOT)
        other['title']['boot'] = FOREIGN_BOOT
        self.assertEqual(qualify(other, STUB_EVENTS[3], 6980), FOREIGN_BOOT)

    def test_bridge_on_stub_on_already_on_refuses_a_greeting_without_boot(self):
        first, _ = self.cold_stub_receipt()
        run = self.out / 'bridge-run'
        log = run / 'bridge.log'
        no_boot = first['hello'].replace(f'boot={STUB_BOOT} ', '')
        log.write_text(log.read_text().replace(first['hello'], no_boot, 1))
        buttons = [call for call in self.calls if call[0] == 'input.button']
        requests = list(self.bridge_requests)
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'bridge greeting has no boot id'):
                self.invoke(port=6980)
        self.assertEqual([call for call in self.calls if call[0] == 'input.button'], buttons)
        self.assertEqual(self.bridge_requests, requests)

    def test_bridge_on_stub_on_already_on_chooses_newest_qualified_receipt(self):
        first, older = self.cold_stub_receipt()
        requests = list(self.bridge_requests)
        newer = self.row / 'routes-bridge-on-2.json'
        wrong_port = self.row / 'routes-bridge-on-3.json'
        corrupt = self.row / 'routes-bridge-on-4.json'
        trace = copy.deepcopy(first)
        trace['witness'] = 'trace'
        trace['chord'] = {'stock': STOCK, 'event': STUB_EVENTS[3]}
        older.write_text(json.dumps(trace))
        newer.write_text(json.dumps(first))
        trace['capture']['port'] = 6981
        wrong_port.write_text(json.dumps(trace))
        corrupt.write_text('{bad')
        for index, path in enumerate((older, newer, wrong_port, corrupt), 1):
            os.utime(path, ns=(index * 1_000_000_000, index * 1_000_000_000))
        run = self.out / 'bridge-run'
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            receipt = self.invoke(port=6980)
        self.assertEqual(receipt['chord'].get('qualified_by', {}).get('receipt'), str(newer))
        self.assertEqual(receipt['title'].get('qualified_by', {}).get('title', {}).get('frame'), 31)
        self.assertEqual(self.bridge_requests, requests)

    def test_bridge_on_stub_on_no_toggle_stops_without_a_retry(self):
        self.configure_stub()
        self.toggle_enabled = False
        with self.assertRaisesRegex(routes.BridgeOnChordHoldError, 'no toggle within 6 s'):
            self.invoke(port=6980)
        self.assertEqual(len([1 for method, args in self.calls
                              if method == 'input.button' and args['state'] == 'press']), 4)
        self.assertIsNone(json.loads((self.row / 'chord-hold-1.json').read_text())['event'])
        self.assertEqual(self.popen, [])

    def test_bridge_on_stub_on_reads_the_title_only_from_the_latest_connection(self):
        self.configure_stub()
        main = self.sample(boot=FOREIGN_BOOT, screen='main_menu', fields={'focus': 'main_menu.top.smash'})
        run = self.locked_capture(''.join(STUB_CAPTURE) + self.stub_connection(FOREIGN_BOOT) + main)
        self.stub_default = ''
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, r'first screen it reports is main_menu/main_menu.top.smash, not the Title'):
                self.invoke(port=6980)
        self.assertFalse((self.row / 'routes-bridge-on.json').exists())

    def test_bridge_on_stub_on_latest_connection_without_active_block_times_out(self):
        self.configure_stub()
        run = self.locked_capture(''.join(STUB_CAPTURE) + self.stub_connection(FOREIGN_BOOT))
        self.stub_default = ''
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'no active ssbu.ui observation of this boot within 10 s'):
                self.invoke(port=6980)
        self.assertFalse((self.row / 'routes-bridge-on.json').exists())

    def test_bridge_on_stub_on_uses_the_last_identical_hello_position(self):
        self.configure_stub()
        main = self.sample(screen='main_menu', fields={'focus': 'main_menu.top.smash'})
        run = self.locked_capture(''.join(STUB_CAPTURE) + self.stub_connection(reconnect=True) + main)
        self.stub_default = ''
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, r'first screen it reports is main_menu/main_menu.top.smash, not the Title'):
                self.invoke(port=6980)
        self.assertFalse((self.row / 'routes-bridge-on.json').exists())

    def test_bridge_on_stub_on_reuse_compares_against_the_latest_connection(self):
        self.cold_stub_receipt()
        buttons = [call for call in self.calls if call[0] == 'input.button']
        requests = list(self.bridge_requests)
        run = self.out / 'bridge-run'
        with (run / 'bridge.log').open('a') as stream:
            stream.write(self.stub_connection(FOREIGN_BOOT))
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, f'from boot {STUB_BOOT}.*greeting is boot {FOREIGN_BOOT}'):
                self.invoke(port=6980)
        self.assertEqual([call for call in self.calls if call[0] == 'input.button'], buttons)
        self.assertEqual(self.bridge_requests, requests)

    def test_bridge_on_stub_on_same_boot_reconnect_still_qualifies(self):
        self.cold_stub_receipt()
        buttons = [call for call in self.calls if call[0] == 'input.button']
        requests = list(self.bridge_requests)
        hello = STUB_CAPTURE[1].replace('reconnects=0', 'reconnects=1')
        run = self.out / 'bridge-run'
        with (run / 'bridge.log').open('a') as stream:
            stream.write(self.stub_connection(reconnect=True, hello=hello))
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            receipt = self.invoke(port=6980)
        self.assertEqual(receipt['hello'], hello.rstrip('\n'))
        self.assertEqual(receipt['chord']['qualified_by']['boot'], STUB_BOOT)
        self.assertEqual([call for call in self.calls if call[0] == 'input.button'], buttons)
        self.assertEqual(self.bridge_requests, requests)

    def test_bridge_on_stub_on_stops_when_the_connection_changes_during_the_title_read(self):
        self.configure_stub()
        self.stub_default = f'[Adapter] reconnect same_boot=1 boot={STUB_BOOT} connects=2 cursor=0\n' + STUB_UI
        with self.assertRaisesRegex(RuntimeError, r'connection changed after the greeting .*Title cannot be read'):
            self.invoke(port=6980)
        self.assertFalse((self.row / 'routes-bridge-on.json').exists())

    def test_bridge_on_stub_on_does_not_accept_prompt_before_a_later_boundary_in_same_read(self):
        self.configure_stub()
        self.stub_default = STUB_UI + '[Adapter] connection idle 6.1 s: reconnecting\n'
        with self.assertRaisesRegex(RuntimeError, r'connection changed after the greeting .*Title cannot be read'):
            self.invoke(port=6980)
        self.assertFalse((self.row / 'routes-bridge-on.json').exists())

    def test_bridge_on_stub_on_waits_for_a_complete_latest_connection(self):
        self.configure_stub()
        self.stub_preamble = STUB_PREAMBLE + self.stub_connection(FOREIGN_BOOT, install=False)
        self.stub_default = STUB_UI + self.sample(boot=FOREIGN_BOOT)
        run = self.out / 'bridge-run'
        waits = []

        def complete(amount):
            if amount == .2 and (run / 'bridge.log').exists():
                waits.append(amount)
                if len(waits) == 2:
                    with (run / 'bridge.log').open('a') as stream:
                        stream.write(STUB_CAPTURE[2])

        self.sleep_hook = complete
        receipt = self.invoke(port=6980)
        self.assertGreaterEqual(len(waits), 2)
        self.assertIn(FOREIGN_BOOT, receipt['hello'])
        self.assertEqual(receipt['title']['boot'], FOREIGN_BOOT)

    def test_bridge_on_stub_on_stops_if_connection_changes_before_cold_receipt_save(self):
        self.configure_stub()
        self.stub_preamble = STUB_PREAMBLE.replace(STUB_CAPTURE[3], '')
        run = self.out / 'bridge-run'

        def reconnect(amount):
            if amount == .1 and self.bridge_requests:
                self.sleep_hook = None
                with (run / 'bridge.log').open('a') as stream:
                    stream.write(self.stub_connection(reconnect=True) + STUB_CAPTURE[3])

        self.sleep_hook = reconnect
        with self.assertRaisesRegex(RuntimeError, r'connection changed after the greeting .*Title cannot be read'):
            self.invoke(port=6980)
        self.assertFalse((self.row / 'routes-bridge-on.json').exists())

    def test_bridge_on_stub_on_stops_if_connection_changes_before_reuse_receipt_save(self):
        self.cold_stub_receipt()
        requests = list(self.bridge_requests)
        buttons = [call for call in self.calls if call[0] == 'input.button']
        run = self.out / 'bridge-run'
        log = run / 'bridge.log'
        log.write_text(log.read_text().replace(STUB_CAPTURE[3], ''))

        def reconnect(amount):
            if amount == .1:
                self.sleep_hook = None
                with log.open('a') as stream:
                    stream.write(self.stub_connection(reconnect=True) + STUB_CAPTURE[3])

        self.sleep_hook = reconnect
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, r'connection changed after the greeting .*Title cannot be read'):
                self.invoke(port=6980)
        self.assertFalse((self.row / 'routes-bridge-on-2.json').exists())
        self.assertEqual(self.bridge_requests, requests)
        self.assertEqual([call for call in self.calls if call[0] == 'input.button'], buttons)

    def test_bridge_on_latest_connection_requires_a_complete_latest_greeting(self):
        latest = getattr(routes, '_bridge_on_latest_connection', lambda _: None)
        first = ''.join(STUB_CAPTURE)
        second = self.stub_connection(FOREIGN_BOOT)
        first_hello = STUB_CAPTURE[1].rstrip('\n')
        second_hello = STUB_CAPTURE[1].replace(STUB_BOOT, FOREIGN_BOOT).rstrip('\n')
        install = STUB_CAPTURE[2].rstrip('\n')
        cases = (
            ('one', first, (first_hello, install, 1)),
            ('two', first + second, (second_hello, install, len(first.splitlines()) + 1)),
            ('idle', first + '[Adapter] connection idle 6.1 s: reconnecting\n', None),
            ('opening', first + self.stub_connection(FOREIGN_BOOT, install=False), None),
        )
        for label, text, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(latest(text), expected)

    def test_bridge_on_trace_keeps_the_first_greeting_across_two_connections(self):
        self.stub = True
        self.events.write_text('\n'.join(STUB_EVENTS[:2]) + '\n')
        self.stub_preamble = ''.join(STUB_CAPTURE) + self.stub_connection(FOREIGN_BOOT)
        receipt = self.invoke(port=6980)
        self.assertEqual(receipt['hello'], STUB_CAPTURE[1].rstrip('\n'))
        self.assertEqual(receipt['witness'], 'trace')

    def test_bridge_on_stub_on_rejects_first_active_main_before_title_in_one_read(self):
        self.configure_stub()
        self.stub_default = self.sample(screen='main') + ''.join(STUB_CAPTURE[11:])
        blocks = state_gate.blocks(self.stub_default, 'ssbu.ui')
        self.assertEqual([block['values']['screen'] for block in blocks], ['main', 'title'])
        with self.assertRaisesRegex(RuntimeError, r'first screen it reports is main/title.press_any, not the Title'):
            self.invoke(port=6980)
        self.assertEqual(len(self.bridge_requests), 1)

    def test_bridge_on_stub_on_skips_inactive_and_foreign_before_title_in_one_read(self):
        self.configure_stub()
        self.stub_default = self.sample(active='no') + self.sample(boot=FOREIGN_BOOT) + STUB_UI
        blocks = state_gate.blocks(self.stub_default, 'ssbu.ui')
        self.assertEqual([(block['boot'], block['values']['observation.active']) for block in blocks],
                         [(STUB_BOOT, 'no'), (FOREIGN_BOOT, 'yes'), (STUB_BOOT, 'yes')])
        receipt = self.invoke(port=6980)
        self.assertEqual((receipt['title']['boot'], receipt['title']['matched']), (STUB_BOOT, 'prompt'))
        self.assertEqual(len(self.bridge_requests), 1)

    def test_bridge_on_stub_on_ignores_blocks_before_the_capture_hello(self):
        self.configure_stub()
        self.stub_preamble = self.sample(screen='main') + STUB_PREAMBLE
        receipt = self.invoke(port=6980)
        self.assertEqual(receipt['title']['matched'], 'prompt')

    def test_bridge_on_stub_on_keeps_first_active_across_a_reconnect_in_one_read(self):
        self.configure_stub()
        self.stub_default = self.sample(screen='main') + '[Adapter] reconnect reason=test\n' + STUB_UI
        with self.assertRaisesRegex(RuntimeError, r'connection changed after the greeting .*Title cannot be read'):
            self.invoke(port=6980)

    def test_bridge_on_stub_on_ignores_a_bad_crc_before_the_title(self):
        self.configure_stub()
        bad = re.sub(r'crc=[0-9a-f]{8}', 'crc=00000000', self.sample(screen='main'))
        self.assertEqual(state_gate.blocks(bad, 'ssbu.ui'), [])
        self.stub_default = bad + ''.join(STUB_CAPTURE[11:])
        receipt = self.invoke(port=6980)
        self.assertEqual(receipt['title']['matched'], 'prompt')

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
        with patch('subprocess.run', side_effect=FileNotFoundError('ss')):
            with self.assertRaisesRegex(RuntimeError, r'ss'):
                routes._bridge_on_listener_owner(6980)

    def test_bridge_on_listener_query_keeps_four_second_cap(self):
        for windows, output, pid in (
                (False, 'LISTEN 0 128 0.0.0.0:6980 0.0.0.0:* users:(("eden",pid=20,fd=9))', 20),
                (True, 'TCP 0.0.0.0:6980 0.0.0.0:0 LISTENING 20', 20)):
            result = subprocess.CompletedProcess([], 0, stdout=output, stderr='')
            with self.subTest(windows=windows), patch.object(routes.host_os, 'WINDOWS', windows), \
                    patch('subprocess.run', return_value=result) as run:
                self.assertEqual([row['pid'] for row in routes._bridge_on_listener_owner(6980)], [pid])
                self.assertEqual(run.call_args.kwargs['timeout'], 4)

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

    def test_bridge_on_under_lan_online_uses_the_file_transport(self):
        marker = self.configure_lan()
        receipt = self.invoke()
        run = self.out / 'bridge-run-offline'
        self.assertEqual((receipt['transport'], receipt['listening'], receipt['listener']), ('file', TRANSPORT, None))
        self.assertEqual(self.owner_calls, [])  # no listener exists, so ss is not asked
        argv, _ = self.popen[0]
        self.assertEqual(argv, [sys.executable, '-I', str(Path(routes.__file__).resolve().parent / 'switch_hw.py'),
                                'capture-bridge', str(run), '--bridge-files', str(marker.parent),
                                '--console', str(run / 'bridge-console.log')])
        self.assertEqual(receipt['capture'], {'pid': 4321, 'port': None, 'bridge_log': str(run / 'bridge.log')})
        saved = json.loads((run / 'capture.json').read_text())
        self.assertEqual((saved['instance'], saved['transport'], saved['bridge_log']),
                         ('offline', 'file', str(run / 'bridge.log')))
        self.assertEqual(self.instance['bridge_log'], str(run / 'bridge.log'))
        self.assertEqual(receipt['title']['matched'], 'prompt')
        self.assertFalse((self.out / 'bridge-run').exists())

    def test_bridge_on_under_lan_online_without_the_marker_refuses_before_any_input(self):
        self.configure_stub()
        self.lan_online = True
        with self.assertRaisesRegex(RuntimeError, r'LAN Online.*file-transport.*before game\.load'):
            self.invoke(port=6980)
        self.assertFalse(any(name == 'input.button' for name, _ in self.calls))
        self.assertEqual(self.popen, [])

    def test_two_lan_online_instances_get_two_captures_and_two_logs(self):
        first_marker = self.configure_lan()
        first = self.invoke()
        first_instance = self.instance
        root = self.root / 'second'
        root.mkdir()
        self.events = root / 'events.log'
        self.instance = {'bridge_events': str(self.events), 'profile': str(root)}
        self.ctx.runner.instances['b'] = self.instance
        self.ctx.on, self.ctx.instance = 'b', self.instance
        self.stub = False
        self.instance['trace_jsonl'] = str(self.trace)
        second_marker = self.configure_lan()
        (root / 'control.jsonl').write_text(self.journal.read_text())
        self.instance['control_log'] = str(root / 'control.jsonl')
        self.calls, self.chord_started, self.seconds = [], None, 0.0
        second = self.invoke()
        runs = [self.out / 'bridge-run-offline', self.out / 'bridge-run-b']
        self.assertEqual([call[0][4] for call in self.popen], [str(run) for run in runs])
        self.assertEqual([call[0][6] for call in self.popen], [str(first_marker.parent), str(second_marker.parent)])
        self.assertEqual([first['capture']['bridge_log'], second['capture']['bridge_log']],
                         [str(run / 'bridge.log') for run in runs])
        self.assertEqual([first_instance['bridge_log'], self.instance['bridge_log']],
                         [str(run / 'bridge.log') for run in runs])
        self.assertEqual([json.loads((run / 'capture.json').read_text())['instance'] for run in runs], ['offline', 'b'])
        self.assertFalse(second['reused'])

    def test_bridge_on_listen_einval_names_lan_online_not_a_busy_port(self):
        self.events.write_text('\n'.join((FAIL_BOOT, FAIL_READY, LISTEN_EINVAL, FAIL_ON)) + '\n')
        with self.assertRaises(RuntimeError) as error:
            self.invoke(port=6979)
        text = str(error.exception)
        self.assertIn(LISTEN_EINVAL, text)
        self.assertIn('LAN Online', text)
        self.assertIn('file-transport', text)
        self.assertNotIn('Another process holds the port', text)
        self.assertEqual(self.popen, [])

    def test_bridge_on_under_lan_online_already_on_reuses_the_file_capture(self):
        # A retried row calls bridge_on again from a new runner process: the bridge is already On, its capture runs.
        self.configure_lan()
        first = self.invoke()
        prior = self.row / 'routes-bridge-on.json'
        self.assertEqual(json.loads(prior.read_text()), first)
        run = self.out / 'bridge-run-offline'
        requests = list(self.bridge_requests)
        buttons = [call for call in self.calls if call[0] == 'input.button']
        del self.instance['bridge_log']
        with (run / 'bridge.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = self.invoke()
        self.assertTrue(second['reused'])
        self.assertEqual((second['transport'], second['listening'], second['listener']), ('file', TRANSPORT, None))
        self.assertEqual(second['chord']['chord'], 'already on')
        self.assertEqual(second['title']['qualified_by']['receipt'], str(prior))
        self.assertFalse(second['title']['checked'])
        self.assertEqual(second['capture'], first['capture'])
        self.assertEqual(self.instance['bridge_log'], str(run / 'bridge.log'))
        self.assertEqual(len(self.popen), 1)  # no second capture
        self.assertEqual([call for call in self.calls if call[0] == 'input.button'], buttons)  # no second chord
        self.assertEqual(self.bridge_requests, requests)
        self.assertEqual(self.owner_calls, [])
        self.assertEqual(json.loads(prior.read_text()), first)
        self.assertFalse((self.out / 'bridge-run').exists())

    def test_bridge_on_with_the_marker_but_lan_online_off_refuses_before_any_input(self):
        # The bridge selects its file transport by the marker alone; with LAN Online off this call drives the socket.
        self.configure_stub()
        marker = Path(self.instance['profile']) / MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        with self.assertRaises(RuntimeError) as error:
            self.invoke(port=6980)
        text = str(error.exception)
        self.assertIn('file-transport', text)
        self.assertIn('core.lan_online', text)
        self.assertNotIn('Another process holds the port', text)
        self.assertFalse(any(name == 'input.button' for name, _ in self.calls))
        self.assertEqual(self.popen, [])

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
