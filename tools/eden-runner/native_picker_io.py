"""Runner adapter for the native picker, using the shared state block parser."""

import re
import time

import native_picker
import state_gate


PROVIDERS_FENCE = re.compile(r'\[Bridge\] req providers result=ok count=\d+ boot=(0x[0-9a-f]{16})(?: |$)')
REPLAY = re.compile(r'\[Bridge\] (?:req )?events .*result=gap(?: |$)')


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

    def _receive(self):
        rotations = self.cursor.rotations
        lines = self.cursor.read()
        if self.cursor.rotations != rotations:
            raise RuntimeError('native picker bridge log rotated or truncated')
        if self.in_flight and any(state_gate.BOUNDARY.search(line) or REPLAY.search(line) for line in lines):
            raise RuntimeError('bridge connection or replay continuity changed while input was unconfirmed')
        return lines

    def _boundary(self, line):
        if state_gate.BOUNDARY.search(line) or REPLAY.search(line):
            self.connection += 1
            return True
        return False

    def _request(self, requests, cap, consume):
        # Discard observations logged before this command, but keep connection
        # changes. The in-band reply fence excludes unsolicited/replayed state.
        for line in self._receive():
            self._boundary(line)
        until = time.monotonic() + cap
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
            time.sleep(.05)
        raise RuntimeError(f'no fenced bridge reply for {requests[-1]}')

    def arm(self, provider, cap):
        self.in_flight = False
        watch = re.compile(r'\[Bridge\] req state_watch name=' + re.escape(provider) + r' result=(ok|error)(?: |$)')
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
                self.boot = fence
                return True
            return None

        self._request(('providers', f'state_watch {provider} keys=observation'), cap, consume)

    def read(self, provider, cap):
        fence = None
        frame = []

        def consume(line, boundary):
            nonlocal fence
            if boundary:
                fence = None
                frame.clear()
            if matched := PROVIDERS_FENCE.search(line):
                fence = matched[1]
                frame.clear()
                return None
            if not fence:
                return None
            if f'[Bridge] state name={provider} result=error ' in line:
                raise RuntimeError(f'{provider}: {line}')
            if state_gate.BEGIN.search(line):
                frame.clear()
            if state_gate.BEGIN.search(line) or frame:
                frame.append(line)
            if not state_gate.END.search(line):
                return None
            found = state_gate.latest('\n'.join(frame), provider)
            frame.clear()
            if found and not found['filtered'] and found['boot'] == fence:
                if self.boot is not None and fence != self.boot:
                    raise RuntimeError('native picker bridge boot changed')
                self.boot = fence
                return native_picker.Sample(found, self.connection)
            return None

        return self._request(('providers', f'state {provider}'), cap, consume)

    def send(self, segment):
        # Process any boundary written after the query, before the one input.
        self.in_flight = True
        self._receive()
        self.routes.play(self.ctx, segment)

    def log(self, evidence):
        self.ctx.log(evidence)