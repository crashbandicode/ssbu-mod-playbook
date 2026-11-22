"""Host-only setup block probes with modelled bridge observations."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import routes
import test_native_picker as recorded


PAGES = ('cpu', 'saves', 'recording', 'fighters_stages', 'display_controls', 'developer')


class MenuIO:
    def __init__(self, ctx, *, opened=False, page='cpu', depth=0, fault=None):
        self.ctx = ctx
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

    def read(self, provider, cap):
        self.rev += 1
        metrics = self.ctx.runner.setup_metrics
        if metrics is not None:
            metrics['bridge_requests'] += 1
            metrics['bridge_boot'] = self.boot
            metrics['revisions'].setdefault(provider, [self.rev, self.rev])[1] = self.rev
        epoch = 2 if self.fault == 'epoch' and self.actions else 1
        if provider == 'tr.menu':
            return recorded.snapshot(provider, self.rev, dict(self.menu), epoch=epoch)
        owner = 'tr.menu' if self.menu['open'] == '1' else 'native'
        if self.fault == 'owner':
            owner = 'unknown'
        return recorded.native(self.rev, epoch=epoch, screen='training', focus='training',
                               **{'screen.context': 'training', 'input.owner': owner})

    def send(self, segment):
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
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ctx = SimpleNamespace(runner=SimpleNamespace(), row_dir=directory.name, on='s',
                                   check_stop=lambda: None)

    def invoke(self, name, io, **kwargs):
        with patch.object(routes, 'BridgePickerIO', return_value=io):
            return getattr(routes, name)(self.ctx, **kwargs)

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

    def send(self, segment):
        if segment not in ('main-menu-down', 'tap-a1'):
            return super().send(segment)
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

        def send(segment):
            if segment == 'main-menu-down' and io.menu['depth'] != '0':
                io.fault = 'stuck'
            original(segment)

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

    def send(self, segment):
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
            super().send(segment)
            if segment == 'tap-b1':
                self.update_focus()


class StopTests(OpenCloseTests):
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
    def __init__(self, ctx, *, step=1, fault=None):
        super().__init__(ctx, fault=fault)
        self.step = step
        self.area_index = 3
        self.areas = ('Lower left', 'Lower right', 'Upper left', 'Upper right')
        self.cpu = [1.0, 1.0]
        self.p1 = [0.0, 0.0]
        self.menu.update({'open': '0', 'game': 'Guard the Edge', 'game.running': '1'})
        self.last_menu = None
        self.menu_rev = 0
        self.refresh_notice()

    def refresh_notice(self):
        self.menu['notice.header'] = f'Edge · {self.step}/4'
        cpu = ', '.join(format(v, '.0f') for v in self.cpu)
        p1 = ', '.join(format(v, '.0f') for v in self.p1)
        self.menu['notice.body'] = (
            f'{self.areas[self.area_index]} ({cpu}) · D-pad choose · A next' if self.step == 1 else
            f'CPU ({cpu}) · Stick move · A next' if self.step == 2 else
            f'P1 ({p1}) · Stick move · A next' if self.step == 3 else
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
        return recorded.snapshot(provider, self.menu_rev, dict(self.menu), epoch=epoch)

    def send(self, segment):
        if self.menu['open'] == '1':
            if segment == 'tap-a1' and self.menu['depth'] == '1' and self.menu['row'] == 'Set up…':
                self.actions.append(segment)
                self.ctx.runner.setup_metrics['inputs'] += 1
                self.menu['ack'] = str(int(self.menu['ack']) + 1)
                self.menu['open'] = '0'
                self.step = 1
                self.refresh_notice()
                return
            return super().send(segment)
        self.actions.append(segment)
        if self.ctx.runner.setup_metrics is not None:
            self.ctx.runner.setup_metrics['inputs'] += 1
        if segment == 'tap-a1':
            if self.fault != 'header_stuck' and self.step < 4:
                self.step += 1
        elif segment == 'main-menu-right':
            self.area_index = (self.area_index + 1) % 4
        elif segment.startswith('wizard-'):
            _, direction, size = segment.split('-')
            if self.fault != 'null':
                amount = {'coarse': 20.0, 'medium': 4.0, 'fine': .7}[size]
                axis = 0 if direction in ('left', 'right') else 1
                sign = -1 if direction in ('left', 'down') else 1
                point = self.cpu if self.step == 2 else self.p1
                point[axis] += sign * amount
                if self.fault == 'disturb' and axis == 1:
                    point[0] += 1
        else:
            raise AssertionError(segment)
        self.refresh_notice()


class WizardTests(OpenCloseTests):
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
        for target, expected in (([40, 1], 2), ([2, 1], 4)):
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

    def test_minus_zero_is_distinct_and_other_axis_is_rechecked(self):
        io = WizardIO(self.ctx, step=2, fault='null')
        io.cpu = [0.0, 1.0]
        io.refresh_notice()
        with self.assertRaisesRegex(RuntimeError, 'unreachable'):
            self.invoke('wizard_place', io, game='Guard the Edge', cpu=['-0', 1], cap=.1, pulse_cap=.002)
        self.assertEqual(len([a for a in io.actions if a.startswith('wizard-')]), 4)
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


if __name__ == '__main__':
    unittest.main()
