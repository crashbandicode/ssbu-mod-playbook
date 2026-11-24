"""Offline closed-loop route tests with a scripted clock and observations."""
import unittest

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
