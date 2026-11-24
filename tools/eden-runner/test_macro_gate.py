"""Offline tests for the shared bounded macro dispatch gate."""
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import closed_loop
import macro_gate


class TicketTests(unittest.TestCase):
    def test_ticket_commit_and_revoke_are_terminal(self):
        committed = macro_gate.Ticket()
        self.assertTrue(committed.commit())
        self.assertFalse(committed.commit())
        self.assertFalse(committed.revoke())
        self.assertEqual(committed.state, 'COMMITTED')

        revoked = macro_gate.Ticket()
        self.assertTrue(revoked.revoke())
        self.assertFalse(revoked.commit())
        self.assertFalse(revoked.revoke())
        self.assertEqual(revoked.state, 'REVOKED')

    def test_commit_and_revoke_have_exactly_one_winner(self):
        for _ in range(300):
            ticket = macro_gate.Ticket()
            start = threading.Barrier(3)
            results = []

            def attempt(action):
                start.wait(timeout=1)
                results.append(action())

            workers = [threading.Thread(target=attempt, args=(ticket.commit,)),
                       threading.Thread(target=attempt, args=(ticket.revoke,))]
            try:
                for worker in workers:
                    worker.start()
                start.wait(timeout=1)
            finally:
                for worker in workers:
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
            self.assertCountEqual(results, [True, False])
            self.assertIn(ticket.state, ('COMMITTED', 'REVOKED'))


class ClaimTests(unittest.TestCase):
    def test_claim_outside_a_bounded_call_succeeds(self):
        self.assertTrue(macro_gate.claim())

    def test_a_claim_at_the_deadline_does_not_change_pending_state(self):
        outcome = macro_gate._Outcome(10)
        with mock.patch.object(macro_gate.time, 'monotonic', return_value=10):
            self.assertFalse(outcome.claim())
            self.assertEqual(outcome.state, 'pending')
            self.assertTrue(outcome.abandon())
            self.assertEqual(outcome.state, 'abandoned')

    def test_a_nearer_gate_deadline_also_closes_the_claim(self):
        outcome = macro_gate._Outcome(10)
        with mock.patch.object(macro_gate.time, 'monotonic', return_value=2):
            self.assertFalse(outcome.claim(limit=2))
            self.assertEqual(outcome.state, 'pending')

    def test_an_unclaimed_worker_remains_abandoned_after_timeout(self):
        entered, release = threading.Event(), threading.Event()
        workers, state = [], []

        def work(_):
            workers.append(threading.current_thread())
            entered.set()
            release.wait(1)
            state.append((macro_gate.abandoned(), macro_gate.claim()))

        try:
            self.assertEqual(macro_gate.bounded_call(work, 'gate', .1), (False, None))
            self.assertTrue(entered.is_set())
            release.set()
            workers[0].join(1)
            self.assertFalse(workers[0].is_alive())
            self.assertEqual(state, [(True, False)])
        finally:
            release.set()
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())

    def test_claimed_worker_is_subject_to_the_safety_bound(self):
        entered, release = threading.Event(), threading.Event()
        workers, state = [], []

        def work(_):
            workers.append(threading.current_thread())
            self.assertTrue(macro_gate.claim())
            entered.set()
            release.wait(1)
            state.append(macro_gate.abandoned())
            return 'done'

        try:
            with mock.patch.object(macro_gate, 'CLAIMED_WAIT_CAP', .05):
                with self.assertRaisesRegex(RuntimeError, 'the acquisition commit was claimed but did not finish'):
                    macro_gate.bounded_call(work, 'gate', .1)
            self.assertTrue(entered.is_set())
        finally:
            release.set()
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())
            self.assertEqual(state, [False])

    def test_claimed_worker_is_not_abandoned_when_the_caller_deadline_passes(self):
        entered, deadline, release = threading.Event(), threading.Event(), threading.Event()
        workers, answers, state = [], [], []
        abandon = macro_gate._Outcome.abandon

        def work(_):
            workers.append(threading.current_thread())
            self.assertTrue(macro_gate.claim())
            entered.set()
            release.wait(1)
            state.append(macro_gate.abandoned())
            return 'done'

        def mark_deadline(outcome):
            result = abandon(outcome)
            if not result:
                deadline.set()
            return result

        caller = threading.Thread(target=lambda: answers.append(macro_gate.bounded_call(work, 'gate', .1)))
        try:
            with mock.patch.object(macro_gate._Outcome, 'abandon', new=mark_deadline):
                caller.start()
                self.assertTrue(entered.wait(1))
                self.assertTrue(deadline.wait(1))
                self.assertTrue(caller.is_alive())
                release.set()
                caller.join(1)
                self.assertFalse(caller.is_alive())
            self.assertEqual(answers, [(True, 'done')])
            self.assertEqual(state, [False])
        finally:
            release.set()
            caller.join(1)
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())

    def test_a_stop_waits_for_a_claimed_commit_to_finish(self):
        entered, stop_seen, release = threading.Event(), threading.Event(), threading.Event()
        workers, errors = [], []

        class StopRun(Exception):
            pass

        def work(_):
            workers.append(threading.current_thread())
            self.assertTrue(macro_gate.claim())
            entered.set()
            release.wait(1)
            return 'done'

        def check():
            if entered.is_set():
                stop_seen.set()
                raise StopRun('stop')

        def call():
            try:
                macro_gate.bounded_call(work, 'gate', 1, check=check)
            except BaseException as error:
                errors.append(error)

        caller = threading.Thread(target=call)
        try:
            caller.start()
            self.assertTrue(stop_seen.wait(1))
            self.assertTrue(caller.is_alive())
            release.set()
            caller.join(1)
            self.assertFalse(caller.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], StopRun)
        finally:
            release.set()
            caller.join(1)
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())

    def test_deferred_cleanup_runs_only_after_the_claimed_call_returns(self):
        entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
        workers, answers = [], []

        def work(_):
            workers.append(threading.current_thread())
            self.assertTrue(macro_gate.defer_cleanup(cleaned.set))
            self.assertTrue(macro_gate.claim())
            entered.set()
            release.wait(1)
            return 'done'

        caller = threading.Thread(target=lambda: answers.append(macro_gate.bounded_call(work, 'gate', .5)))
        try:
            caller.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(cleaned.is_set())
            release.set()
            caller.join(1)
            self.assertFalse(caller.is_alive())
            self.assertEqual(answers, [(True, 'done')])
            self.assertTrue(cleaned.wait(1))
        finally:
            release.set()
            caller.join(1)
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())


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

    def assert_requalification_after_bound(self, *, until=None, completed, error=None):
        entered, release = threading.Event(), threading.Event()
        workers, seen = [], []
        bounded_call = macro_gate.bounded_call

        def witness(remaining):
            workers.append(threading.current_thread())
            seen.append(remaining)
            entered.set()
            release.wait(2)
            if error is not None:
                raise error
            return True

        def bounded(call, method, cap, **kwargs):
            if kwargs.get('with_completion'):
                join = threading.Thread.join

                def ordered_join(worker, timeout=None):
                    self.assertTrue(entered.wait(1))
                    self.assertIs(worker, workers[0])
                    self.now += method + .1
                    if completed:
                        release.set()
                        join(worker, 1)
                        self.assertFalse(worker.is_alive())
                    else:
                        join(worker, 0)

                with mock.patch.object(threading.Thread, 'join', new=ordered_join):
                    return bounded_call(call, method, cap, **kwargs)
            return bounded_call(call, method, cap, **kwargs)

        try:
            with mock.patch.object(macro_gate, 'bounded_call', side_effect=bounded):
                with self.assertRaises(closed_loop.DispatchRejected):
                    self.dispatch(requalify=witness, until=until)
            self.assertEqual(len(seen), 1)
            self.assertEqual(self.sent, [])
            self.assertEqual(len(self.events), 1)
            expected = ('not sent: the bound passed during requalification' if completed
                        else 'not sent: requalification did not answer within the bound')
            self.assertEqual(self.events[0]['outcome'], expected)
            if not completed:
                self.assertTrue(workers[0].is_alive())
        finally:
            release.set()
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())

    def test_requalification_that_outlasts_the_bound_never_sends(self):
        for until in (None, 4.0):
            with self.subTest(until=until):
                self.now, self.polls, self.sent, self.events = 0.0, [], [], []
                self.assert_requalification_after_bound(until=until, completed=True)

    def test_a_requalification_unanswered_at_the_bound_never_sends(self):
        for until in (None, 4.0):
            with self.subTest(until=until):
                self.now, self.polls, self.sent, self.events = 0.0, [], [], []
                self.assert_requalification_after_bound(until=until, completed=False)

    def test_a_stop_raised_after_the_requalification_bound_is_discarded(self):
        class StopRun(Exception):
            pass

        self.assert_requalification_after_bound(completed=True, error=StopRun('late STOP'))

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
