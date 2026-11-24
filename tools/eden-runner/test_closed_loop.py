"""Offline closed-loop route tests with a scripted clock and observations."""
import unittest

import closed_loop
from closed_loop import RouteStopped, Step, run


class Script:
    def __init__(self, observe):
        self.now = 0.0
        self.script = observe
        self.sends = []
        self.events = []
        self.finishes = 0

    def observe(self):
        value = self.script(self)
        self.events.append(('read', value))
        return value

    def send(self, macro):
        self.sends.append((macro, self.now))
        self.events.append(('send', macro))

    def finish(self):
        self.finishes += 1
        self.events.append(('finish',))

    def sleep(self, seconds):
        self.now += seconds

    def run(self, steps, **kwargs):
        return run('fixture', steps, self.observe, self.send, self.finish,
                   clock=lambda: self.now, sleep=self.sleep, poll=1, **kwargs)


def step(name, source, destination, **kwargs):
    return Step(name, lambda value: value == source, lambda value: value == destination,
                name, cap=kwargs.pop('cap', 60), **kwargs)


class ClosedLoopTests(unittest.TestCase):
    def test_a_step_waits_while_the_input_channel_is_busy_and_then_sends_once(self):
        fake = Script(lambda f: 'done' if f.sends else 'source')
        polls = []

        def idle():
            value = 'macro playing' if len(polls) < 3 else None
            polls.append((fake.now, value))
            fake.events.append(('idle', value))
            return value

        result = fake.run([step('press', 'source', 'done')], idle=idle)
        self.assertEqual(fake.sends, [('press', 3)])
        self.assertEqual([value for _, value in polls[:4]], ['macro playing'] * 3 + [None])
        sent = fake.events.index(('send', 'press'))
        first_idle = fake.events.index(('idle', None))
        self.assertIn(('read', 'source'), fake.events[first_idle + 1:sent])
        self.assertEqual(result['steps'][0]['idle_waits'], 1)
        self.assertEqual(result['steps'][0]['idle_wait_ms'], 3000)
        self.assertEqual(result['stalls'][0]['kind'], 'idle-wait')

    def test_a_busy_channel_past_the_bound_stops_with_nothing_sent(self):
        fake = Script(lambda f: 'source')
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('press', 'source', 'done')], idle=lambda: 'macro playing', idle_cap=2)
        self.assertEqual(caught.exception.step, 'press')
        self.assertIn('input channel busy for 2 s: macro playing; nothing was sent', caught.exception.reason)
        self.assertEqual(fake.sends, [])
        self.assertEqual(fake.finishes, 0)
        self.assertEqual(caught.exception.receipt['steps'][0]['idle_waits'], 1)
        self.assertEqual(caught.exception.receipt['stalls'][0]['kind'], 'idle-wait')

    def test_the_read_before_the_wait_never_authorises_the_send(self):
        fake = Script(lambda f: 'source' if f.now < 2 else 'done')
        result = fake.run([step('press', 'source', 'done')],
                          idle=lambda: 'macro playing' if fake.now < 2 else None)
        self.assertEqual(fake.sends, [])
        self.assertEqual(fake.finishes, 0)
        self.assertEqual(result['stalls'][0]['kind'], 'idle-wait')

    def test_a_busy_rejection_waits_and_sends_once_more(self):
        fake = Script(lambda f: 'done' if f.sends else 'source')
        attempts = []
        send = fake.send

        def dispatch(macro):
            attempts.append(macro)
            fake.events.append(('attempt', macro))
            if len(attempts) == 1:
                raise RuntimeError('macro.play: input_busy')
            send(macro)

        fake.send = dispatch
        result = fake.run([step('press', 'source', 'done')], idle=lambda: None)
        self.assertEqual(attempts, ['press', 'press'])
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(result['steps'][0]['sends'], 1)
        self.assertEqual(result['steps'][0]['busy_rejections'], 1)
        first, second = (i for i, event in enumerate(fake.events) if event == ('attempt', 'press'))
        self.assertIn(('finish',), fake.events[first + 1:second])
        self.assertIn(('read', 'source'), fake.events[first + 1:second])
        self.assertEqual(result['stalls'][0]['kind'], 'busy-rejected')

    def test_two_busy_rejections_stop_without_a_third_dispatch(self):
        fake = Script(lambda f: 'source')
        attempts = []

        def dispatch(macro):
            attempts.append(macro)
            raise RuntimeError('macro.play busy')

        fake.send = dispatch
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('press', 'source', 'done')], idle=lambda: None)
        self.assertEqual(attempts, ['press', 'press'])
        self.assertEqual(fake.finishes, 2)
        self.assertEqual(caught.exception.reason, 'dispatch rejected as busy twice; nothing was delivered')
        self.assertEqual(caught.exception.receipt['steps'][0]['sends'], 0)
        self.assertEqual(caught.exception.receipt['steps'][0]['busy_rejections'], 2)
        self.assertEqual([event['kind'] for event in caught.exception.receipt['stalls']],
                         ['busy-rejected', 'busy-rejected'])

    def test_a_resend_also_waits_for_idle(self):
        fake = Script(lambda f: 'done' if len(f.sends) >= 2 else 'source')
        busy = []

        def idle():
            if fake.sends and len(busy) < 3:
                busy.append(fake.now)
                fake.events.append(('idle', 'macro playing'))
                return 'macro playing'
            fake.events.append(('idle', None))
            return None

        result = fake.run([step('press', 'source', 'done', lost_after=2)], idle=idle)
        self.assertEqual(len(fake.sends), 2)
        self.assertEqual(len(busy), 3)
        self.assertGreaterEqual(fake.sends[1][1] - busy[0], 3)
        first_idle = fake.events.index(('idle', 'macro playing'))
        next_idle = next(i for i in range(first_idle, len(fake.events)) if fake.events[i] == ('idle', None))
        self.assertNotIn(('finish',), fake.events[first_idle:next_idle])
        self.assertEqual(result['steps'][0]['idle_waits'], 1)
        self.assertIn('idle-wait', [event.get('kind') for event in result['stalls']])

    def test_a_rejected_resend_never_acts_on_a_new_target(self):
        attempts = []
        fake = Script(lambda f: ('source', 'tile-1' if len(attempts) >= 2 else 'tile-0'))
        send = fake.send

        def dispatch(macro):
            attempts.append(macro)
            if len(attempts) == 2:
                raise RuntimeError('macro.play busy')
            send(macro)

        fake.send = dispatch
        item = Step('press', lambda value: value[0] == 'source', lambda value: value[0] == 'done',
                    'press', cap=20, lost_after=2, target=lambda value: value[1])
        with self.assertRaises(RouteStopped) as caught:
            fake.run([item], idle=lambda: None)
        self.assertEqual(attempts, ['press', 'press'])
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 2)
        self.assertIn('source target changed', caught.exception.reason)

    def test_a_transition_after_a_rejected_resend_does_not_finish_twice(self):
        attempts = []
        fake = Script(lambda f: 'done' if len(attempts) >= 2 else 'source')
        send = fake.send

        def dispatch(macro):
            attempts.append(macro)
            if len(attempts) == 2:
                raise RuntimeError('macro.play busy')
            send(macro)

        fake.send = dispatch
        result = fake.run([step('press', 'source', 'done', lost_after=2)], idle=lambda: None)
        self.assertEqual(attempts, ['press', 'press'])
        self.assertEqual(fake.finishes, 2)
        self.assertEqual(result['steps'][0]['sends'], 1)
        self.assertEqual([event.get('kind', 'resend') for event in result['stalls']],
                         ['resend', 'busy-rejected'])

    def test_a_rejection_without_an_idle_callback_waits_and_reads_again(self):
        fake = Script(lambda f: 'done' if f.sends else 'source')
        attempts, send = [], fake.send

        def dispatch(macro):
            attempts.append(fake.now)
            if len(attempts) == 1:
                raise RuntimeError('input_busy')
            send(macro)

        fake.send = dispatch
        result = fake.run([step('press', 'source', 'done')])
        self.assertEqual(attempts, [0, 1])
        self.assertEqual(result['steps'][0]['sends'], 1)
        self.assertEqual(result['steps'][0]['busy_rejections'], 1)
        self.assertEqual(result['steps'][0]['idle_waits'], 1)
        self.assertEqual(result['steps'][0]['idle_wait_ms'], 1000)
        self.assertEqual(result['stalls'][0]['kind'], 'busy-rejected')

    def test_a_target_changed_while_waiting_to_resend_stays_unconfirmed(self):
        fake = Script(lambda f: ('source', 'tile-1' if f.now >= 6 else 'tile-0'))
        item = Step('press', lambda value: value[0] == 'source', lambda value: value[0] == 'done',
                    'press', cap=20, lost_after=2, target=lambda value: value[1])
        with self.assertRaises(RouteStopped) as caught:
            fake.run([item], idle=lambda: 'macro playing' if fake.sends and 3 <= fake.now < 6 else None)
        self.assertIn('source target changed', caught.exception.reason)
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 0)

    def test_a_busy_timeout_before_a_resend_keeps_the_input_unconfirmed(self):
        fake = Script(lambda f: 'source')
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('press', 'source', 'done', lost_after=2)], idle_cap=2,
                     idle=lambda: 'macro playing' if fake.sends else None)
        self.assertIn('input channel busy for 2 s', caught.exception.reason)
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 0)
        self.assertEqual(caught.exception.receipt['steps'][0]['sends'], 1)
        self.assertEqual(caught.exception.receipt['stalls'][0]['kind'], 'idle-wait')

    def test_a_one_second_idle_wait_is_counted_but_not_a_stall(self):
        fake = Script(lambda f: 'done' if f.sends else 'source')
        result = fake.run([step('press', 'source', 'done')],
                          idle=lambda: 'macro recording' if fake.now < 1 else None)
        self.assertEqual(result['steps'][0]['idle_waits'], 1)
        self.assertEqual(result['steps'][0]['idle_wait_ms'], 1000)
        self.assertEqual(result['stalls'], [])

    def test_a_changed_source_target_never_confirms_a_prepared_retry(self):
        for change_on in (2, 3):
            with self.subTest(change_on=change_on):
                at_three, changed = [], []

                def observe(fake):
                    if fake.now == 3:
                        at_three.append(True)
                        if len(at_three) == change_on:
                            changed.append(True)
                    return ('room', 'other' if changed else 'create')

                fake = Script(observe)
                item = Step('press', lambda value: value == ('room', 'create'),
                            lambda value: value[0] == 'done', 'press', cap=20, lost_after=2,
                            target=lambda value: value[1], source=lambda value: value[0] == 'room')
                with self.assertRaises(RouteStopped) as caught:
                    fake.run([item])
                self.assertIn('source target changed', caught.exception.reason)
                self.assertEqual(len(fake.sends), 1)
                self.assertEqual(fake.finishes, 0)

    def test_a_continuity_error_from_a_read_stops_with_a_receipt_and_never_sends_again(self):
        message = 'bridge connection or replay continuity changed while input was unconfirmed'
        for before_send in (False, True):
            with self.subTest(before_send=before_send):
                reads = []

                def observe(fake):
                    reads.append(fake.now)
                    if before_send or fake.sends:
                        raise RuntimeError(message)
                    return 'source'

                fake = Script(observe)
                with self.assertRaises(RouteStopped) as caught:
                    fake.run([step('press', 'source', 'done')])
                error = caught.exception
                self.assertEqual(error.step, 'locate' if before_send else 'press')
                self.assertEqual(error.reason, f'bridge continuity lost, nothing was sent after it: {message}')
                self.assertEqual(len(fake.sends), 0 if before_send else 1)
                self.assertEqual(len(reads), 1 if before_send else 2)
                self.assertEqual(fake.finishes, 0)
                if before_send:
                    self.assertEqual(error.receipt['continuity'], 'lost')
                else:
                    self.assertEqual(error.receipt['steps'][0]['continuity'], 'lost')
                    self.assertEqual(error.receipt['steps'][0]['sends'], 1)

    def test_an_unanswered_read_keeps_the_lost_timer_and_the_retry_runs(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        fake = Script(lambda f: 'done' if len(f.sends) >= 2 else
                      no_read if f.sends and f.now == 3 else 'source')
        result = fake.run([step('press', 'source', 'done', lost_after=3)])
        self.assertEqual(len(fake.sends), 2)
        self.assertEqual(fake.sends[1][1], 6)
        self.assertEqual(result['steps'][0]['unanswered'], 1)
        self.assertEqual(fake.finishes, 2)

    def test_an_unanswered_read_never_authorises_a_send(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        fake = Script(lambda f: 'done' if f.sends else no_read if f.now < 2 else 'source')
        item = Step('press', lambda value: True, lambda value: value == 'done', 'press', cap=10)
        result = fake.run([item])
        self.assertEqual(fake.sends, [('press', 2)])
        self.assertGreaterEqual(result['steps'][0]['unanswered'], 2)

    def test_unanswered_location_reads_are_recorded_when_already_done(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        fake = Script(lambda f: no_read if f.now < 2 else 'done')
        result = fake.run([step('press', 'source', 'done')])
        self.assertEqual(result['entered_at'], 'done')
        self.assertEqual(result['steps'][0]['unanswered'], 2)
        self.assertEqual(fake.sends, [])

    def test_a_grace_location_witness_is_not_evaluated_twice(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        deadline_reads, done_reads = [], []

        def observe(f):
            if f.now < 1:
                return no_read
            deadline_reads.append(True)
            return no_read if len(deadline_reads) == 1 else 'done'

        def done(value):
            if value != 'done':
                return False
            done_reads.append(True)
            return len(done_reads) == 1

        fake = Script(observe)
        item = Step('press', lambda value: value == 'source', done, 'press', cap=5)
        result = fake.run([item], locate_cap=1)
        self.assertEqual(result['entered_at'], 'done')
        self.assertEqual(len(done_reads), 1)
        self.assertEqual(result['steps'][0]['unanswered'], 2)
        self.assertEqual(fake.sends, [])

    def test_a_grace_location_source_does_not_authorize_a_first_send(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        deadline_reads = []

        def observe(f):
            if f.now < 1:
                return no_read
            deadline_reads.append(True)
            return no_read if len(deadline_reads) == 1 else 'source'

        fake = Script(observe)
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('press', 'source', 'done')], locate_cap=1)
        self.assertEqual(caught.exception.step, 'locate')
        self.assertIn('last answered read', caught.exception.reason)
        self.assertEqual(fake.sends, [])

    def test_a_reply_just_after_the_deadline_is_used(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        grace = []

        def observe(f):
            if not f.sends:
                return 'source'
            if f.now >= 4:
                grace.append(True)
                return no_read if len(grace) == 1 else 'done'
            return no_read

        fake = Script(observe)
        result = fake.run([step('press', 'source', 'done', cap=4)])
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 1)
        self.assertEqual(result['steps'][0]['unanswered'], 4)
        self.assertEqual(len(grace), 2)

    def test_only_unanswered_reads_until_the_cap_stop_with_the_last_answered_read(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        fake = Script(lambda f: 'source' if not f.sends else no_read)
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('press', 'source', 'done', cap=4)])
        self.assertIn("last answered read 'source'", caught.exception.reason)
        self.assertIn('unanswered reads', caught.exception.reason)
        self.assertEqual(caught.exception.receipt['steps'][0]['unanswered'], 5)
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 0)

    def test_unanswered_reads_are_skipped_while_the_next_step_waits_to_be_ready(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        arrived = []

        def observe(f):
            if not f.sends:
                return 'first'
            if len(f.sends) >= 2:
                return 'done'
            if not arrived:
                arrived.append(True)
                return 'arrived'
            return no_read if f.now < 3 else 'second'

        fake = Script(observe)
        result = fake.run([step('first', 'first', 'arrived'), step('second', 'second', 'done')])
        self.assertEqual(fake.sends, [('first', 0), ('second', 3)])
        self.assertEqual(result['steps'][1]['unanswered'], 2)
        self.assertEqual(fake.finishes, 2)

    def test_a_grace_ready_source_does_not_authorize_a_first_send(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        arrived, deadline_reads = [], []

        def observe(f):
            if not f.sends:
                return 'first'
            if not arrived:
                arrived.append(True)
                return 'arrived'
            if f.now >= 3:
                deadline_reads.append(True)
                return no_read if len(deadline_reads) == 1 else 'second'
            return no_read

        fake = Script(observe)
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('first', 'first', 'arrived'),
                      step('second', 'second', 'done', ready_cap=2)])
        self.assertEqual(caught.exception.step, 'second')
        self.assertIn('last answered read', caught.exception.reason)
        self.assertEqual(fake.sends, [('first', 0)])
        self.assertEqual(fake.finishes, 1)

    def test_a_grace_source_can_authorize_the_retry_only_if_the_lost_timer_was_satisfied(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        reads_at_three = []

        def observe(f):
            if len(f.sends) >= 2:
                return 'done'
            if f.sends and f.now == 3:
                reads_at_three.append(True)
                if len(reads_at_three) == 2:
                    return no_read
            return 'source'

        fake = Script(observe)
        result = fake.run([step('press', 'source', 'done', cap=3, lost_after=2)])
        self.assertEqual(len(fake.sends), 2)
        self.assertEqual(result['steps'][0]['unanswered'], 1)
        self.assertEqual(len(result['stalls']), 1)
        self.assertEqual(fake.finishes, 2)

    def test_a_grace_source_without_a_satisfied_lost_timer_does_not_resend(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        deadline_reads = []

        def observe(f):
            if not f.sends or f.now <= 2:
                return 'source'
            if f.now >= 4:
                deadline_reads.append(True)
                if len(deadline_reads) == 2:
                    return 'source'
            return no_read

        fake = Script(observe)
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('press', 'source', 'done', cap=4, lost_after=3)])
        self.assertIn('last answered read', caught.exception.reason)
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 0)

    def test_a_missing_retry_witness_does_not_finish_or_authorize_a_send(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        reads_at_three = []

        def observe(f):
            if len(f.sends) >= 2:
                return 'done'
            if f.sends and f.now == 3:
                reads_at_three.append(True)
                if len(reads_at_three) == 3:
                    return no_read
            return 'source'

        fake = Script(observe)
        result = fake.run([step('press', 'source', 'done', cap=10, lost_after=2)])
        self.assertEqual(result['steps'][0]['unanswered'], 1)
        self.assertEqual(len(fake.sends), 2)
        first_finish = fake.events.index(('finish',))
        self.assertEqual(fake.events[first_finish - 1], ('read', 'source'))
        self.assertIn(('read', no_read), fake.events[:first_finish])

    def test_a_missing_read_pauses_the_changed_target_timer(self):
        no_read = getattr(closed_loop, 'NO_READ', object())
        fake = Script(lambda f: ('source', 'tile-0') if not f.sends else
                      no_read if f.now == 3 else ('source', 'tile-1'))
        item = Step('press', lambda o: o[0] == 'source', lambda o: o[0] == 'done',
                    'press', cap=20, lost_after=3, target=lambda o: o[1])
        with self.assertRaises(RouteStopped) as caught:
            fake.run([item])
        self.assertIn('source target changed after the send', caught.exception.reason)
        self.assertEqual(fake.now, 6)
        self.assertEqual(caught.exception.receipt['steps'][0]['unanswered'], 1)
        self.assertEqual(fake.finishes, 0)

    def test_every_step_runs_once_on_the_plain_path(self):
        fake = Script(lambda f: ['one', 'two', 'done'][len(f.sends)])
        result = fake.run([step('first', 'one', 'two'), step('second', 'two', 'done')])
        self.assertEqual([s[0] for s in fake.sends], ['first', 'second'])
        self.assertEqual(result['entered_at'], 'first')
        self.assertEqual([(s['name'], s['sends'], s['skipped']) for s in result['steps']],
                         [('first', 1, False), ('second', 1, False)])
        self.assertEqual(fake.finishes, 2)
        for index, event in enumerate(fake.events):
            if event[0] == 'send':
                self.assertEqual(fake.events[index - 1][0], 'read')

    def test_a_lost_input_is_sent_again_and_then_passes(self):
        fake = Script(lambda f: 'done' if len(f.sends) >= 2 else 'source')
        result = fake.run([step('press', 'source', 'done', lost_after=4)])
        self.assertEqual(len(fake.sends), 2)
        self.assertGreaterEqual(fake.sends[1][1] - fake.sends[0][1], 4)
        self.assertEqual(result['steps'][0]['sends'], 2)
        self.assertEqual(fake.finishes, 2)
        # The lost send is confirmed only by the read that authorises the resend: read, finish, send.
        index = fake.events.index(('finish',))
        self.assertEqual(fake.events[index - 1], ('read', 'source'))
        self.assertEqual(fake.events[index + 1][0], 'send')

    def test_a_lost_input_is_resent_only_on_the_same_target(self):
        fake = Script(lambda f: ('done', None) if len(f.sends) >= 2 else ('source', 'tile-0'))
        item = Step('press', lambda o: o[0] == 'source', lambda o: o[0] == 'done',
                    'press', cap=20, lost_after=2, target=lambda o: o[1])
        result = fake.run([item])
        self.assertEqual([macro for macro, _ in fake.sends], ['press', 'press'])
        self.assertEqual(result['steps'][0]['sends'], 2)
        self.assertEqual(fake.finishes, 2)

    def test_a_changed_target_stops_without_finish_or_resend(self):
        fake = Script(lambda f: ('source', 'tile-0' if not f.sends else 'tile-1'))
        item = Step('press', lambda o: o[0] == 'source', lambda o: o[0] == 'done',
                    'press', cap=20, lost_after=2, target=lambda o: o[1])
        with self.assertRaises(RouteStopped) as caught:
            fake.run([item])
        self.assertEqual(caught.exception.step, 'press')
        self.assertIn('source target changed after the send', caught.exception.reason)
        self.assertIn('tile-0', caught.exception.reason)
        self.assertIn('tile-1', caught.exception.reason)
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 0)
        self.assertEqual(caught.exception.receipt['stalls'], [])

    def test_a_changed_fresh_retry_target_does_not_authorize_resending(self):
        reads_at_three = []

        def observe(f):
            if not f.sends or f.now < 3:
                return ('source', 'tile-0')
            if f.now == 3:
                reads_at_three.append(True)
                return ('source', 'tile-0' if len(reads_at_three) == 1 else 'tile-1')
            return ('source', 'tile-1')

        fake = Script(observe)
        item = Step('press', lambda o: o[0] == 'source', lambda o: o[0] == 'done',
                    'press', cap=20, lost_after=2, target=lambda o: o[1])
        with self.assertRaises(RouteStopped) as caught:
            fake.run([item])
        self.assertIn('source target changed after the send', caught.exception.reason)
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 0)
        self.assertEqual(caught.exception.receipt['stalls'], [])

    def test_a_retry_read_that_overruns_the_cap_never_finishes_or_resends(self):
        reads_at_three = []

        def observe(f):
            if f.sends and f.now == 3:
                reads_at_three.append(True)
                if len(reads_at_three) == 2:
                    f.now = 6
            return 'source'

        fake = Script(observe)
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('press', 'source', 'done', cap=5, lost_after=2)])
        self.assertIn('no transition within cap', caught.exception.reason)
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 0)
        self.assertEqual(caught.exception.receipt['stalls'], [])

    def test_a_send_error_stops_with_delivery_unknown(self):
        fake = Script(lambda f: 'source')

        def send(macro):
            fake.sends.append((macro, fake.now))
            raise RuntimeError('no reply to macro.play')

        fake.send = send
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('press', 'source', 'done')])
        self.assertEqual(caught.exception.step, 'press')
        self.assertEqual(caught.exception.reason, 'dispatch failed, delivery unknown: no reply to macro.play')
        self.assertEqual(caught.exception.receipt['steps'][0]['sends'], 1)
        self.assertEqual(caught.exception.receipt['steps'][0]['dispatch'], 'unknown')
        self.assertFalse(caught.exception.receipt['steps'][0]['skipped'])
        self.assertEqual(fake.finishes, 0)
        self.assertEqual(caught.exception.receipt['stalls'], [])

    def test_each_resend_reports_a_stall_event(self):
        fake = Script(lambda f: ('done', None) if len(f.sends) >= 3 else ('source', 'tile-0'))
        item = Step('press', lambda o: o[0] == 'source', lambda o: o[0] == 'done',
                    'press', cap=20, lost_after=2, target=lambda o: o[1])
        callbacks = []

        def on_stall(event, obs):
            callbacks.append((dict(event), obs))
            return {**event, 'extra': 'recorded'}

        result = fake.run([item], on_stall=on_stall)
        self.assertEqual(len(fake.sends), 3)
        self.assertEqual([event['send'] for event in result['stalls']], [1, 2])
        self.assertEqual([event['step'] for event in result['stalls']], ['press', 'press'])
        self.assertTrue(all(event['target'] == repr('tile-0') for event in result['stalls']))
        self.assertTrue(all(event['seconds_on_source'] >= 2 for event in result['stalls']))
        self.assertTrue(all(event['extra'] == 'recorded' for event in result['stalls']))
        self.assertEqual([obs for _, obs in callbacks], [('source', 'tile-0')] * 2)

    def test_the_last_lost_send_also_reports_a_stall(self):
        fake = Script(lambda f: 'source')
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('press', 'source', 'done', lost_after=2, sends=2)])
        self.assertEqual([event['send'] for event in caught.exception.receipt['stalls']], [1, 2])
        self.assertEqual(caught.exception.reason, 'no transition after 2 sends')
        self.assertEqual(fake.finishes, 2)

    def test_none_from_stall_callback_keeps_the_original_event(self):
        fake = Script(lambda f: 'done' if len(f.sends) >= 2 else 'source')
        seen = []
        result = fake.run([step('press', 'source', 'done', lost_after=2)],
                          on_stall=lambda event, obs: seen.append((dict(event), obs)))
        self.assertEqual(result['stalls'], [seen[0][0]])
        self.assertEqual(seen[0][1], 'source')
        self.assertEqual(result['stalls'][0]['target'], 'None')

    def test_a_slow_transition_is_waited_out_without_a_second_send(self):
        fake = Script(lambda f: 'source' if not f.sends else
                      ('done' if f.now - f.sends[-1][1] >= 40 else 'flight'))
        result = fake.run([step('press', 'source', 'done', cap=60)])
        self.assertEqual(len(fake.sends), 1)
        self.assertGreaterEqual(fake.now, 40)
        self.assertEqual(result['steps'][0]['sends'], 1)

    def test_source_reads_after_the_send_then_the_destination_need_one_send(self):
        def observe(f):
            if not f.sends:
                return 'source'
            elapsed = f.now - f.sends[0][1]
            return 'source' if elapsed < 3 else 'flight' if elapsed < 6 else 'done'
        fake = Script(observe)
        result = fake.run([step('press', 'source', 'done', lost_after=4)])
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(result['steps'][0]['sends'], 1)

    def test_a_bounce_does_not_restart_the_retry_budget(self):
        fake = Script(lambda f: 'two' if len(f.sends) % 2 == 0 else 'one')
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('first', 'one', 'two'), step('second', 'two', 'done')])
        self.assertEqual(caught.exception.step, 'second')
        self.assertEqual(caught.exception.reason, 'no transition after 3 sends')
        self.assertEqual([macro for macro, _ in fake.sends], ['second', 'first'] * 3)
        self.assertEqual(caught.exception.receipt['steps'][1]['sends'], 3)

    def test_a_flicker_back_to_the_source_resets_the_lost_timer(self):
        def observe(f):
            if not f.sends:
                return 'source'
            elapsed = f.now - f.sends[0][1]
            return ('source' if elapsed < 2 else 'flight' if elapsed < 4 else
                    'source' if elapsed < 6 else 'done')
        fake = Script(observe)
        fake.run([step('press', 'source', 'done', lost_after=4)])
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 1)

    def test_three_lost_inputs_stop_on_the_named_step(self):
        fake = Script(lambda f: 'source')
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('press', 'source', 'done', lost_after=4)])
        error = caught.exception
        self.assertEqual(error.step, 'press')
        self.assertIn('no transition after 3 sends', error.reason)
        self.assertEqual(len(fake.sends), 3)
        self.assertEqual(fake.finishes, 3)
        self.assertEqual(error.receipt['steps'][0]['sends'], 3)

    def test_no_readable_state_until_the_cap_stops_without_finish_or_resend(self):
        fake = Script(lambda f: 'source' if not f.sends else 'error')
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('press', 'source', 'done', cap=6)])
        self.assertEqual(caught.exception.step, 'press')
        self.assertIn('cap', caught.exception.reason)
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 0)

    def test_the_route_starts_at_the_step_matching_the_first_read(self):
        fake = Script(lambda f: 'two' if not f.sends else 'done')
        result = fake.run([step('first', 'one', 'two'), step('second', 'two', 'done')])
        self.assertEqual(result['entered_at'], 'second')
        self.assertEqual([s[0] for s in fake.sends], ['second'])
        self.assertTrue(result['steps'][0]['skipped'])

    def test_an_already_finished_route_sends_nothing(self):
        fake = Script(lambda f: 'done')
        result = fake.run([step('first', 'one', 'two'), step('second', 'two', 'done')])
        self.assertEqual(result['entered_at'], 'done')
        self.assertEqual(fake.sends, [])
        self.assertTrue(all(s['skipped'] for s in result['steps']))

    def test_an_unknown_first_screen_stops_on_locate(self):
        fake = Script(lambda f: 'unexpected')
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('first', 'one', 'done')], locate_cap=3)
        self.assertEqual(caught.exception.step, 'locate')
        self.assertIn('unexpected', caught.exception.reason)
        self.assertEqual(fake.sends, [])

    def test_a_jump_back_to_an_earlier_screen_resumes_there(self):
        def observe(f):
            return ['two', 'one', 'two', 'done'][len(f.sends)]
        fake = Script(observe)
        result = fake.run([step('first', 'one', 'two'), step('second', 'two', 'done')])
        self.assertEqual(result['entered_at'], 'second')
        self.assertEqual([s[0] for s in fake.sends], ['second', 'first', 'second'])
        self.assertEqual(fake.finishes, 3)

    def test_not_ready_within_ready_cap_stops_with_zero_sends(self):
        fake = Script(lambda f: 'arrived' if not f.events else 'waiting')
        with self.assertRaises(RouteStopped) as caught:
            fake.run([step('first', 'arrived', 'arrived', cap=3),
                      step('second', 'second', 'done', ready_cap=2)])
        self.assertEqual(caught.exception.step, 'second')
        self.assertIn('not ready', caught.exception.reason)
        self.assertEqual(caught.exception.receipt['steps'][-1]['sends'], 0)
        self.assertFalse(caught.exception.receipt['steps'][-1]['skipped'])
        self.assertEqual(fake.sends, [])

    def test_the_last_matching_step_wins_at_location(self):
        fake = Script(lambda f: 'shared' if not f.sends else 'done')
        result = fake.run([step('first', 'shared', 'middle'), step('second', 'shared', 'done')])
        self.assertEqual(result['entered_at'], 'second')
        self.assertEqual([macro for macro, _ in fake.sends], ['second'])

    def test_a_callable_macro_and_hook_use_the_fresh_retry_witness(self):
        fake = Script(lambda f: 'done' if len(f.sends) >= 2 else 'source')
        witnesses = []
        item = Step('press', lambda value: value == 'source', lambda value: value == 'done',
                    lambda value: 'send-' + value, cap=20, lost_after=2,
                    before_send=lambda value: witnesses.append(value))
        fake.run([item])
        self.assertEqual(witnesses, ['source', 'source'])
        self.assertEqual([macro for macro, _ in fake.sends], ['send-source', 'send-source'])
        for index, event in enumerate(fake.events):
            if event[0] == 'send':
                before = [item for item in fake.events[:index] if item != ('finish',)]
                self.assertEqual(before[-1], ('read', 'source'))

    def test_a_target_that_changes_on_the_last_read_before_the_resend_keeps_the_input_unconfirmed(self):
        # Lost on tile-0 for lost_after, same on the preflight read, then tile-1 on the read that would
        # authorise the resend: no resend, no finish, a stop that names the changed target.
        def state(fake):
            if not fake.sends:
                return ('source', 'tile-0')
            reads_after = sum(1 for item in fake.events[fake.events.index(('send', 'press')):] if item[0] == 'read')
            # Reads 1 to 3 time the loss, read 4 is the preflight; from read 5 the hand is on another tile.
            return ('source', 'tile-0') if reads_after < 4 else ('source', 'tile-1')
        fake = Script(state)
        item = Step('press', lambda o: o[0] == 'source', lambda o: o[0] == 'done',
                    'press', cap=20, lost_after=2, target=lambda o: o[1])
        with self.assertRaises(RouteStopped) as caught:
            fake.run([item])
        self.assertIn('source target changed after the send', caught.exception.reason)
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(fake.finishes, 0)

    def test_a_different_overlapping_step_after_send_jumps_back(self):
        fake = Script(lambda f: 'shared' if len(f.sends) < 2 else 'done')
        result = fake.run([step('first', 'shared', 'middle'), step('second', 'shared', 'done')])
        self.assertEqual(result['entered_at'], 'second')
        self.assertEqual([macro for macro, _ in fake.sends], ['second', 'first'])
        self.assertEqual(fake.finishes, 2)

    def test_a_destination_reached_while_waiting_is_skipped(self):
        def observe(f):
            if not f.sends:
                return 'first'
            return 'arrived' if f.now < 2 else 'done'
        fake = Script(observe)
        result = fake.run([step('first', 'first', 'arrived'), step('second', 'second', 'done')])
        self.assertEqual([macro for macro, _ in fake.sends], ['first'])
        self.assertTrue(result['steps'][1]['skipped'])

    def test_a_jump_back_during_ready_wait_is_observed_before_retry(self):
        def observe(f):
            if not f.sends:
                return 'one'
            if len(f.sends) == 1:
                return 'arrived' if f.now < 2 else 'one'
            return 'two' if len(f.sends) == 2 else 'done'
        fake = Script(observe)
        result = fake.run([step('first', 'one', 'arrived'), step('second', 'two', 'done')])
        self.assertEqual(result['entered_at'], 'first')
        self.assertEqual([macro for macro, _ in fake.sends], ['first', 'first', 'second'])

    def test_final_witness_completes_without_an_extra_read(self):
        def observe(f):
            if not f.sends:
                return 'source'
            if ('read', 'done') in f.events:
                raise AssertionError('read after completion')
            return 'done'
        fake = Script(observe)
        fake.run([step('press', 'source', 'done')])
        self.assertEqual(fake.finishes, 1)


if __name__ == '__main__':
    unittest.main()
