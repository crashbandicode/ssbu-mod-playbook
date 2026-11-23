"""Checked CSS/SSS moves for hardware and Eden bridge routes."""

from collections import deque
from dataclasses import dataclass
import re
import time


TILE = 'tile.cursor'
UI = 'ssbu.ui'
ACTIVE = {TILE: '1', UI: 'yes'}
SCENES = {'css': {'css.training', 'css.smash'}, 'sss': {'stage_select'}}
DIRECTIONS = {'up', 'down', 'left', 'right'}
FORMS = {'css': {'default'}, 'sss': {'normal', 'battlefield', 'omega'}}
ABSENT = {'', 'none', 'unknown'}
UI_FOCUS_FIELDS = ('screen', 'screen.context', 'focus', 'focus.source', 'input.owner', 'dialog')


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


def check_sample(state, provider, *, loading_ui=False, pending_placement=False, entering_stage=False, stepping=False):
    block, values = state.block, state.values
    active = values.get('observation.active') == ACTIVE[provider]
    loading = loading_ui and provider == UI and values.get('observation.active') == 'no'
    empty_boot = (loading and all(values.get(key) == 'unknown' for key in UI_FOCUS_FIELDS)
                  and values.get('ack') == '0' and values.get('ack.result') == 'unsupported')
    focus = values.get('focus', '')
    placing = (pending_placement and provider == TILE and focus == 'unknown'
               and values.get('local.token') == 'placed')
    entering = (entering_stage and provider == TILE and focus == 'unknown'
                and values.get('screen') == 'sss' and values.get('settled') == '0'
                and values.get('layout') == values.get('hovered') == 'unknown')
    at = values.get('at')
    idle = all(values.get(key) == 'unknown'
               for key in ('settled.ack', 'ack.result', 'result', 'direction', 'from', 'to'))
    # The hand carries the CPU's coin: the old lines then never name a focus or a fighter (WIRE.md).
    carrying_cpu = values.get('p1.carried') == '1'
    step = (stepping and provider == TILE and focus == 'unknown'
            and values.get('kind') == 'unknown' and values.get('name') == 'unknown'
            and values.get('form') == 'unknown'
            and ((values.get('settled') == '0' and values.get('hovered') == 'unknown'
                  and values.get('settled.ack') == 'unknown')
                 or (values.get('settled') == '1' and values.get('hovered') == at
                     and isinstance(at, str) and re.fullmatch(r'[0-9]+', at)
                     and ((values.get('settled.ack') is not None and values.get('settled.ack') == values.get('ack'))
                          or (carrying_cpu and idle)))))
    if (block['name'] != provider or block['schema'] != provider + '/1'
            or block['filtered'] or block['rev'] <= 0
            or not re.fullmatch(r'0x[0-9a-f]{16}', block['boot'])
            or any(values.get(key) != str(block[key]) for key in ('schema', 'boot', 'rev'))
            or (number(state, 'observation') == 0 and not empty_boot)
            or not (loading or (active and (focus not in ABSENT or placing or entering or step)))):
        raise RuntimeError(f'{provider}: incomplete, inactive or wrong-schema observation')
    return state


def same_epoch(previous, current, *, in_flight):
    if previous.block['boot'] != current.block['boot']:
        raise RuntimeError('bridge boot changed')
    if previous.connection != current.connection and in_flight:
        raise RuntimeError('bridge connection changed while input was unconfirmed')


def wait_sample(io, provider, until, after=None, *, in_flight=False, loading_ui=False,
                pending_placement=False, stepping=False):
    settling = loading_ui and provider == UI and after is not None
    while time.monotonic() < until:
        state = check_sample(io.read(provider, until - time.monotonic()), provider,
                             loading_ui=settling, pending_placement=pending_placement, stepping=stepping)
        if after is None:
            return state
        same_epoch(after, state, in_flight=in_flight or settling)
        _, revision, observation = state.clock
        _, prev_revision, prev_observation = after.clock
        if settling:
            values = state.values
            if (revision < prev_revision or observation < prev_observation
                    or (revision == prev_revision and values != after.values)):
                raise RuntimeError(f'{UI}: stale or contradictory settling observation')
            if values['observation.active'] == 'no':
                if (any(values.get(key) != 'unknown' for key in UI_FOCUS_FIELDS)
                        or values.get('ack') != '0' or values.get('ack.result') != 'unsupported'):
                    raise RuntimeError(f'{UI}: contradictory inactive settling observation')
                after = state
                continue
        if revision > prev_revision and observation > prev_observation:
            return state
    raise RuntimeError(f'{provider}: no newer active observation')


def wait_scene_transition(io, before, until, source, target, *, label, pending_destinations=(),
                          additional_targets=(), accept_source=False):
    """Wait for one already-sent scene-changing input, on the fenced connection."""
    destinations = [
        (candidate, {key: value for key, value in candidate.items() if key not in ('dialog', 'input.owner')})
        for candidate in (target, *additional_targets)
    ]
    passive_source = {**source, 'dialog': 'unknown', 'input.owner': 'unknown'}
    previous = before
    departed = False
    while time.monotonic() < until:
        state = check_sample(io.read(UI, until - time.monotonic()), UI, loading_ui=True)
        same_epoch(previous, state, in_flight=True)
        values = state.values
        if (any(not values.get(key) for key in UI_FOCUS_FIELDS)
                or values.get('ack') != '0' or values.get('ack.result') != 'unsupported'):
            raise RuntimeError(f'{UI}: incomplete {label} scene observation')
        _, revision, observation = state.clock
        _, prior_revision, prior_observation = previous.clock
        if (revision < prior_revision or observation < prior_observation
                or (revision == prior_revision and values != previous.values)):
            raise RuntimeError(f'{UI}: stale or contradictory {label} scene observation')
        if revision == prior_revision:
            continue
        previous = state
        if values['observation.active'] == 'no':
            if any(values[key] != 'unknown' for key in UI_FOCUS_FIELDS):
                raise RuntimeError(f'{UI}: contradictory inactive {label} scene observation')
            departed = True
            continue
        if not departed and any(
            all(values.get(key) == value for key, value in candidate.items())
            for candidate in (source, passive_source)
        ):
            if (accept_source and observation > prior_observation
                    and all(values.get(key) == value for key, value in source.items())):
                return state
            continue
        if any(all(values.get(key) == value for key, value in pending.items())
               for pending in pending_destinations):
            departed = True
            continue
        matched = next((candidate for candidate, destination in destinations
                        if all(values.get(key) == value for key, value in destination.items())), None)
        if (matched is not None and values['input.owner'] in ('native', 'unknown')
                and values['dialog'] in ('none', 'unknown')
                and values['focus'] != before.values['focus']):
            if observation > prior_observation and all(values[key] == value for key, value in matched.items()):
                return state
            departed = True
            continue
        expected = target.get('focus', target.get('focus.id', target['screen']))
        details = ', '.join(f'{key}={values.get(key, "missing")}' for key in (
            'screen', 'screen.context', 'input.owner', 'dialog', 'focus.source', 'focus.id'))
        raise RuntimeError(f'unexpected {label} focus {values["focus"]!r}; expected {expected!r}. '
                           f'Last {UI}: {details}')
    raise RuntimeError(f'{UI}: no newer qualified {label} scene transition')


def native_owner(state, screen, previous=None):
    values = state.values
    if (values.get('input.owner') != 'native' or values.get('screen') not in SCENES[screen]
            or values.get('focus.source') != TILE):
        raise RuntimeError(f'{screen}: native screen does not own tile input')
    if previous is not None and values['screen'] != previous.values['screen']:
        raise RuntimeError('native screen changed during tile selection')


def native_input_owner(state, screen, previous=None, *, expected_context=None):
    native_owner(state, screen, previous)
    if state.values.get('dialog') != 'none':
        raise RuntimeError(f'{screen}: native dialog is not positively clear')
    if expected_context is not None and (
        state.values.get('screen.context') != expected_context
        or (screen == 'css' and expected_context == 'training' and state.values['screen'] != 'css.training')
    ):
        raise RuntimeError(f'{screen}: expected native {expected_context} context')


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


def settled(state, *, after_step=False):
    at = state.values.get('at', 'unknown')
    if (state.values.get('settled') != '1' or at != state.values.get('hovered')
            or (at != 'none' and not re.fullmatch(r'[0-9]+', at))):
        raise RuntimeError('tile hover has not settled natively')
    idle = all(state.values.get(key) == 'unknown'
               for key in ('settled.ack', 'ack.result', 'result', 'direction', 'from', 'to'))
    if (after_step or not idle) and number(state, 'settled.ack') != number(state, 'ack'):
        raise RuntimeError('tile hover has not settled natively')
    return at


def wait_stage_tile(io, selected, until):
    """Wait for the active Stage Select provider after the native scene transition."""
    previous = None
    while (remaining := until - time.monotonic()) > 0:
        state = check_sample(io.read(TILE, remaining), TILE, entering_stage=True)
        same_epoch(selected, state, in_flight=True)
        if state.values.get('screen') != 'sss':
            raise RuntimeError('tile.cursor did not confirm the sss screen')
        if previous is not None:
            _, revision, observation = state.clock
            _, prior_revision, prior_observation = previous.clock
            if (revision < prior_revision or observation < prior_observation
                    or ((revision == prior_revision or observation == prior_observation)
                        and state.values != previous.values)):
                raise RuntimeError('tile.cursor: stale or contradictory Stage Select observation')
        if state.values['focus'] != 'unknown':
            context(state, 'sss')
            token_held(state, 'sss')
            settled(state)
            Target.read(state)
            return state
        previous = state
    raise RuntimeError('tile.cursor: no qualified Training Stage Select observation before the route deadline')


def check_context(expected, current, screen):
    if context(current, screen) != context(expected, screen):
        raise RuntimeError('tile layout, generation or local seat changed')


def baseline(io, screen, tile=None, previous_ui=None, cap=4, *, expected_context=None):
    until = time.monotonic() + cap
    # The bridge maintains one watch per connection. Tile steps use that watch;
    # native ownership and transitions use separately fenced full queries.
    io.arm(TILE, cap)
    ui = wait_sample(io, UI, until, previous_ui)
    native_input_owner(ui, screen, previous_ui, expected_context=expected_context)
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


def move(io, screen, before, ui, at, direction, dest, expected_tile, cap, *, expected_context=None):
    until = time.monotonic() + cap
    io.send('tile-' + direction)
    prior_ack = number(before, 'ack')
    prior_applied = number(before, 'applied')
    latest = before
    while True:
        latest = wait_sample(io, TILE, until, latest, in_flight=True, stepping=True)
        check_context(before, latest, screen)
        token_held(latest, screen)
        values = latest.values
        ack, applied = number(latest, 'ack'), number(latest, 'applied')
        pending = values.get('focus') == 'unknown'
        if pending and (ack, applied) != (prior_ack + 1, prior_applied + 1):
            raise RuntimeError('tile identity pending without the outstanding step')
        if ack == prior_ack and applied == prior_applied and values.get('at') == at:
            continue
        if ack != prior_ack + 1 or applied != prior_applied + 1:
            raise RuntimeError(f'wrong step ACK/application delta: {ack - prior_ack}/{applied - prior_applied}')
        step = tuple(values.get(key) for key in ('direction', 'from', 'to', 'ack.result', 'result'))
        if step != (direction, at, dest, 'handled', 'handled'):
            raise RuntimeError(f'wrong step: expected {direction} {at}->{dest}, observed {step!r}')
        if pending and values.get('at') != dest:
            raise RuntimeError('pending tile coordinate disagrees with the outstanding step')
        if values.get('settled') != '1':
            continue
        if pending:
            if settled(latest, after_step=True) != dest:
                raise RuntimeError('settled tile coordinate disagrees with the outstanding step')
            continue
        if settled(latest, after_step=True) != dest or Target.read(latest) != expected_tile:
            raise RuntimeError('settled tile identity disagrees with the qualified topology')
        after_ui = wait_sample(io, UI, until, ui, in_flight=True)
        native_input_owner(after_ui, screen, ui, expected_context=expected_context)
        io.log({'tile_step': direction, 'from': at, 'to': dest, 'ack': ack,
                'applied': applied, 'settled_ack': number(latest, 'settled.ack'),
                'kind': expected_tile.kind, 'boot': latest.block['boot'], 'rev': latest.block['rev']})
        io.finish()
        return latest, after_ui


def hover(io, screen, target, topology=None, cap=4, *, expected_context=None):
    current, ui = baseline(io, screen, cap=cap, expected_context=expected_context)
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
        current, ui = baseline(io, screen, current, ui, cap, expected_context=expected_context)
        if at != 'none' and topology.targets[at] != Target.read(current):
            raise RuntimeError('tile identity changed before input')
        current, ui = move(io, screen, current, ui, at, direction, dest, topology.targets[dest], cap,
                           expected_context=expected_context)
        at = dest
    return current, ui, target


def confirm(io, screen, prepared, *, next_screen='css.training', cap=4, transition_cap=60, expected_context=None):
    previous, prior_ui, target = prepared
    if screen == 'sss' and next_screen == 'css.training':
        expected_context = 'training'
    before, ui = baseline(io, screen, previous, prior_ui, cap, expected_context=expected_context)
    if Target.read(before) != target:
        raise RuntimeError('pre-A native target identity changed')
    at = settled(before)
    io.log({'tile_confirm_before': at, 'kind': target.kind, 'name': target.name, 'form': target.form,
            'ack': number(before, 'ack'), 'applied': number(before, 'applied'),
            'seat': before.values['seat'], 'boot': before.block['boot'], 'rev': before.block['rev']})
    context = expected_context
    if context is None and ui.values.get('screen.context') not in (None, *ABSENT):
        context = ui.values['screen.context']
    io.send('tap-a1')
    if screen == 'css':
        until = time.monotonic() + cap
        latest = before
        while True:
            latest = wait_sample(io, TILE, until, latest, in_flight=True, pending_placement=True)
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
                # The placed identity can settle before the medal owns focus.
                if values['focus'] == 'unknown':
                    continue
                break
            token_held(latest, screen)
            if settled(latest) != at or Target.read(latest) != target:
                raise RuntimeError('tile changed before local placement')
        after = wait_sample(io, UI, until, ui, in_flight=True)
        native_input_owner(after, screen, ui, expected_context=expected_context)
        result = {'seat': before.values['seat'], 'placed': True}
    elif next_screen == 'css.training':
        source = {key: ui.values[key] for key in
                  ('screen', 'screen.context', 'input.owner', 'dialog', 'focus', 'focus.source')}
        destination = {'screen': next_screen, 'screen.context': 'training', 'input.owner': 'native',
                       'dialog': 'none', 'focus.source': TILE}
        after = wait_scene_transition(io, ui, time.monotonic() + transition_cap, source, destination, label='stage')
        result = {'screen': next_screen}
    else:
        until = time.monotonic() + transition_cap
        after = ui
        while True:
            after = wait_sample(io, UI, until, after, in_flight=True)
            values = after.values
            if values.get('screen') == next_screen:
                owner, dialog = values.get('input.owner'), values.get('dialog')
                destination = (values.get('focus') == next_screen and values.get('focus.source') == TILE
                               and (context is None or values.get('screen.context') == context))
                if owner == 'unknown' and dialog == 'unknown':
                    if not destination:
                        raise RuntimeError(f'after stage confirmation, unexpected pending {next_screen} observation')
                    continue
                if owner != 'native' or dialog != 'none':
                    raise RuntimeError(f'after stage confirmation, {next_screen} is owned by {owner!r} with dialog {dialog!r}')
                if not destination:
                    raise RuntimeError(f'after stage confirmation, unexpected {next_screen} observation')
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


def pick(io, screen, target, *, topology=None, next_screen='css.training', cap=4, transition_cap=60,
         expected_context=None):
    return confirm(io, screen, hover(io, screen, target, topology, cap, expected_context=expected_context),
                   next_screen=next_screen, cap=cap, transition_cap=transition_cap, expected_context=expected_context)


def acquire_local_token(io, target, topology, cap=4, *, expected_context=None):
    """Keep a qualified held token or take back this controller's placed medal with one B."""
    until = time.monotonic() + cap
    io.arm(TILE, cap)
    ui = wait_sample(io, UI, until)
    current = wait_sample(io, TILE, until)
    context(current, 'css')
    native_input_owner(ui, 'css', expected_context=expected_context)
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
    take_back_local_token(io, current, ui, cap, expected_context=expected_context)
    return None

CPU_FIELDS = (
    'hand.token', 'p1.coin', 'p1.coin.at', 'cpu.token', 'cpu.placed.at',
    'cpu.placed.kind', 'cpu.placed.name', 'cpu.placed.form', 'carried.kind',
    'carried.name', 'carried.form', 'target.token', 'token.settled',
)

TAKE_FIELDS = ('take.ack', 'take.applied')


def take_lines(state):
    if any(key not in state.values for key in TAKE_FIELDS):
        raise RuntimeError('tile.cursor: this build has no L3 + B (no take.ack line)')
    for key in TAKE_FIELDS:
        number(state, key)
    return state


def take_cpu(io, cap=4, *, expected_context='training'):
    before, ui = _coin_baseline(io, cap, expected_context)
    take_lines(before)
    _p1_placed(before)
    if before.values.get('cpu.token') != 'placed':
        raise RuntimeError(f"the CPU's coin is not resting on a tile: {before.values.get('cpu.token')!r}")
    start_ack, start_applied = number(before, 'take.ack'), number(before, 'take.applied')
    tile_ack, tile_applied = number(before, 'ack'), number(before, 'applied')
    p1_at = before.values['p1.coin.at']
    io.send('tile-take')
    until = time.monotonic() + cap
    latest = before
    while True:
        try:
            latest = take_lines(cpu_lines(wait_sample(io, TILE, until, latest, in_flight=True,
                                                      stepping=True, pending_placement=True)))
        except RuntimeError as exc:
            if str(exc) == f'{TILE}: no newer active observation' and time.monotonic() >= until:
                break
            raise
        check_context(before, latest, 'css')
        if (number(latest, 'ack'), number(latest, 'applied')) != (tile_ack, tile_applied):
            raise RuntimeError("a tile step arrived while taking the CPU's coin")
        ack, applied = number(latest, 'take.ack'), number(latest, 'take.applied')
        if ack > start_ack + 1 or applied > start_applied + 1:
            raise RuntimeError('L3 + B was read more than once')
        if ack < start_ack or applied < start_applied:
            raise RuntimeError('L3 + B counters regressed')
        values = latest.values
        if (values.get('local.token') == 'held' or values.get('carried') == '0'
                or values.get('p1.carried') == '0'):
            raise RuntimeError("L3 + B took P1's own coin")
        if values.get('p1.coin') not in ('placed', 'unknown') or values.get('p1.coin.at') not in (p1_at, 'unknown'):
            raise RuntimeError("P1's coin changed while taking the CPU's coin")
        if (ack == start_ack + 1 and applied == start_applied + 1 and values.get('p1.carried') == '1'
                and values.get('cpu.token') == 'held' and values.get('p1.coin') == 'placed'
                and values.get('p1.coin.at') == p1_at and values.get('settled') == '1'):
            settled(latest)
            _coin_finish(io, ui, until, expected_context)
            return latest
    if number(latest, 'take.ack') == start_ack:
        raise RuntimeError(f'L3 + B was not read within {cap} s (take.ack unchanged)')
    if number(latest, 'take.applied') == start_applied:
        raise RuntimeError("L3 + B was read but the CPU's coin was not taken")
    raise RuntimeError(f"the CPU's coin was taken but its hover did not settle within {cap} s")


def take_noop(io, holding, cap=4, quiet=1.0, *, expected_context='training'):
    if holding not in ('0', '1'):
        raise ValueError(f'invalid holding {holding!r}')
    before, ui = _coin_baseline(io, cap, expected_context)
    take_lines(before)
    values = before.values
    held = 'cpu.token' if holding == '1' else 'local.token'
    if values.get('p1.carried') != holding or values.get(held) != 'held':
        raise RuntimeError(f"holding {holding} requires p1.carried={holding} and {held}=held; "
                           f"saw {values.get('p1.carried')!r} and {values.get(held)!r}")
    watched = ('p1.carried', 'carried', 'local.token', 'p1.coin', 'p1.coin.at', 'cpu.token',
               'cpu.placed.at', 'at', 'hovered', 'ack', 'applied', 'blocked', 'take.applied')
    if any(field not in values for field in watched):
        raise RuntimeError('tile.cursor: incomplete L3 + B no-op observation')
    unchanged = {field: values[field] for field in watched}
    ack, applied = number(before, 'take.ack'), number(before, 'take.applied')
    io.send('tile-take')
    until = time.monotonic() + cap + quiet
    latest, first, blocks = before, None, 0
    while True:
        try:
            latest = take_lines(cpu_lines(wait_sample(io, TILE, until, latest, in_flight=True,
                                                      stepping=True, pending_placement=True)))
        except RuntimeError as exc:
            if str(exc) == f'{TILE}: no newer active observation' and time.monotonic() >= until:
                break
            raise
        check_context(before, latest, 'css')
        for field, old in unchanged.items():
            new = latest.values.get(field)
            if new != old:
                raise RuntimeError(f'L3 + B changed {field}: {old!r} -> {new!r}')
        current = number(latest, 'take.ack')
        if current > ack + 1:
            raise RuntimeError('L3 + B was read more than once')
        if current < ack or (first is not None and current != ack + 1):
            raise RuntimeError('L3 + B take.ack regressed')
        if current == ack + 1:
            if first is None:
                first = time.monotonic()
            else:
                blocks += 1
                if blocks >= 5 and time.monotonic() - first >= quiet:
                    _coin_finish(io, ui, until, expected_context)
                    return {'take.ack': [ack, current], 'take.applied': [applied, applied],
                            'unchanged': unchanged, 'blocks': blocks}
    if first is None:
        raise RuntimeError(f'L3 + B was not read within {cap + quiet} s (take.ack unchanged)')
    raise RuntimeError(f'L3 + B did not remain unchanged for {quiet} s within the deadline')


def take_back_local_token(io, current, ui, cap=4, *, expected_context=None):
    until = time.monotonic() + cap
    wanted = Target.read(current, 'placed.')
    ui = wait_sample(io, UI, until, ui)
    native_input_owner(ui, 'css', expected_context=expected_context)
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
        state = wait_sample(io, TILE, until, state, in_flight=True, stepping=True)
        check_context(before, state, 'css')
        if (number(state, 'ack') != number(before, 'ack')
                or number(state, 'applied') != number(before, 'applied')):
            raise RuntimeError('a tile step arrived while taking back the local token')
        if state.values.get('local.token') == 'held':
            token_held(state, 'css')
            if state.values.get('settled') != '1':
                if state.values.get('settled') != '0' or state.values.get('target.at') != before.values['placed.at']:
                    raise RuntimeError('the held token is not returning to the medal tile')
                continue
            if settled(state) != before.values['placed.at'] or Target.read(state) != wanted:
                raise RuntimeError('the held token did not settle on the medal tile')
            if state.values['focus'] == before.values['focus']:
                raise RuntimeError('local token takeback did not change native focus')
            native_input_owner(wait_sample(io, UI, until, ui, in_flight=True), 'css',
                               expected_context=expected_context)
            io.log({'local_token_taken_back': state.values['seat'], 'boot': state.block['boot'],
                    'rev': state.block['rev']})
            io.finish()
            return state
        if state.values.get('local.token') != 'placed' or Target.read(state, 'placed.') != wanted:
            raise RuntimeError('local medal changed while taking it back')


class CoinBlocked(RuntimeError):
    def __init__(self, wanted, pointed):
        self.wanted, self.pointed = str(wanted), str(pointed)
        super().__init__(f"the game points at coin {self.pointed} at coin {self.wanted}'s position")


def cpu_lines(state):
    if any(key not in state.values for key in CPU_FIELDS):
        raise RuntimeError("tile.cursor: this build does not report the CPU's coin (no hand.token line)")
    return state


def p1_medal(state):
    return tuple(state.values.get(key) for key in ('local.token', 'placed.at', 'placed.kind', 'placed.name',
                                                   'placed.form'))


def _coin_baseline(io, cap, expected_context):
    until = time.monotonic() + cap
    io.arm(TILE, cap)
    ui = wait_sample(io, UI, until)
    native_input_owner(ui, 'css', expected_context=expected_context)
    state = cpu_lines(wait_sample(io, TILE, until, stepping=True, pending_placement=True))
    same_epoch(ui, state, in_flight=True)
    if context(state, 'css')[2] != 0:
        raise RuntimeError('CPU coin selection requires local seat 0')
    return state, ui


def _coin_finish(io, ui, until, expected_context):
    after = wait_sample(io, UI, until, ui, in_flight=True)
    native_input_owner(after, 'css', ui, expected_context=expected_context)
    io.finish()


def _p1_placed(state):
    values = state.values
    if (values.get('local.token') != 'placed' or values.get('carried') != 'none'
            or values.get('p1.coin') != 'placed' or values.get('p1.coin.at') != values.get('placed.at')):
        raise RuntimeError(f'P1 medal is not placed with an empty hand: {p1_medal(state)!r}')
    number(state, 'p1.coin.at')
    Target.read(state, 'placed.')


def reach_token(io, slot, cap=4, *, expected_context='training'):
    slot = str(slot)
    if slot not in ('0', '1'):
        raise RuntimeError(f'invalid coin slot {slot!r}')
    before, ui = _coin_baseline(io, cap, expected_context)
    _p1_placed(before)
    hand = before.values.get('hand.token')
    if hand not in ('none', '0', '1'):
        raise RuntimeError(f'hand.token is not at rest: {hand!r}')
    if hand == slot:
        return before
    direction = ('left' if slot == '1' else 'right') if hand == 'none' else (
        'right' if slot == '1' else 'left')
    medal, coin_at = p1_medal(before), before.values['p1.coin.at']
    prior_ack, prior_applied = number(before, 'ack'), number(before, 'applied')
    io.send('tile-' + direction)
    until = time.monotonic() + cap
    latest = before
    while True:
        latest = cpu_lines(wait_sample(io, TILE, until, latest, in_flight=True, stepping=True,
                                       pending_placement=True))
        check_context(before, latest, 'css')
        values = latest.values
        if (p1_medal(latest) != medal or values.get('p1.coin') != 'placed'
                or values.get('p1.coin.at') != coin_at):
            raise RuntimeError('P1 medal or coin position changed while reaching the coin')
        ack, applied = number(latest, 'ack'), number(latest, 'applied')
        if (ack, applied) == (prior_ack, prior_applied):
            continue
        if values.get('result') == 'edge':
            raise RuntimeError(f'coin reach hit edge: {direction}')
        if (ack, applied) != (prior_ack + 1, prior_applied + 1):
            raise RuntimeError('a tile step arrived while reaching the coin')
        if (values.get('ack.result'), values.get('result'), values.get('direction'),
                values.get('target.token')) != ('handled', 'handled', direction, slot):
            raise RuntimeError(f'coin reach response does not match {direction} to {slot}')
        pointed = values.get('hand.token')
        if pointed in ('none', '0', '1') and pointed != slot:
            _coin_finish(io, ui, until, expected_context)
            raise CoinBlocked(slot, pointed)
        if pointed == slot and values.get('token.settled') == '1':
            _coin_finish(io, ui, until, expected_context)
            return latest


def take_token(io, slot, cap=4, *, expected_context='training'):
    slot = str(slot)
    if slot not in ('0', '1'):
        raise RuntimeError(f'invalid coin slot {slot!r}')
    before, ui = _coin_baseline(io, cap, expected_context)
    _p1_placed(before)
    hand = before.values.get('hand.token')
    if hand != slot:
        raise RuntimeError(f'refusing A: hand.token is {hand}, not {slot}')
    coin_at = before.values['p1.coin.at']
    io.send('tap-a1')
    until = time.monotonic() + cap
    latest = before
    picked_up = False
    while True:
        try:
            latest = cpu_lines(wait_sample(io, TILE, until, latest, in_flight=True, stepping=True,
                                           pending_placement=True))
        except RuntimeError as exc:
            if str(exc) == f'{TILE}: no newer active observation' and time.monotonic() >= until:
                if picked_up:
                    raise RuntimeError(f'coin {slot} was picked up but its hover did not settle within {cap} s') from exc
                raise RuntimeError(f'A did not pick up coin {slot} within {cap} s') from exc
            raise
        check_context(before, latest, 'css')
        if (number(latest, 'ack') != number(before, 'ack')
                or number(latest, 'applied') != number(before, 'applied')):
            raise RuntimeError('a tile step arrived while taking a coin')
        values = latest.values
        carried = values.get('p1.carried')
        held = (values.get('cpu.token') == 'held' and values.get('p1.coin') == 'placed'
                and values.get('p1.coin.at') == coin_at) if slot == '1' else (
                    values.get('local.token') == 'held' and values.get('carried') == '0')
        if carried == slot and held:
            picked_up = True
            if values.get('settled') == '1':
                settled(latest)
                _coin_finish(io, ui, until, expected_context)
                return latest
            continue
        hand = values.get('hand.token')
        other = '0' if slot == '1' else '1'
        if ((hand in ('none', '0', '1') and hand != slot)
                or carried == other or values.get('carried') == other):
            raise RuntimeError(f'p1.carried is {carried} after A')


def _cpu_held(state, p1_at):
    values = state.values
    if (values.get('p1.carried') != '1' or values.get('cpu.token') != 'held'
            or values.get('p1.coin') != 'placed' or values.get('p1.coin.at') != p1_at):
        raise RuntimeError(f'CPU coin or P1 coin changed while walking: {values.get("p1.carried")}, '
                           f'{values.get("cpu.token")}, {values.get("p1.coin")}, {values.get("p1.coin.at")}')


def walk_cpu(io, target, topology, cap=4, *, expected_context='training'):
    before, ui = _coin_baseline(io, cap, expected_context)
    if topology is None or topology.layout != before.values.get('layout'):
        raise RuntimeError('active layout has no validated topology for CPU placement')
    goals = [tile for tile, identity in topology.targets.items() if identity == target]
    if len(goals) != 1:
        raise RuntimeError('CPU target identity is absent or ambiguous in the active topology')
    goal = goals[0]
    p1_at = before.values.get('p1.coin.at')
    _cpu_held(before, p1_at)
    number(before, 'p1.coin.at')
    probes = 0
    while True:
        at = settled(before)
        if at == goal:
            if Target.read(before, 'carried.') != target:
                raise RuntimeError('CPU carried identity disagrees with target')
            return before, goal
        if at not in topology.neighbors:
            probes += 1
            if probes > 3:
                raise RuntimeError('CPU coin could not enter the validated topology')
            direction, expected = 'left', None
        else:
            if topology.targets.get(at) != Target.read(before, 'carried.'):
                raise RuntimeError('CPU carried identity disagrees with the active topology')
            direction, expected = topology.step(at, goal)
        prior_ack, prior_applied = number(before, 'ack'), number(before, 'applied')
        io.send('tile-' + direction)
        until = time.monotonic() + cap
        latest = before
        while True:
            latest = cpu_lines(wait_sample(io, TILE, until, latest, in_flight=True, stepping=True))
            check_context(before, latest, 'css')
            _cpu_held(latest, p1_at)
            values = latest.values
            ack, applied = number(latest, 'ack'), number(latest, 'applied')
            if (ack, applied) == (prior_ack, prior_applied):
                continue
            if (ack, applied) != (prior_ack + 1, prior_applied + 1):
                raise RuntimeError('wrong CPU tile step ACK/application delta')
            dest = values.get('to')
            if (values.get('direction'), values.get('from'), values.get('ack.result'), values.get('result')) != (
                    direction, at, 'handled', 'handled') or (expected is not None and dest != expected):
                raise RuntimeError(f'wrong CPU tile step: {direction} {at} -> {dest}')
            if values.get('settled') != '1':
                continue
            if dest not in topology.neighbors or settled(latest, after_step=True) != dest:
                raise RuntimeError('CPU tile step did not settle on a qualified topology identity')
            identity = topology.targets.get(dest)
            if identity is None:
                raise RuntimeError('CPU tile step has no qualified topology identity')
            parts = tuple(values.get('carried.' + key, 'unknown') for key in ('kind', 'name', 'form'))
            expected_identity = (identity.kind, identity.name, identity.form)
            if any(actual != 'unknown' and actual != wanted for actual, wanted in zip(parts, expected_identity)):
                raise RuntimeError('CPU tile step did not settle on a qualified topology identity')
            if 'unknown' in parts:
                continue
            _coin_finish(io, ui, until, expected_context)
            break
        before, ui = _coin_baseline(io, cap, expected_context)
        _cpu_held(before, p1_at)
        if (before.values.get('at') != latest.values.get('at')
                or number(before, 'ack') != number(latest, 'ack')
                or number(before, 'applied') != number(latest, 'applied')):
            raise RuntimeError('CPU tile changed before the next input')


def place_cpu(io, target, tile, p1_before, cap=4, *, expected_context='training'):
    before, ui = _coin_baseline(io, cap, expected_context)
    p1_at = before.values.get('p1.coin.at')
    _cpu_held(before, p1_at)
    if (p1_at != p1_before[1] or settled(before) != str(tile)
            or Target.read(before, 'carried.') != target):
        raise RuntimeError('pre-A carried identity changed')
    io.send('tap-a1')
    until = time.monotonic() + cap
    latest = before
    while True:
        try:
            latest = cpu_lines(wait_sample(io, TILE, until, latest, in_flight=True, stepping=True,
                                           pending_placement=True))
        except RuntimeError as exc:
            if str(exc) == f'{TILE}: no newer active observation' and time.monotonic() >= until:
                raise RuntimeError(f'A did not place the CPU coin within {cap} s') from exc
            raise
        check_context(before, latest, 'css')
        if (number(latest, 'ack') != number(before, 'ack')
                or number(latest, 'applied') != number(before, 'applied')):
            raise RuntimeError('a tile step arrived while placing the CPU coin')
        values = latest.values
        if values.get('cpu.token') == 'placed':
            identity = tuple(values.get('cpu.placed.' + key) for key in ('at', 'kind', 'name', 'form'))
            if (identity != (str(tile), target.kind, target.name, target.form)
                    or p1_medal(latest) != p1_before or values.get('carried') != 'none'
                    or values.get('p1.coin') != 'placed' or values.get('p1.coin.at') != p1_at):
                raise RuntimeError(f'cpu.placed is {identity!r}, not the target')
            _coin_finish(io, ui, until, expected_context)
            return latest
        if (values.get('p1.coin') not in ('placed', 'unknown')
                or values.get('p1.coin.at') not in (p1_at, 'unknown')):
            raise RuntimeError('P1 coin changed while placing the CPU coin')
        for key, wanted in zip(('at', 'kind', 'name', 'form'), p1_before[1:]):
            if values.get('p1.placed.' + key, 'unknown') not in ('unknown', wanted):
                raise RuntimeError('P1 coin changed while placing the CPU coin')
        if values.get('cpu.token') == 'held':
            if values.get('p1.carried') not in ('1', 'unknown'):
                raise RuntimeError('CPU coin changed hands while placing')
            if values.get('at') not in (str(tile), 'unknown'):
                raise RuntimeError('CPU coin changed tile while placing')
            parts = tuple(values.get('carried.' + key, 'unknown') for key in ('kind', 'name', 'form'))
            expected_identity = (target.kind, target.name, target.form)
            if any(actual != 'unknown' and actual != wanted for actual, wanted in zip(parts, expected_identity)):
                raise RuntimeError('CPU carried identity changed while placing')


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
