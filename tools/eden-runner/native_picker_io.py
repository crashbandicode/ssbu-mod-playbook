"""Runner adapter for the native picker, using the shared state block parser."""

from dataclasses import dataclass
import re
import time

import native_picker
import state_gate


PROVIDERS_FENCE = re.compile(r'\[Bridge\] req providers result=ok count=\d+ boot=(0x[0-9a-f]{16})(?: |$)')
UI_PROVIDER_GEN = re.compile(r'\[Bridge\] provider name=ssbu\.ui schema_id=\d+ version=\d+ rev=\d+ dropped=\d+ toggle_gen=(\d+)')
STALE_UI_REPLY = re.compile(r'\[Bridge\] state name=ssbu\.ui result=empty reason=stale_toggle boot=(0x[0-9a-f]{16})')
STALE_UI = object()
STALE_UI_RETRY_S = .1
REPLAY = re.compile(r'\[Bridge\] since result=gap(?: |$)')
EVENT_CLOCK = re.compile(r' ev=(\d+) frame=(\d+) wall=(\d+)$')
EVENTS_ON = re.compile(r'\[Bridge\] req events result=ok on=1 latest=(\d+) frame=(\d+) wall=(\d+)$')
SINCE_END = re.compile(
    r'\[Bridge\] since result=ok cursor=(\d+) from=(\d+) oldest=\d+ latest=(\d+) count=(\d+)'
    r' boot=(0x[0-9a-f]{16}) frame=\d+ wall=\d+$'
)
STAGE_CHANGE = re.compile(r'\[Bridge\] stage id=(-?\d+)' + EVENT_CLOCK.pattern)
RING_LOSS = re.compile(r'\[Bridge\] lost lines=')


@dataclass
class _StageWindow:
    boot: str
    cursor: int
    frame: int
    wall_ms: int
    before_press: int

    def observe_before_press(self, line):
        if RING_LOSS.search(line):
            raise RuntimeError('bridge event continuity was lost during Training launch')
        if match := EVENT_CLOCK.search(line):
            self.before_press = max(self.before_press, int(match[1]))

    def stage_from(self, events):
        for sequence, line in sorted(events.items(), reverse=True):
            if sequence <= self.before_press:
                break
            if match := STAGE_CHANGE.search(line):
                stage, _, frame, wall_ms = map(int, match.groups())
                # Replay keeps the producer clock, even if an old ring line was drained late.
                if frame >= self.frame and wall_ms > self.wall_ms:
                    if stage >= 0:
                        return stage, {'sequence': sequence, 'frame': frame, 'wall_ms': wall_ms, 'boot': self.boot}
                    return None
        return None


class BridgePickerIO:
    def __init__(self, ctx):
        import routes

        self.routes = routes
        self.ctx = ctx
        try:
            path = routes.instance_path(ctx, 'bridge_log')
        except KeyError as exc:
            raise RuntimeError('native tile selection needs a bridge_log for this instance') from exc
        self.cursor = routes.LogCursor(path, hold=False)
        self.connection = 0
        self.boot = None
        self.in_flight = False
        self.stage_window = None
        self.pending = getattr(ctx.runner, 'native_ui_pending', {})
        ctx.runner.native_ui_pending = self.pending
        if ctx.on in self.pending:
            raise RuntimeError(f'native input {self.pending[ctx.on]!r} remains unconfirmed on {ctx.on}')

    def _receive(self):
        rotations = self.cursor.rotations
        lines = self.cursor.read()
        if self.cursor.rotations != rotations:
            raise RuntimeError('native picker bridge log rotated or truncated')
        if self.in_flight and any(state_gate.BOUNDARY.search(line) or REPLAY.search(line) for line in lines):
            raise RuntimeError('bridge connection or replay continuity changed while input was unconfirmed')
        if self.stage_window is not None and any(RING_LOSS.search(line) for line in lines):
            raise RuntimeError('bridge event continuity was lost during Training launch')
        return lines

    def _boundary(self, line):
        if state_gate.BOUNDARY.search(line) or REPLAY.search(line):
            self.connection += 1
            return True
        return False

    def _request(self, requests, cap, consume, *, until=None, connection=None):
        # Discard observations logged before this command, but keep connection
        # changes. The in-band reply fence excludes unsolicited/replayed state.
        for line in self._receive():
            self._boundary(line)
        if connection is not None and self.connection != connection:
            raise RuntimeError('bridge connection or replay continuity changed during native UI read')
        if until is None:
            until = time.monotonic() + cap
        if time.monotonic() < until:
            self.routes.bridge_v2(self.ctx, *requests)
        while time.monotonic() < until:
            self.ctx.check_stop()
            answer = None
            for line in self._receive():
                boundary = self._boundary(line)
                candidate = consume(line, boundary)
                if candidate is not None:
                    answer = candidate
                if boundary:
                    answer = None
            if answer is not None:
                return answer
            time.sleep(min(.05, max(0, until - time.monotonic())))
        raise RuntimeError(f'no fenced bridge reply for {requests[-1]}')

    def arm(self, provider, cap):
        watch = re.compile(
            r'\[Bridge\] req state_watch name=' + re.escape(provider) + r' result=(ok|error)(?: |$)'
        )
        fence = None

        def consume(line, boundary):
            nonlocal fence
            if boundary:
                fence = None
            if matched := PROVIDERS_FENCE.search(line):
                fence = matched[1]
            if fence and (matched := watch.search(line)):
                if matched[1] == 'error':
                    raise RuntimeError(f'{provider}: {line}')
                if not re.search(r' keys=1 max=1(?: |$)', line):
                    raise RuntimeError(f'{provider}: unexpected watch limit: {line}')
                self.boot = fence
                return True
            return None

        # Route decisions use explicit full reads. Let the arming watch expire
        # after one block even if an input or its confirmation fails.
        self._request(('providers', f'state_watch {provider} keys=observation max=1'), cap, consume)

    def read(self, provider, cap):
        fence = None
        catalog_generation = fence_generation = None
        frame = []
        pending_reply = False
        retry_epoch = None
        awaiting_retry = False
        native_ui = provider == native_picker.UI

        def consume(line, boundary):
            nonlocal fence, catalog_generation, fence_generation, pending_reply, retry_epoch, awaiting_retry
            if boundary:
                if retry_epoch is not None:
                    raise RuntimeError('bridge connection or replay continuity changed during native UI read')
                fence = None
                catalog_generation = fence_generation = None
                frame.clear()
            if awaiting_retry:
                return None
            payload = state_gate.SUFFIX.sub('', line) if native_ui else line
            if native_ui and (matched := UI_PROVIDER_GEN.fullmatch(payload)):
                catalog_generation = matched[1]
            if matched := PROVIDERS_FENCE.search(line):
                if pending_reply:
                    raise RuntimeError(f'{provider}: overlapping fence for unfinished state reply')
                fence = matched[1]
                fence_generation, catalog_generation = catalog_generation, None
                if retry_epoch is not None:
                    boot, _, generation = retry_epoch
                    if fence != boot:
                        raise RuntimeError('native picker bridge boot changed during native UI read')
                    if generation is not None and fence_generation != generation:
                        raise RuntimeError('bridge toggle generation changed during native UI read')
                frame.clear()
                return None
            if not fence:
                return None
            if f'[Bridge] state name={provider} result=error ' in line:
                raise RuntimeError(f'{provider}: {line}')
            if native_ui and (matched := STALE_UI_REPLY.fullmatch(payload)):
                if pending_reply:
                    raise RuntimeError(f'{provider}: overlapping explicit state reply')
                if not frame and matched[1] == fence:
                    if self.boot is not None and fence != self.boot:
                        raise RuntimeError('native picker bridge boot changed')
                    retry_epoch = (fence, self.connection, fence_generation)
                    awaiting_retry = True
                    return STALE_UI
            begin = state_gate.BEGIN.search(line)
            if begin:
                if pending_reply:
                    raise RuntimeError(f'{provider}: overlapping explicit state reply')
                pending_reply = self.in_flight and begin[1] == provider and begin[6] == '0'
                if pending_reply and begin[4] != fence:
                    raise RuntimeError(f'{provider}: explicit state reply boot disagrees with its fence')
                frame.clear()
            if begin or frame:
                frame.append(line)
            if not state_gate.END.search(line):
                return None
            found = state_gate.latest('\n'.join(frame), provider)
            frame.clear()
            if pending_reply:
                pending_reply = False
                if found is None:
                    raise RuntimeError(f'{provider}: malformed explicit state reply')
            if found and not found['filtered'] and found['boot'] == fence:
                if self.boot is not None and fence != self.boot:
                    raise RuntimeError('native picker bridge boot changed')
                self.boot = fence
                return native_picker.Sample(found, self.connection)
            return None

        requests = ('providers', f'state {provider}')
        if not native_ui:
            return self._request(requests, cap, consume)

        until = time.monotonic() + cap
        while True:
            connection = None if retry_epoch is None else retry_epoch[1]
            answer = self._request(requests, cap, consume, until=until, connection=connection)
            if answer is not STALE_UI:
                return answer
            time.sleep(min(STALE_UI_RETRY_S, max(0, until - time.monotonic())))
            self.ctx.check_stop()
            awaiting_retry = False
            fence = None
            catalog_generation = fence_generation = None

    def mark_training_stage(self, before, cap):
        """Fence the bridge event history while the checked native CSS still owns the route."""
        fence = None
        window = None

        def consume(line, boundary):
            nonlocal fence, window
            if boundary:
                fence = window = None
            if match := PROVIDERS_FENCE.search(line):
                fence = match[1]
            if fence and (match := EVENTS_ON.search(line)):
                if fence != before.block['boot'] or self.connection != before.connection:
                    raise RuntimeError('bridge connection changed before Training launch')
                seq, frame, wall_ms = map(int, match.groups())
                window = _StageWindow(fence, seq, frame, wall_ms, seq)
                return window
            if window is not None:
                window.observe_before_press(line)
            return None

        self.stage_window = self._request(('providers', 'events on'), cap, consume)

    def training_stage(self, cap):
        """Read an exact replay through the current ring head after native Training entry."""
        window = self.stage_window
        until = time.monotonic() + cap
        while (remaining := until - time.monotonic()) > 0:
            fence = None
            events = {}

            def consume(line, _boundary):
                nonlocal fence
                if match := PROVIDERS_FENCE.search(line):
                    fence = match[1]
                    events.clear()
                if fence is None:
                    return None
                if fence != window.boot:
                    raise RuntimeError('bridge boot changed during Training stage proof')
                if match := EVENT_CLOCK.search(line):
                    sequence = int(match[1])
                    if sequence > window.cursor and events.setdefault(sequence, line) != line:
                        raise RuntimeError('conflicting stage event replay')
                if match := SINCE_END.search(line):
                    cursor, start, latest, count = map(int, match.groups()[:4])
                    if cursor != window.cursor or start != cursor + 1 or match[5] != window.boot:
                        raise RuntimeError('wrong Training stage replay fence')
                    through_reply = {seq: event for seq, event in events.items() if seq <= latest}
                    if latest < cursor or count != latest - cursor or len(through_reply) != count:
                        raise RuntimeError('incomplete stage event replay')
                    return (window.stage_from(through_reply),)
                return None

            result, = self._request(('providers', f'since {window.cursor}'), remaining, consume)
            if result is not None:
                return result
            time.sleep(min(.05, max(0, until - time.monotonic())))
        raise RuntimeError('no fresh gameplay stage event after Training launch')

    def send(self, segment):
        # Process any boundary written after the query, before the one input.
        self.in_flight = True
        lines = self._receive()
        if self.stage_window is not None:
            for line in lines:
                self.stage_window.observe_before_press(line)
        self.pending[self.ctx.on] = segment
        self.routes.play(self.ctx, segment)

    def finish(self):
        self.pending.pop(self.ctx.on, None)
        self.in_flight = False
        self.stage_window = None

    def log(self, evidence):
        self.ctx.log(evidence)
