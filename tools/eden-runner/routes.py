"""Shared Eden setup routes: proven building blocks every lane reuses instead of hand-building a boot route.

Each public function is a runner step hook (rows.json: {"run": "routes:<function>", "args": {...}, "check": true})
that reaches a named game state, proves it with a game-state or image gate, and returns a small receipt. Waits poll
game state (Eden status, a plugin log line, a tile-cursor line, an image gate) with a cap; there is no fixed sleep
where a state exists. Every block fails closed: a missing state raises with what it saw, and nothing guesses.

Blocks (docs/ROUTES.md has what each guarantees on exit):
  configure_boot   mute, memory_layout_mode 0, optional GDB stub on, saved and read back, before game.load
  load_game        game.load, the no-hold GDB kick before any wait, then status loading_complete and drawing
  wait_lines       the boot lines the plugins print (instead of a fixed post-load sleep)
  to_main          title or attract demo or How-to-Play video -> main menu (Start promptly, image gate, capped)
  main_to_stage    main menu -> Training's stage select
  pick_stage       stage select -> the named stage's tile (tile cursor L3+D-pad macros, at == to) -> A -> CSS
  pick_fighter     CSS: P1 token onto the named fighter's tile (hovered == to) and placed
  enter_training   CSS ready -> Training, waiting for the plugin's Training line
  chord            play a same-frame chord macro (reset L+R+A, TR save/load, tile steps) by name
  fixture          read fighters, stage, damage and positions from game state, assert them, reset, settle
  back_to_main     Training -> main menu, proven by the main gate
Online pair blocks are in routes_online.py.

Library files: library/routes/index.json (macros with sha256 and provenance) and library/routes/tables.json
(stage tiles and fighter kinds, each with its source). Rows don't copy them.
"""
import hashlib
import json
import os
import re
import socket
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUTES = HERE / 'library' / 'routes'
LIBRARY = HERE / 'library'


# --- small utilities ------------------------------------------------------------------------------------------------

def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _save(ctx, name, value):
    (Path(ctx.row_dir) / f'{name}.json').write_text(json.dumps(value, indent=1, default=str) + '\n')
    return value


def routes_index():
    return json.loads((ROUTES / 'index.json').read_text())


def tables():
    return json.loads((ROUTES / 'tables.json').read_text())


def instance_path(ctx, key):
    """An instances.json file by key ("tile" finds tile_log, like the runner's gate sources)."""
    inst = ctx.instance
    if key.startswith('sdmc:') and inst.get('profile'):
        return Path(inst['profile']) / 'user' / 'sdmc' / key[5:]  # e.g. sdmc:ultimate/TrainingRooms/events.log
    for name in (key, key + '_log', key[:-4] if key.endswith('_log') else None):
        if name and name in inst and isinstance(inst[name], str):
            return Path(inst[name])
    raise KeyError(f'instance has no file {key!r} (keys: {sorted(k for k in inst if isinstance(inst[k], str))})')


# --- a log reader that follows rotation -----------------------------------------------------------------------------

class LogCursor:
    """Read the lines a file gains from now on, across rotation and truncation.

    The cursor keeps the open handle of the file it started on. When the path is renamed away (rotation: a new inode
    at the path), it first drains what the old handle still has, then opens the new file from its start. When the
    same file shrinks (truncation), it reads from 0. Lines are returned whole; a partial last line waits for its
    newline. `mark()` restarts from the current end, so a wait sees only lines written after the input it follows.
    """

    def __init__(self, path, from_start=False, hold=None):
        self.path = Path(path)
        self.handle = None
        self.inode = None
        self.offset = 0
        self.pending = b''
        self.backlog = []
        self.rotations = 0
        # On Windows an open handle without FILE_SHARE_DELETE would make the plugin's own rotation (rename) fail, so
        # the cursor opens, reads and closes each time; lines written to the old file after the last read and before
        # its rename are then lost. Poll often (wait() polls every 0.1 s).
        self.hold = (os.name != 'nt') if hold is None else hold
        self._open(seek_end=not from_start)

    def _open(self, seek_end):
        if not self.hold:
            try:
                stat = os.stat(self.path)
            except OSError:
                self.inode, self.offset = None, 0
                return
            self.inode, self.offset = stat.st_ino, (stat.st_size if seek_end else 0)
            return
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        try:
            self.handle = open(self.path, 'rb')
        except OSError:
            self.inode = None
            return
        self.inode = os.fstat(self.handle.fileno()).st_ino
        if seek_end:
            self.handle.seek(0, os.SEEK_END)

    def mark(self):
        self.pending = b''
        self.backlog = []
        self._open(seek_end=True)

    def _read_unheld(self):
        try:
            now = os.stat(self.path)
        except OSError:
            return b''
        if self.inode is None or now.st_ino != self.inode or now.st_size < self.offset:
            if self.inode is not None:
                self.rotations += 1
            self.inode, self.offset = now.st_ino, 0
        try:
            with open(self.path, 'rb') as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return b''
        self.offset += len(data)
        return data

    def read(self):
        if not self.hold:
            data = self.pending + self._read_unheld()
            lines = data.split(b'\n')
            self.pending = lines.pop()
            return [line.decode('utf-8', 'replace').rstrip('\r') for line in lines if line.strip()]
        chunks = []
        if self.handle is None:
            self._open(seek_end=False)
            if self.handle is None:
                return []
        try:
            now = os.stat(self.path)
        except OSError:
            now = None
        chunks.append(self.handle.read())
        if now is not None and now.st_ino != self.inode:
            self.rotations += 1
            self._open(seek_end=False)  # the new file: everything in it is new
            if self.handle is not None:
                chunks.append(self.handle.read())
        elif now is not None and now.st_size < self.handle.tell():
            self.rotations += 1
            self.handle.seek(0)
            chunks.append(self.handle.read())
        data = self.pending + b''.join(chunks)
        lines = data.split(b'\n')
        self.pending = lines.pop()
        return [line.decode('utf-8', 'replace').rstrip('\r') for line in lines if line.strip()]

    def wait(self, pattern, cap, ctx=None, interval=0.1):
        """The first new line matching pattern (a regex), or RuntimeError after cap seconds with the lines seen."""
        regex = re.compile(pattern)
        seen = []
        until = time.monotonic() + cap
        while True:
            if ctx is not None:
                ctx.check_stop()
            lines = self.backlog + self.read()
            self.backlog = []
            for index, line in enumerate(lines):
                seen.append(line)
                found = regex.search(line)
                if found:
                    self.backlog = lines[index + 1:]  # later lines stay for the next wait
                    return found
            if time.monotonic() >= until:
                raise RuntimeError(f'no line matching {pattern!r} in {self.path} within {cap} s; '
                                   f'last lines: {seen[-6:]}')
            time.sleep(interval)

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


# --- gates and macros -----------------------------------------------------------------------------------------------

# Gates the routes check. Each starts from a proven library entry (library/index.json) and adds what made it pass on
# every host: "scale": "fit" with the 1280x720 frame the ROI was captured on (the XPS renders at 0.5x), and the
# tolerance the passing remaining-2 boot used (main/stage 18, css 20, Training HUD 18: the XPS docs-lb4 value; the
# Yoga and Surface passed at 8). A plan gate named "routes.<name>" overrides the route's own.
ROUTE_GATES = {
    'main': {'library': 'main', 'tolerance': 18, 'cap': 8},
    'stage': {'library': 'stage', 'tolerance': 18, 'cap': 45},
    'css': {'library': 'css', 'tolerance': 20, 'cap': 60},
    'training': {'library': 'training-hud-unpaused', 'tolerance': 18, 'cap': 30},
    'native-pause': {'library': 'native-pause', 'tolerance': 18, 'cap': 15},
    'quit-focus': {'library': 'quit-focus', 'tolerance': 18, 'cap': 15},
    'loaded': {'library': 'loaded'},
}


def _gate_spec(name):
    spec = dict(ROUTE_GATES[name])
    entry = json.loads((LIBRARY / 'index.json').read_text())['gates'][spec.pop('library')]
    merged = dict(entry)
    if 'reference' in merged:
        merged['reference'] = str(LIBRARY / merged['reference'])
        merged.setdefault('scale', 'fit')
        merged.setdefault('frame', [1280, 720])
    merged.update(spec)
    return merged


def gate(ctx, name, cap=None):
    """Check a route gate through the runner (receipts, screenshots, failure evidence all apply)."""
    key = f'routes.{name}'
    gates = ctx.runner.plan.setdefault('gates', {})
    if key not in gates:
        gates[key] = _gate_spec(name)
    if desktop_shots(ctx) and name in RECORDER_GATES:
        return RECORDER_GATES[name](ctx, cap or gates[key].get('cap', 30))
    if cap is None:
        return ctx.gate(key)
    # A short look must not change the gate's own cap for later callers (harness-lib-xps-1 r05, 03:00Z: a 3 s look
    # in back_to_main left routes.css at 3 s, and the CSS load after the stage pick outlasted it).
    saved = gates[key]
    gates[key] = {**saved, 'cap': cap}
    try:
        return ctx.gate(key)
    finally:
        gates[key] = saved


def desktop_shots(ctx):
    """True where ui.screenshot grabs the whole desktop (the Ally: screenshot_target "screen"; a window grab is black
    under Vulkan there). Image gates then read a game frame from Eden's recorder instead (frame())."""
    return ctx.instance.get('screenshot_target') == 'screen'


def frame(ctx, name, cap=10.0):
    """One game frame as a PNG in the row folder, from Eden's GIF recorder (game pixels only, 1280 wide)."""
    from PIL import Image
    stamp = time.strftime('%H%M%S', time.gmtime()) + f'-{time.time_ns() % 10**6:06d}'
    gif = Path(ctx.row_dir) / f'{ctx.on}-{stamp}-{name}.frame.gif'
    png = gif.with_suffix('').with_suffix('.png')
    ctx.call('recording.start', {'path': str(gif), 'fps': 10, 'width': 1280, 'duration_ms': 300})
    until = time.monotonic() + cap
    while True:
        ctx.check_stop()
        status = ctx.call('recording.status', {}) or {}
        if isinstance(status, dict) and (status.get('state') == 'error' or status.get('error')):
            raise RuntimeError(f'recording for frame {name}: {status}')
        if gif.is_file() and (not isinstance(status, dict) or status.get('state') != 'recording'):
            try:
                with Image.open(gif) as im:
                    im.convert('RGB').save(png)
                gif.unlink(missing_ok=True)
                return png
            except OSError:
                pass  # still being finalised
        if time.monotonic() > until:
            raise RuntimeError(f'no recorder frame for {name} within {cap} s: {status}')
        time.sleep(0.2)


def _frame_gate(ctx, key, spec, cap):
    """An image gate on recorder frames: the same reference, ROI, frame size, scale and tolerance as the gate."""
    reference = Path(spec['reference'])
    tolerance = spec.get('tolerance', 8)
    until = time.monotonic() + cap
    last = None
    while True:
        png = frame(ctx, 'gate-' + key)
        diff, note = ctx.runner.compare_region(png, reference, spec['roi'], spec.get('reference_is_roi'),
                                               spec.get('scale') or 'fit', spec.get('frame'), tolerance)
        if diff <= tolerance:
            ctx.log({'gate': key, 'on': ctx.on, 'pass': True, 'frame': str(png), 'diff': round(diff, 2),
                     **(note or {})})
            if last is not None:
                last.unlink(missing_ok=True)
            return str(png)
        if last is not None:
            last.unlink(missing_ok=True)
        last = png
        if time.monotonic() > until:
            raise RuntimeError(f'gate {key} on {ctx.on} did not pass within {cap} s (recorder frames; last diff '
                               f'{diff:.1f} > {tolerance}; {png})')
        time.sleep(0.3)


# Windows (recorder frames). The library references were cut from 1280x674 window grabs, so they don't match a true
# 1280x720 recorder frame (harness-lib-ally-1 r03, 03:51Z: the main menu was on screen, mean difference 113). The
# main menu has its own recorder reference, cut from that boot's verified frame. The selects are recognised by the
# tile cursor's screen name, and Training by fresh game-state lines. The pause and quit frames are kept as evidence;
# leaving Training is proven by the select or the main menu that follows.
RECORDER_MAIN = {'kind': 'image', 'reference': str(ROUTES / 'refs' / 'main-recorder.png'),
                 # The sidebar icons: the same whichever main tile is highlighted (r08: back from the selects,
                 # Games & More was highlighted and a Smash-tile crop missed it).
                 'sha256': '1f11f1a2355a14dca5aa1c6170d5f57c0619a77caac63c6af4ecc201af195b1b',
                 'roi': [1150, 160, 1230, 640], 'reference_is_roi': True, 'frame': [1280, 720], 'scale': 'fit',
                 'tolerance': 18, 'from_run': 'harness-lib-ally-1'}


def _until(cap, check):
    until = time.monotonic() + cap
    while True:
        found = check()
        if found:
            return found
        if time.monotonic() > until:
            return None
        time.sleep(0.3)


def _recorder_main(ctx, cap):
    return _frame_gate(ctx, 'routes.main-recorder', RECORDER_MAIN, cap)


def _probe_gate(screen):
    def check(ctx, cap):
        if _until(cap, lambda: select_screen(ctx, cap=min(2.5, cap)) == screen) is None:
            raise RuntimeError(f'gate routes.{screen}: no tile-cursor step on screen={screen} within {cap} s')
        return str(frame(ctx, f'gate-routes.{screen}'))
    return check


def _training_state(ctx, cap):
    try:
        read_state(ctx, cap=min(cap, 6.0))
    except RuntimeError as error:
        raise RuntimeError(f'gate routes.training: {error}') from None
    return str(frame(ctx, 'gate-routes.training'))


def _evidence_only(name):
    def check(ctx, cap):
        path = frame(ctx, f'evidence-{name}')
        ctx.log({'gate': f'routes.{name}', 'on': ctx.on, 'checked': False, 'frame': str(path),
                 'why': 'no recorder reference; the next screen proves the step'})
        return str(path)
    return check


RECORDER_GATES = {'main': _recorder_main, 'css': _probe_gate('css'), 'stage': _probe_gate('sss'),
                  'training': _training_state, 'native-pause': _evidence_only('native-pause'),
                  'quit-focus': _evidence_only('quit-focus')}


def passes(ctx, name, cap):
    """True when the gate passes within cap, False when it does not (no exception; the caller decides)."""
    try:
        gate(ctx, name, cap=cap)
        return True
    except RuntimeError:
        return False


def macro_file(name):
    """(path, sha256, cap) of a route macro, checked against library/routes/index.json, or a library segment."""
    index = routes_index()['macros']
    if name in index:
        entry = index[name]
        path = ROUTES / entry['file']
    else:
        entry = json.loads((LIBRARY / 'index.json').read_text())['segments'][name]
        path = LIBRARY / entry['file']
    if _sha256(path) != entry['sha256']:
        raise RuntimeError(f'route macro {name}: {path} does not match its sha256 {entry["sha256"][:12]}')
    return path, entry['sha256'], entry.get('cap', 30)


def play(ctx, name):
    """Play one macro by name (same-frame chords live in these files, never as separate presses) and wait for it."""
    path, sha, cap = macro_file(name)
    ctx.call('macro.play', {'path': str(path)})
    ctx.runner.wait_macro(ctx.on, cap)
    ctx.log({'route_macro': name, 'sha256': sha})
    return sha


# What TrainingRooms' diag build prints for its chords (states.rs:1226 and :821). A chord with an expectation waits for
# the line written after the press, so a chord that never registered fails instead of passing on its input.
CHORD_LINES = {
    # The save logs one `state saved` line, for the last entry it wrote (harness-lib-xps-1 r07: `chords_passed
    # bits=0x1`, `state saved_notice slot=1`, `state saved slot=1 entry=1 (file queued)`), so any entry counts.
    'tr-save': ('diag_log', r'\[TrainingRooms\] state saved slot=\d+ entry=\d+'),
    'tr-load': ('diag_log', r'\[TrainingRooms\] state placed entry=\d+ x=(-?[\d.]+) y=(-?[\d.]+) lr=(-?\d+)'),
}


def chord(ctx, name, expect=None, source=None, cap=10):
    """Step hook: press a named same-frame chord (reset-lra, tr-save, tr-load, tile-up, ...) and, when the chord has
    a known diag line (or `expect` is given), wait for that line after the press."""
    source, expect = (source, expect) if expect else CHORD_LINES.get(name, (None, None))
    cursor = LogCursor(instance_path(ctx, source)) if expect else None
    try:
        sha = play(ctx, name)
        line = cursor.wait(expect, cap, ctx).group(0) if cursor else None
    finally:
        if cursor:
            cursor.close()
    return {'chord': name, 'sha256': sha, 'line': line}


# --- boot -----------------------------------------------------------------------------------------------------------

class GdbStub:
    """Minimal GDB remote client (remaining-2 gdbdump.Stub): enough for '?' and 'c'."""

    def __init__(self, port, wait_s=10.0):
        until = time.monotonic() + wait_s
        while True:
            try:
                self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)
                break
            except OSError:
                if time.monotonic() > until:
                    raise
                time.sleep(0.5)
        self.sock.settimeout(30)
        self.buf = b''

    def send(self, payload):
        data = payload.encode('latin-1')
        self.sock.sendall(b'$' + data + b'#' + ('%02x' % (sum(data) & 0xff)).encode())

    def ask(self, payload):
        self.send(payload)
        while True:
            while self.buf[:1] in (b'+', b'-'):
                self.buf = self.buf[1:]
            found = re.search(rb'\$([^#]*)#([0-9a-fA-F]{2})', self.buf)
            if found:
                self.buf = self.buf[found.end():]
                self.sock.sendall(b'+')
                return found.group(1).decode('latin-1')
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError('stub closed the connection')
            self.buf += data

    def close(self):
        self.sock.close()


def _setting(ctx, key, value):
    ctx.call('settings.set', {'key': key, 'value': value})
    got = ctx.call('settings.get', {'key': key}) or {}
    if str(got.get('value')).lower() != str(value).lower():
        raise RuntimeError(f'setting {key} reads {got!r}, not {value!r}')
    return got.get('value')


def configure_boot(ctx, gdb_port=None, volume=0, memory_layout_mode=0):
    """Before game.load (the lifecycle's defer_load): mute, memory_layout_mode 0 and, with gdb_port, the GDB stub on.

    Every value is set, read back and saved. The stub must be on before game.load; tracing and the stub can't be on
    together (EDEN.md), so a boot with the stub reads game state from plugin logs, not trace points.
    """
    status = ctx.call('status') or {}
    if status.get('running'):
        raise RuntimeError('configure_boot runs before game.load; the game is already running')
    out = {'core.volume': _setting(ctx, 'core.volume', volume),
           'core.memory_layout_mode': _setting(ctx, 'core.memory_layout_mode', memory_layout_mode)}
    if gdb_port:
        out['core.gdbstub_port'] = _setting(ctx, 'core.gdbstub_port', int(gdb_port))
        out['core.use_gdbstub'] = _setting(ctx, 'core.use_gdbstub', True)
    ctx.call('settings.save', {})
    return _save(ctx, 'routes-configure-boot', out)


def _kick(port, cap):
    """The no-hold kick: connect when the stub answers, '?', 'c', close without a detach (remaining-2 gdb_rc11)."""
    until = time.monotonic() + cap
    error = None
    while time.monotonic() < until:
        try:
            stub = GdbStub(port, 5.0)
            first = stub.ask('?')
            stub.send('c')
            time.sleep(1.0)
            stub.close()
            return {'first': first[:120]}
        except OSError as exc:
            error = repr(exc)[:160]
            time.sleep(1.0)
    return {'error': error}


def _drawing(ctx, cap):
    until = time.monotonic() + cap
    status = {}
    while time.monotonic() < until:
        ctx.check_stop()
        status = ctx.call('status') or {}
        if (status.get('game_fps') or 0) > 1:
            return True, status
        if status.get('running') is False and status.get('loading_complete') is not None:
            break
        time.sleep(1.0)
    return False, status


def load_game(ctx, gdb_port=None, cap=900, min_fps=30):
    """game.load, then (stub on) the no-hold kick BEFORE any wait, then the game's own state: loading_complete, no
    shaders building, game_fps at least min_fps. Fails closed: a guest that never draws after three kicks, or Eden
    that stops running, raises with the polls it saw."""
    t0 = time.monotonic()
    ctx.call('game.load', {'path': ctx.instance['game']}, job_cap=240)
    out = {}
    if gdb_port:
        out['kick'] = [_kick(int(gdb_port), 60.0)]
        drawing, status = _drawing(ctx, 90.0)
        for _ in range(3):
            if drawing:
                break
            out['kick'].append(_kick(int(gdb_port), 15.0))
            drawing, status = _drawing(ctx, 60.0)
        if not drawing:
            _save(ctx, 'routes-load', out)
            raise RuntimeError(f'the guest did not draw after the direct continue: {out}')
    polls = []
    until = t0 + cap
    while True:
        ctx.check_stop()
        status = ctx.call('status') or {}
        polls.append({k: status.get(k) for k in ('running', 'loading_complete', 'shaders_building', 'game_fps')})
        if status.get('loading_complete') and not status.get('shaders_building') and (
                status.get('game_fps') or 0) >= min_fps:
            break
        if status.get('running') is False and len(polls) > 3:
            raise RuntimeError(f'Eden stopped running while loading: {polls[-3:]}')
        if time.monotonic() > until:
            raise RuntimeError(f'still loading after {cap} s: {polls[-3:]}')
        time.sleep(1.0)
    out.update(seconds=round(time.monotonic() - t0, 1), polls=len(polls), last=polls[-1])
    return _save(ctx, 'routes-load', out)


def wait_lines(ctx, lines, cap=60):
    """Wait for each [source, regex] pair, e.g. a plugin's install line, instead of a fixed post-load sleep.

    Reads the whole file (a boot line was written before this step began); rotation does not matter at boot.
    """
    found = {}
    until = time.monotonic() + cap
    for source, pattern in lines:
        regex = re.compile(pattern)
        while True:
            ctx.check_stop()
            path = instance_path(ctx, source)
            text = path.read_text(errors='replace') if path.is_file() else ''
            match = regex.search(text)
            if match:
                found[f'{source}:{pattern}'] = match.group(0)[:200]
                break
            if time.monotonic() > until:
                raise RuntimeError(f'no {pattern!r} in {path} within {cap} s')
            time.sleep(0.5)
    return _save(ctx, 'routes-boot-lines', found)


# --- menus ----------------------------------------------------------------------------------------------------------

def to_main(ctx, tries=8, look_s=6):
    """Title, opening movie, attract demo or How-to-Play video -> the main menu, with no idle gap.

    Looks for the main menu for look_s; when it isn't there, presses Start (the library's attract-cancel, one Plus)
    at once and looks again, up to `tries` times. Start does nothing on the main menu itself, so a press that lands
    there is harmless. The main menu falls back to the attract demo after about a minute idle (HARNESS-LESSONS), so
    every route block that starts on the main menu calls this first instead of trusting an older gate.
    """
    presses = 0
    for attempt in range(tries + 1):
        if passes(ctx, 'main', look_s):
            out = {'main': True, 'start_presses': presses, 'shot': str(ctx.shot('routes-main'))}
            ctx.log({'route': 'to_main', **out})
            return out
        if attempt < tries:
            play(ctx, 'attract-cancel')
            presses += 1
    raise RuntimeError(f'main menu not reached after {presses} Start presses')


def main_to_stage(ctx):
    """Main menu -> Training's stage select (library main-stage, lr-03). Calls to_main first: never trust a gate
    pinned to an earlier screen."""
    to_main(ctx)
    play(ctx, 'main-stage')
    how = wait_sss(ctx)
    return {'screen': 'stage', 'proof': how, 'shot': str(ctx.shot('routes-stage'))}


def wait_sss(ctx, cap=45):
    """The stage select, by the library `stage` gate (the Training name, shown while tile 0 is hovered) or, when
    another stage is hovered, by a tile-cursor step logged with screen=sss."""
    until = time.monotonic() + cap
    while True:
        if passes(ctx, 'stage', 3):
            return 'stage-gate'
        if select_screen(ctx) == 'sss':
            return 'tile-cursor screen=sss'
        if time.monotonic() > until:
            raise RuntimeError(f'the stage select did not appear within {cap} s')


# --- the tile cursor ------------------------------------------------------------------------------------------------

STEP = r'step screen={screen} dir={dir} from=(\S+) to=(\S+)(?: token=(\S+))?'
SETTLED = r'settled screen={screen} at=(\d+) hovered=(\S+)'


class TileWalker:
    """L3 + D-pad steps on the CSS or the SSS, each one a same-frame macro, each one proven by the tile cursor's log.

    A step is good when the log shows `step screen=<s> dir=<d> from=<a> to=<b>` and, unless b is `none` (an edge),
    `settled screen=<s> at=<b>`. On the CSS `hovered` must equal `at` too; on the SSS the game keeps the hover in Lua,
    so `hovered` is `none` (TILE-CURSOR.md).
    """

    def __init__(self, ctx, screen):
        self.ctx, self.screen = ctx, screen
        self.cursor = LogCursor(instance_path(ctx, 'tile_log'))
        self.trace = []
        self.at = None
        self.token = None

    def step(self, direction, cap=6):
        self.cursor.mark()
        play(self.ctx, f'tile-{direction}')
        stepped = self.cursor.wait(STEP.format(screen=self.screen, dir=direction), cap, self.ctx)
        source, target = stepped.group(1), stepped.group(2)
        record = {'dir': direction, 'from': source, 'to': target}
        if stepped.group(3):
            record['token'] = stepped.group(3)
        self.token = stepped.group(3)
        if target != 'none':
            settled = self.cursor.wait(SETTLED.format(screen=self.screen), cap, self.ctx)
            at, hovered = settled.group(1), settled.group(2)
            record.update(at=at, hovered=hovered)
            if at != target:
                raise RuntimeError(f'{self.screen} step {direction} logged to={target} but settled at={at}')
            if self.screen == 'css' and hovered != at:
                raise RuntimeError(f'css settled at={at} but the game hovers {hovered}')
            self.at = int(at)
        elif source != 'none':
            self.at = int(source)  # an edge: the hand stays where the step started
        self.trace.append(record)
        return target

    def to_edge(self, direction, limit=40):
        for _ in range(limit):
            if self.step(direction) == 'none':
                return self.at
        raise RuntimeError(f'{self.screen}: no edge after {limit} {direction} steps')

    def walk_to(self, target):
        """Top-left first, then Down while the row below still starts at or before the target, then Right.

        Row widths are read from the log (the tile below), never assumed: the CSS count depends on the save's DLC.
        """
        self.to_edge('up')
        self.to_edge('left')
        if self.at is None:
            raise RuntimeError(f'{self.screen}: the hand never settled on a tile (does it carry a token?)')
        while self.at != target:
            if self.at < target:
                before = self.at
                below = self.step('down')
                if below == 'none' or int(below) > target:
                    if below != 'none':
                        self.step('up')
                        if self.at != before:
                            raise RuntimeError(f'{self.screen}: down/up did not return to {before}: {self.trace[-2:]}')
                    while self.at < target:
                        if self.step('right') == 'none':
                            raise RuntimeError(f'{self.screen}: row edge before tile {target}: {self.trace[-3:]}')
                    if self.at != target:
                        raise RuntimeError(f'{self.screen}: passed tile {target}: {self.trace[-3:]}')
            else:
                raise RuntimeError(f'{self.screen}: at {self.at} past target {target}: {self.trace[-3:]}')
        return {'screen': self.screen, 'at': self.at, 'steps': len(self.trace), 'trace': self.trace}


# --- stage and fighters by name -------------------------------------------------------------------------------------

def _norm(name):
    return re.sub(r'[^a-z0-9]', '', str(name).lower())


def stage_entry(name):
    """The Training stage select tile for a stage name or native id, from tables.json (TrainingRooms' own table)."""
    rows = tables()['sss_training_tiles']
    for entry in rows:
        if _norm(entry['name']) == _norm(name) or str(entry['id']) == str(name):
            return entry
    raise KeyError(f'stage {name!r} is not in TrainingRooms lobby_stages (tables.json)')


def fighter_entry(name):
    """The CSS tile for a fighter name or native kind id, from tables.json (TrainingRooms' roster grid)."""
    for entry in tables()['css_tiles']:
        if 'kind' in entry and (_norm(entry['name']) == _norm(name) or str(entry['kind']) == str(name)
                                or str(name).lower() == hex(entry['kind'])):
            return entry
    raise KeyError(f'fighter {name!r} has no single CSS tile in TrainingRooms roster_grid (shared slots and Random '
                   'are not picked by kind)')


def pick_stage(ctx, stage, allow_derived=False):
    """Stage select -> the named stage, by tile-cursor steps, then A -> the character select.

    The target tile comes from tables.json. A tile no run has proven is refused unless allow_derived is true; the
    stage id is then confirmed from game state after the Training load (confirm_stage). The block re-checks the stage
    select first (never a gate from an earlier screen), and ends on the CSS gate. The old library stage-css segment
    is not used: its start gate is the Training name crop, which fails once the hand left tile 0 (class d).
    """
    entry = stage_entry(stage)
    if not entry.get('proven') and not allow_derived:
        raise RuntimeError(f'stage {entry["name"]} tile {entry["tile"]} is derived, not proven; pass allow_derived '
                           'and confirm the id after the load')
    wait_sss(ctx)
    walker = TileWalker(ctx, 'sss')
    walk = walker.walk_to(entry['tile'])
    still = str(ctx.shot(f'routes-sss-{_norm(entry["name"])}'))
    play(ctx, 'tap-a1')
    gate(ctx, 'css')
    expected = {'stage_id': entry['id'], 'stage': entry['name'], 'tile': entry['tile']}
    ctx.runner.routes_expect = {**getattr(ctx.runner, 'routes_expect', {}), **expected}
    return _save(ctx, 'routes-pick-stage', {**expected, 'walk': walk, 'still': still,
                                            'derived': not entry.get('proven')})


def _probe_step(ctx, walker, direction='left', cap=2.5):
    """One L3 step and its line, or None when the hand moved nothing (an empty hand logs no step)."""
    walker.cursor.mark()
    play(ctx, f'tile-{direction}')
    try:
        found = walker.cursor.wait(STEP.format(screen='css', dir=direction), cap, ctx)
    except RuntimeError:
        return None
    return {'from': found.group(1), 'to': found.group(2), 'token': found.group(3)}


def pick_fighters(ctx, p1, cpu=None, coin_route=False):
    """CSS: P1's token onto `p1` by TrainingRooms' tile table, checked by `settled at=N hovered=N`, then placed.

    The hand starts holding P1's token below the grid. The CPU's coin (`cpu` with coin_route=true) is NOT PROVEN: P1
    is placed on Steve, coin-pick pushes the empty hand onto the coin, A, and an L3 step must log token=p2; the coin
    is walked and dropped, B returns P1's token and P1 is walked. On the XPS the approach missed four times.
    """
    gate(ctx, 'css')
    p1_entry = fighter_entry(p1)
    cpu_entry = fighter_entry(cpu) if cpu is not None else None
    if cpu_entry is not None and not coin_route:
        # NOT PROVEN (harness-lib-xps-1, 2026-11-16 02:52-03:09Z): four analog approaches to the CPU's coin either
        # missed it or pressed A where the READY banner took it and started Training with a random CPU. Until a
        # route is proven, set the CPU with the lane's own fixture and assert it with routes:fixture.
        raise RuntimeError('pick_fighters: the CSS coin route for the CPU is not proven; pass coin_route=true only to '
                           'test it, or set the CPU by fixture and assert it with routes:fixture {"cpu": ...}')
    random_tile = next(e['tile'] for e in tables()['css_tiles'] if e.get('random'))
    walker = TileWalker(ctx, 'css')
    out = {'p1': p1_entry, 'cpu': cpu_entry}
    if cpu_entry is not None:
        # lr-04's proven coin route starts from P1's token placed on the first tile of the last row (Steve, 78):
        # lr04-cpu-approach and lr04-cpu-pick push the empty hand right and down onto the coin, press A, then one
        # L3+Left step that must log token=p2. A stray A with P1 placed and the CPU on Random starts the match
        # (harness-lib-xps-1 r05, 02:52Z: short hops from tile 85 started Training as Mii Gunner vs Daisy), so no
        # other A is pressed until the coin is proven in the hand.
        park = next(e['tile'] for e in tables()['css_tiles'] if e.get('name') == 'Steve')
        out['park'] = walker.walk_to(park)
        play(ctx, 'tap-a1')
        walker.cursor.mark()
        play(ctx, 'coin-pick')  # lr-04's right pushes without its down pushes (they left the hand on the panel)
        found = walker.cursor.wait(r'step screen=css dir=left from=(\S+) to=(\S+) token=p2', 4, ctx)
        picked = {'from': found.group(1), 'to': found.group(2), 'token': 'p2'}
        out['coin'] = picked
        walker.at = None if picked['to'] == 'none' else int(picked['to'])
        out['cpu_walk'] = walker.walk_to(cpu_entry['tile'])
        out['cpu_still'] = str(ctx.shot(f'routes-css-cpu-{_norm(cpu_entry["name"])}'))
        play(ctx, 'tap-a1')
        play(ctx, 'tap-b1')
        back = _probe_step(ctx, walker, 'up')
        if not back or back['token'] not in (None, 'p1'):
            raise RuntimeError(f'B did not return P1\'s token to the hand: {back}')
        out['p1_back'] = back
        walker.at = None if back['to'] == 'none' else int(back['to'])
    out['p1_walk'] = walker.walk_to(p1_entry['tile'])
    out['p1_still'] = str(ctx.shot(f'routes-css-p1-{_norm(p1_entry["name"])}'))
    play(ctx, 'tap-a1')
    ctx.runner.routes_expect = {**getattr(ctx.runner, 'routes_expect', {}), 'p1_kind': p1_entry['kind'],
                                **({'cpu_kind': cpu_entry['kind']} if cpu_entry else {})}
    out['ready_still'] = str(ctx.shot('routes-css-ready'))
    return _save(ctx, 'routes-pick-fighters', out)


# --- Training: game state ---------------------------------------------------------------------------------------------

POS = (r'\[TrainingRooms\] state pos entry=(\d+) kind=(-?\d+) x=(-?[\d.]+) y=(-?[\d.]+) lr=(-?[\d.]+) '
       r'dmg=(-?[\d.]+) status=(-?\d+) situation=(-?\d+)')
# TrainingRooms diag lines that print natives::stage_id() (cpu.rs camera hooks; btt.rs layouts).
STAGE_LINE = r'(?:\[TrainingRooms\] cpu_event camera_(?:setup|mode) [^\n]*|\[Btt\] layouts )stage=(-?\d+)'


def read_state(ctx, cap=6.0):
    """One fresh dump of both bodies: touch diag-pos.req beside settings.json; TrainingRooms (diag build) prints
    `[TrainingRooms] state pos entry=N kind= x= y= lr= dmg= status= situation=` for P1 (0) and the CPU (1) and
    deletes the request (states.rs). Only lines written after the request count."""
    request = instance_path(ctx, 'settings').parent / 'diag-pos.req'
    cursor = LogCursor(instance_path(ctx, 'diag_log'))
    try:
        request.write_text('')
        bodies = {}
        regex = re.compile(POS)
        until = time.monotonic() + cap
        while len(bodies) < 2:
            ctx.check_stop()
            for line in cursor.read():
                found = regex.search(line)
                if found:
                    bodies[int(found.group(1))] = {
                        'kind': int(found.group(2)), 'x': float(found.group(3)), 'y': float(found.group(4)),
                        'lr': float(found.group(5)), 'dmg': float(found.group(6)), 'status': int(found.group(7)),
                        'situation': int(found.group(8))}
            if len(bodies) >= 2:
                break
            if time.monotonic() > until:
                raise RuntimeError(f'no fresh state pos lines for both bodies within {cap} s (got {sorted(bodies)}); '
                                   'is this the TrainingRooms diag build, in Training?')
            time.sleep(0.1)
        return bodies
    finally:
        cursor.close()


def enter_training(ctx, cap=90):
    """CSS ready -> Training (Plus), proven by the game: fresh state lines for both bodies, then the HUD gate.
    A fresh profile's fighter tip is closed with A while the HUD gate fails (remaining-2 boot_training)."""
    stage_cursor = LogCursor(instance_path(ctx, 'diag_log'))
    play(ctx, 'enter-training')
    until = time.monotonic() + cap
    state = None
    while state is None:
        try:
            state = read_state(ctx, cap=4.0)
        except RuntimeError:
            if time.monotonic() > until:
                raise
    for _ in range(4):
        if passes(ctx, 'training', 8):
            break
        play(ctx, 'tap-a1')
    else:
        gate(ctx, 'training')
    lines = [line for line in stage_cursor.read() if re.search(STAGE_LINE, line)]
    stage_cursor.close()
    ctx.runner.routes_stage_lines = lines[-4:]
    return _save(ctx, 'routes-enter-training', {'state': state, 'stage_lines': lines[-4:],
                                                'shot': str(ctx.shot('routes-training'))})


def stage_now(ctx):
    """The native stage id from the newest camera line TrainingRooms printed in this Training (cpu.rs), or None."""
    lines = getattr(ctx.runner, 'routes_stage_lines', None) or []
    path = instance_path(ctx, 'diag_log')
    if path.is_file():
        lines = lines + [line for line in path.read_text(errors='replace').splitlines()[-400:]
                         if re.search(STAGE_LINE, line)]
    found = [re.search(STAGE_LINE, line) for line in lines]
    return int(found[-1].group(1)) if found and found[-1] else None


def settled(previous, current):
    """Both bodies grounded and waiting (status 0, situation 0) with x/y unchanged across two fresh reads (the
    remaining-2-d1 predicate, D1 ruling 2026-11-15, generalised to both bodies; facing is asserted separately)."""
    if previous is None or current is None:
        return False
    for entry in (0, 1):
        a, b = previous.get(entry), current.get(entry)
        if a is None or b is None:
            return False
        if a['status'] != 0 or b['status'] != 0 or a['situation'] != 0 or b['situation'] != 0:
            return False
        if (a['x'], a['y']) != (b['x'], b['y']):
            return False
    return True


def fixture(ctx, p1=None, cpu=None, stage=None, reset=True, damage=0.0, lr=None, reads=6):
    """Per-row fixture: read who and where from game state, assert it, reset, and settle. Fails as FIXTURE.

    p1/cpu: fighter names or kinds (default: what the route picked); stage: name or id (default: what the route
    picked); reset: the native L+R+A reset first; damage: both bodies' damage after it; lr: P1's facing if given.
    Settled means both bodies grounded and waiting with x/y equal on two consecutive fresh reads, at most `reads`.
    """
    expect = dict(getattr(ctx.runner, 'routes_expect', {}))
    want_p1 = fighter_entry(p1)['kind'] if p1 is not None else expect.get('p1_kind')
    want_cpu = fighter_entry(cpu)['kind'] if cpu is not None else expect.get('cpu_kind')
    want_stage = stage_entry(stage)['id'] if stage is not None else expect.get('stage_id')
    out = {'expect': {'p1_kind': want_p1, 'cpu_kind': want_cpu, 'stage_id': want_stage}, 'reads': []}
    before = read_state(ctx)
    out['before'] = before
    if reset:
        play(ctx, 'reset-lra')
    previous = None
    for n in range(reads):
        current = read_state(ctx)
        out['reads'].append(current)
        if settled(previous, current):
            break
        previous = current
    else:
        _save(ctx, 'routes-fixture', out)
        raise RuntimeError(f'FIXTURE: bodies not settled within {reads} fresh reads: {out["reads"][-2:]}')
    problems = []
    if want_p1 is not None and current[0]['kind'] != want_p1:
        problems.append(f'P1 kind {current[0]["kind"]} != {want_p1}')
    if want_cpu is not None and current[1]['kind'] != want_cpu:
        problems.append(f'CPU kind {current[1]["kind"]} != {want_cpu}')
    if damage is not None:
        for entry in (0, 1):
            if abs(current[entry]['dmg'] - float(damage)) > 1e-3:
                problems.append(f'entry {entry} damage {current[entry]["dmg"]} != {damage}')
    if lr is not None and current[0]['lr'] != float(lr):
        problems.append(f'P1 lr {current[0]["lr"]} != {lr}')
    if want_stage is not None:
        now = stage_now(ctx)
        out['stage_id'] = now
        if now != want_stage:
            problems.append(f'stage id {now} != {want_stage}')
    out['settled'] = current
    names = {e['kind']: e['name'] for e in tables()['css_tiles'] if 'kind' in e}
    out['fighters'] = {'p1': names.get(current[0]['kind']), 'cpu': names.get(current[1]['kind'])}
    out['shot'] = str(ctx.shot('routes-fixture'))
    _save(ctx, 'routes-fixture', out)
    if problems:
        raise RuntimeError('FIXTURE: ' + '; '.join(problems))
    return {'settled': current, 'fighters': out['fighters'], 'stage_id': out.get('stage_id'),
            'reads': len(out['reads'])}


# --- back out --------------------------------------------------------------------------------------------------------

def select_screen(ctx, cap=2.5):
    """'sss' or 'css' when an L3+Up step is logged on that screen (the tile cursor names it), else None. On the CSS a
    step logs only while the hand carries a token; on the SSS the hand always steps."""
    cursor = LogCursor(instance_path(ctx, 'tile_log'))
    try:
        play(ctx, 'tile-up')
        return cursor.wait(r'step screen=(css|sss) ', cap, ctx).group(1)
    except RuntimeError:
        return None
    finally:
        cursor.close()


def back_to_main(ctx, presses=10):
    """Training -> main menu: native pause, Quit Training, confirm (library frames from lr-03), then back out of the
    character and stage selects until the main gate passes. Each look is a gate; nothing waits on a fixed sleep."""
    looks = []
    if passes(ctx, 'training', 4):
        play(ctx, 'pause-open')
        gate(ctx, 'native-pause')
        play(ctx, 'focus-native-quit-frames')
        gate(ctx, 'quit-focus')
        play(ctx, 'confirm-native-quit-frames')
        looks.append('training-quit')
    for n in range(presses):
        # 6 s, not 3: a B pressed while the main menu is still drawing opens its sidebar, and the main gate (the
        # highlighted Smash tile) then fails until another B closes it (harness-lib-xps-1 r08, 03:16Z).
        if passes(ctx, 'main', 6):
            return _save(ctx, 'routes-back-to-main', {'looks': looks, 'shot': str(ctx.shot('routes-main-back'))})
        # The character and stage selects go back only on a held B (a 4-frame tap does nothing there:
        # harness-lib-xps-1 r05 retry, 02:56Z, eight taps left the stage select up). leave-css holds B 90 frames.
        if passes(ctx, 'css', 3):
            looks.append('css')
            play(ctx, 'leave-css')
        elif select_screen(ctx) == 'sss':
            # The library `stage` gate is the Training name crop: it fails once another stage is hovered, so the
            # stage select is recognised by the tile cursor's own screen name instead.
            looks.append('sss')
            play(ctx, 'leave-css')
        elif looks.count('other') >= 3:
            # Three B presses past the selects and still no main menu: one B too many reaches the title screen
            # (harness-lib-ally-1 r08). to_main presses Start until the main menu shows.
            looks.append('to_main')
            out = to_main(ctx)
            return _save(ctx, 'routes-back-to-main', {'looks': looks, 'to_main': out})
        else:
            looks.append('other')
            play(ctx, 'tap-b1')
    raise RuntimeError(f'not back on the main menu after {presses} looks: {looks}')
