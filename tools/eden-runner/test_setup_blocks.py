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


if __name__ == '__main__':
    unittest.main()
