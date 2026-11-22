"""Host-only setup block probes with modelled bridge observations."""

import hashlib
import json
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
        if self.fault == 'owner' or self.fault == 'owner_after_input' and self.actions:
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


class SetIO(MenuIO):
    def __init__(self, ctx, kind, row_id, before, *, fault=None):
        super().__init__(ctx, page='cpu', fault=fault)
        self.kind = kind
        self.row_id = row_id
        self.stored = {row_id: list(before)}
        self.menu.update({'action': row_id, 'row_id': row_id, 'row': row_id,
                          'editor': '', 'editor.sel': '-1', 'editor.sel_key': ''})

    def send(self, segment):
        if segment == 'tr-menu-open' or segment == 'tap-b1' and self.menu['depth'] == '0':
            return super().send(segment)
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

    def send(self, segment):
        if self.menu['open'] == '0' or not self.editor and not (
                segment == 'tap-a1' and self.menu['depth'] == '1' and self.menu['action'] == 'edge_difficulty'):
            result = super().send(segment)
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
        return result, waited.call_count, cursor.call_count, reads

    def test_already_stored_skips_change_and_writer_and_rereads(self):
        io = SetIO(self.ctx, 'toggle', 'lra_reset', [1, 0])
        result, writers, cursors, reads = self.run_set(io, 'toggle', [1, 0])
        self.assertEqual(result['skipped'], ['change', 'writer'])
        self.assertEqual((writers, cursors, len(reads)), (0, 0, 2))
        self.assertEqual(io.actions, ['tr-menu-open', 'tap-b1'])
        self.assertEqual(result['verified']['before'], result['verified']['after'])

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

    def test_friendly_toggle_and_list_values_preserve_original_arguments(self):
        for value, target in (('On', [1, 0]), ('OFF', [0, 1]), (True, [1, 0]), (False, [0, 1])):
            with self.subTest(value=value):
                io = SetIO(self.ctx, 'toggle', 'lra_reset', target)
                result, _, _, _ = self.run_set(io, 'toggle', value)
                self.assertEqual(result['args']['value'], value)
                self.assertEqual(result['verified']['value_stored'], target)
        io = SetIO(self.ctx, 'list', 'sdi_strength', [0, 0, 1])
        result, _, _, _ = self.run_set(io, 'list', 2)
        self.assertEqual(result['args']['value'], 2)
        self.assertEqual(result['verified']['value_stored'], [0, 0, 1])

    def test_changed_toggle_list_and_checklist_require_writer(self):
        for kind, row, before, target in (('toggle', 'lra_reset', [0, 1], [1, 0]),
                                           ('list', 'sdi_strength', [1, 0, 0], [0, 0, 1]),
                                           ('checklist', 'mash_state', [1, 0, 0], [0, 1, 1])):
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
