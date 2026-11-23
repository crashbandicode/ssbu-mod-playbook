"""state_gate: framed bridge state blocks and the gate's freshness/transition rules (no Eden, no console)."""
import re
import sys
import time
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state_gate as sg


def block(rev, payload, name='tr.menu', boot='0x00000000000000ab', crc=None, parts=None, filtered=0, end_rev=None):
    lines = [f'[Bridge] state begin name={name} schema={name}/1 rev={rev} boot={boot} lines={len(payload)} '
             f'filtered={filtered} frame=10 wall=20']
    for n, item in enumerate(payload):
        if parts and n in parts:
            chunks = [item[i:i + parts[n]] for i in range(0, len(item), parts[n])]
            lines += [f'[Bridge] state.{n}.part={k + 1}/{len(chunks)} {c} frame=10 wall=20' for k, c in enumerate(chunks)]
        else:
            lines.append(f'[Bridge] state.{n} {item} frame=10 wall=20')
    crc = crc if crc is not None else f'{zlib.crc32(chr(10).join(payload).encode()):08x}'
    lines.append(f'[Bridge] state end name={name} rev={end_rev or rev} crc={crc} frame=11 wall=21')
    return '\n'.join(lines) + '\n'


MENU = ['schema=tr.menu/1', 'ack=3', 'ack.result="handled"', 'focus=fighters_stages/-/24/game.1', 'open=1',
        'page="fighters_stages"']


def menu(**change):
    out = []
    for item in MENU:
        key = item.split('=')[0]
        out.append(f'{key}={change[key]}' if key in change else item)
    return out


class BlockTests(unittest.TestCase):
    def test_a_complete_block_parses_with_strings_unquoted(self):
        b = sg.latest(block(5, MENU), 'tr.menu')
        self.assertEqual((b['rev'], b['values']['page'], b['values']['open'], b['values']['ack.result']),
                         (5, 'fighters_stages', '1', 'handled'))

    def test_partial_bad_crc_wrong_count_and_torn_blocks_are_not_blocks(self):
        good = block(5, MENU)
        self.assertIsNone(sg.latest(good.rsplit('\n', 2)[0], 'tr.menu'), 'no end line')
        self.assertIsNone(sg.latest(block(5, MENU, crc='deadbeef'), 'tr.menu'))
        cut = good.replace('[Bridge] state.3 focus=fighters_stages/-/24/game.1 frame=10 wall=20\n', '')
        self.assertIsNone(sg.latest(cut, 'tr.menu'), 'a missing line')
        # A reply torn across a reconnect: begin of rev 5, then the new connection's block of rev 6 (whole) — only 6.
        torn = good.split('[Bridge] state end')[0] + block(6, menu(ack='4'))
        found = sg.blocks(torn, 'tr.menu')
        self.assertEqual([b['rev'] for b in found], [6])
        self.assertIsNone(sg.latest(block(5, MENU, end_rev=7), 'tr.menu'), 'an end of another revision')

    def test_parts_reassemble_losslessly_and_a_missing_part_rejects_the_block(self):
        row = 'rows=' + ','.join(f'"Row \\"{i}\\" \\\\ x"' for i in range(60))
        text = block(9, MENU + [row], parts={6: 100})
        self.assertEqual(sg.latest(text, 'tr.menu')['values']['rows'].count('Row "'), 60)
        broken = '\n'.join(l for l in text.splitlines() if '.part=2/' not in l)
        self.assertIsNone(sg.latest(broken, 'tr.menu'))

    def test_suffixes_with_event_seq_are_stripped(self):
        text = block(5, MENU).replace(' frame=10', ' ev=12 frame=10')
        self.assertEqual(sg.latest(text, 'tr.menu')['rev'], 5)


class GateTests(unittest.TestCase):
    base = sg.latest(block(5, MENU), 'tr.menu')

    def test_a_newer_block_with_every_predicate_passes(self):
        b = sg.latest(block(6, menu(ack='4', focus='fighters_stages/22/0/start.1')), 'tr.menu')
        self.assertTrue(sg.satisfies(b, {'page': 'fighters_stages'}, self.base, transition='focus', ack=1, input_gate=True)[0])

    def test_the_same_or_an_older_revision_never_passes(self):
        self.assertFalse(sg.satisfies(self.base, {'page': 'fighters_stages'}, self.base)[0])

    def test_an_ignored_input_plus_an_unrelated_revision_does_not_pass_a_navigation_gate(self):
        # A notice changed (new rev) but the press was ignored: same focus, same ack.
        b = sg.latest(block(6, MENU + ['notice.body="Eggs 1"']), 'tr.menu')
        self.assertEqual(sg.satisfies(b, {}, self.base, transition='focus')[0], False)
        self.assertEqual(sg.satisfies(b, {}, self.base, ack=1)[0], False)

    def test_a_missing_predicate_key_another_boot_or_another_schema_fails(self):
        b = sg.latest(block(6, MENU), 'tr.menu')
        self.assertIn('missing', sg.satisfies(b, {'game': 'eggs'}, self.base)[1])
        other = sg.latest(block(6, MENU, boot='0x01'), 'tr.menu')
        self.assertIn('boot', sg.satisfies(other, {}, self.base)[1])

    def test_ack_must_advance_by_exactly_one_and_be_handled(self):
        two = sg.latest(block(6, menu(ack='5')), 'tr.menu')
        self.assertFalse(sg.satisfies(two, {}, self.base, ack=1)[0])
        ignored = sg.latest(block(6, menu(ack='4', **{'ack.result': '"ignored"'})), 'tr.menu')
        self.assertFalse(sg.satisfies(ignored, {}, self.base, ack=1)[0])



class LinkTests(unittest.TestCase):
    def test_the_capture_tracks_the_boot_and_the_event_cursor_across_a_reconnect(self):
        import switch_hw
        link = {'boot': None, 'cursor': 0, 'connects': 1}
        hello = (b'[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=x port=6970 guards=0x3fffff boot=0x00ab '
                 b'uptime_ms=5 reconnects=0 event_latest=0 state_api=1 frame=1 wall=2')
        self.assertIn(b'connected boot=0x00ab', switch_hw.link_line(link, hello))
        self.assertIsNone(switch_hw.link_line(link, b'[TrainingRooms] notice_draw header="a" body="b" ev=41 frame=3 wall=4'))
        self.assertEqual(link['cursor'], 41)
        link['connects'] = 2
        self.assertIn(b'same_boot=1', switch_hw.link_line(link, hello.replace(b'reconnects=0', b'reconnects=1')))
        self.assertEqual(link['cursor'], 41, 'the cursor survives a same-boot reconnect')
        self.assertIn(b'same_boot=0', switch_hw.link_line(link, hello.replace(b'0x00ab', b'0x00cd')))
        self.assertEqual(link['cursor'], 0, 'a new boot starts a new event sequence')

    def test_a_silent_connection_gets_a_heartbeat_then_is_reconnected(self):
        import socket
        import tempfile
        import switch_hw
        sent, connects = [], []

        class Silent:
            def settimeout(self, _):
                pass

            def sendall(self, data):
                sent.append(data)

            def recv(self, _):
                time.sleep(0.02)
                raise socket.timeout()

            def close(self):
                pass

        def connect(*_a, **_k):
            connects.append(1)
            return Silent()

        with tempfile.TemporaryDirectory() as d:
            switch_hw.capture_bridge(d, '127.0.0.1', 1, f'{d}/console.log', stop=lambda: len(connects) >= 3,
                                     connect=connect, retry_s=0.01, heartbeat_s=0.1, idle_s=0.3)
            log = open(f'{d}/bridge.log', 'rb').read()
        self.assertGreaterEqual(len(connects), 3, 'reconnected after silence')
        self.assertIn(b'providers\n', sent, 'a heartbeat before giving up')
        self.assertIn(b'[Adapter] connection idle', log)

    def test_state_and_reconnect_verbs_pass_the_v2_spool(self):
        import switch_hw
        self.assertTrue({'state', 'state_watch', 'providers', 'since', 'events'} <= switch_hw.V2_VERBS)


class ReviewNegativeTests(unittest.TestCase):
    """tr-review-astra-3 18:10Z findings 1-3, through the production parser, matcher and capture handshake."""
    base = sg.latest(block(5, MENU), 'tr.menu')

    def test_a_where_only_match_on_a_notice_bump_fails_an_input_gate(self):
        notice = sg.latest(block(6, MENU + ['notice.body="Eggs 1"']), 'tr.menu')
        self.assertFalse(sg.satisfies(notice, {'page': 'fighters_stages'}, self.base, input_gate=True)[0])
        self.assertFalse(sg.satisfies(notice, {'page': 'fighters_stages'}, None, transition='focus')[0], 'no baseline')
        self.assertTrue(sg.satisfies(notice, {'page': 'fighters_stages'})[0], 'a plain assertion may pass')

    def test_a_focus_gate_fails_when_the_new_block_has_no_focus(self):
        nofocus = sg.latest(block(6, [l for l in MENU if not l.startswith('focus=')]), 'tr.menu')
        ok, why = sg.satisfies(nofocus, {}, self.base, transition='focus', input_gate=True)
        self.assertFalse(ok)
        self.assertIn('focus missing', why)

    def test_a_new_boot_hello_between_begin_and_end_tears_the_block(self):
        whole = block(6, menu(ack='4'))
        head, tail = whole.split('[Bridge] state end')
        hello = ('[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=x port=6970 guards=0x3fffff boot=0x00cd '
                 'uptime_ms=5 reconnects=0 event_latest=0 state_api=1 frame=1 wall=2\n')
        self.assertEqual(sg.blocks(head + hello + '[Bridge] state end' + tail, 'tr.menu'), [])

    def test_a_complete_block_then_a_new_hello_leaves_nothing(self):
        before = block(6, menu(ack='4'))
        hello = ('[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=x port=6970 guards=0x3fffff boot=0x00cd '
                 'uptime_ms=5 reconnects=0 event_latest=0 state_api=1 frame=1 wall=2\n')
        self.assertEqual(sg.blocks(before + hello, 'tr.menu'), [], 'a pre-hello block never answers')
        after = block(1, menu(ack='0'), boot='0x00cd')
        self.assertEqual([b['rev'] for b in sg.blocks(before + hello + after, 'tr.menu')], [1])

    def test_a_multibyte_character_cut_at_a_part_boundary_reassembles(self):
        row = 'rows="' + 'x' * 393 + '\u00b7' + 'y' * 50 + '"'
        raw = row.encode()
        cut = 400 if raw[399] & 0xC0 != 0x80 else 399
        while raw[cut] & 0xC0 == 0x80:
            cut -= 1
        parts = [raw[:cut].decode(), raw[cut:].decode()]
        lines = MENU + [row]
        crc = f'{zlib.crc32(chr(10).join(lines).encode()):08x}'
        text = block(9, MENU + ['rows=placeholder']).replace('[Bridge] state.6 rows=placeholder frame=10 wall=20\n',
            f'[Bridge] state.6.part=1/2 {parts[0]} frame=10 wall=20\n[Bridge] state.6.part=2/2 {parts[1]} frame=10 wall=20\n')
        text = re.sub(r'crc=[0-9a-f]{8}', f'crc={crc}', text)
        b = sg.latest(text, 'tr.menu')
        self.assertIsNotNone(b)
        self.assertIn('\u00b7', b['values']['rows'])

    def test_resume_waits_for_the_hello_and_uses_the_same_boot_cursor_including_zero(self):
        import switch_hw
        link = {'boot': None, 'cursor': 0, 'connects': 1}
        hello = (b'[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=x port=6970 guards=0x3fffff boot=0x00ab '
                 b'uptime_ms=5 reconnects=0 event_latest=0 state_api=1 frame=1 wall=2')
        switch_hw.link_line(link, hello)
        self.assertEqual(link.pop('resume'), b'events on\n', 'first connect: no since')
        link['connects'] = 2
        switch_hw.link_line(link, hello.replace(b'reconnects=0', b'reconnects=1'))
        self.assertEqual(link.pop('resume'), b'events on\nsince 0\n', 'same boot, cursor zero is still resumed')
        switch_hw.link_line(link, b'[TrainingRooms] x ev=7 frame=1 wall=2')
        switch_hw.link_line(link, hello.replace(b'0x00ab', b'0x00cd'))
        self.assertEqual(link.pop('resume'), b'events on\n', 'a new boot never replays the old cursor')


HELLO = ('[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=x port=6970 guards=0x3fffff boot=0x00000000000000ab '
         'uptime_ms=5 reconnects=1 event_latest=0 state_api=1 frame=1 wall=2\n')


class RunnerEntryTests(unittest.TestCase):
    """The runner's own state_baseline / play / state gate (runner.py) on a bridge log the fake capture appends to. The
    gate is an input gate because a press followed the baseline, never because of a gate or test flag."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        import runner
        import switch_hw
        self.dir = tempfile.TemporaryDirectory()
        self.log = Path(self.dir.name) / 'bridge.log'
        self.log.write_text('')
        self.replies = []

        def answer(run, request):  # the capture's reply to `state tr.menu`: the next scripted text, once
            if self.replies:
                with self.log.open('a') as f:
                    f.write(self.replies.pop(0))
        patcher = mock.patch.object(switch_hw, 'bridge_v2', side_effect=answer)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)
        r = runner.Runner.__new__(runner.Runner)
        r.instances = {'tr': {'bridge_log': str(self.log)}}
        r.current_row = {'id': 'row-a'}
        r.log = lambda record: None
        r.check_stop = lambda: None
        r.play = lambda on, name: None
        self.r = r

    def baseline(self, text):
        self.replies.append(text)
        return self.r._step_state_baseline('tr', {'state_baseline': 'tr.menu', 'cap': 2})

    def press(self, name='tap-down'):
        return self.r._step_play('tr', {'play': name})

    def gate(self, text, **spec):
        self.replies.append(text)
        gate = {'kind': 'state', 'provider': 'tr.menu', **spec}
        return self.r.state_gate('tr', 'g', gate, time.monotonic() + 1.5)

    def cadence(self, *, poll_s=None):
        from unittest import mock
        import runner
        import switch_hw

        class Clock:
            now = 100.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds + .001

        clock, asks = Clock(), []

        def answer(_run, _request):
            elapsed = clock.now - 100.0
            asks.append(elapsed)
            phase = 'flight' if .45 <= elapsed < .95 else 'waiting'
            with self.log.open('a') as output:
                output.write(block(len(asks), [f'phase={phase}']))

        gate = {'kind': 'state', 'provider': 'tr.menu', 'where': {'phase': 'flight'}}
        if poll_s is not None:
            gate['poll_s'] = poll_s
        with (mock.patch.object(runner.time, 'monotonic', side_effect=clock.monotonic),
              mock.patch.object(runner.time, 'sleep', side_effect=clock.sleep),
              mock.patch.object(switch_hw, 'bridge_v2', side_effect=answer)):
            result = self.r.state_gate('tr', 'timed', gate, 101.0)
        return asks, result

    def test_poll_s_sees_half_second_phase_with_four_queries_within_a_second(self):
        asks, result = self.cadence(poll_s=.2)
        self.assertGreaterEqual(len(asks), 4)
        self.assertLess(asks[-1], 1)
        self.assertEqual(result['values']['phase'], 'flight')

    def test_default_polling_retains_once_per_second_cadence(self):
        from unittest import mock
        import runner
        import switch_hw

        class Clock:
            now = 100.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds + .001

        clock, asks = Clock(), []

        def answer(_run, _request):
            asks.append(clock.now - 100.0)
            with self.log.open('a') as output:
                output.write(block(len(asks), ['phase=waiting']))

        with (mock.patch.object(runner.time, 'monotonic', side_effect=clock.monotonic),
              mock.patch.object(runner.time, 'sleep', side_effect=clock.sleep),
              mock.patch.object(switch_hw, 'bridge_v2', side_effect=answer)):
            with self.assertRaisesRegex(RuntimeError, 'did not pass'):
                self.r.state_gate('tr', 'timed', {'provider': 'tr.menu', 'where': {'phase': 'flight'}}, 101.0)
        self.assertEqual(asks, [0.0])

    def test_a_focus_change_after_the_press_passes_and_names_the_press(self):
        self.baseline(block(5, MENU))
        self.press()
        out = self.gate(block(6, menu(focus='fighters_stages/-/23/game.2')), where={'open': 1})
        self.assertEqual((out['rev'], out['values']['press'], out['values']['confirmed']), (6, 'tap-down', 'focus moved'))

    def test_one_handled_ack_with_the_same_focus_passes(self):
        self.baseline(block(5, MENU))
        self.press('tap-a')
        out = self.gate(block(6, menu(ack='4')), where={'open': 1})
        self.assertEqual(out['values']['confirmed'], 'one handled ack')

    def test_a_where_only_or_notice_only_change_after_a_press_fails(self):
        self.baseline(block(5, MENU))
        self.press()
        with self.assertRaisesRegex(RuntimeError, 'focus did not change'):
            self.gate(block(6, MENU + ['notice.body="Eggs 1"']), where={'open': 1})

    def test_other_ack_deltas_and_an_ignored_ack_fail(self):
        self.baseline(block(5, MENU))
        self.press('tap-a')
        with self.assertRaisesRegex(RuntimeError, 'ack moved 2'):
            self.gate(block(6, menu(ack='5')), where={'open': 1})
        self.baseline(block(7, menu(ack='5')))
        self.press('tap-a')
        with self.assertRaisesRegex(RuntimeError, "'ignored'"):
            self.gate(block(8, menu(ack='6', **{'ack.result': '"ignored"'})), where={'open': 1})
        self.baseline(block(9, MENU))
        self.press('tap-a')
        with self.assertRaisesRegex(RuntimeError, 'only exactly one handled ack'):
            self.gate(block(10, menu(ack='5')), where={'open': 1}, ack=2)

    def test_a_one_step_focus_gate_fails_a_two_step_move(self):
        # The review's replay: cursor 1 -> 26 with ACK 3 -> 5 after one `up-1` press (a repeated input before the read).
        self.baseline(block(5, menu(focus='fighters_stages/-/1/trio_practice')))
        self.press('tap-up')
        two = block(6, menu(focus='fighters_stages/-/26/game.5', ack='5'))
        with self.assertRaisesRegex(RuntimeError, 'did not pass'):
            self.gate(two, where={'open': 1, 'focus': 'fighters_stages/-/27/row'}, transition='focus')
        # Without the pin, the two handled acks alone still fail it.
        self.baseline(block(9, menu(focus='fighters_stages/-/1/trio_practice')))
        self.press('tap-up')
        with self.assertRaisesRegex(RuntimeError, 'ack moved 2'):
            self.gate(block(10, menu(focus='fighters_stages/-/26/game.5', ack='5')), where={'open': 1}, transition='focus')
        # Pinned destination alone also catches a skipped row with a single ack.
        self.baseline(block(7, menu(focus='fighters_stages/-/1/trio_practice')))
        self.press('tap-up')
        with self.assertRaisesRegex(RuntimeError, 'focus='):
            self.gate(block(8, menu(focus='fighters_stages/-/26/game.5', ack='4')),
                      where={'open': 1, 'focus': 'fighters_stages/-/27/row'}, transition='focus')

    def test_a_focus_gate_never_passes_on_an_ack_alone(self):
        self.baseline(block(5, MENU))
        self.press('tap-up')
        with self.assertRaisesRegex(RuntimeError, 'did not change'):
            self.gate(block(6, menu(ack='4')), where={'open': 1}, transition='focus')

    def test_a_missing_baseline_focus_never_counts_as_a_transition(self):
        self.baseline(block(5, [l for l in MENU if not l.startswith('focus=')]))
        self.press()
        with self.assertRaisesRegex(RuntimeError, 'baseline focus missing'):
            self.gate(block(6, MENU), where={'open': 1})

    def test_without_a_press_a_gate_is_a_plain_assertion_and_a_transition_gate_refuses(self):
        self.assertEqual(self.gate(block(6, MENU), where={'open': 1})['rev'], 6)
        self.baseline(block(7, MENU))
        with self.assertRaisesRegex(RuntimeError, 'none since this row'):
            self.gate(block(8, menu(focus='x/-/1/y')), where={'open': 1}, transition='focus')

    def test_a_complete_block_before_a_new_hello_never_satisfies_the_gate(self):
        self.baseline(block(5, MENU))
        self.press()
        good = block(6, menu(focus='fighters_stages/-/23/game.2'))
        with self.assertRaisesRegex(RuntimeError, 'did not pass'):
            self.gate(good + HELLO, where={'open': 1})

    def test_a_baseline_never_carries_into_another_row(self):
        self.baseline(block(5, MENU))
        self.r.current_row = {'id': 'row-b'}
        self.press()
        out = self.gate(block(6, menu(focus='fighters_stages/-/23/game.2')), where={'open': 1})
        self.assertNotIn('press', out['values'], 'no baseline in this row: the press is unbound, the gate is plain')



class CaptureBackoffTests(unittest.TestCase):
    def test_accept_then_eof_before_hello_backs_off_and_a_hello_resets_it(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        import switch_hw
        hello = (b'[Bridge] hello plugin=ssbu-hw-bridge/0.1.0 abi=1 tr=x port=6970 guards=0x3fffff boot=0x00ab '
                 b'uptime_ms=5 reconnects=0 event_latest=0 state_api=1 frame=1 wall=2\n')
        script = [b''] * 6 + [hello] + [b''] * 2  # six accepts closed before a hello, one good greeting, two more
        sleeps, conns = [], []

        class Sock:
            def __init__(self, data):
                self.data = [data] if data else []

            def settimeout(self, t):
                pass

            def sendall(self, b):
                pass

            def recv(self, n):
                return self.data.pop(0) if self.data else b''

            def close(self):
                pass

        def connect(addr, timeout):
            conns.append(addr)
            return Sock(script.pop(0))
        run = Path(tempfile.mkdtemp())
        with mock.patch.object(switch_hw.time, 'sleep', side_effect=sleeps.append):
            switch_hw.capture_bridge(run, 'h', 1, run / 'console.log', stop=lambda: not script, connect=connect)
        self.assertEqual(sleeps[:6], [0.5, 1.0, 2.0, 4.0, 5.0, 5.0])
        self.assertEqual(sleeps[6:8], [0.5, 1.0], 'the hello reset the backoff')


if __name__ == '__main__':
    unittest.main()
