"""Offline tests for the shared bounded macro dispatch gate."""
import threading
import time
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
        self.assertEqual(self.events[0]['outcome'], 'the caller declared no witness, accepted')

    def test_busy_then_idle_requalifies_once_before_sending(self):
        witness = mock.Mock(return_value=True)
        self.assertTrue(self.dispatch(requalify=witness))
        witness.assert_called_once()
        self.assertAlmostEqual(witness.call_args.args[0], 15 - self.now)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.events[0]['outcome'], 'requalified, accepted')
        self.assertEqual(len(self.events), 1)

    def test_requalification_can_return_no_longer_needed(self):
        witness = mock.Mock(return_value=False)
        self.assertFalse(self.dispatch(requalify=witness))
        witness.assert_called_once()
        self.assertAlmostEqual(witness.call_args.args[0], 15 - self.now)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'not sent: no longer needed')

    def test_a_requalification_error_is_recorded_once_and_propagated(self):
        def refuse(remaining):
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

                def requalify(remaining):
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
        workers = []
        self.now, self.polls, self.sent, self.events = 0.0, [], [], []

        def blocked_status():
            workers.append(threading.current_thread())
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
            workers[0].join(1)
            self.assertFalse(workers[0].is_alive())

    def test_a_one_shot_stop_from_the_check_is_not_mistaken_for_a_status_or_witness_failure(self):
        class Cancelled(RuntimeError):
            pass

        for phase in ('status', 'requalification'):
            with self.subTest(phase=phase):
                self.now, self.polls, self.sent, self.events = 0.0, [], [], []
                armed = False

                def check():
                    nonlocal armed
                    if armed:
                        armed = False
                        raise Cancelled('one-shot STOP')

                def interrupted():
                    nonlocal armed
                    armed = True
                    check()

                if phase == 'status':
                    self.status = interrupted
                    requalify = lambda remaining: True
                else:
                    self.status = lambda: {'state': 'playing'} if self.now < .4 else {'state': 'idle'}
                    requalify = lambda remaining: interrupted()
                with self.assertRaisesRegex(Cancelled, 'one-shot STOP'):
                    self.dispatch(requalify=requalify, check=check)
                self.assertEqual(self.sent, [])
                self.assertEqual(self.events, [])

    def test_a_slow_first_idle_poll_counts_as_a_wait(self):
        def status():
            self.now += 3
            return {'state': 'idle'}

        self.status = status
        self.assertTrue(self.dispatch(requalify=macro_gate.BLIND))
        self.assertEqual(len(self.polls), 1)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'the caller declared no witness, accepted')
        self.assertEqual(self.events[0]['seconds'], 3)

    def test_an_event_sink_exception_does_not_change_the_send_decision(self):
        seen = []

        def broken(event):
            seen.append(event)
            raise OSError('journal unavailable')

        self.assertTrue(self.dispatch(requalify=macro_gate.BLIND, on_event=broken))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['outcome'], 'the caller declared no witness, accepted')

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

    def test_requalification_receives_only_the_remaining_budget(self):
        seen = []

        def witness(remaining):
            seen.append(remaining)
            return True

        self.assertTrue(self.dispatch(requalify=witness, until=4.0))
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0], 4.0 - self.now)
        self.assertGreater(seen[0], 0)
        self.assertEqual(self.events[0]['outcome'], 'requalified, accepted')

    def test_requalification_that_outlasts_the_bound_never_sends(self):
        for until in (None, 4.0):
            with self.subTest(until=until):
                self.now, self.polls, self.sent, self.events = 0.0, [], [], []
                seen = []

                def witness(remaining):
                    seen.append(remaining)
                    self.now += remaining + .1
                    return True

                with self.assertRaises(closed_loop.DispatchRejected):
                    self.dispatch(requalify=witness, until=until)
                self.assertEqual(len(seen), 1)
                self.assertEqual(self.sent, [])
                self.assertEqual(len(self.events), 1)
                self.assertEqual(self.events[0]['outcome'], 'not sent: the bound passed during requalification')

    def test_a_stop_raised_after_the_requalification_bound_is_discarded(self):
        class StopRun(Exception):
            pass

        def witness(remaining):
            self.now += remaining + .1
            raise StopRun('late STOP')

        with self.assertRaises(closed_loop.DispatchRejected):
            self.dispatch(requalify=witness)
        self.assertEqual(self.sent, [])
        self.assertEqual([event['outcome'] for event in self.events],
                         ['not sent: the bound passed during requalification'])

    def test_an_exception_raised_in_time_propagates_even_if_the_waiter_is_delayed(self):
        def witness(remaining):
            raise RuntimeError('witness failed in time')

        bounded_call = macro_gate.bounded_call

        def delayed(*args, **kwargs):
            result = bounded_call(*args, **kwargs)
            if kwargs.get('with_completion'):
                self.now = 16
            return result

        with mock.patch.object(macro_gate, 'bounded_call', side_effect=delayed):
            with self.assertRaisesRegex(RuntimeError, 'witness failed in time'):
                self.dispatch(requalify=witness)
        self.assertEqual(self.sent, [])
        self.assertEqual([event['outcome'] for event in self.events], ['not sent: witness failed in time'])

    def test_rejected_dispatch_after_requalification_is_recorded_as_rejected(self):
        original = self.call

        def call(method, params=None):
            if method == 'macro.play':
                raise RuntimeError('input_busy')
            return original(method, params)

        with mock.patch.object(self, 'call', side_effect=call):
            with self.assertRaises(closed_loop.DispatchRejected):
                self.dispatch(requalify=lambda remaining: True)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.events), 1)
        self.assertIn('requalified, rejected: input_busy', self.events[0]['outcome'])

    def test_unknown_dispatch_error_after_requalification_is_recorded_as_delivery_unknown(self):
        original = self.call

        def call(method, params=None):
            if method == 'macro.play':
                raise RuntimeError('socket closed')
            return original(method, params)

        with mock.patch.object(self, 'call', side_effect=call):
            with self.assertRaisesRegex(RuntimeError, 'socket closed'):
                self.dispatch(requalify=lambda remaining: True)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'requalified, delivery unknown: socket closed')

    def test_an_already_typed_rejection_is_not_reported_as_delivery_unknown(self):
        original = self.call

        def call(method, params=None):
            if method == 'macro.play':
                raise closed_loop.DispatchRejected('refused')
            return original(method, params)

        with mock.patch.object(self, 'call', side_effect=call):
            with self.assertRaisesRegex(closed_loop.DispatchRejected, 'refused'):
                self.dispatch(requalify=lambda remaining: True)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'requalified, rejected: refused')

    def test_dispatch_event_is_not_emitted_before_macro_play_resolves(self):
        original = self.call

        def call(method, params=None):
            if method == 'macro.play':
                self.assertEqual(self.events, [])
            return original(method, params)

        with mock.patch.object(self, 'call', side_effect=call):
            self.assertTrue(self.dispatch(requalify=lambda remaining: True))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['outcome'], 'requalified, accepted')


class RealTimeRequalificationTests(unittest.TestCase):
    def test_blocked_requalification_is_abandoned_with_one_event(self):
        for outcome in ('true', 'false', 'error'):
            with self.subTest(outcome=outcome):
                entered, release, exited = threading.Event(), threading.Event(), threading.Event()
                events, sent, polls, workers = [], [], [], []

                def call(method, params=None):
                    if method == 'macro.status':
                        polls.append(1)
                        return {'state': 'playing' if len(polls) == 1 else 'idle'}
                    sent.append(params)

                def witness(remaining):
                    workers.append(threading.current_thread())
                    entered.set()
                    try:
                        release.wait(.8)
                        if outcome == 'error':
                            raise RuntimeError('late error')
                        return outcome == 'true'
                    finally:
                        exited.set()

                try:
                    started = time.monotonic()
                    with mock.patch.object(macro_gate, 'IDLE_CAP', .35):
                        with self.assertRaises(closed_loop.DispatchRejected):
                            macro_gate.dispatch(call, Path('/macro'), name='test',
                                                requalify=witness, on_event=events.append)
                    elapsed = time.monotonic() - started
                    self.assertTrue(entered.is_set())
                    self.assertLessEqual(elapsed, .60)
                    self.assertEqual(sent, [])
                    self.assertEqual([event['outcome'] for event in events],
                                     ['not sent: requalification did not answer within the bound'])
                finally:
                    release.set()
                    self.assertTrue(exited.wait(1))
                    workers[0].join(1)
                    self.assertFalse(workers[0].is_alive())
                self.assertEqual(sent, [])
                self.assertEqual(len(events), 1)

    def test_requalification_which_answers_in_time_sends_once(self):
        calls, events = [], []

        def call(method, params=None):
            calls.append(method)
            if method == 'macro.status':
                return {'state': 'playing' if calls.count(method) == 1 else 'idle'}
            return {}

        def witness(remaining):
            time.sleep(.35)
            return True

        with mock.patch.object(macro_gate, 'IDLE_CAP', .7):
            self.assertTrue(macro_gate.dispatch(call, Path('/macro'), name='test',
                                                requalify=witness, on_event=events.append))
        self.assertEqual(calls.count('macro.play'), 1)
        self.assertEqual([event['outcome'] for event in events], ['requalified, accepted'])
        self.assertGreaterEqual(events[0]['seconds'], .5)

    def test_stop_during_a_blocked_requalification_propagates_without_an_event(self):
        class StopRun(Exception):
            pass

        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        workers, events, calls = [], [], []

        def call(method, params=None):
            calls.append(method)
            if method == 'macro.status':
                return {'state': 'playing' if calls.count(method) == 1 else 'idle'}

        def witness(remaining):
            workers.append(threading.current_thread())
            entered.set()
            try:
                release.wait(1)
                return True
            finally:
                exited.set()

        def check():
            if entered.is_set():
                raise StopRun('STOP-INPUT')

        started = time.monotonic()
        try:
            with self.assertRaises(StopRun):
                macro_gate.dispatch(call, Path('/macro'), name='test', requalify=witness,
                                    check=check, on_event=events.append)
            self.assertLess(time.monotonic() - started, .7)
            self.assertNotIn('macro.play', calls)
            self.assertEqual(events, [])
        finally:
            release.set()
            self.assertTrue(exited.wait(1))
            workers[0].join(1)
            self.assertFalse(workers[0].is_alive())
