"""Host-only setup block probes with modelled bridge observations."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fake_clock
import routes
import test_native_picker as recorded


PAGES = ('cpu', 'saves', 'recording', 'fighters_stages', 'display_controls', 'developer')
READ_COST = .0001  # One bridge read, as the fake models it.


class MenuIO:
    def __init__(self, ctx, *, opened=False, page='cpu', depth=0, fault=None):
        self.ctx = ctx
        self.clock = ctx.clock
        self._observed_values = {}
        self.connection = 1
        self.boot = '0x0000000000000001'
        self.rev = 0
        self.actions = []
        self.fault = fault
        self.menu = {'scene': 'training', 'keyboard': '0', 'open': str(int(opened)),
                     'page': page, 'depth': str(depth), 'cursor': '0', 'group': '-1',
                     'row': '', 'row_id': '', 'action': 'row', 'focus': page + '/-/0/row',
                     'ack': '0', 'ack.result': 'handled'}

    def arm(self, provider, cap):
        pass

    def observe(self, provider, cap, sample):
        values = {key: value for key, value in sample.values.items() if key not in ('rev', 'observation')}
        if self._observed_values.get(provider) == values:
            self.clock.spend(min(cap, READ_COST))
        self._observed_values[provider] = values
        return sample

    def read(self, provider, cap):
        self.rev += 1
        metrics = self.ctx.runner.setup_metrics
        if metrics is not None:
            metrics['bridge_requests'] += 1
            metrics['bridge_boot'] = self.boot
            metrics['revisions'].setdefault(provider, [self.rev, self.rev])[1] = self.rev
        epoch = 2 if self.fault == 'epoch' and self.actions else 1
        if provider == 'tr.menu':
            return self.observe(provider, cap, recorded.snapshot(provider, self.rev, dict(self.menu), epoch=epoch))
        owner = 'tr.menu' if self.menu['open'] == '1' else 'native'
        if self.fault == 'owner' or self.fault == 'owner_after_input' and self.actions:
            owner = 'unknown'
        return self.observe(provider, cap, recorded.native(self.rev, epoch=epoch, screen='training', focus='training',
                                                          **{'screen.context': 'training', 'input.owner': owner}))

    def send(self, segment, until=None):
        self.actions.append(segment)
        metrics = self.ctx.runner.setup_metrics
        if metrics is not None:
            metrics['inputs'] += 1
        if segment == 'tr-menu-open':
            self.menu['open'] = '1'
            return
        if self.fault != 'no_ack':
            self.menu['ack'] = str(int(self.menu['ack']) + 1)
        if self.fault == 'ignored':
            self.menu['ack.result'] = 'ignored'
        if segment == 'tap-b1':
            depth = int(self.menu['depth'])
            if depth:
                self.menu['depth'] = str(depth - 1)
            else:
                self.menu['open'] = '0'
        elif segment == 'tap-r1':
            if self.fault != 'page_stuck':
                self.menu['page'] = PAGES[(PAGES.index(self.menu['page']) + 1) % len(PAGES)]
        else:
            raise AssertionError(segment)
        self.menu['focus'] = self.menu['page'] + '/-/0/row'

    def finish(self):
        pass


class OpenCloseTests(unittest.TestCase):
    def setUp(self):
        self.clock = fake_clock.install(self, routes, routes.native_picker)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ctx = SimpleNamespace(runner=SimpleNamespace(), row_dir=directory.name, on='s',
                                   instance={'bridge_log': str(Path(directory.name) / 'bridge.log')},
                                   check_stop=lambda: None, clock=self.clock)

    def invoke(self, name, io, **kwargs):
        with patch.object(routes, 'BridgePickerIO', return_value=io):
            result = getattr(routes, name)(self.ctx, **kwargs)
        json.dumps(result)
        return result

    def test_open_and_go_to_page_with_one_fenced_input_per_edge(self):
        io = MenuIO(self.ctx)
        result = self.invoke('tr_open', io, page='recording', cap=1)
        self.assertEqual(io.actions, ['tr-menu-open', 'tap-r1', 'tap-r1'])
        self.assertEqual(result['verified']['open'], '1')
        self.assertEqual(result['verified']['owner'], 'tr.menu')
        self.assertEqual(result['verified']['page'], 'recording')
        self.assertEqual(result['verified']['depth'], 0)
        self.assertEqual(result['inputs'], 3)
        self.assertTrue((Path(self.ctx.row_dir) / 'routes-tr-open.json').is_file())

    def test_already_open_backs_out_without_closing(self):
        io = MenuIO(self.ctx, opened=True, depth=2)
        result = self.invoke('tr_open', io, page='saves', cap=1)
        self.assertEqual(io.actions, ['tap-b1', 'tap-b1', 'tap-r1'])
        self.assertEqual(result['skipped'], ['open'])
        self.assertEqual(io.menu['open'], '1')

    def test_close_pops_before_the_final_b(self):
        io = MenuIO(self.ctx, opened=True, depth=2)
        result = self.invoke('tr_close', io, cap=1)
        self.assertEqual(io.actions, ['tap-b1', 'tap-b1', 'tap-b1'])
        self.assertEqual(result['verified'], {'open': '0', 'owner': 'native'})
        self.assertTrue((Path(self.ctx.row_dir) / 'routes-tr-close.json').is_file())

    def test_already_closed_checks_owner_without_input(self):
        io = MenuIO(self.ctx)
        result = self.invoke('tr_close', io, cap=1)
        self.assertEqual(io.actions, [])
        self.assertEqual(result['skipped'], ['close'])
        self.assertEqual(result['verified'], {'open': '0', 'owner': 'native'})

    def test_idle_already_idle_requires_no_input(self):
        io = MenuIO(self.ctx)
        io.menu.update({'game.running': '0', 'notice.header': ''})
        result = self.invoke('tr_idle', io, cap=1)
        self.assertEqual(io.actions, [])
        self.assertEqual(result['steps'], [])

    def test_idle_waits_for_transient_notice_without_spending_input_steps(self):
        for header in ('CPU Character', 'Fighters'):
            with self.subTest(header=header):
                io = MenuIO(self.ctx)
                io.menu.update({'game.running': '0', 'notice.header': header})
                clock, reads = [0.0], [0]
                read = io.read

                def delayed_read(provider, cap):
                    if provider == 'tr.menu':
                        reads[0] += 1
                        if reads[0] > 15:
                            io.menu['notice.header'] = ''
                    return read(provider, cap)

                io.read = delayed_read
                with patch.object(routes.time, 'monotonic', side_effect=lambda: clock[0]), \
                     patch.object(routes.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
                    result = self.invoke('tr_idle', io, cap=5)
                self.assertEqual(io.actions, [])
                self.assertEqual(result['steps'], [f'waited for notice "{header}" to clear'])
                self.assertGreater(reads[0], 12)
                self.assertLess(clock[0], 5)

    def test_idle_notice_that_never_clears_fails_at_cap(self):
        io = MenuIO(self.ctx)
        io.menu.update({'game.running': '0', 'notice.header': 'CPU Character'})
        clock = [0.0]
        with patch.object(routes.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(routes.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaisesRegex(RuntimeError, 'expected plain idle Training within 1 s.*CPU Character'):
                self.invoke('tr_idle', io, cap=1)
        self.assertEqual(clock[0], 1)
        self.assertEqual(io.actions, [])

    def test_idle_waits_for_native_owner_without_input(self):
        io = MenuIO(self.ctx, fault='owner')
        io.menu.update({'game.running': '0', 'notice.header': ''})
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
            if clock[0] >= .6:
                io.fault = None

        with patch.object(routes.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(routes.time, 'sleep', side_effect=sleep):
            result = self.invoke('tr_idle', io, cap=2)
        self.assertEqual(result['verified']['input.owner'], 'native')
        self.assertEqual(io.actions, [])

    def test_idle_observes_success_after_twelfth_input_step(self):
        io = MenuIO(self.ctx, opened=True)
        io.menu.update({'game.running': '0', 'notice.header': ''})
        calls = []

        def close(_io, _until, after):
            calls.append(after)
            if len(calls) == 12:
                io.menu['open'] = '0'

        with patch.object(routes, '_close_menu', side_effect=close):
            result = self.invoke('tr_idle', io, cap=1)
        self.assertEqual(len(calls), 12)
        self.assertEqual(result['verified']['open'], '0')

    def test_idle_closes_open_menu(self):
        io = MenuIO(self.ctx, opened=True, depth=2)
        io.menu.update({'game.running': '0', 'notice.header': ''})
        result = self.invoke('tr_idle', io, cap=1)
        self.assertEqual(io.actions, ['tap-b1', 'tap-b1', 'tap-b1'])
        self.assertTrue(result['steps'])

    def test_idle_backs_out_of_wizard_to_menu_or_idle(self):
        for ending in ('menu', 'idle'):
            with self.subTest(ending=ending):
                io = BackWizardIO(self.ctx, ending=ending)
                result = self.invoke('tr_idle', io, cap=1)
                self.assertEqual(io.actions, ['tap-b1', 'tap-b1'] + (['tap-b1'] if ending == 'menu' else []))
                self.assertEqual(result['steps'][:2], ['B out of wizard page Edge · 2/4',
                                                      'B out of wizard page Edge · 1/4'])
                self.assertEqual(result['verified']['open'], '0')
                self.assertEqual(result['verified']['game.running'], '0')
                self.assertIsNone(io.pending_input)

    def test_idle_wizard_no_change_clears_pending_without_retry(self):
        io = BackWizardIO(self.ctx, stuck=True)
        clock = [0.0]
        read = io.read

        def delayed_read(provider, cap):
            if provider == 'tr.menu' and io.pending_input:
                clock[0] += min(.25, cap)
            return read(provider, cap)

        io.read = delayed_read
        with patch.object(routes.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(RuntimeError, 'tr_idle: expected the wizard page to change after B; saw'):
                self.invoke('tr_idle', io, cap=5)
        self.assertEqual(io.actions, ['tap-b1'])
        self.assertIsNone(io.pending_input)
        self.assertEqual(clock[0], 5.0)

    def test_idle_wizard_cancel_publication_after_four_seconds(self):
        io = BackWizardIO(self.ctx, ending='menu')
        clock = [0.0]
        release = [None]
        read, send = io.read, io.send

        def delayed_send(segment, until=None):
            if segment == 'tap-b1' and io.menu['open'] == '0' and io.step == 1:
                io.actions.append(segment)
                io.ctx.runner.setup_metrics['inputs'] += 1
                io.pending_input = segment
                release[0] = clock[0] + 4.0
                return
            send(segment, until=until)

        def delayed_read(provider, cap):
            if provider == 'tr.menu' and io.pending_input:
                clock[0] += min(.25, cap)
                if release[0] is not None and clock[0] >= release[0]:
                    io.menu.update({'open': '1', 'depth': '0', 'game.running': '0',
                                    'notice.header': '', 'notice.body': ''})
                    release[0] = None
            return read(provider, cap)

        io.send, io.read = delayed_send, delayed_read
        with patch.object(routes.time, 'monotonic', side_effect=lambda: clock[0]):
            result = self.invoke('tr_idle', io, cap=8)
        self.assertEqual(result['steps'], ['B out of wizard page Edge · 2/4',
                                          'B out of wizard page Edge · 1/4', 'close menu'])
        self.assertEqual(io.actions, ['tap-b1', 'tap-b1', 'tap-b1'])
        self.assertGreaterEqual(clock[0], 4.0)
        self.assertLess(clock[0], 8.0)
        self.assertIsNone(io.pending_input)

    def test_idle_stops_running_guard_edge_with_existing_helper(self):
        io = MenuIO(self.ctx)
        io.menu.update({'game.running': '1', 'game': 'Guard the Edge', 'notice.header': 'Guard the Edge'})

        def stop(ctx, game, cap, metrics):
            self.assertEqual(game, 'Guard the Edge')
            io.menu['game.running'] = '0'
            io.menu['notice.header'] = ''

        with patch.object(routes, '_drill_stop', side_effect=stop):
            result = self.invoke('tr_idle', io, cap=1)
        self.assertTrue(result['steps'])
        self.assertEqual(io.menu['game.running'], '0')

    def test_unknown_page_is_refused_before_any_input(self):
        io = MenuIO(self.ctx)
        with self.assertRaisesRegex(RuntimeError, 'page'):
            self.invoke('tr_open', io, page='unknown', cap=1)
        self.assertEqual(io.actions, [])

    def test_page_change_requires_ack_progress_and_epoch(self):
        for fault, message in (('no_ack', 'not handled'), ('ignored', 'handled ACK'),
                               ('epoch', 'connection'), ('page_stuck', 'page')):
            with self.subTest(fault=fault):
                io = MenuIO(self.ctx, opened=True, fault=fault)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.invoke('tr_open', io, page='saves', cap=.02)

    def test_owner_is_required_before_open(self):
        io = MenuIO(self.ctx, fault='owner')
        with self.assertRaisesRegex(RuntimeError, 'owner'):
            self.invoke('tr_open', io, cap=.02)
        self.assertEqual(io.actions, [])

    def test_open_final_unknown_owner_is_failure_receipt(self):
        io = MenuIO(self.ctx, opened=True, fault='owner_after_input')
        with self.assertRaisesRegex(RuntimeError, r'expected.*owner.*tr.menu.*saw.*unknown'):
            self.invoke('tr_open', io, page='saves', cap=.03)
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-tr-open.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)
        self.assertEqual(receipt['last_seen']['input.owner'], 'unknown')

    def test_repeated_block_receipts_do_not_overwrite(self):
        io = MenuIO(self.ctx, opened=True)
        first = self.invoke('tr_open', io, cap=.1)
        first_path = Path(self.ctx.row_dir) / 'routes-tr-open.json'
        original = first_path.read_bytes()
        second = self.invoke('tr_open', io, page='saves', cap=.1)
        self.assertTrue(first['ok'])
        self.assertTrue(second['ok'])
        self.assertEqual(first_path.read_bytes(), original)
        self.assertEqual(json.loads((Path(self.ctx.row_dir) / 'routes-tr-open-2.json').read_text()), second)


class RowsIO(MenuIO):
    def __init__(self, ctx, *, fault=None):
        super().__init__(ctx, opened=True, page='fighters_stages', fault=fault)
        self.page_index = 0
        self.row_index = 0
        self.page_rows = [('other', 'Other', 'other'), ('game.6', 'Guard the Edge', '')]
        self.game_rows = [('start.6', 'Start', ''), ('edge_difficulty', 'CPU fighter and mode', 'edge_difficulty'),
                          ('row', 'Set up…', '')]
        self.update_focus()

    def update_focus(self):
        nested = self.menu['depth'] != '0'
        action, row, row_id = (self.game_rows[self.row_index] if nested else self.page_rows[self.page_index])
        self.menu.update({'cursor': str(self.page_index), 'group': '26' if nested else '-1',
                          'action': action, 'row': row, 'row_id': row_id,
                          'focus': f'fighters_stages/{26 if nested else -1}/{self.page_index}/{action}'})

    def send(self, segment, until=None):
        if segment not in ('main-menu-down', 'tap-a1'):
            return super().send(segment, until=until)
        self.actions.append(segment)
        if self.ctx.runner.setup_metrics is not None:
            self.ctx.runner.setup_metrics['inputs'] += 1
        if self.fault != 'no_ack':
            self.menu['ack'] = str(int(self.menu['ack']) + 1)
        if self.fault == 'ignored':
            self.menu['ack.result'] = 'ignored'
        if segment == 'tap-a1':
            if self.fault != 'no_depth':
                self.menu['depth'] = '1'
                self.row_index = 0
        elif self.fault != 'stuck':
            if self.menu['depth'] == '0':
                self.page_index = (self.page_index + 1) % len(self.page_rows)
            else:
                self.row_index = (self.row_index + 1) % len(self.game_rows)
        self.update_focus()
        if self.fault == 'no_group' and self.menu['depth'] != '0':
            self.menu['group'] = '-1'
        if self.fault == 'wrong_page':
            self.menu['page'] = 'saves'


class GotoTests(OpenCloseTests):
    def test_dropped_first_input_retries_once(self):
        io = RowsIO(self.ctx)
        send = io.send
        dropped = False

        def send_with_drop(segment, until=None):
            nonlocal dropped
            if segment == 'main-menu-down' and not dropped:
                dropped = True
                io.actions.append(segment)
                io.ctx.runner.setup_metrics['inputs'] += 1
                return
            send(segment, until=until)

        io.send = send_with_drop
        with patch.object(routes, 'INPUT_LAG_S', .003, create=True):
            result = self.invoke('tr_goto', io, page='fighters_stages', path=['game.6'], cap=.1)
        self.assertEqual(result['retries'], 1)
        self.assertEqual(io.actions, ['main-menu-down', 'main-menu-down'])

    def test_late_input_and_retry_double_ack_fails(self):
        io = RowsIO(self.ctx)
        send = io.send
        first = True

        def send_with_late_input(segment, until=None):
            nonlocal first
            if segment == 'main-menu-down' and first:
                first = False
                io.actions.append(segment)
                io.ctx.runner.setup_metrics['inputs'] += 1
                return
            send(segment, until=until)
            send(segment, until=until)

        io.send = send_with_late_input
        with patch.object(routes, 'INPUT_LAG_S', .003, create=True):
            with self.assertRaisesRegex(RuntimeError, 'input handled twice'):
                self.invoke('tr_goto', io, page='fighters_stages', path=['game.6'], cap=.1)

    def test_row_title_matches_normalized_meaning(self):
        io = RowsIO(self.ctx)
        result = self.invoke('tr_goto', io, page='fighters_stages',
                             path=['game.6', {'row': '  SET   UP... '}], cap=1)
        self.assertEqual(result['verified']['row'], 'Set up…')

    def test_goto_final_unknown_owner_is_failure_receipt(self):
        io = RowsIO(self.ctx, fault='owner_after_input')
        with self.assertRaisesRegex(RuntimeError, r'expected.*owner.*tr.menu.*saw.*unknown'):
            self.invoke('tr_goto', io, page='fighters_stages', path=['game.6'], cap=.03)
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-tr-goto.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)
        self.assertEqual(receipt['last_seen']['input.owner'], 'unknown')

    def test_nested_path_uses_focused_identity_not_outer_cursor(self):
        io = RowsIO(self.ctx)
        result = self.invoke('tr_goto', io, page='fighters_stages',
                             path=['game.6', {'action': 'row', 'row': 'Set up…'}], cap=1)
        self.assertEqual(io.actions, ['main-menu-down', 'tap-a1', 'main-menu-down', 'main-menu-down'])
        self.assertEqual((result['verified']['cursor'], result['verified']['group']), ('1', '26'))
        self.assertEqual(result['verified']['row'], 'Set up…')
        self.assertEqual(io.menu['open'], '1')
        self.assertTrue((Path(self.ctx.row_dir) / 'routes-tr-goto.json').is_file())

    def test_root_and_nested_wrap_refuse_missing_rows(self):
        for path in (['not-found'], ['game.6', 'not-found']):
            with self.subTest(path=path):
                io = RowsIO(self.ctx)
                with self.assertRaisesRegex(RuntimeError, 'row not found before wrapping'):
                    self.invoke('tr_goto', io, page='fighters_stages', path=path, cap=1)

    def test_unacknowledged_epoch_and_unchanged_identity_fail(self):
        for fault, message in (('no_ack', 'not handled'), ('ignored', 'handled ACK'),
                               ('epoch', 'connection'), ('no_depth', 'depth'),
                               ('no_group', 'group'), ('wrong_page', 'page')):
            with self.subTest(fault=fault):
                io = RowsIO(self.ctx, fault=fault)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.invoke('tr_goto', io, page='fighters_stages', path=['game.6', 'row'], cap=.05)
        io = RowsIO(self.ctx)
        original = io.send

        def send(segment, until=None):
            if segment == 'main-menu-down' and io.menu['depth'] != '0':
                io.fault = 'stuck'
            original(segment, until=until)

        io.send = send
        with self.assertRaisesRegex(RuntimeError, 'focus identity did not change'):
            self.invoke('tr_goto', io, page='fighters_stages', path=['game.6', 'row'], cap=.1)

    def test_invalid_path_is_refused_before_input(self):
        for path in ([], [''], [{'row': 'x', 'extra': 'y'}], [42]):
            with self.subTest(path=path):
                io = RowsIO(self.ctx)
                with self.assertRaisesRegex(RuntimeError, 'path'):
                    self.invoke('tr_goto', io, page='fighters_stages', path=path, cap=.1)
                self.assertEqual(io.actions, [])


class StopIO(RowsIO):
    def __init__(self, ctx, *, fault=None):
        super().__init__(ctx, fault=fault)
        self.menu.update({'game': 'Guard the Edge', 'game.running': '1',
                          'notice.header': '', 'notice.body': ''})

    def send(self, segment, until=None):
        if segment == 'tap-a1' and self.menu['depth'] == '1':
            self.actions.append(segment)
            if self.ctx.runner.setup_metrics is not None:
                self.ctx.runner.setup_metrics['inputs'] += 1
            if self.fault != 'no_ack':
                self.menu['ack'] = str(int(self.menu['ack']) + 1)
            if self.fault == 'ignored':
                self.menu['ack.result'] = 'ignored'
            if self.fault != 'stop_stuck':
                self.menu['game.running'] = '0'
        else:
            super().send(segment, until=until)
            if segment == 'tap-b1':
                self.update_focus()


class StopTests(OpenCloseTests):
    def test_stop_failure_receipt_records_last_seen_without_verified(self):
        io = StopIO(self.ctx)
        io.menu['game.running'] = '0'
        with self.assertRaisesRegex(RuntimeError, 'running'):
            self.invoke('drill_stop', io, game='Guard the Edge', cap=.1)
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-drill-stop.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)
        self.assertEqual(receipt['last_seen']['game.running'], '0')

    def test_stop_uses_running_transition_and_closes_the_menu(self):
        io = StopIO(self.ctx)
        result = self.invoke('drill_stop', io, game='Guard the Edge', cap=1)
        self.assertEqual(io.actions, ['main-menu-down', 'tap-a1', 'tap-a1', 'tap-b1', 'tap-b1'])
        self.assertEqual(result['verified'], {'game': 'Guard the Edge', 'running': '0'})
        self.assertEqual(io.menu['row'], 'Guard the Edge')
        self.assertTrue((Path(self.ctx.row_dir) / 'routes-drill-stop.json').is_file())

    def test_wizard_wrong_game_and_stopped_are_refused_before_input(self):
        for field, value, message in (('notice.header', 'Edge · 2/4', 'wizard'),
                                      ('game.running', '0', 'running'),
                                      ('game', 'Juggler', 'game')):
            with self.subTest(field=field):
                io = StopIO(self.ctx)
                io.menu[field] = value
                with self.assertRaisesRegex(RuntimeError, message):
                    self.invoke('drill_stop', io, game='Guard the Edge', cap=.1)
                self.assertEqual(io.actions, [])
        io = StopIO(self.ctx)
        with self.assertRaisesRegex(RuntimeError, 'game'):
            self.invoke('drill_stop', io, game='Juggler', cap=.1)
        self.assertEqual(io.actions, [])

    def test_stop_requires_ack_epoch_and_running_change(self):
        for fault, message in (('no_ack', 'not handled'), ('epoch', 'connection'),
                               ('ignored', 'handled ACK'), ('stop_stuck', 'running')):
            with self.subTest(fault=fault):
                io = StopIO(self.ctx, fault=fault)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.invoke('drill_stop', io, game='Guard the Edge', cap=.03)


class WizardIO(RowsIO):
    def __init__(self, ctx, *, step=1, fault=None, pages=4):
        super().__init__(ctx, fault=fault)
        self.step = step
        self.pages = pages
        self.area_index = 3
        self.areas = ('Lower left', 'Lower right', 'Upper left', 'Upper right')
        self.cpu = [1.0, 1.0]
        self.p1 = [0.0, 0.0]
        self.menu.update({'open': '0', 'game': 'Guard the Edge', 'game.running': '1'})
        self.last_menu = None
        self.menu_rev = 0
        self.refresh_notice()

    def refresh_notice(self):
        self.menu['notice.header'] = f'Edge · {self.step}/{self.pages}'
        cpu = ', '.join(format(v, '.0f') for v in self.cpu)
        p1 = ', '.join(format(v, '.0f') for v in self.p1)
        kind = (('area', 'cpu', 'p1', 'preview') if self.pages == 4 else ('cpu', 'p1', 'preview'))[self.step - 1]
        self.menu['notice.body'] = (
            f'{self.areas[self.area_index]} ({cpu}) · D-pad choose · A next' if kind == 'area' else
            f'CPU ({cpu}) · Stick move · A next' if kind == 'cpu' else
            f'P1 ({p1}) · Stick move · A next' if kind == 'p1' else
            f'CPU ({cpu}) · 100% · P1 ({p1}) · A/+ start')

    def read(self, provider, cap):
        if provider != 'tr.menu':
            return super().read(provider, cap)
        if self.last_menu != self.menu:
            self.menu_rev += 1
            self.last_menu = dict(self.menu)
        metrics = self.ctx.runner.setup_metrics
        if metrics is not None:
            metrics['bridge_requests'] += 1
            metrics['bridge_boot'] = self.boot
            metrics['revisions'].setdefault(provider, [self.menu_rev, self.menu_rev])[1] = self.menu_rev
        epoch = 2 if self.fault == 'epoch' and self.actions else 1
        return self.observe(provider, cap, recorded.snapshot(provider, self.menu_rev, dict(self.menu), epoch=epoch))

    def send(self, segment, until=None):
        if self.menu['open'] == '1':
            if segment == 'tap-a1' and self.menu['depth'] == '1' and self.menu['row'] == 'Set up…':
                self.actions.append(segment)
                self.ctx.runner.setup_metrics['inputs'] += 1
                self.menu['ack'] = str(int(self.menu['ack']) + 1)
                self.menu.update({'open': '0', 'game': 'Guard the Edge', 'game.running': '1'})
                self.step = 1
                self.refresh_notice()
                return
            return super().send(segment, until=until)
        self.actions.append(segment)
        if self.ctx.runner.setup_metrics is not None:
            self.ctx.runner.setup_metrics['inputs'] += 1
        if segment == 'tap-a1':
            if self.fault != 'header_stuck' and self.step < self.pages:
                self.step += 1
        elif segment == 'main-menu-right':
            self.area_index = (self.area_index + 1) % 4
        elif segment.startswith('wizard-'):
            _, direction, size = segment.split('-')
            if self.fault != 'null':
                first_hidden = self.fault == 'unpublished_once' and sum(
                    action.startswith('wizard-') for action in self.actions) == 1
                amount = .2 if first_hidden else {'coarse': 20.0, 'medium': 4.0, 'fine': .7}[size]
                axis = 0 if direction in ('left', 'right') else 1
                sign = -1 if direction in ('left', 'down') else 1
                point = self.cpu if self.step == (2 if self.pages == 4 else 1) else self.p1
                point[axis] += sign * amount
                if self.fault == 'disturb' and axis == 1:
                    point[0] += 1
        else:
            raise AssertionError(segment)
        self.refresh_notice()


class BackWizardIO(WizardIO):
    def __init__(self, ctx, *, ending='idle', stuck=False):
        super().__init__(ctx, step=2)
        self.ending = ending
        self.stuck = stuck
        self.pending_input = None

    def send(self, segment, until=None):
        if segment == 'tap-b1' and self.menu['open'] == '0' and self.menu['notice.header'].startswith('Edge · '):
            self.actions.append(segment)
            self.ctx.runner.setup_metrics['inputs'] += 1
            self.pending_input = segment
            if not self.stuck:
                if self.step > 1:
                    self.step -= 1
                    self.refresh_notice()
                else:
                    self.menu.update({'open': '1' if self.ending == 'menu' else '0', 'depth': '0',
                                      'game.running': '0', 'notice.header': '', 'notice.body': ''})
            return
        return super().send(segment, until=until)

    def finish(self):
        self.pending_input = None


class WizardTests(OpenCloseTests):
    def test_preview_owner_scope_preserves_pause_and_unknown(self):
        for owner in ('native.pause', 'unknown'):
            with self.subTest(owner=owner):
                io = WizardIO(self.ctx, step=4)
                read = io.read

                def read_owner(provider, cap):
                    if provider == routes.native_picker.UI:
                        return recorded.native(1, screen='training', focus='training',
                                               **{'screen.context': 'training', 'input.owner': owner})
                    return read(provider, cap)

                io.read = read_owner
                result = self.invoke('wizard_place', io, game='Guard the Edge', cap=.1, pulse_cap=.002)
                self.assertTrue(result['ok'])
                self.assertEqual(result['verified']['owners']['Edge · 4/4'], owner)
                self.assertEqual(result['verified']['scope'],
                                 'published coordinates only; input readiness is not certified')
                self.assertEqual(result['verified']['limitations'], [
                    'Edge · 4/4: input.owner=native.pause observed while this page was published'
                ] if owner == 'native.pause' else [])

    def test_wrong_or_stopped_game_never_accepts_wizard_or_sends_input(self):
        for step in (2, 4):
            for game, running in (('Eggs-ercise', '1'), ('Guard the Edge', '0')):
                with self.subTest(step=step, game=game, running=running):
                    io = WizardIO(self.ctx, step=step)
                    io.menu.update({'game': game, 'game.running': running})
                    with self.assertRaisesRegex(RuntimeError, 'expected.*Guard the Edge'):
                        self.invoke('wizard_place', io, game='Guard the Edge', cap=.1, pulse_cap=.002)
                    self.assertEqual(io.actions, [])

    def test_three_page_wizard_uses_page_meaning(self):
        io = WizardIO(self.ctx, pages=3)
        result = self.invoke('wizard_place', io, game='Guard the Edge',
                             cpu=[1, 1], p1=[0, 0], cap=.2, pulse_cap=.002)
        self.assertEqual([page['kind'] for page in result['verified']['pages']], ['CPU', 'P1', 'Preview'])
        self.assertEqual(result['verified']['pages'][-1]['header'], 'Edge · 3/3')

    def test_area_request_on_three_page_wizard_fails_before_input(self):
        io = WizardIO(self.ctx, pages=3)
        with self.assertRaisesRegex(RuntimeError, 'expected an Area page to choose Upper left'):
            self.invoke('wizard_place', io, game='Guard the Edge', area='Upper left', cap=.1, pulse_cap=.002)
        self.assertEqual(io.actions, [])

    def test_unrecognized_wizard_body_fails_without_input(self):
        io = WizardIO(self.ctx)
        io.menu['notice.body'] = 'An unfamiliar future wizard page'
        with self.assertRaisesRegex(RuntimeError, 'expected an Area, CPU, P1 or Preview page'):
            self.invoke('wizard_place', io, game='Guard the Edge', cap=.1, pulse_cap=.002)
        self.assertEqual(io.actions, [])

    def test_wizard_rejects_double_page_advance(self):
        io = WizardIO(self.ctx)
        send = io.send

        def double_advance(segment, until=None):
            send(segment, until=until)
            if segment == 'tap-a1':
                send(segment, until=until)

        io.send = double_advance
        with self.assertRaisesRegex(RuntimeError, 'expected wizard header Edge · 2/4; saw'):
            self.invoke('wizard_place', io, game='Guard the Edge', cap=.1, pulse_cap=.002)

    def test_unpublished_pulse_then_reached_target_records_uncertainty(self):
        io = WizardIO(self.ctx, step=2, fault='unpublished_once')
        io.cpu = [0.0, 1.0]
        io.refresh_notice()
        result = self.invoke('wizard_place', io, game='Guard the Edge', cpu=[1, 1], cap=.2, pulse_cap=.002)
        self.assertEqual(result['verified']['unconfirmed_pulses'], 1)
        self.assertFalse(result['verified']['pulses']['cpu'][0]['published'])
        self.assertEqual(result['verified']['cpu_shown'], ['1', '1'])

    def test_two_unpublished_pulses_then_coordinate_change(self):
        io = WizardIO(self.ctx, step=2)
        io.cpu = [0.0, 1.0]
        io.refresh_notice()
        send = io.send

        def delayed(segment, until=None):
            if segment.startswith('wizard-') and len([a for a in io.actions if a.startswith('wizard-')]) < 2:
                io.actions.append(segment)
                return
            send(segment, until=until)

        io.send = delayed
        result = self.invoke('wizard_place', io, game='Guard the Edge', cpu=[1, 1], cap=.2, pulse_cap=.002)
        self.assertEqual([x['published'] for x in result['verified']['pulses']['cpu']], [False, False, True])
        self.assertEqual(result['verified']['cpu_shown'], ['1', '1'])

    def test_three_unpublished_pulses_fail_and_clear_pending(self):
        io = WizardIO(self.ctx, step=2, fault='null')
        pending = []
        send, finish = io.send, io.finish

        def track(segment, until=None):
            pending[:] = [segment]
            send(segment, until=until)

        def clear():
            pending.clear()
            finish()

        io.send, io.finish = track, clear
        with self.assertRaisesRegex(RuntimeError, r"wizard CPU unreachable: expected .* shown .* after 3 unpublished 'wizard-right-fine' pulses"):
            self.invoke('wizard_place', io, game='Guard the Edge', cpu=[2, 1], cap=.2, pulse_cap=.002)
        self.assertEqual(len([a for a in io.actions if a.startswith('wizard-')]), 3)
        self.assertEqual(pending, [])

    def test_no_fenced_reply_for_two_pulses_retries_then_moves(self):
        io = WizardIO(self.ctx, step=2)
        io.cpu = [0.0, 1.0]
        io.refresh_notice()
        read, send = io.read, io.send

        def send_with_misses(segment, until=None):
            count = len([a for a in io.actions if a.startswith('wizard-')])
            if segment.startswith('wizard-') and count < 2:
                io.actions.append(segment)
            else:
                send(segment, until=until)

        def read_with_misses(provider, cap):
            count = len([a for a in io.actions if a.startswith('wizard-')])
            if provider == 'tr.menu' and count in (1, 2):
                raise RuntimeError('no fenced bridge reply for state tr.menu')
            return read(provider, cap)

        io.read, io.send = read_with_misses, send_with_misses
        result = self.invoke('wizard_place', io, game='Guard the Edge', cpu=[1, 1], cap=.2, pulse_cap=.002)
        self.assertEqual([x['published'] for x in result['verified']['pulses']['cpu']], [False, False, True])

    def test_wizard_requires_exact_shown_coordinates_and_rejects_tolerance(self):
        io = WizardIO(self.ctx, step=4)
        with self.assertRaisesRegex(RuntimeError, 'expected Preview coordinates'):
            self.invoke('wizard_place', io, game='Guard the Edge', cpu=[2, 1], p1=[0, 0], cap=.1, pulse_cap=.002)
        with self.assertRaisesRegex(TypeError, 'tolerance'):
            self.invoke('wizard_place', io, game='Guard the Edge', tolerance=1)
        self.assertEqual(io.actions, [])

    def test_analog_macros_are_indexed_full_state_events(self):
        base = Path(routes.ROUTES)
        entries = json.loads((base / 'index.json').read_text())['macros']
        for direction, axis, sign in (('left', 0, -1), ('right', 0, 1), ('up', 1, 1), ('down', 1, -1)):
            for size, tilt, held in (('coarse', 1, 10), ('medium', 1, 2), ('fine', .35, 1)):
                name = f'wizard-{direction}-{size}'
                entry = entries[name]
                data = (base / entry['file']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'])
                macro = json.loads(data)
                self.assertEqual([event['frame'] for event in macro['events']], [0, 1, held + 1])
                self.assertEqual(macro['events'][1]['sticks'][axis], sign * tilt)
                for event in macro['events']:
                    self.assertEqual(set(event), {'frame', 'buttons', 'sticks', 'triggers', 'motion'})
                    self.assertEqual(len(event['motion']), 2)

    def test_far_odd_even_and_area_reaches_preview(self):
        io = WizardIO(self.ctx)
        result = self.invoke('wizard_place', io, game='Guard the Edge', area='Upper right',
                             cpu=[32, 2], p1=[3, 0], cap=1, pulse_cap=.002)
        self.assertEqual(result['verified']['cpu_shown'], ['32', '2'])
        self.assertEqual(result['verified']['p1_shown'], ['3', '0'])
        self.assertTrue(any(x.endswith('coarse') for x in io.actions))
        self.assertTrue(any(x.endswith('medium') for x in io.actions))
        self.assertTrue(any(x.endswith('fine') for x in io.actions))
        self.assertEqual(io.step, 4)
        self.assertEqual(io.actions[-1], 'tap-a1')
        self.assertTrue((Path(self.ctx.row_dir) / 'routes-wizard-place.json').is_file())

    def test_null_coarse_and_fine_fail_after_required_pulses(self):
        for target, expected in (([40, 1], 3), ([2, 1], 3)):
            with self.subTest(target=target):
                io = WizardIO(self.ctx, step=2, fault='null')
                with self.assertRaisesRegex(RuntimeError, 'unreachable'):
                    self.invoke('wizard_place', io, game='Guard the Edge', cpu=target, cap=.1, pulse_cap=.002)
                self.assertEqual(len([a for a in io.actions if a.startswith('wizard-')]), expected)

    def test_header_epoch_and_cap_fail_closed(self):
        for fault, message in (('header_stuck', 'header|wizard'), ('epoch', 'connection')):
            with self.subTest(fault=fault):
                io = WizardIO(self.ctx, fault=fault)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.invoke('wizard_place', io, game='Guard the Edge', cap=.02, pulse_cap=.002)
        io = WizardIO(self.ctx, step=2, fault='null')
        with self.assertRaisesRegex(RuntimeError, 'unreachable|deadline'):
            self.invoke('wizard_place', io, game='Guard the Edge', cpu=[40, 1], cap=.001, pulse_cap=.002)

    def test_player_floor_ray_failure_reports_unadvanced_body(self):
        io = WizardIO(self.ctx, step=3, fault='header_stuck')
        with self.assertRaisesRegex(RuntimeError, r'P1 \(0, 0\)'):
            self.invoke('wizard_place', io, game='Guard the Edge', cap=.02, pulse_cap=.002)
        self.assertEqual(io.actions, ['tap-a1'])

    def test_minus_zero_is_distinct_and_other_axis_is_rechecked(self):
        io = WizardIO(self.ctx, step=2, fault='null')
        io.cpu = [0.0, 1.0]
        io.refresh_notice()
        with self.assertRaisesRegex(RuntimeError, 'unreachable'):
            self.invoke('wizard_place', io, game='Guard the Edge', cpu=['-0', 1], cap=.1, pulse_cap=.002)
        self.assertEqual(len([a for a in io.actions if a.startswith('wizard-')]), 3)
        io = WizardIO(self.ctx, step=2, fault='disturb')
        result = self.invoke('wizard_place', io, game='Guard the Edge',
                             cpu=[1, 2], cap=.5, pulse_cap=.002)
        self.assertEqual(result['verified']['cpu_shown'], ['1', '2'])
        self.assertTrue(any('left' in x for x in io.actions))

    def test_setup_row_opens_when_wizard_not_up(self):
        io = WizardIO(self.ctx)
        io.menu.update({'open': '1', 'notice.header': '', 'notice.body': '', 'game.running': '0', 'game': ''})
        io.last_menu = None
        result = self.invoke('wizard_place', io, game='Guard the Edge', cap=.5, pulse_cap=.002)
        self.assertEqual(result['verified']['preview_body'], io.menu['notice.body'])
        self.assertEqual(io.menu['row'], 'Set up…')


class SetIO(MenuIO):
    def __init__(self, ctx, kind, row_id, before, *, fault=None):
        super().__init__(ctx, page='cpu', fault=fault)
        self.kind = kind
        self.row_id = row_id
        self.stored = {row_id: list(before)}
        self.menu.update({'action': row_id, 'row_id': row_id, 'row': row_id,
                          'editor': '', 'editor.sel': '-1', 'editor.sel_key': ''})

    def send(self, segment, until=None):
        if segment == 'tr-menu-open' or segment == 'tap-b1' and self.menu['depth'] == '0':
            return super().send(segment, until=until)
        self.actions.append(segment)
        if self.ctx.runner.setup_metrics is not None:
            self.ctx.runner.setup_metrics['inputs'] += 1
        if self.fault != 'no_ack':
            self.menu['ack'] = str(int(self.menu['ack']) + 1)
        if segment == 'tap-a1' and self.menu['depth'] == '0':
            if self.kind == 'toggle':
                self.stored[self.row_id] = list(reversed(self.stored[self.row_id]))
            else:
                self.menu['depth'] = '1'
                selected = self.stored[self.row_id].index(1) if self.kind == 'list' else 0
                if self.row_id == 'eggsercise':
                    selected = max(0, selected - 1)
                self.menu['editor.sel'] = str(selected)
        elif segment in ('main-menu-down', 'main-menu-up'):
            delta = 1 if segment.endswith('down') else -1
            count = len(self.stored[self.row_id]) - int(self.row_id in ('eggsercise', 'hitbox_vis'))
            cursor = (int(self.menu['editor.sel']) + delta) % count
            self.menu['editor.sel'] = str(cursor)
        elif segment == 'tap-a1':
            cursor = int(self.menu['editor.sel'])
            if self.kind == 'list':
                if self.row_id == 'eggsercise':
                    cursor += 1
                self.stored[self.row_id] = [int(index == cursor) for index in range(len(self.stored[self.row_id]))]
                self.menu['depth'] = '0'
                self.menu['editor.sel'] = '-1'
            else:
                if self.row_id == 'hitbox_vis' and cursor >= 3:
                    cursor += 1
                self.stored[self.row_id][cursor] ^= 1
        elif segment == 'tap-b1':
            self.menu['depth'] = '0'
            self.menu['editor.sel'] = '-1'
        else:
            raise AssertionError(segment)


class PracticeSetIO(RowsIO):
    def __init__(self, ctx, *, fault=None):
        super().__init__(ctx, fault=fault)
        self.menu.update({'open': '0', 'editor': '', 'editor.sel': '-1', 'editor.sel_key': ''})
        self.stored = {'edge_difficulty': [1, 0, 0], 'edge_cpu_fighter': [1] + [0] * len(routes.tables()['roster_catalog']),
                       'edge_cpu_level': [0] * 8 + [1]}
        self.editor = False
        self.popup = False
        self.cell = 0
        self.level = 9

    def send(self, segment, until=None):
        if self.menu['open'] == '0' or not self.editor and not (
                segment == 'tap-a1' and self.menu['depth'] == '1' and self.menu['action'] == 'edge_difficulty'):
            result = super().send(segment, until=until)
            if segment == 'tap-b1' and self.menu['open'] == '1':
                self.update_focus()
            return result
        self.actions.append(segment)
        if self.ctx.runner.setup_metrics is not None:
            self.ctx.runner.setup_metrics['inputs'] += 1
        if self.fault != 'no_ack':
            self.menu['ack'] = str(int(self.menu['ack']) + 1)
        if not self.editor:
            self.editor = True
            self.menu.update({'depth': '2', 'group': '-1', 'editor': 'PracticeCpu(GuardEdge)',
                              'editor.sel': '0', 'editor.sel_key': ''})
        elif not self.popup and segment.startswith('main-menu-'):
            self.cell = routes.tr_grid_step(self.cell, segment.removeprefix('main-menu-'))
            self.menu['editor.sel'] = str(self.cell)
        elif not self.popup and segment == 'tap-a1':
            self.popup = True
            self.stored['edge_cpu_fighter'] = [0, 1] + [0] * (len(routes.tables()['roster_catalog']) - 1)
            self.menu.update({'editor.sel': '0', 'editor.sel_key': f'CPU {self.level}'})
        elif self.popup and segment == 'main-menu-down':
            choice = (int(self.menu['editor.sel']) + 1) % 2
            self.stored['edge_difficulty'] = [int(i == choice) for i in range(3)]
            self.menu.update({'editor.sel': str(choice), 'editor.sel_key': 'Perfect' if choice else f'CPU {self.level}'})
        elif self.popup and segment == 'tap-x1':
            self.level = self.level % 9 + 1
            self.stored['edge_cpu_level'] = [int(i == self.level - 1) for i in range(9)]
            self.menu['editor.sel_key'] = f'CPU {self.level}'
        elif self.popup and segment == 'tap-b1':
            self.popup = False
            self.menu.update({'editor.sel': str(self.cell), 'editor.sel_key': ''})
        elif not self.popup and segment == 'tap-b1':
            self.editor = False
            self.menu.update({'depth': '1', 'editor': '', 'editor.sel': '-1', 'editor.sel_key': ''})
            self.update_focus()
        else:
            raise AssertionError(segment)


class SetTests(OpenCloseTests):
    def run_set(self, io, kind, value, *, missing_writer=False, final=None, path=None):
        reads = []

        def fetch(ctx, cap=20):
            data = final if reads and final is not None else io.stored
            reads.append(dict(data))
            return json.dumps({'menu': data}).encode()

        def writer(ctx, cursor, until):
            if missing_writer:
                raise RuntimeError('settings writer missing')
            return '[TrainingRooms] settings.json written by the writer thread'

        with (patch.object(routes, 'BridgePickerIO', return_value=io),
              patch.object(routes, '_fetch_settings', side_effect=fetch),
              patch.object(routes, '_wait_writer', side_effect=writer) as waited,
              patch.object(routes, 'instance_path', return_value=Path(self.ctx.row_dir) / 'bridge.log'),
              patch.object(routes, 'LogCursor', return_value=object()) as cursor):
            result = routes.tr_set(self.ctx, page=io.menu['page'], path=path or [io.menu['row_id']],
                                   kind=kind, value=value, cap=1)
        json.dumps(result)
        return result, waited.call_count, cursor.call_count, reads

    def training_ui_reads(self, io, inactive, *, malformed=False):
        events = []
        read, send = io.read, io.send
        self.ctx.runner.setup_metrics = None
        io.rev = 568

        def observed(provider, cap):
            if provider == 'ssbu.ui' and inactive[0]:
                io.rev = max(io.rev, 575)
            deadline = self.clock.monotonic() + cap
            sample = read(provider, cap)
            if provider == 'ssbu.ui' and inactive[0]:
                inactive[0] -= 1
                sample.values.update({key: 'unknown' for key in routes.native_picker.UI_FOCUS_FIELDS})
                sample.values.update({'observation.active': 'no', 'ack': '0', 'ack.result': 'unsupported'})
                if malformed:
                    sample.block['schema'] = 'ssbu.ui/2'
                self.clock.spend(min(max(0, deadline - self.clock.monotonic()), .1))
            events.append(('read', provider, sample.block['rev'], sample.values.get('observation.active')))
            return sample

        def sent(segment, until=None):
            events.append(('send', segment))
            return send(segment, until=until)

        io.read, io.send = observed, sent
        return events

    def test_tr_set_waits_through_an_inactive_ui_read(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
        inactive = [0]
        events = self.training_ui_reads(io, inactive)
        io.read('ssbu.ui', 1)
        io.read('tr.menu', 1)
        inactive[0] = 1
        try:
            result, _, _, _ = self.run_set(io, 'toggle', [0, 1])
        except RuntimeError as error:
            self.fail(f'tr_set rejected a transient inactive read: {error}')
        self.assertTrue(result['ok'])
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-tr-set.json').read_text())
        self.assertTrue(receipt['ok'])
        self.assertEqual(events[:5], [('read', 'ssbu.ui', 569, 'yes'),
                                      ('read', 'tr.menu', 570, '1'),
                                      ('read', 'tr.menu', 571, '1'),
                                      ('read', 'tr.menu', 572, '1'),
                                      ('read', 'ssbu.ui', 576, 'no')])
        active = next(index for index, event in enumerate(events[5:], 5)
                      if event[0:2] == ('read', 'ssbu.ui') and event[3] == 'yes')
        self.assertFalse(any(event[0] == 'send' for event in events[4:active]))

    def test_tr_set_fails_closed_when_ui_stays_inactive(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
        events = self.training_ui_reads(io, [100])
        start = self.clock.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'no active ssbu.ui observation.*cap'):
            self.run_set(io, 'toggle', [0, 1])
        self.assertAlmostEqual(self.clock.monotonic() - start, 1)
        self.assertEqual(io.actions, [])
        self.assertFalse(any(event[0] == 'send' for event in events))
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-tr-set.json').read_text())
        self.assertFalse(receipt['ok'])

    def test_an_active_ui_read_costs_no_extra_request(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
        events = self.training_ui_reads(io, [0])
        result, _, _, _ = self.run_set(io, 'toggle', [0, 1])
        self.assertTrue(result['ok'])
        self.assertEqual(sum(event[0] == 'read' for event in events), 13)
        self.assertEqual(result['bridge_requests'], 13)

    def test_tr_set_rejects_a_malformed_inactive_ui_read_immediately(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
        events = self.training_ui_reads(io, [1], malformed=True)
        with self.assertRaisesRegex(RuntimeError, 'incomplete, inactive or wrong-schema observation'):
            self.run_set(io, 'toggle', [0, 1])
        self.assertEqual(io.actions, [])
        self.assertEqual(sum(event[0:2] == ('read', 'ssbu.ui') for event in events), 1)

    def test_tr_idle_waits_through_inactive_ui_before_closing(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
        io.menu.update({'open': '1', 'game.running': '0', 'notice.header': ''})
        events = self.training_ui_reads(io, [1])
        result = self.invoke('tr_idle', io, cap=1)
        self.assertTrue(result['ok'])
        active = next(index for index, event in enumerate(events)
                      if event[0:2] == ('read', 'ssbu.ui') and event[3] == 'yes')
        self.assertFalse(any(event[0] == 'send' for event in events[:active]))
        self.assertIn('tap-b1', io.actions)

    def test_tr_idle_fails_closed_without_an_active_ui(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
        io.menu.update({'open': '1', 'game.running': '0', 'notice.header': ''})
        self.training_ui_reads(io, [100])
        with self.assertRaisesRegex(RuntimeError, 'no active ssbu.ui observation.*cap'):
            self.invoke('tr_idle', io, cap=1)
        self.assertEqual(io.actions, [])

    def test_training_routes_recheck_keyboard_after_inactive_ui(self):
        for route in ('tr_idle', 'tr_close'):
            with self.subTest(route=route):
                io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
                io.menu.update({'game.running': '0', 'notice.header': ''})
                self.training_ui_reads(io, [1])
                read = io.read

                def keyboard_opens(provider, cap):
                    state = read(provider, cap)
                    if provider == 'ssbu.ui' and state.values['observation.active'] == 'no':
                        io.menu['keyboard'] = '1'
                    return state

                io.read = keyboard_opens
                with self.assertRaisesRegex(RuntimeError, 'keyboard'):
                    self.invoke(route, io, cap=1)
                self.assertEqual(io.actions, [])

    def test_last_ui_read_cannot_qualify_after_cap_or_hide_last_inactive(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
                io.menu.update({'game.running': '0', 'notice.header': ''})
                self.training_ui_reads(io, [1])
                read = io.read

                def late_reply(provider, cap):
                    if provider == 'ssbu.ui' and io.rev >= 576:
                        self.clock.spend(cap)
                        if missing:
                            raise RuntimeError('no fenced bridge reply for state ssbu.ui')
                    return read(provider, cap)

                io.read = late_reply
                with self.assertRaisesRegex(RuntimeError, 'no active ssbu.ui observation.*cap.*rev=576'):
                    self.invoke('tr_idle', io, cap=1)
                self.assertEqual(io.actions, [])

    def test_tr_settings_waits_through_inactive_ui_before_applying(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
        events = self.training_ui_reads(io, [1])
        settings = json.dumps({'menu': io.stored}).encode()
        with (patch.object(routes, 'BridgePickerIO', return_value=io),
              patch.object(routes, '_fetch_settings', return_value=settings),
              patch.object(routes, '_apply_settings', return_value=('ok', 'ok')) as apply,
              patch.object(routes, '_save', side_effect=lambda ctx, name, value: value)):
            result = routes.tr_settings(self.ctx, values={'lra_reset': [1, 0]}, cap=1)
        self.assertEqual(result['verified']['values'], {'lra_reset': [1, 0]})
        self.assertEqual([event[3] for event in events if event[0:2] == ('read', 'ssbu.ui')], ['no', 'yes'])
        apply.assert_called_once()

    def test_tr_settings_fails_closed_without_an_active_ui(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
        self.training_ui_reads(io, [100])
        with (patch.object(routes, 'BridgePickerIO', return_value=io),
              patch.object(routes, '_fetch_settings') as fetch,
              patch.object(routes, '_apply_settings') as apply):
            with self.assertRaisesRegex(RuntimeError, 'no active ssbu.ui observation.*cap'):
                routes.tr_settings(self.ctx, values={'lra_reset': [1, 0]}, cap=1)
        fetch.assert_not_called()
        apply.assert_not_called()

    def test_oversized_sdi_settings_cannot_certify_product_index(self):
        io = SetIO(self.ctx, 'list', 'sdi_strength', [0] * 8 + [1])
        data = json.dumps({'menu': io.stored}).encode()
        with (patch.object(routes, 'BridgePickerIO', return_value=io),
              patch.object(routes, '_fetch_settings', return_value=data),
              patch.object(routes, 'LogCursor') as cursor):
            with self.assertRaisesRegex(RuntimeError, r'unsupported: settings file menu.sdi_strength has 9 slots; product schema has 4'):
                routes.tr_set(self.ctx, page='cpu', path=['sdi_strength'], kind='list', value=8, cap=.2)
            cursor.assert_not_called()
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-tr-set.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)

    def test_product_width_list_index_three_is_accepted(self):
        io = SetIO(self.ctx, 'list', 'sdi_strength', [0, 0, 0, 1])
        result, writers, _, _ = self.run_set(io, 'list', 3)
        self.assertEqual(result['verified']['width'], 4)
        self.assertEqual(result['verified']['value_stored'], [0, 0, 0, 1])
        self.assertEqual(writers, 0)

    def test_product_width_rejects_bad_list_index_and_array_length(self):
        io = SetIO(self.ctx, 'list', 'sdi_strength', [0, 0, 0, 1])
        with self.assertRaisesRegex(RuntimeError, 'unsupported list index 8 for menu.sdi_strength: product schema has 4 options'):
            self.run_set(io, 'list', 8)
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [1, 0])
        with self.assertRaisesRegex(RuntimeError, r'expected settings menu.lra_reset array of length 2'):
            self.run_set(io, 'toggle', [1, 0, 0])

    def test_product_widths_cover_every_menu_setting_kind(self):
        kinds = json.loads((Path(routes.ROUTES) / 'tr-menu-kinds.json').read_text())
        widths = json.loads((Path(routes.ROUTES) / 'tr-setting-widths.json').read_text())['widths']
        for kind in ('toggle', 'list', 'checklist'):
            self.assertFalse(set(kinds[kind]) - set(widths))
        self.assertTrue(all(widths[row] == 2 for row in kinds['toggle']))

    def test_already_stored_skips_change_and_writer_and_rereads(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [1, 0])
        result, writers, cursors, reads = self.run_set(io, 'toggle', [1, 0])
        self.assertEqual(result['skipped'], ['change', 'writer'])
        self.assertEqual((writers, cursors, len(reads)), (0, 0, 2))
        self.assertEqual(io.actions, ['tr-menu-open', 'tap-b1'])
        self.assertEqual(result['verified']['before'], result['verified']['after'])

    def test_tr_set_uses_live_set_not_stale_file_for_already_equal(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
        log = Path(self.ctx.row_dir) / 'bridge.log'
        line = '[Bridge] req set result=ok frame=2899 wall=383391489'
        log.write_text(line + '\n')
        (log.parent / 'live-settings.json').write_text(json.dumps({
            'set_reply': line, 'reload': '[TrainingRooms] diag_settings_reload rerolls=0x0 issues=0',
            'settings': {'menu': {'lra_reset': [0, 1]}}}))
        reads = []

        def fetch(ctx, cap=20):
            reads.append(True)
            return json.dumps({'menu': {'lra_reset': [1, 0]} if len(reads) == 1 else io.stored}).encode()

        with (patch.object(routes, 'BridgePickerIO', return_value=io),
              patch.object(routes, '_fetch_settings', side_effect=fetch),
              patch.object(routes, '_wait_writer', return_value='writer'),
              patch.object(routes, 'instance_path', return_value=log)):
            result = routes.tr_set(self.ctx, page='cpu', path=['lra_reset'], kind='toggle', value='on', cap=1)
        self.assertIn('tap-a1', io.actions)
        self.assertNotIn('change', result['skipped'])
        self.assertEqual(result['verified']['before'], [0, 1])
        self.assertEqual(result['before_source'], 'live-set')

    def test_malformed_boolean_and_float_settings_are_not_certified_off(self):
        for values in ([False, True], [0.0, 1.0]):
            with self.subTest(values=values):
                io = SetIO(self.ctx, 'toggle', 'lra_reset', values)
                with self.assertRaisesRegex(RuntimeError, r'expected a 0/1 integer array for menu.lra_reset'):
                    self.run_set(io, 'toggle', [0, 1])

    def test_duplicate_settings_key_is_not_certified_off(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
        raw = b'{"menu":{"lra_reset":[1,0],"lra_reset":[0,1]}}'
        with (patch.object(routes, 'BridgePickerIO', return_value=io),
              patch.object(routes, '_fetch_settings', return_value=raw)):
            with self.assertRaisesRegex(RuntimeError, 'duplicate'):
                routes.tr_set(self.ctx, page='cpu', path=['lra_reset'], kind='toggle', value=[0, 1], cap=.1)
        self.assertNotIn('tap-a1', io.actions)

    def test_malformed_final_settings_are_not_certified_off(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1])
        with self.assertRaisesRegex(RuntimeError, r'expected a 0/1 integer array for menu.lra_reset'):
            self.run_set(io, 'toggle', [0, 1], final={'lra_reset': [False, True]})
        receipt = json.loads((Path(self.ctx.row_dir) / 'routes-tr-set.json').read_text())
        self.assertFalse(receipt['ok'])
        self.assertNotIn('verified', receipt)

    def test_friendly_toggle_and_list_values_preserve_original_arguments(self):
        for value, target in (('On', [1, 0]), ('OFF', [0, 1]), (True, [1, 0]), (False, [0, 1])):
            with self.subTest(value=value):
                io = SetIO(self.ctx, 'toggle', 'lra_reset', target)
                result, _, _, _ = self.run_set(io, 'toggle', value)
                self.assertEqual(result['args']['value'], value)
                self.assertEqual(result['verified']['value_stored'], target)
        io = SetIO(self.ctx, 'list', 'sdi_strength', [0, 0, 1, 0])
        result, _, _, _ = self.run_set(io, 'list', 2)
        self.assertEqual(result['args']['value'], 2)
        self.assertEqual(result['verified']['value_stored'], [0, 0, 1, 0])

    def test_changed_toggle_list_and_checklist_require_writer(self):
        for kind, row, before, target in (('toggle', 'lra_reset', [0, 1], [1, 0]),
                                           ('list', 'sdi_strength', [1, 0, 0, 0], [0, 0, 1, 0]),
                                           ('checklist', 'mash_state', [1, 0, 0] + [0] * 27,
                                            [0, 1, 1] + [0] * 27)):
            with self.subTest(kind=kind):
                io = SetIO(self.ctx, kind, row, before)
                result, writers, cursors, reads = self.run_set(io, kind, target)
                self.assertEqual((writers, cursors, len(reads)), (1, 1, 2))
                self.assertEqual(result['verified']['after'], target)
                io = SetIO(self.ctx, kind, row, before)
                with self.assertRaisesRegex(RuntimeError, 'writer'):
                    self.run_set(io, kind, target, missing_writer=True)

    def test_readback_change_and_wrong_kind_fail(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [1, 0])
        with self.assertRaisesRegex(RuntimeError, 'read.back|settings'):
            self.run_set(io, 'toggle', [1, 0], final={'lra_reset': [0, 1]})
        for kind in ('list', 'checklist'):
            io = SetIO(self.ctx, 'toggle', 'lra_reset', [1, 0])
            with self.assertRaisesRegex(RuntimeError, 'kind'):
                self.run_set(io, kind, [1, 0])

    def test_ack_and_epoch_failure_do_not_pass(self):
        for fault, message in (('no_ack', 'not handled'), ('epoch', 'connection')):
            with self.subTest(fault=fault):
                io = SetIO(self.ctx, 'toggle', 'lra_reset', [0, 1], fault=fault)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.run_set(io, 'toggle', [1, 0])

    def test_practice_cpu_fighter_mode_and_level_are_read_back(self):
        io = PracticeSetIO(self.ctx)
        result, writers, _, _ = self.run_set(io, 'practice_cpu',
                                             {'fighter': 'Mario', 'mode': 'Perfect', 'level': 9},
                                             path=['game.6', 'edge_difficulty'])
        self.assertEqual(writers, 1)
        self.assertEqual(result['verified']['after']['edge_difficulty'], [0, 1, 0])
        self.assertEqual(result['verified']['after']['edge_cpu_fighter'][1], 1)
        self.assertEqual(result['verified']['mode'], 'Perfect')

    def test_practice_cpu_changes_level_and_no_op_skips_writer(self):
        io = PracticeSetIO(self.ctx)
        value = {'fighter': 'Mario', 'mode': 'Perfect', 'level': 2}
        result, writers, _, _ = self.run_set(io, 'practice_cpu', value, path=['game.6', 'edge_difficulty'])
        self.assertEqual(writers, 1)
        self.assertEqual(io.actions.count('tap-x1'), 2)
        self.assertEqual(result['verified']['after']['edge_cpu_level'], [0, 1, 0, 0, 0, 0, 0, 0, 0])
        io = PracticeSetIO(self.ctx)
        io.stored.update(result['verified']['after'])
        result, writers, cursors, _ = self.run_set(io, 'practice_cpu', value, path=['game.6', 'edge_difficulty'])
        self.assertEqual((writers, cursors), (0, 0))
        self.assertEqual(result['skipped'], ['change', 'writer'])

    def test_source_retired_option_and_hitbox_slots_are_mapped(self):
        io = SetIO(self.ctx, 'list', 'eggsercise', [0, 1, 0, 0, 0])
        result, _, _, _ = self.run_set(io, 'list', [0, 0, 1, 0, 0])
        self.assertEqual(result['verified']['after'], [0, 0, 1, 0, 0])
        io = SetIO(self.ctx, 'checklist', 'hitbox_vis', [0, 0, 0, 0, 0, 0, 0])
        result, _, _, _ = self.run_set(io, 'checklist', [0, 0, 0, 0, 1, 0, 0])
        self.assertEqual(result['verified']['after'], [0, 0, 0, 0, 1, 0, 0])
        io = SetIO(self.ctx, 'checklist', 'hitbox_vis', [0, 0, 0, 0, 0, 0, 0])
        with self.assertRaisesRegex(RuntimeError, 'retired'):
            self.run_set(io, 'checklist', [0, 0, 0, 1, 0, 0, 0])


if __name__ == '__main__':
    unittest.main()
