"""routes:tr_menu_in_room: the TrainingRooms menu driven inside the local-wireless waiting room.

Host-only, with the modelled bridge observations of test_setup_blocks. In the room the route has `tr.menu` alone
(`scene=lobby`): the dashboard reader does not observe there (TrainingRooms rc.16 smoke, intnext-a24cdbef-yoga-1,
2026-11-28: `ssbu.ui` read `screen=unknown`, `observation.active=no` after hosting), so every input is fenced by one
handled ACK of `tr.menu` and nothing else is read.
"""

import hashlib
import json
import unittest
from pathlib import Path

import routes
import test_setup_blocks as blocks


# The room's taps, and the Training tap each one stands for in the model.
ROOM_SEGMENTS = {'room-tap-a': 'tap-a1', 'room-tap-b': 'tap-b1', 'room-tap-r': 'tap-r1',
                 'room-tap-down': 'main-menu-down'}


def room(base):
    """A waiting-room model: `scene=lobby`, the room's own tap segments, and no dashboard to read."""
    class RoomIO(base):
        def __init__(self, ctx, **kwargs):
            super().__init__(ctx, **kwargs)
            self.menu['scene'] = 'lobby'

        def read(self, provider, cap):
            if provider != 'tr.menu':
                raise AssertionError(f'the room route read {provider}')
            return super().read(provider, cap)

        def send(self, segment, until=None):
            if segment in ('tap-a1', 'tap-b1', 'tap-r1', 'main-menu-down'):
                raise AssertionError(f'the room route sent the Training segment {segment}')
            super().send(ROOM_SEGMENTS.get(segment, segment), until=until)
            self.actions[-1] = segment

    return RoomIO


class RoomMenuTests(unittest.TestCase):
    # The fixture of the Training block tests, without running their cases a second time.
    setUp = blocks.OpenCloseTests.setUp
    invoke = blocks.OpenCloseTests.invoke

    def test_opens_the_menu_and_reaches_a_page_on_tr_menu_alone(self):
        io = room(blocks.MenuIO)(self.ctx)
        result = self.invoke('tr_menu_in_room', io, page='recording', cap=1)
        self.assertEqual(io.actions, ['tr-menu-open', 'room-tap-r', 'room-tap-r'])
        self.assertEqual(result['block'], 'tr_menu_in_room')
        self.assertEqual({key: result['verified'][key] for key in ('scene', 'open', 'page', 'depth')},
                         {'scene': 'lobby', 'open': '1', 'page': 'recording', 'depth': 0})
        self.assertEqual(result['inputs'], 3)
        self.assertTrue((Path(self.ctx.row_dir) / 'routes-tr-menu-in-room.json').is_file())

    def test_walks_to_the_target_row_without_activating_it(self):
        io = room(blocks.RowsIO)(self.ctx)
        result = self.invoke('tr_menu_in_room', io, page='fighters_stages', path=['game.6', 'edge_difficulty'], cap=1)
        self.assertEqual(io.actions, ['room-tap-down', 'room-tap-a', 'room-tap-down'])
        self.assertEqual({key: result['verified'][key] for key in ('scene', 'page', 'depth', 'action', 'row')},
                         {'scene': 'lobby', 'page': 'fighters_stages', 'depth': 1, 'action': 'edge_difficulty',
                          'row': 'CPU fighter and mode'})

    def test_press_activates_the_target_with_one_handled_ack(self):
        io = room(blocks.RowsIO)(self.ctx)
        result = self.invoke('tr_menu_in_room', io, page='fighters_stages', path=['game.6', 'start.6'], then='press',
                             cap=1)
        self.assertEqual(io.actions, ['room-tap-down', 'room-tap-a', 'room-tap-a'])
        self.assertEqual(result['verified']['pressed'], {'action': 'start.6', 'row': 'Start'})
        self.assertEqual(result['verified']['ack'], 3)
        self.assertEqual(result['verified']['scene'], 'lobby')

    def test_close_backs_out_and_closes(self):
        io = room(blocks.MenuIO)(self.ctx, opened=True, depth=2)
        result = self.invoke('tr_menu_in_room', io, then='close', cap=1)
        self.assertEqual(io.actions, ['room-tap-b', 'room-tap-b', 'room-tap-b'])
        self.assertEqual(result['verified']['open'], '0')
        closed = room(blocks.MenuIO)(self.ctx)
        self.assertEqual(self.invoke('tr_menu_in_room', closed, then='close', cap=1)['skipped'], ['close'])
        self.assertEqual(closed.actions, [])

    def test_outside_the_room_nothing_is_sent(self):
        for change, wanted in (({'scene': 'training'}, 'waiting room'), ({'keyboard': '1'}, 'keyboard')):
            with self.subTest(change=change):
                io = room(blocks.MenuIO)(self.ctx)
                io.menu.update(change)
                with self.assertRaisesRegex(RuntimeError, wanted):
                    self.invoke('tr_menu_in_room', io, page='saves', cap=1)
                self.assertEqual(io.actions, [])

    def test_arguments_are_refused_before_any_read(self):
        for kwargs in ({'page': 'unknown'}, {'path': ['game.6']}, {'page': 'saves', 'then': 'jump'},
                       {'page': 'saves', 'path': []}, {'page': 'saves', 'then': 'press'}, {'page': 'saves', 'cap': 0}):
            with self.subTest(kwargs=kwargs):
                io = room(blocks.MenuIO)(self.ctx)
                with self.assertRaises(RuntimeError):
                    self.invoke('tr_menu_in_room', io, **kwargs)
                self.assertEqual((io.actions, io.rev), ([], 0))

    def test_an_input_the_menu_did_not_handle_stops_the_route(self):
        for fault in ('ignored', 'no_ack', 'page_stuck'):
            with self.subTest(fault=fault):
                io = room(blocks.MenuIO)(self.ctx, opened=True, fault=fault)
                with self.assertRaises(RuntimeError):
                    self.invoke('tr_menu_in_room', io, page='saves', cap=.05)

    def test_the_training_routes_still_refuse_the_room(self):
        io = blocks.MenuIO(self.ctx)
        io.menu['scene'] = 'lobby'
        with self.assertRaisesRegex(RuntimeError, 'Training'):
            self.invoke('tr_open', io, page='saves', cap=1)
        self.assertEqual(io.actions, [])


class RoomSegmentTests(unittest.TestCase):
    def test_the_room_taps_are_library_macros_with_the_flown_bytes(self):
        index = routes.routes_index()['macros']
        for name, button in (('room-tap-a', 1), ('room-tap-b', 2), ('room-tap-r', 128), ('room-tap-down', 32768)):
            with self.subTest(name=name):
                path, sha, _ = routes.macro_file(name)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha)
                macro = json.loads(path.read_text())
                self.assertEqual(macro['duration_frames'], 19)
                self.assertEqual({event['buttons'] for event in macro['events']}, {0, button})
                self.assertEqual(index[name]['frames'], 19)
                self.assertIn('intnext-a24cdbef-yoga-1', index[name]['source'])


if __name__ == '__main__':
    unittest.main()
