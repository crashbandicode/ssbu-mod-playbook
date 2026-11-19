"""Checked CSS/SSS moves for hardware and Eden bridge routes."""

from collections import deque
from dataclasses import dataclass
import re
import time


TILE = 'tile.cursor'
UI = 'ssbu.ui'
SCENES = {'css': {'css.training', 'css.smash'}, 'sss': {'stage_select'}}
DIRECTIONS = {'up', 'down', 'left', 'right'}
FORMS = {'css': {'default'}, 'sss': {'normal', 'battlefield', 'omega'}}
ABSENT = {'', 'none', 'unknown'}


@dataclass(frozen=True)
class Sample:
    block: dict
    connection: int

    @property
    def values(self):
        return self.block['values']

    @property
    def clock(self):
        return self.block['boot'], self.block['rev'], number(self, 'observation')


@dataclass(frozen=True)
class Target:
    kind: str
    name: str
    form: str

    @classmethod
    def read(cls, state, prefix=''):
        parts = [state.values.get(prefix + key, 'unknown') for key in ('kind', 'name', 'form')]
        kind, name, form = parts
        if (not re.fullmatch(r'[0-9]+', kind) or name in ABSENT
                or form not in FORMS.get(state.values.get('screen'), ())):
            raise RuntimeError(f'{prefix}tile identity {parts!r} is incomplete')
        return cls(*parts)


@dataclass(frozen=True)
class Topology:
    """Directed neighbors and stock identities recorded for one active layout fingerprint."""

    layout: str
    neighbors: dict
    targets: dict

    def step(self, current, goal):
        search = deque([(current, None)])
        visited = {current}
        while search:
            tile, first = search.popleft()
            if tile == goal:
                return first
            for direction, dest in self.neighbors.get(tile, {}).items():
                if direction not in DIRECTIONS or dest not in self.targets:
                    raise RuntimeError(f'invalid topology edge {tile} {direction} {dest}')
                if dest not in visited:
                    visited.add(dest)
                    search.append((dest, first or (direction, dest)))
        raise RuntimeError(f'no validated tile path from {current} to {goal}')


def number(state, key):
    text = state.values.get(key, '')
    if not re.fullmatch(r'[0-9]+', text):
        raise RuntimeError(f'{state.block["name"]}: invalid {key}={text!r}')
    return int(text)


def check_sample(state, provider):
    block, values = state.block, state.values
    if (block['name'] != provider or block['schema'] != provider + '/1'
            or block['filtered'] or block['rev'] <= 0
            or not re.fullmatch(r'0x[0-9a-f]{16}', block['boot'])
            or any(values.get(key) != str(block[key]) for key in ('schema', 'boot', 'rev'))
            or values.get('observation.active') != '1' or number(state, 'observation') <= 0
            or values.get('focus', 'unknown') in ABSENT):
        raise RuntimeError(f'{provider}: incomplete, inactive or wrong-schema observation')
    return state


def same_epoch(previous, current, *, in_flight):
    if previous.block['boot'] != current.block['boot']:
        raise RuntimeError('bridge boot changed')
    if previous.connection != current.connection and in_flight:
        raise RuntimeError('bridge connection changed while input was unconfirmed')


def wait_sample(io, provider, until, after=None, *, in_flight=False):
    while time.monotonic() < until:
        state = check_sample(io.read(provider, until - time.monotonic()), provider)
        if after is None:
            return state
        same_epoch(after, state, in_flight=in_flight)
        _, revision, observation = state.clock
        _, prev_revision, prev_observation = after.clock
        if revision > prev_revision and observation > prev_observation:
            return state
    raise RuntimeError(f'{provider}: no newer active observation')


def native_owner(state, screen, previous=None):
    values = state.values
    if (values.get('input.owner') != 'native' or values.get('screen') not in SCENES[screen]
            or values.get('focus.source') != TILE):
        raise RuntimeError(f'{screen}: native screen does not own tile input')
    if previous is not None and values['screen'] != previous.values['screen']:
        raise RuntimeError('native screen changed during tile selection')


def context(state, screen):
    values = state.values
    if values.get('screen') != screen or values.get('layout', 'unknown') in ABSENT:
        raise RuntimeError(f'{screen}: no qualified active tile layout')
    if number(state, 'generation') <= 0 or number(state, 'controller') != 0:
        raise RuntimeError(f'{screen}: no qualified local controller or layout generation')
    seat = number(state, 'seat')
    return values['layout'], values['generation'], seat


def token_held(state, screen):
    values = state.values
    token, carried = values.get('local.token'), values.get('carried')
    if token == 'held' and carried == values.get('seat'):
        return
    if screen == 'sss' and token == 'none' and carried == 'none':
        return
    raise RuntimeError(f'{screen}: local token ownership is {token!r}, carried slot is {carried!r}')


def settled(state):
    at = state.values.get('at', 'unknown')
    if (state.values.get('settled') != '1' or at != state.values.get('hovered')
            or (at != 'none' and not re.fullmatch(r'[0-9]+', at))
            or number(state, 'settled.ack') != number(state, 'ack')):
        raise RuntimeError('tile hover has not settled natively')
    return at


def check_context(expected, current, screen):
    if context(current, screen) != context(expected, screen):
        raise RuntimeError('tile layout, generation or local seat changed')


def baseline(io, screen, tile=None, previous_ui=None, cap=4):
    until = time.monotonic() + cap
    # The bridge maintains one watch per connection. Tile steps use that watch;
    # native ownership and transitions use separately fenced full queries.
    io.arm(TILE, cap)
    ui = wait_sample(io, UI, until, previous_ui)
    native_owner(ui, screen, previous_ui)
    fresh = wait_sample(io, TILE, until, tile)
    if fresh.connection != ui.connection or fresh.block['boot'] != ui.block['boot']:
        raise RuntimeError('tile and native owner are from different bridge connections')
    context(fresh, screen)
    token_held(fresh, screen)
    at = settled(fresh)
    if tile is not None:
        check_context(tile, fresh, screen)
        if (at != settled(tile) or number(fresh, 'ack') != number(tile, 'ack')
                or number(fresh, 'applied') != number(tile, 'applied')):
            raise RuntimeError('idle tile checkpoint changed before input')
    return fresh, ui


def move(io, screen, before, ui, at, direction, dest, expected_tile, cap):
    until = time.monotonic() + cap
    io.send('tile-' + direction)
    prior_ack = number(before, 'ack')
    prior_applied = number(before, 'applied')
    latest = before
    while True:
        latest = wait_sample(io, TILE, until, latest, in_flight=True)
        check_context(before, latest, screen)
        token_held(latest, screen)
        values = latest.values
        ack, applied = number(latest, 'ack'), number(latest, 'applied')
        if ack == prior_ack and applied == prior_applied and values.get('at') == at:
            continue
        if ack != prior_ack + 1 or applied != prior_applied + 1:
            raise RuntimeError(f'wrong step ACK/application delta: {ack - prior_ack}/{applied - prior_applied}')
        step = tuple(values.get(key) for key in ('direction', 'from', 'to', 'ack.result', 'result'))
        if step != (direction, at, dest, 'handled', 'handled'):
            raise RuntimeError(f'wrong step: expected {direction} {at}->{dest}, observed {step!r}')
        if values.get('settled') != '1':
            continue
        if settled(latest) != dest or Target.read(latest) != expected_tile:
            raise RuntimeError('settled tile identity disagrees with the qualified topology')
        after_ui = wait_sample(io, UI, until, ui, in_flight=True)
        native_owner(after_ui, screen, ui)
        io.log({'tile_step': direction, 'from': at, 'to': dest, 'ack': ack,
                'applied': applied, 'settled_ack': number(latest, 'settled.ack'),
                'kind': expected_tile.kind, 'boot': latest.block['boot'], 'rev': latest.block['rev']})
        io.finish()
        return latest, after_ui


def hover(io, screen, target, topology=None, cap=4):
    current, ui = baseline(io, screen, cap=cap)
    at = settled(current)
    if target is None or (at != 'none' and Target.read(current) == target):
        if at == 'none':
            raise RuntimeError('selecting the current tile requires actual settled native hover')
        return current, ui, Target.read(current)
    if topology is None or topology.layout != current.values['layout']:
        raise RuntimeError('active layout has no validated topology for this target')
    matches = [tile for tile, identity in topology.targets.items() if identity == target]
    if len(matches) != 1:
        raise RuntimeError('target identity is absent or ambiguous in the active layout')
    goal = matches[0]
    if at != 'none' and topology.targets.get(at) != Target.read(current):
        raise RuntimeError('current tile identity disagrees with the qualified topology')
    while at != goal:
        direction, dest = topology.step(at, goal)
        current, ui = baseline(io, screen, current, ui, cap)
        if at != 'none' and topology.targets[at] != Target.read(current):
            raise RuntimeError('tile identity changed before input')
        current, ui = move(io, screen, current, ui, at, direction, dest, topology.targets[dest], cap)
        at = dest
    return current, ui, target


def confirm(io, screen, prepared, *, next_screen='css.training', cap=4, transition_cap=60):
    previous, prior_ui, target = prepared
    before, ui = baseline(io, screen, previous, prior_ui, cap)
    if Target.read(before) != target:
        raise RuntimeError('pre-A native target identity changed')
    at = settled(before)
    io.log({'tile_confirm_before': at, 'kind': target.kind, 'name': target.name, 'form': target.form,
            'ack': number(before, 'ack'), 'applied': number(before, 'applied'),
            'seat': before.values['seat'], 'boot': before.block['boot'], 'rev': before.block['rev']})
    io.send('tap-a1')
    if screen == 'css':
        until = time.monotonic() + cap
        latest = before
        while True:
            latest = wait_sample(io, TILE, until, latest, in_flight=True)
            check_context(before, latest, screen)
            if (number(latest, 'ack') != number(before, 'ack')
                    or number(latest, 'applied') != number(before, 'applied')):
                raise RuntimeError('tile step arrived while confirming local placement')
            values = latest.values
            if values.get('local.token') == 'placed':
                if (values.get('carried') != 'none' or values.get('placed.at') != at
                        or Target.read(latest, 'placed.') != target
                        or values['focus'] == before.values['focus']):
                    raise RuntimeError('local placement does not match the chosen fighter')
                break
            token_held(latest, screen)
            if settled(latest) != at or Target.read(latest) != target:
                raise RuntimeError('tile changed before local placement')
        after = wait_sample(io, UI, until, ui, in_flight=True)
        native_owner(after, screen, ui)
        result = {'seat': before.values['seat'], 'placed': True}
    else:
        until = time.monotonic() + transition_cap
        after = ui
        while True:
            after = wait_sample(io, UI, until, after, in_flight=True)
            values = after.values
            if values.get('screen') == next_screen and values.get('input.owner') == 'native':
                if values['focus'] == ui.values['focus']:
                    raise RuntimeError('stage confirmation did not change native focus')
                break
            if values.get('screen') not in ('unknown', 'stage_select'):
                raise RuntimeError(f'after stage confirmation, unexpected native screen {values.get("screen")!r}')
        result = {'screen': next_screen}
    result.update({'tile': at, 'kind': target.kind, 'name': target.name, 'form': target.form})
    io.log({'tile_confirm_after': result, 'boot': after.block['boot'], 'rev': after.block['rev']})
    io.finish()
    return result


def pick(io, screen, target, *, topology=None, next_screen='css.training', cap=4, transition_cap=60):
    return confirm(io, screen, hover(io, screen, target, topology, cap),
                   next_screen=next_screen, cap=cap, transition_cap=transition_cap)


def acquire_local_token(io, target, topology, cap=4):
    """Keep a qualified held token or take back this controller's placed medal with one B."""
    until = time.monotonic() + cap
    io.arm(TILE, cap)
    ui = wait_sample(io, UI, until)
    current = wait_sample(io, TILE, until)
    context(current, 'css')
    native_owner(ui, 'css')
    same_epoch(ui, current, in_flight=True)
    if current.values.get('local.token') == 'held':
        token_held(current, 'css')
        return None
    wanted = Target.read(current, 'placed.')
    if wanted == target:
        if current.values.get('local.token') != 'placed' or current.values.get('carried') != 'none':
            raise RuntimeError('the intended local medal is not placed')
        return {'tile': str(number(current, 'placed.at')), 'seat': current.values['seat'], 'placed': True,
                'kind': target.kind, 'name': target.name, 'form': target.form}
    if (topology is None or topology.layout != current.values['layout']
            or list(topology.targets.values()).count(target) != 1):
        raise RuntimeError('local token takeback needs the qualified target in the active layout')
    ui = wait_sample(io, UI, until, ui)
    native_owner(ui, 'css')
    before = wait_sample(io, TILE, until, current)
    same_epoch(ui, before, in_flight=True)
    check_context(current, before, 'css')
    if (before.values.get('local.token') != 'placed' or before.values.get('carried') != 'none'
            or Target.read(before, 'placed.') != wanted
            or number(before, 'placed.at') != number(current, 'placed.at')):
        raise RuntimeError('local medal placement changed before B')
    io.send('tap-b1')
    until = time.monotonic() + cap
    state = before
    while True:
        state = wait_sample(io, TILE, until, state, in_flight=True)
        check_context(before, state, 'css')
        if (number(state, 'ack') != number(before, 'ack')
                or number(state, 'applied') != number(before, 'applied')):
            raise RuntimeError('a tile step arrived while taking back the local token')
        if state.values.get('local.token') == 'held':
            token_held(state, 'css')
            if state.values['focus'] == before.values['focus']:
                raise RuntimeError('local token takeback did not change native focus')
            native_owner(wait_sample(io, UI, until, ui, in_flight=True), 'css')
            io.log({'local_token_taken_back': state.values['seat'], 'boot': state.block['boot'],
                    'rev': state.block['rev']})
            io.finish()
            return None
        if state.values.get('local.token') != 'placed' or Target.read(state, 'placed.') != wanted:
            raise RuntimeError('local medal changed while taking it back')

def verify_room_selection(current, expected_key, generation):
    values = check_sample(current, UI).values
    if (values.get('screen') != 'ldn.room_list' or values.get('input.owner') != 'native'
            or values.get('ldn.room.ready') != 'yes'):
        raise RuntimeError('native Local Wireless room selection is not ready')
    if str(number(current, 'ldn.scan.generation')) != generation or generation == '0':
        raise RuntimeError('native Local Wireless scan generation changed')
    bssid, session = expected_key
    if (not re.fullmatch(r'[0-9a-f]{12}', bssid) or not re.fullmatch(r'[0-9a-f]{32}', session)
            or (bssid, session) != (values.get('ldn.room.bssid'), values.get('ldn.room.session'))
            or values.get('focus.id') != f'ldn.room.{bssid}.{session}'):
        raise RuntimeError('selected Local Wireless room key differs from the owned host room')


def request_join(current, expected_key, generation, send):
    """Rehearse the exact selection predicate while native Join is release-blocked."""
    verify_room_selection(current, expected_key, generation)
    refuse_join()


def refuse_join():
    raise RuntimeError('Local Wireless Join is disabled pending the exact-release trace of the selected room record')