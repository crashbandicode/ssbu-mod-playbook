"""Offline tests for the shared bounded macro dispatch gate."""
import threading
import unittest
from pathlib import Path
from unittest import mock

import closed_loop
import macro_gate


class MacroGateTests(unittest.TestCase):
    def setUp(self):
        self.now, self.polls, self.sent, self.events = 0.0, [], [], []
        self.status = lambda: {'state': 'playing'} if self.now < 1.2 else {'state': 'idle'}
        self.clock = mock.patch.object(macro_gate.time, 'monotonic', side_effect=lambda: self.now)
        self.sleep = mock.patch.object(macro_gate.time, 'sleep', side_effect=self.advance)
        self.clock.start()
        self.sleep.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.sleep.stop)

    def advance(self, seconds):
        self.now += seconds

    def call(self, method, params=None):
        if method == 'macro.status':
            self.polls.append(self.now)
            return self.status()
        if method == 'macro.play':
            self.sent.append((self.now, params))
            return {}
        raise AssertionError(method)

    def dispatch(self, **options):
        options.setdefault('on_event', self.events.append)
        return macro_gate.dispatch(self.call, Path('/macro'), name='leave-css', **options)

    def test_fast_idle_sends_after_one_poll_without_an_event(self):
        self.status = lambda: {'state': 'idle'}
        self.assertTrue(self.dispatch())
        self.assertEqual(self.polls, [0])
        self.assertEqual(self.sent, [(0, {'path': '/macro'})])
        self.assertEqual(self.events, [])

    def test_busy_then_idle_without_a_witness_is_rejected_and_recorded_once(self):
        with self.assertRaises(closed_loop.DispatchRejected):
            self.dispatch()
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'not sent: no witness to requalify after the wait')

    def test_busy_then_idle_with_blind_declaration_is_sent_and_recorded_once(self):
        self.assertTrue(self.dispatch(requalify=macro_gate.BLIND))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'sent: the caller declared no witness')

    def test_busy_then_idle_requalifies_once_before_sending(self):
        witness = mock.Mock(return_value=True)
        self.assertTrue(self.dispatch(requalify=witness))
        witness.assert_called_once_with()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.events[0]['outcome'], 'requalified, sent')
        self.assertEqual(len(self.events), 1)

    def test_requalification_can_return_no_longer_needed(self):
        witness = mock.Mock(return_value=False)
        self.assertFalse(self.dispatch(requalify=witness))
        witness.assert_called_once_with()
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'not sent: no longer needed')

    def test_a_requalification_error_is_recorded_once_and_propagated(self):
        def refuse():
            raise RuntimeError('screen changed')

        with self.assertRaisesRegex(RuntimeError, 'screen changed'):
            self.dispatch(requalify=refuse)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'not sent: screen changed')

    def test_still_busy_at_fifteen_seconds_is_refused_and_recorded_once(self):
        self.status = lambda: {'state': 'playing'}
        with self.assertRaisesRegex(closed_loop.DispatchRejected, 'not sent: input channel busy'):
            self.dispatch(requalify=macro_gate.BLIND)
        self.assertEqual(self.sent, [])
        self.assertGreaterEqual(self.now, 15)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'not sent: still busy')

    def test_a_nearer_caller_deadline_bounds_the_wait(self):
        self.status = lambda: {'state': 'playing'} if self.now < 10.2 else {'state': 'idle'}
        with self.assertRaises(closed_loop.DispatchRejected):
            self.dispatch(until=9, requalify=macro_gate.BLIND)
        self.assertEqual(self.sent, [])
        self.assertLessEqual(self.now, 9.000001)
        self.assertEqual(len(self.events), 1)

    def test_a_stop_during_the_idle_gate_propagates_at_once(self):
        class StopRun(Exception):
            pass

        for phase in ('check', 'status', 'waiting', 'requalification'):
            with self.subTest(phase=phase):
                self.now, self.polls, self.sent, self.events = 0.0, [], [], []
                self.status = lambda: {'state': 'playing'} if self.now < .4 else {'state': 'idle'}

                def check():
                    if phase == 'check' or phase == 'waiting' and self.now >= .2:
                        raise StopRun('STOP-INPUT')

                def stopped_status():
                    raise StopRun('STOP-INPUT')

                def requalify():
                    if phase == 'requalification':
                        raise StopRun('STOP-INPUT')
                    return True

                if phase == 'status':
                    self.status = stopped_status
                with self.assertRaisesRegex(StopRun, 'STOP-INPUT'):
                    self.dispatch(requalify=requalify, check=check)
                self.assertEqual(self.sent, [])
                self.assertEqual(self.events, [])
                self.assertLessEqual(self.now, .4)

        entered, release = threading.Event(), threading.Event()
        self.now, self.polls, self.sent, self.events = 0.0, [], [], []

        def blocked_status():
            entered.set()
            release.wait()
            return {'state': 'idle'}

        def check_blocked():
            if entered.is_set():
                raise StopRun('STOP-INPUT')

        self.status = blocked_status
        try:
            with self.assertRaisesRegex(StopRun, 'STOP-INPUT'):
                self.dispatch(requalify=macro_gate.BLIND, check=check_blocked)
            self.assertEqual(self.sent, [])
            self.assertEqual(self.events, [])
        finally:
            release.set()

    def test_a_slow_first_idle_poll_counts_as_a_wait(self):
        def status():
            self.now += 3
            return {'state': 'idle'}

        self.status = status
        self.assertTrue(self.dispatch(requalify=macro_gate.BLIND))
        self.assertEqual(len(self.polls), 1)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'sent: the caller declared no witness')
        self.assertEqual(self.events[0]['seconds'], 3)

    def test_an_event_sink_exception_does_not_change_the_send_decision(self):
        seen = []

        def broken(event):
            seen.append(event)
            raise OSError('journal unavailable')

        self.assertTrue(self.dispatch(requalify=macro_gate.BLIND, on_event=broken))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['outcome'], 'sent: the caller declared no witness')

    def test_an_event_sink_exception_cannot_turn_a_refusal_into_a_send(self):
        seen = []

        def broken(event):
            seen.append(event)
            raise OSError('journal unavailable')

        with self.assertRaises(closed_loop.DispatchRejected):
            self.dispatch(on_event=broken)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['outcome'], 'not sent: no witness to requalify after the wait')
