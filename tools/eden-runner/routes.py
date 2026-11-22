"""Shared Eden setup routes: proven building blocks every lane reuses instead of hand-building a boot route.

Each public function is a runner step hook (rows.json: {"run": "routes:<function>", "args": {...}, "check": true})
that reaches a named game state, proves it with a game-state or image gate, and returns a small receipt. Waits poll
game state (Eden status, a plugin log line, a tile-cursor line, an image gate) with a cap; there is no fixed sleep
where a state exists. Every block fails closed: a missing state raises with what it saw, and nothing guesses.

Blocks (docs/ROUTES.md has what each guarantees on exit):
  configure_boot   mute, memory_layout_mode 0, optional GDB stub on, saved and read back, before game.load
  load_game        game.load, the no-hold GDB kick before any wait, then status loading_complete and drawing
  wait_lines       the boot lines the plugins print (instead of a fixed post-load sleep)
  to_main          Title or How to Play -> Main Menu (native ownership gate, capped)
  main_to_stage    main menu -> Training's stage select
  pick_stage       stage select -> the named stage's tile (tile cursor L3+D-pad macros, at == to) -> A -> CSS
  pick_fighter     CSS: P1 token onto the named fighter's tile (hovered == to) and placed
  enter_training   CSS ready -> Training, waiting for the plugin's Training line
  chord            play a same-frame chord macro (reset L+R+A, TR save/load, tile steps) by name
  fixture          read fighters, stage, damage and positions from game state, assert them, reset, settle
  back_to_main     Training -> main menu, proven by the main gate
  pick_stage_hw    Switch: prepare the named verified stage tile for confirmation
  pick_fighter_hw  Switch: prepare the named verified fighter tile for confirmation
  confirm_pick     Switch: recheck the native tile and confirm local placement or stage transition
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
from collections import deque
from pathlib import Path

import native_picker
import state_gate
import trace_attempts
from native_picker_io import BridgePickerIO

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
    """Check a route gate and return its evidence path."""
    key = f'routes.{name}'
    if name in ('main', 'css', 'stage', 'training', 'native-pause', 'quit-focus'):
        default = SWITCH_CAPS.get(name, 30) if on_switch(ctx) else ROUTE_GATES[name]['cap']
        return _native_screen_gate(ctx, name, default if cap is None else cap)
    if name == 'tr-menu':
        return _tr_menu_gate(ctx, cap=10 if cap is None else cap)
    if name == 'ready':
        raise RuntimeError('CSS Ready gate needs a bridge field for the aggregate Ready banner')
    gates = ctx.runner.plan.setdefault('gates', {})
    if on_switch(ctx):
        if name not in SWITCH_REFS:
            raise RuntimeError(f'gate routes.{name}: no real-Switch reference (SWITCH_REFS)')
        return _frame_gate(ctx, f'routes.{name}', _switch_spec(name), cap or SWITCH_CAPS.get(name, 30))
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


def _native_screen_gate(ctx, name, cap):
    io = BridgePickerIO(ctx)
    until = time.monotonic() + cap
    before = native_picker.wait_sample(io, native_picker.UI, until)
    state = native_picker.wait_sample(io, native_picker.UI, until, before)
    if state.connection != before.connection:
        raise RuntimeError('bridge connection changed during native screen check')
    if name in ('css', 'stage'):
        native_picker.native_input_owner(state, 'css' if name == 'css' else 'sss')
    elif name == 'main':
        if not _main_top(state):
            raise _missing_navigation_state('Main Menu', state, 'qualified Main top focus')
    else:
        pause = {**TRAINING_OWNER, 'screen': 'pause', 'focus.source': 'native'}
        expected = {
            'training': TRAINING_SCREEN,
            'native-pause': {**pause, 'focus.id': 'cpu_count'},
            'quit-focus': {**pause, 'focus.id': 'quit_training'},
        }[name]
        if not _fields_match(state, expected):
            raise _missing_navigation_state(name, state, str(expected))
    key = f'routes.{name}'
    filename = f'gate-{key}'
    receipt = _save(ctx, filename, {
        'gate': key, 'on': ctx.on, 'pass': True, 'provider': native_picker.UI,
        **_navigation_receipt(state),
    })
    ctx.log(receipt)
    return str(Path(ctx.row_dir) / f'{filename}.json')


SWITCH_SCHEMES = ('usb://', 'pi://', 'switch://', 'autopilot://')
# Real-Switch gates: references cut from the capture card's 1280x720 OBS stills (the switch-hw-ledger-rc15 preamble,
# reviewed), each checked to pass on its own screen and fail on the others (library/routes/refs/hw-refs.json).
SWITCH_REFS = {'main': 'hw-main', 'stage': 'hw-sss', 'css': 'hw-css', 'ready': 'hw-ready',
               'training': 'hw-training', 'tr-menu': 'hw-tr-menu', 'profile': 'hw-profile',
               'launch-error': 'hw-launch-error', 'home': 'hw-home', 'corrupt': 'hw-corrupt-dialog'}
SWITCH_CAPS = {'main': 10, 'stage': 45, 'css': 60, 'ready': 10, 'training': 60, 'tr-menu': 10, 'profile': 10,
               'launch-error': 5, 'home': 10, 'corrupt': 3}


def on_switch(ctx):
    """True when this instance is a real Switch driven through the pad (switch_hw.py up)."""
    return str(ctx.instance.get('socket', '')).startswith(SWITCH_SCHEMES)


def _switch_spec(name):
    ref = SWITCH_REFS[name]
    table = json.loads((ROUTES / 'refs' / 'hw-refs.json').read_text())[ref]
    spec = {'kind': 'image', 'reference': str(ROUTES / 'refs' / f'{ref}.png'), 'sha256': table['sha256'],
            'roi': table['roi'], 'reference_is_roi': True, 'frame': [1280, 720], 'scale': 'fit', 'tolerance': 18,
            'dim_tolerant': True}
    if name in SWITCH_INK:
        spec['ink_limit'] = SWITCH_INK[name]
    return spec


# Gates whose label sits on a translucent plate: the text shape decides when the pixels differ (ink_compare).
SWITCH_INK = {'training': 0.3}


def desktop_shots(ctx):
    """True where ui.screenshot grabs the whole desktop (the Ally: screenshot_target "screen"; a window grab is black
    under Vulkan there). Image gates then read a game frame from Eden's recorder instead (frame())."""
    return ctx.instance.get('screenshot_target') == 'screen'


def frame(ctx, name, cap=10.0):
    """One game frame as a PNG in the row folder: on a real Switch an OBS still of the capture card; on Eden, one
    frame from Eden's GIF recorder (game pixels only, 1280 wide)."""
    if on_switch(ctx):
        return Path(ctx.shot(name))
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


def dim_compare(path, spec, tolerance):
    """For a screen the console has auto-dimmed (after ~5 min without input; luminance about 1/3), the live ROI is
    scaled up to the reference's mean brightness, then compared again. Gains from 1.3 to 4 count as dimming, and
    the gain is clamped there. Returns (difference, gain), or (inf, None) when the frame isn't dimmer.
    (hw-run-2 F04, 2026-11-16: a dimmed TR page failed a brightness-based gate.)"""
    from PIL import Image, ImageStat
    with Image.open(path) as im, Image.open(spec['reference']) as ref:
        frame = im.convert('RGB')
        if spec.get('frame') and frame.size != tuple(spec['frame']):
            frame = frame.resize(tuple(spec['frame']), Image.Resampling.BOX)
        live = frame.crop(tuple(spec['roi']))
        want = ref.convert('RGB')
        if live.size != want.size:
            return float('inf'), None
        lum_live = sum(ImageStat.Stat(live).mean) / 3
        lum_ref = sum(ImageStat.Stat(want).mean) / 3
        if lum_live <= 0 or lum_ref / lum_live < 1.3:
            return float('inf'), None
        gain = min(4.0, lum_ref / lum_live)
        lifted = live.point(lambda v: min(255, int(round(v * gain))))
        # A dimmed copy of the screen keeps its structure; a flat dark frame lifted to the same mean doesn't.
        if sum(ImageStat.Stat(lifted).stddev) < 0.5 * sum(ImageStat.Stat(want).stddev):
            return float('inf'), None
        return _mean_diff(lifted, want), round(gain, 2)


def structure_ok(path, spec):
    """The live ROI has at least a third of the reference's contrast (per-channel standard deviation), after the
    same brightness lift a dimmed screen gets: the screen's own detail is there, not just its mean colour."""
    from PIL import Image, ImageStat
    with Image.open(path) as im, Image.open(spec['reference']) as ref:
        frame = im.convert('RGB')
        if spec.get('frame') and frame.size != tuple(spec['frame']):
            frame = frame.resize(tuple(spec['frame']), Image.Resampling.BOX)
        live = frame.crop(tuple(spec['roi']))
        want = ref.convert('RGB')
        lum_live = sum(ImageStat.Stat(live).mean) / 3
        lum_ref = sum(ImageStat.Stat(want).mean) / 3
        gain = min(4.0, max(1.0, lum_ref / lum_live)) if lum_live > 0 else 1.0
        return sum(ImageStat.Stat(live).stddev) * gain >= sum(ImageStat.Stat(want).stddev) / 3


def ink_compare(path, spec):
    """Text-shape mismatch of the live ROI against the reference (ink masks, routes._ink): for a label on a
    translucent plate, whose background is the stage behind it. The Training HUD's "Total Damage" plate shows the
    stage through it, so hw-training (cut on the Training stage's grid) differs by 52 on Peach's Castle
    (switch-hw-feature-ledger run-1 S-B07, 19:34Z) while its text is the same."""
    from PIL import Image
    with Image.open(path) as im, Image.open(spec['reference']) as ref:
        frame = im.convert('RGB')
        if spec.get('frame') and frame.size != tuple(spec['frame']):
            frame = frame.resize(tuple(spec['frame']), Image.Resampling.BOX)
        live = frame.crop(tuple(spec['roi']))
        want = ref.convert('RGB')
        if live.size != want.size:
            return 1.0
        return ink_mismatch(_ink(live), _ink(want))


def _mean_diff(a, b):
    first, second = a.tobytes(), b.tobytes()
    return sum(abs(x - y) for x, y in zip(first, second)) / len(first) if first and len(first) == len(second) \
        else float('inf')


def match_frame(png, spec, compare_region):
    """(difference, note) of one frame against an image spec: the plain compare; for dim-tolerant specs the
    brightness-lifted compare when the plain one fails; and the contrast floor on any pass."""
    tolerance = spec.get('tolerance', 8)
    diff, note = compare_region(png, Path(spec['reference']), spec['roi'], spec.get('reference_is_roi'),
                                spec.get('scale') or 'fit', spec.get('frame'), tolerance)
    if diff > tolerance and spec.get('dim_tolerant'):
        dimmed, gain = dim_compare(png, spec, tolerance)
        if dimmed <= tolerance:
            diff, note = dimmed, {**(note or {}), 'dimmed_gain': gain}
    if diff > tolerance and spec.get('ink_limit') is not None:
        mismatch = ink_compare(png, spec)
        if mismatch <= spec['ink_limit']:
            diff, note = 0.0, {**(note or {}), 'ink_mismatch': round(mismatch, 3)}
    if diff <= tolerance and spec.get('dim_tolerant') and not structure_ok(png, spec):
        # A flat frame (black loading screen, blank capture) can sit within the tolerance of a dark,
        # text-light reference such as the TR menu's key line; the reference's text must be there.
        diff, note = float('inf'), {**(note or {}), 'flat': True}
    return diff, note


def _frame_gate(ctx, key, spec, cap):
    """An image gate on recorder frames: the same reference, ROI, frame size, scale and tolerance as the gate."""
    reference = Path(spec['reference'])
    tolerance = spec.get('tolerance', 8)
    until = time.monotonic() + cap
    last = None
    while True:
        png = frame(ctx, 'gate-' + key)
        diff, note = match_frame(png, spec, ctx.runner.compare_region)
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
    try:
        cursor = LogCursor(instance_path(ctx, source)) if expect else None
    except KeyError:
        cursor = None  # that log isn't available here (a real Switch has no diag log); the chord still plays
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

NATIVE_INPUT = {'input.owner': 'native', 'dialog': 'none'}
MAIN_TOP = {**NATIVE_INPUT, 'screen': 'main_menu', 'screen.layer': 'top', 'focus.source': 'native'}
MAIN_TABS = ('smash', 'spirits', 'games_and_more', 'vault', 'online')
TITLE_PROMPT = {**NATIVE_INPUT, 'screen': 'title', 'focus.source': 'native',
                'focus.id': 'press_any', 'focus.index': '0', 'focus': 'title.press_any'}
TITLE_OPENING = {**NATIVE_INPUT, 'screen': 'title', 'screen.context': 'opening',
                 'focus.source': 'native', 'focus.id': 'opening_playback', 'focus': 'title.opening_playback'}
# The measured cold stock opening took 129.874 s, with playback visible 5.5 s into the route.
TITLE_OPENING_READY_S = 180
HOW_TO_PLAY_SKIP = {**NATIVE_INPUT, 'screen': 'attract', 'screen.context': 'how_to_play',
                    'focus.source': 'native', 'focus.id': 'skip_how_to_play', 'focus.index': '0',
                    'focus': 'attract.skip_how_to_play'}
_UNOWNED_FOCUS = dict.fromkeys(('focus.source', 'focus.id', 'focus.index', 'focus.label',
                               'input.owner', 'dialog'), 'unknown')
_TITLE_WITHOUT_PROMPT = {**_UNOWNED_FOCUS, 'screen': 'title', 'focus': 'title',
                         'screen.context': 'unknown'}
_HOW_TO_PLAY_WITHOUT_PROMPT = {**_UNOWNED_FOCUS, 'screen': 'attract', 'focus': 'attract',
                               'screen.context': 'how_to_play'}
_MAIN_WITHOUT_FOCUS = {**_UNOWNED_FOCUS, 'screen': 'main_menu', 'screen.layer': 'top',
                       'focus': 'main_menu.top', 'screen.context': 'unknown'}
TRAINING_OWNER = {**NATIVE_INPUT, 'screen.context': 'training'}
TRAINING_SCREEN = {**TRAINING_OWNER, 'screen': 'training'}
TRAINING_CSS = {**TRAINING_OWNER, 'screen': 'css.training', 'focus.source': native_picker.TILE}
TRAINING_SSS = {**TRAINING_OWNER, 'screen': 'stage_select', 'focus.source': native_picker.TILE}
_TRAINING_SSS_SETTLING = tuple(
    {**_UNOWNED_FOCUS, 'screen': 'stage_select', 'focus': 'stage_select', 'screen.context': context}
    for context in ('unknown', 'training')
)
_TRAINING_CSS_SETTLING = {**_UNOWNED_FOCUS, 'screen': 'css.training', 'focus': 'css.training',
                         'screen.context': 'training'}
GAMES_TRAINING = {**NATIVE_INPUT, 'screen': 'main_menu', 'screen.layer': 'submenu',
                  'screen.tab': 'games_and_more', 'focus.source': 'native', 'focus.id': 'training',
                  'focus': 'main_menu.submenu.training'}
_GAMES_TRAINING_SETTLING = {
    **_UNOWNED_FOCUS,
    'screen': 'main_menu', 'screen.tab': 'games_and_more', 'screen.layer': 'submenu',
    'focus': 'main_menu.submenu', 'screen.context': 'unknown',
}


def _main_tab(tab):
    return {**MAIN_TOP, 'screen.tab': tab, 'focus.id': tab, 'focus': f'main_menu.top.{tab}'}


def _main_top(state):
    tab = state.values.get('screen.tab')
    return tab in MAIN_TABS and _fields_match(state, _main_tab(tab))


def _navigation_receipt(state):
    boot, revision, observation = state.clock
    return {'screen': state.values['screen'], 'focus': state.values['focus'], 'boot': boot,
            'revision': revision, 'observation': observation}


def _missing_navigation_state(route, state, expected):
    fields = ('screen', 'screen.context', 'screen.layer', 'input.owner', 'dialog', 'focus.source', 'focus.id')
    observed = ', '.join(f'{field}={state.values.get(field, "missing")}' for field in fields) if state else 'no reply'
    return RuntimeError(f'{route}: requires {expected}; last ssbu.ui: {observed}')


def to_main(ctx, tries=8, look_s=6, *, cap=None):
    """Wait for one qualified cold opening, then run the bounded native prompt route."""
    io = BridgePickerIO(ctx)
    started = time.monotonic()
    navigation_cap = (tries + 1) * look_s if cap is None else cap
    until = started + navigation_cap
    main_targets = tuple(_main_tab(tab) for tab in MAIN_TABS)
    state = None
    presses = 0
    opening_seen = waiting_for_opening = False
    while (remaining := until - time.monotonic()) > 0:
        sample = native_picker.check_sample(io.read(native_picker.UI, min(remaining, look_s)),
                                            native_picker.UI, loading_ui=True)
        fresh = _check_title_sample(sample, state)
        state = sample
        now = time.monotonic()
        if now >= until:
            continue
        if fresh and state.values['observation.active'] == 'yes':
            main = _main_top(state)
            prompt = _fields_match(state, TITLE_PROMPT) or _fields_match(state, HOW_TO_PLAY_SKIP)
            if waiting_for_opening and (main or prompt):
                waiting_for_opening = False
                until = now + navigation_cap
                ctx.log({'route': 'to_main', 'phase': 'navigation', 'elapsed_s': now - started,
                         'navigation_cap_s': navigation_cap, **_navigation_receipt(state)})
            if main or (tries > 0 and prompt):
                break
            playback = _fields_match(state, TITLE_OPENING)
            if playback and not opening_seen:
                opening_seen = waiting_for_opening = True
                until = started + TITLE_OPENING_READY_S
                ctx.log({'route': 'to_main', 'phase': 'opening_readiness', 'elapsed_s': now - started,
                         'opening_readiness_cap_s': TITLE_OPENING_READY_S, 'navigation_cap_s': navigation_cap,
                         **_navigation_receipt(state)})
            if waiting_for_opening and not (
                playback or _fields_match(state, _TITLE_WITHOUT_PROMPT)
            ):
                raise _missing_navigation_state('Title opening', state, 'qualified opening or its Title handoff')
        ctx.check_stop()
        time.sleep(min(.05, max(0, until - time.monotonic())))
    else:
        if waiting_for_opening:
            raise _missing_navigation_state('Title opening', state,
                                            f'a qualified destination within {TITLE_OPENING_READY_S} s of route entry')
        raise _missing_navigation_state('to_main', state, 'qualified Main, Title or How to Play focus')

    for source, route, destinations, pending in (
        (TITLE_PROMPT, 'Title', (*main_targets, HOW_TO_PLAY_SKIP),
         (_TITLE_WITHOUT_PROMPT, _HOW_TO_PLAY_WITHOUT_PROMPT, _MAIN_WITHOUT_FOCUS)),
        (HOW_TO_PLAY_SKIP, 'How to Play', main_targets, (_HOW_TO_PLAY_WITHOUT_PROMPT, _MAIN_WITHOUT_FOCUS)),
    ):
        if not _fields_match(state, source):
            continue
        before = _armed_native_sample(io, state, until, route, settling=True)
        while any(_fields_match(before, passive) for passive in pending):
            _check_title_sample(before)
            before = native_picker.wait_sample(io, native_picker.UI, until, before, loading_ui=True)
        _check_title_sample(before)
        if any(_fields_match(before, target) for target in destinations):
            state = before
            continue
        if not _fields_match(before, source):
            raise _missing_navigation_state(route, before, f'qualified {route} source after arming')
        if time.monotonic() >= until:
            raise RuntimeError(f'{route} source deadline expired before Plus')
        io.send('attract-cancel')
        presses += 1
        state = native_picker.wait_scene_transition(
            io, before, until, source, destinations[0], label=route,
            pending_destinations=pending, additional_targets=destinations[1:],
        )
        io.finish()

    out = {'main': True, 'start_presses': presses, **_navigation_receipt(state)}
    ctx.log({'route': 'to_main', **out})
    return out



def _check_title_sample(state, previous=None):
    values = state.values
    incomplete = any(not values.get(key) for key in native_picker.UI_FOCUS_FIELDS)
    contradicts_inactive = values['observation.active'] == 'no' and any(
        values.get(key) != 'unknown' for key in native_picker.UI_FOCUS_FIELDS)
    if incomplete or contradicts_inactive or values.get('ack') != '0' or values.get('ack.result') != 'unsupported':
        raise RuntimeError('ssbu.ui: incomplete or contradictory Title observation')
    if previous is None:
        return True
    native_picker.same_epoch(previous, state, in_flight=True)
    _, revision, observation = state.clock
    _, prior_revision, prior_observation = previous.clock
    if (revision < prior_revision or observation < prior_observation
            or (revision == prior_revision and values != previous.values)):
        raise RuntimeError('ssbu.ui: stale or contradictory Title observation')
    return revision > prior_revision and observation > prior_observation


def main_to_stage(ctx):
    """Enter Training Stage Select from a qualified Main Menu owner."""
    return _native_main_to_stage(ctx)


def wait_sss(ctx, cap=45):
    """Wait for the bridge to confirm Stage Select owns input."""
    return _native_screen_gate(ctx, 'stage', cap)


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


def _expect_file(ctx):
    out = getattr(ctx.runner, 'out', None)
    return Path(out) / 'routes-expect.json' if out else None


def expected(ctx):
    """What the route picked (stage id, fighter kinds), kept in the run's out folder so a `run --retry` in a new
    runner process still asserts it (harness-lib-surface-1: r06 ran in the retry process and lost the stage id)."""
    found = dict(getattr(ctx.runner, 'routes_expect', {}) or {})
    path = _expect_file(ctx)
    if not found and path is not None and path.is_file():
        found = json.loads(path.read_text())
    return found


def remember(ctx, **values):
    merged = {**expected(ctx), **values}
    ctx.runner.routes_expect = merged
    path = _expect_file(ctx)
    if path is not None:
        path.write_text(json.dumps(merged) + '\n')
    return merged



def pick_stage(ctx, stage, allow_derived=False, transition_cap=60, *, topology=None, form='normal',
               next_screen='css.training'):
    """Select the typed native stage and verify the expected CSS transition."""
    entry = stage_entry(stage)
    io = BridgePickerIO(ctx)
    prepared = native_picker.hover(io, 'sss', _native_target(entry, form), _tile_topology(topology),
                                   expected_context=_stage_pick_context(next_screen))
    return _finish_native_pick(ctx, io, 'sss', prepared, entry, next_screen, transition_cap)


def _probe_step(ctx, walker, direction='left', cap=2.5):
    """One L3 step and its line, or None when the hand moved nothing (an empty hand logs no step)."""
    walker.cursor.mark()
    play(ctx, f'tile-{direction}')
    try:
        found = walker.cursor.wait(STEP.format(screen='css', dir=direction), cap, ctx)
    except RuntimeError:
        return None
    return {'from': found.group(1), 'to': found.group(2), 'token': found.group(3)}



def pick_fighters(ctx, p1, cpu=None, coin_route=False, *, topology=None, form='default', take_back=False):
    """Place the verified local fighter token in CSS."""
    if coin_route:
        raise RuntimeError('the stick-based coin route is not supported')
    topology = topology or library_topology('css-training')
    metrics = {'inputs': 0, 'bridge_requests': 0, 'bridge_boot': None, 'revisions': {}}
    previous = getattr(ctx.runner, 'setup_metrics', None)
    ctx.runner.setup_metrics = metrics
    start = time.monotonic()
    try:
        options = ({'take_back': True, 'expected_context': 'training'} if cpu is not None else
                   {'take_back': True} if take_back else {})
        p1_result = pick_local_fighter(ctx, p1, topology=topology, form=form, **options)
        p1_ms = round((time.monotonic() - start) * 1000)
        cpu_result = pick_cpu(ctx, cpu, topology=topology, form=form) if cpu is not None else None
    finally:
        ctx.runner.setup_metrics = previous
        if previous is not None:
            _merge_setup_metrics(previous, metrics)
    receipt = {**(p1_result if cpu is None else {}), 'block': 'pick_fighters',
               'args': {'p1': p1, 'cpu': cpu, 'form': form, 'take_back': take_back},
               'verified': {'p1': p1_result, 'cpu': cpu_result}, **metrics,
               'ms': {'p1': p1_ms, 'cpu': round((time.monotonic() - start) * 1000) - p1_ms},
               'skipped': ['pick_cpu'] if cpu is None else [], 'p1': p1_result, 'cpu': cpu_result}
    return _save(ctx, 'routes-pick-fighters', receipt)


def _merge_setup_metrics(destination, source):
    destination['inputs'] += source['inputs']
    destination['bridge_requests'] += source['bridge_requests']
    if source['bridge_boot'] is not None:
        destination['bridge_boot'] = source['bridge_boot']
    for provider, revisions in source['revisions'].items():
        destination['revisions'].setdefault(provider, list(revisions))[1] = revisions[1]


# --- Training: game state ---------------------------------------------------------------------------------------------

POS = (r'\[TrainingRooms\] state pos entry=(\d+) kind=(-?\d+) x=(-?[\d.]+) y=(-?[\d.]+) lr=(-?[\d.]+) '
       r'dmg=(-?[\d.]+) status=(-?\d+) situation=(-?\d+)')
# TrainingRooms diag lines that print natives::stage_id() (cpu.rs camera hooks; btt.rs layouts).
STAGE_LINE = r'(?:\[TrainingRooms\] cpu_event camera_(?:setup|mode) [^\n]*|\[Btt\] layouts )stage=(-?\d+)'


def _switch_hw():
    import importlib.util
    spec = importlib.util.spec_from_file_location('switch_hw', Path(__file__).with_name('switch_hw.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def native_state(ctx, fresh=(), cap=6.0, *, roster_entries=()):
    """The bridge's native reads (ssbu-hw-bridge; `switch_hw.py up --bridge-host` or an Eden bridge capture): the newest
    frame/wall, both bodies' `state pos`, stage, roster, items, speed and status ids (`switch_hw.bridge_state`).
    `fresh` (pos, speed, roster, status) are asked for first. Needs the instance's `bridge_log`."""
    options = {'roster_entries': roster_entries} if roster_entries else {}
    state = _switch_hw().bridge_state(instance_path(ctx, 'bridge_log').parent, tuple(fresh), cap, **options)
    metrics = getattr(getattr(ctx, 'runner', None), 'setup_metrics', None)
    if metrics is not None:
        metrics['bridge_requests'] += len(fresh)
    return state


def wait_native(ctx, test, cap=10.0, fresh=(), every=0.25, *, roster_entries=()):
    """Poll `native_state` until `test(state)` is true (e.g. the frame advancing in a fight, a held item); the last
    state is returned, or RuntimeError after `cap` s with it."""
    until = time.monotonic() + cap
    while True:
        ctx.check_stop()
        options = {'roster_entries': roster_entries} if roster_entries else {}
        state = native_state(ctx, fresh, cap=max(1.0, min(6.0, until - time.monotonic())), **options)
        if test(state):
            return state
        if time.monotonic() > until:
            raise RuntimeError(f'native state did not match within {cap} s: frame={state.get("frame")} '
                               f'stage={state.get("stage")} roster={state.get("roster")}')
        time.sleep(every)


def wait_scene(ctx, cls, cap=30.0):
    """Wait on the bridge's own scene class (protocol v2: training, local_vs, menu, local_wireless, internet_online,
    unknown) instead of blind pad timing; returns the scene (mode, melee, init, ldn, class, frame)."""
    return _switch_hw().wait_scene(instance_path(ctx, 'bridge_log').parent, cls, cap)


def bridge_v2(ctx, *requests):
    """Queue protocol v2 requests (caps, scene, fighters, input_watch, inject, inject_start, inject_stop, peek, chain,
    watch) for the instance's bridge capture."""
    return _switch_hw().bridge_v2(instance_path(ctx, 'bridge_log').parent, *requests)


def frame_advancing(ctx, frames=30, cap=10.0):
    """The game is running (not paused or loading): the bridge's frame counter moves by `frames` within `cap` s."""
    start = native_state(ctx).get('frame')
    if start is None:
        raise RuntimeError('no bridge frame yet (is the bridge connected?)')
    return wait_native(ctx, lambda s: (s.get('frame') or 0) >= start + frames, cap)


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



def enter_training(ctx, cap=90, *, stage_ids=None, fighters=None):
    """Activate a placed local fighter and confirm native Training loaded."""
    return _with_setup_metrics(ctx, lambda metrics: _enter_training(ctx, cap, stage_ids, fighters, metrics))


def _enter_training(ctx, cap, stage_ids, fighters, metrics):
    wanted = expected(ctx)
    kinds = {seat: wanted.get(f'{seat}_kind') for seat in ('p1', 'cpu')}
    for seat, name in (fighters or {}).items():
        if seat not in kinds:
            raise ValueError(f'unknown fighter seat {seat!r}')
        kinds[seat] = fighter_entry(name)['kind']
    started = time.monotonic()
    result = _native_enter_training(ctx, cap, stage_ids)
    entry_ms = round((time.monotonic() - started) * 1000)
    remaining = cap - (time.monotonic() - started)
    if remaining <= 0:
        raise RuntimeError('Training entry deadline expired before roster verification')
    state = wait_native(ctx, lambda s: 0 in s.get('roster', {}) and 1 in s.get('roster', {}),
                        cap=remaining, fresh=('roster',), roster_entries=(0, 1))
    roster = state.get('roster', {})
    actual = (roster.get(0, {}).get('kind'), roster.get(1, {}).get('kind'))
    if (None in actual or (kinds['p1'] is not None and actual[0] != kinds['p1'])
            or (kinds['cpu'] is not None and actual[1] != kinds['cpu'])):
        raise RuntimeError(f'Training loaded fighters P1 kind {actual[0]}, CPU kind {actual[1]}; '
                           f'expected P1 {kinds["p1"]}, CPU {kinds["cpu"]}')
    checked = {seat: 'checked' if kind is not None else 'unchecked' for seat, kind in kinds.items()}
    return _save(ctx, 'routes-enter-training', {
        **result, 'block': 'enter_training', 'args': {'cap': cap, 'stage_ids': stage_ids, 'fighters': fighters},
        'verified': {'stage_id': result['stage_id'], 'roster': roster, 'fighters_checked': checked},
        'roster': roster, 'fighters_checked': checked, **metrics,
        'ms': {'entry': entry_ms, 'roster': round((time.monotonic() - started) * 1000) - entry_ms},
        'skipped': [],
    })


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
    expect = expected(ctx)
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
    try:
        cursor = LogCursor(instance_path(ctx, 'tile_log'))
    except KeyError:
        return None  # This legacy probe requires a configured tile log.
    try:
        play(ctx, 'tile-up')
        return cursor.wait(r'step screen=(css|sss) ', cap, ctx).group(1)
    except RuntimeError:
        return None
    finally:
        cursor.close()


_PLACED_IDENTITY = ('placed.at', 'placed.kind', 'placed.name', 'placed.form')


def _take_back_training_token(io, state):
    until = time.monotonic() + 9
    tile = native_picker.wait_sample(io, native_picker.TILE, until)
    native_picker.same_epoch(state, tile, in_flight=True)
    native_picker.context(tile, 'css')
    native_picker.native_input_owner(state, 'css', expected_context='training')
    if tile.values.get('local.token') == 'held':
        native_picker.token_held(tile, 'css')
        return None
    if tile.values.get('local.token') != 'placed' or tile.values.get('carried') != 'none':
        raise RuntimeError('Training CSS: local token ownership is not a placed local medal')
    placed_at = native_picker.number(tile, 'placed.at')
    before = _armed_native_sample(io, state, until, 'Training return')
    if not _fields_match(before, TRAINING_CSS):
        raise _missing_navigation_state('Training return', before, 'qualified Training CSS before the token input')
    native_picker.native_input_owner(before, 'css', expected_context='training')
    tile_before = native_picker.wait_sample(io, native_picker.TILE, until, tile)
    native_picker.same_epoch(before, tile_before, in_flight=True)
    try:
        native_picker.check_context(tile, tile_before, 'css')
        unchanged = (tile_before.values.get('local.token') == 'placed'
                     and tile_before.values.get('carried') == 'none'
                     and native_picker.number(tile_before, 'placed.at') == placed_at
                     and all(tile_before.values.get(key) == tile.values.get(key) for key in _PLACED_IDENTITY))
        before_ack = native_picker.number(tile_before, 'ack')
        before_applied = native_picker.number(tile_before, 'applied')
    except (RuntimeError, TypeError) as exc:
        raise RuntimeError('local medal placement changed before B') from exc
    if not unchanged:
        raise RuntimeError('local medal placement changed before B')
    if time.monotonic() >= until:
        raise RuntimeError('Training return source deadline expired before leave-css')
    io.send('leave-css')
    current = tile_before
    while time.monotonic() < until:
        try:
            current = native_picker.wait_sample(io, native_picker.TILE, until, current, in_flight=True)
        except RuntimeError as exc:
            if str(exc) in (f'{native_picker.TILE}: incomplete, inactive or wrong-schema observation',
                            f'{native_picker.TILE}: no newer active observation'):
                break
            raise
        if current.values.get('screen') != 'css':
            break
        native_picker.check_context(tile_before, current, 'css')
        try:
            counters_unchanged = (native_picker.number(current, 'ack') == before_ack
                                  and native_picker.number(current, 'applied') == before_applied)
        except (RuntimeError, TypeError):
            counters_unchanged = False
        if not counters_unchanged:
            raise RuntimeError('a tile step arrived while taking back the local token')
        if current.values.get('local.token') == 'held':
            native_picker.token_held(current, 'css')
            ui = native_picker.wait_scene_transition(
                io, before, until, TRAINING_CSS, TRAINING_SSS, label='Training return',
                pending_destinations=(*_TRAINING_SSS_SETTLING, _GAMES_TRAINING_SETTLING),
                additional_targets=(GAMES_TRAINING,), accept_source=True,
            )
            io.finish()
            return ('css-token-back' if _fields_match(ui, TRAINING_CSS) else 'css'), ui
        if (current.values.get('local.token') != 'placed'
                or any(current.values.get(key) != tile_before.values.get(key)
                       for key in _PLACED_IDENTITY)):
            raise RuntimeError('local medal changed while taking it back')
    ui = native_picker.wait_scene_transition(
        io, before, until, TRAINING_CSS, TRAINING_SSS, label='Training return',
        pending_destinations=(*_TRAINING_SSS_SETTLING, _GAMES_TRAINING_SETTLING),
        additional_targets=(GAMES_TRAINING,),
    )
    io.finish()
    return 'css', ui


def back_to_main(ctx, presses=10):
    """Return through fenced Training owners, taking back a placed CSS token first."""
    looks = []
    io = BridgePickerIO(ctx)
    state = native_picker.wait_sample(io, native_picker.UI, time.monotonic() + 4)
    if _fields_match(state, TRAINING_SCREEN):
        quit_training(ctx)
        looks.append('training-quit')
        next_state = native_picker.wait_sample(io, native_picker.UI, time.monotonic() + 9)
        native_picker.same_epoch(state, next_state, in_flight=True)
        state = next_state

    # With Instant Back the CSS hold can return directly to Games & More.
    # Its unowned shell grants no further input until the Training row qualifies.
    return_edges = (
        (TRAINING_CSS, TRAINING_SSS, 'leave-css', 'css',
         (*_TRAINING_SSS_SETTLING, _GAMES_TRAINING_SETTLING), (GAMES_TRAINING,)),
        (TRAINING_SSS, GAMES_TRAINING, 'tap-b1', 'sss', (_GAMES_TRAINING_SETTLING,), ()),
        (GAMES_TRAINING, MAIN_TOP, 'tap-b1', 'games_and_more', (), ()),
    )
    for _ in range(presses):
        if _main_top(state):
            return _save(ctx, 'routes-back-to-main', {'looks': looks, **_navigation_receipt(state)})
        if _fields_match(state, TRAINING_CSS):
            after_token = _take_back_training_token(io, state)
            if after_token is not None:
                label, state = after_token
                looks.append(label)
                continue
        for source, target, macro, label, pending, additional_targets in return_edges:
            if _fields_match(state, source):
                break
        else:
            raise _missing_navigation_state('back_to_main', state, 'qualified Training return owner')
        _, state = _watched_native_input(io, state, source, target, macro, time.monotonic() + 9,
                                         route='Training return', scene_change=True,
                                         pending_destinations=pending, additional_targets=additional_targets)
        looks.append(label)
        io.finish()
    if _main_top(state):
        return _save(ctx, 'routes-back-to-main', {'looks': looks, **_navigation_receipt(state)})
    raise _missing_navigation_state('back_to_main', state, f'qualified Main top within {presses} return moves')


# --- a real Switch (switch_hw.py up; the pad plays the same macros, OBS stills feed the image gates) ----------------

def _need_switch(ctx, block):
    if not on_switch(ctx):
        raise RuntimeError(f'{block} drives a real Switch; this instance is not one')


def switch_boot(ctx, cap=420):
    """Finish an already launched Switch game at qualified Main."""
    _need_switch(ctx, 'switch_boot')
    try:
        result = to_main(ctx, cap=cap)
    except RuntimeError as exc:
        raise RuntimeError(
            f'Switch boot needs qualified Title or Main from ssbu.ui. '
            f'Switch HOME and system applets have no bridge owner or focus. {exc}'
        ) from exc
    return _save(ctx, 'routes-switch-boot', result)


# --- TrainingRooms' preloader ("Fighters" notice): no menu input until it is done ---------------------------------
# After a Training scene starts, TR's Cycle Preload builds the planned fighters and reports it in TR's notice box
# (the middle of the screen): "Fighters / Loading n/m" while it works, then one final state ("All loaded (..)",
# "Ready n/m", "Loaded n/m", "Swap .. to load"). Pad input is lost while it builds (switch-hw-session-1 T-01, 15:03Z:
# an R into the TR menu was lost at "Loading 4/7", 12 s after the Training HUD). Each Loading state shows 90 frames
# and the final one 180 (Swap: 120), so the box also disappears between steps: an absent box alone is no proof.
# The release build logs none of it ([PreloadStatus] is diagnostic-only); its loads show as "[NRO hook] Loaded x".
# The quick menu hides the notice. References: hw-notice-fighters / hw-notice-loading (hardware OBS frames).
NOTICE_INSIDE = (400, 294, 880, 306)  # inside the box, above its header text
NOTICE_ABOVE = (400, 272, 880, 284)   # the stage just above the box
NOTICE_WORD_SLIDE = 48                # "Loading n/m" is centred, so two-digit counts move the word
NRO_LOADED = re.compile(rb'\[NRO hook\] Loaded ')


def _ink(image):
    """Text mask of a crop: pixels brighter than halfway between its darkest and brightest. Dimming keeps it."""
    gray = image.convert('L')
    lo, hi = gray.getextrema()
    if hi - lo < 40:
        return None
    cut = (lo + hi) / 2
    return [v > cut for v in gray.tobytes()]


def ink_mismatch(a, b):
    """Differing text pixels over all text pixels of two masks (0 = the same glyphs, 1 = nothing in common)."""
    if a is None or b is None or len(a) != len(b):
        return 1.0
    union = sum(1 for x, y in zip(a, b) if x or y)
    return sum(1 for x, y in zip(a, b) if x != y) / union if union else 1.0


def _notice_ref(name):
    from PIL import Image
    entry = json.loads((ROUTES / 'refs' / 'hw-refs.json').read_text())[name]
    path = ROUTES / 'refs' / f'{name}.png'
    if _sha256(path) != entry['sha256']:
        raise RuntimeError(f'{path} does not match its sha256 {entry["sha256"][:12]}')
    with Image.open(path) as im:
        return _ink(im), tuple(entry['roi'])


def preload_notice(png, limit=0.3):
    """What TR's notice box shows in one 1280x720 frame: 'loading' (the preloader at "Loading n/m"), 'fighters'
    (the preloader at any other state: its work is over), 'other' (another notice), or None (no box)."""
    from PIL import Image, ImageStat
    with Image.open(png) as im:
        frame = im.convert('RGB')
    if frame.size != (1280, 720):
        frame = frame.resize((1280, 720), Image.Resampling.BOX)
    gray = frame.convert('L')
    inside, above = ImageStat.Stat(gray.crop(NOTICE_INSIDE)), ImageStat.Stat(gray.crop(NOTICE_ABOVE))
    # A flat near-black band under the lit stage; a dimmed screen keeps the ratio, the TR menu is dark above too.
    if not (inside.stddev[0] < 6 and inside.mean[0] < 40 and above.mean[0] > 2 * inside.mean[0] + 15):
        return None
    header, roi = _notice_ref('hw-notice-fighters')
    if ink_mismatch(_ink(frame.crop(roi)), header) > limit:
        return 'other'
    word, (x0, y0, x1, y1) = _notice_ref('hw-notice-loading')
    best = min(ink_mismatch(_ink(frame.crop((x0 + dx, y0, x1 + dx, y1))), word)
               for dx in range(-NOTICE_WORD_SLIDE, NOTICE_WORD_SLIDE + 1))
    return 'loading' if best <= limit else 'fighters'


def _preload_record(ctx):
    out = getattr(ctx.runner, 'out', None)
    return Path(out) / 'routes-preload.json' if out else None


def _log_size(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def _loaded_since(path, offset):
    """True when the log gained an "[NRO hook] Loaded" line after offset (or shrank: rotated, unknown)."""
    size = _log_size(path)
    if size is None or size < offset:
        return True
    with open(path, 'rb') as f:
        f.seek(offset)
        return bool(NRO_LOADED.search(f.read()))


def preload_clear(ctx, cap=180, appear=10.0):
    """Gate: TR's preloader is done in this Training scene, so menu input isn't lost. Call it on the Training HUD.

    Passes on the preloader's final notice (a "Fighters" notice that isn't "Loading"), or when no notice comes within
    `appear` seconds and the log shows no fighter load meanwhile (nothing to preload). A pass records the console log
    size in out/routes-preload.json; later calls pass at once while the log gains no "[NRO hook] Loaded" line (a new
    scene or a fighter change loads one and re-arms the gate). Not a sleep: every wait ends on what the screen shows.
    """
    try:
        log = instance_path(ctx, 'console_log')
    except KeyError:
        log = None
    record = _preload_record(ctx)
    if log is not None and record is not None and record.is_file():
        last = json.loads(record.read_text())
        if last.get('log') == str(log) and not _loaded_since(log, last.get('size', 0)):
            return {'preload': 'done earlier', 'since': last.get('at')}
    start = time.monotonic()
    offset = _log_size(log) if log is not None else None
    quiet_since = start
    looks, kept = [], []
    while True:
        ctx.check_stop()
        png = Path(frame(ctx, 'preload'))
        kind = preload_notice(png)
        now = time.monotonic()
        if log is not None and offset is not None and _loaded_since(log, offset):
            quiet_since, offset = now, _log_size(log) or 0
        if not looks or kind != looks[-1][1]:
            looks.append((round(now - start, 1), kind))
            kept.append(png)
        elif png not in kept:
            png.unlink(missing_ok=True)
        seen = any(k == 'loading' for _, k in looks)
        done = None
        if kind == 'fighters':
            done = 'final notice'
        elif kind != 'loading' and not seen and now - start >= appear and now - quiet_since >= appear / 2:
            # No box, or another notice (a minigame's standing score, FS-58 "Eggs-ercise"), which the preloader's
            # notice would queue behind: no Loading seen and no fighter load in the log meanwhile.
            done = 'no notice'
        if done:
            result = {'preload': done, 'seconds': round(now - start, 1), 'looks': looks,
                      'evidence': [str(p) for p in kept[-3:]]}
            if record is not None and log is not None:
                record.write_text(json.dumps({'log': str(log), 'size': _log_size(log) or 0,
                                              'at': time.strftime('%H:%M:%SZ', time.gmtime()), **result},
                                             default=str) + '\n')
            ctx.log({'gate': 'preload-clear', 'on': ctx.on, 'pass': True, **result})
            return result
        if now - start > cap:
            raise RuntimeError(f'preload_clear: the preloader did not finish within {cap} s; looks {looks[-6:]}; '
                               f'last {png}')
        time.sleep(0.3)


def main_menu_step(ctx, direction, from_focus, to_focus, cap=5):
    """Check caller-supplied native top-tab endpoints around exactly one D-pad tap."""
    if not isinstance(direction, str) or direction not in native_picker.DIRECTIONS:
        raise RuntimeError(f'unsupported Main Menu direction {direction!r}')
    if from_focus not in MAIN_TABS or to_focus not in MAIN_TABS or from_focus == to_focus:
        raise RuntimeError('Main Menu step needs two distinct canonical top-tab focus IDs')

    source, target = _main_tab(from_focus), _main_tab(to_focus)

    io = BridgePickerIO(ctx)
    until = time.monotonic() + cap
    initial = native_picker.wait_sample(io, native_picker.UI, until)
    before, state = _watched_native_input(io, initial, source, target, f'main-menu-{direction}', until)
    io.finish()
    return _save(ctx, 'routes-main-menu-step', {
        'direction': direction, 'from_focus': from_focus, 'to_focus': to_focus,
        'boot': state.clock[0], 'from_observation': before.clock[2],
        'to_observation': state.clock[2],
    })


def _armed_native_sample(io, initial, until, route, *, settling=False):
    remaining = until - time.monotonic()
    if remaining <= 0:
        raise RuntimeError(f'{route} source deadline expired before arming ssbu.ui')
    io.arm(native_picker.UI, remaining)
    if io.connection != initial.connection or io.boot != initial.clock[0]:
        raise RuntimeError(f'bridge connection or boot changed while arming {route} watch')
    before = native_picker.wait_sample(io, native_picker.UI, until, initial, loading_ui=settling)
    if before.connection != initial.connection:
        raise RuntimeError(f'bridge connection changed before {route} input')
    return before


def _watched_native_input(io, initial, source, target, macro, until, *, route='Main Menu', scene_change=False,
                          settling=False, pending_destinations=(), additional_targets=()):
    source_focus = source.get('focus', source.get('focus.id', source['screen']))
    if not _fields_match(initial, source):
        raise RuntimeError(f'{route} source must be native focus {source_focus!r}')
    before = _armed_native_sample(io, initial, until, route, settling=settling)
    if not _fields_match(before, source):
        raise RuntimeError(f'{route} source changed before {macro!r} at {source_focus!r}')

    io.send(macro)
    if scene_change:
        return before, native_picker.wait_scene_transition(
            io, before, until, source, target, label=route, pending_destinations=pending_destinations,
            additional_targets=additional_targets,
        )
    state = before
    while True:
        state = native_picker.wait_sample(io, native_picker.UI, until, state, in_flight=True)
        if _fields_match(state, target) and state.values['focus'] != before.values['focus']:
            return before, state
        if not _fields_match(state, source):
            expected = target.get('focus', target.get('focus.id', target['screen']))
            raise RuntimeError(f'unexpected {route} focus {state.values["focus"]!r} after {macro!r}; '
                               f'expected {expected!r}')


def _native_main_to_stage(ctx):
    """Enter Training Stage Select through the verified Games & More checkpoints."""
    menu = {'screen': 'main_menu', 'screen.tab': 'games_and_more', 'input.owner': 'native',
            'dialog': 'none', 'focus.source': 'native'}
    checkpoints = [
        ({**menu, 'screen.layer': layer, 'focus.id': focus, 'focus': f'main_menu.{layer}.{focus}'}, macro)
        for layer, focus, macro in (
            ('top', 'games_and_more', 'tap-a1'),
            ('submenu', 'classic_mode', 'main-menu-down'),
            ('submenu', 'mob_smash', 'main-menu-left'),
            ('submenu', 'training', 'tap-a1'),
        )
    ]
    io = BridgePickerIO(ctx)
    state = native_picker.wait_sample(io, native_picker.UI, time.monotonic() + 5)
    for source, direction, target in (('smash', 'right', 'vault'), ('vault', 'up', 'games_and_more')):
        if _fields_match(state, _main_tab(source)):
            _, state = _watched_native_input(io, state, _main_tab(source), _main_tab(target),
                                             f'main-menu-{direction}', time.monotonic() + 5)
            io.finish()
    for index, (source, _) in enumerate(checkpoints):
        if _fields_match(state, source):
            break
    else:
        raise _missing_navigation_state('Training entry', state, 'qualified route to Games & More or Training')

    settling = False
    for (source, macro), (target, _) in zip(checkpoints[index:], checkpoints[index + 1:]):
        # Opening the submenu invalidates the previous Lua focus until the
        # new layer activates. Send nothing while that transition settles.
        layer_change = source['screen.layer'] != target['screen.layer']
        pending = ()
        if layer_change:
            pending = ({
                **_UNOWNED_FOCUS,
                'screen': 'main_menu', 'screen.tab': 'games_and_more', 'screen.layer': 'submenu',
                'focus': 'main_menu.submenu', 'screen.context': 'unknown',
            },)
        _, state = _watched_native_input(
            io, state, source, target, macro, time.monotonic() + 5, scene_change=layer_change,
            settling=settling, pending_destinations=pending,
        )
        io.finish()
        settling = layer_change

    stage = {'screen': 'stage_select', 'screen.context': 'training', 'input.owner': 'native',
             'dialog': 'none', 'focus.source': native_picker.TILE}
    source, macro = checkpoints[-1]
    until = time.monotonic() + 45
    _, selected = _watched_native_input(io, state, source, stage, macro, until,
                                        route='Training Stage Select', scene_change=True,
                                        pending_destinations=_TRAINING_SSS_SETTLING)
    native_picker.wait_stage_tile(io, selected, until)
    active = native_picker.wait_sample(io, native_picker.UI, until, selected, in_flight=True)
    if not _fields_match(active, stage):
        raise RuntimeError('native Training Stage Select ownership changed during tile.cursor check')
    io.finish()
    return {'screen': 'stage', 'proof': active.values['focus']}


def quit_training(ctx, cap=45):
    """Open native Training Pause, focus Quit, and prove the return to Training CSS."""
    owner = {'screen.context': 'training', 'input.owner': 'native', 'dialog': 'none'}
    training = {**owner, 'screen': 'training'}
    pause = {**owner, 'screen': 'pause', 'focus.source': 'native'}
    cpu = {**pause, 'focus.id': 'cpu_count'}
    quit_focus = {**pause, 'focus.id': 'quit_training'}
    css = TRAINING_CSS
    transitions = (
        (training, cpu, 'pause-open'),
        (cpu, quit_focus, 'focus-native-quit-frames'),
        (quit_focus, css, 'confirm-native-quit-frames'),
    )
    io = BridgePickerIO(ctx)
    until = time.monotonic() + cap
    state = native_picker.wait_sample(io, native_picker.UI, until)
    for source, target, macro in transitions:
        _, state = _watched_native_input(io, state, source, target, macro, until, route='Training',
                                         scene_change=target is css,
                                         pending_destinations=(_TRAINING_CSS_SETTLING,) if target is css else ())
        io.finish()
    boot, revision, observation = state.clock
    return _save(ctx, 'routes-quit-training', {
        'screen': state.values['screen'], 'proof': state.values['focus'],
        'boot': boot, 'revision': revision, 'observation': observation,
    })


def _fields_match(state, criteria):
    return all(state.values.get(key) == value for key, value in criteria.items())


def _native_enter_training(ctx, cap, stage_ids):
    io = BridgePickerIO(ctx)
    until = time.monotonic() + 5
    ui = native_picker.wait_sample(io, native_picker.UI, until)
    native_picker.native_input_owner(ui, 'css', expected_context='training')
    tile = native_picker.wait_sample(io, native_picker.TILE, until)
    native_picker.same_epoch(ui, tile, in_flight=True)
    ui = _armed_native_sample(io, ui, until, 'Training')
    native_picker.native_input_owner(ui, 'css', expected_context='training')
    tile = native_picker.wait_sample(io, native_picker.TILE, until, tile)
    native_picker.same_epoch(ui, tile, in_flight=True)
    native_picker.context(tile, 'css')
    if tile.values.get('local.token') != 'placed' or tile.values.get('carried') != 'none':
        raise RuntimeError('Training entry needs a verified, placed local fighter')
    native_picker.number(tile, 'placed.at')
    native_picker.Target.read(tile, 'placed.')
    owner = {'screen.context': 'training', 'input.owner': 'native', 'dialog': 'none'}
    source = {**owner, 'screen': 'css.training', 'focus.source': native_picker.TILE, 'focus': ui.values['focus']}
    live = {**owner, 'screen': 'training'}
    io.mark_training_stage(ui, 5)
    io.send('enter-training')
    after = native_picker.wait_scene_transition(io, ui, time.monotonic() + cap, source, live, label='native')
    stage_id = expected(ctx).get('stage_id')
    if stage_ids is None and stage_id is not None:
        stage_ids = {stage_id}
    actual_stage, stage_event = io.training_stage(min(6, cap))
    if stage_ids is not None and actual_stage not in stage_ids:
        raise RuntimeError(f'Training loaded stage {actual_stage!r}; expected one of {sorted(stage_ids)}')
    io.finish()
    return {'focus': after.values['focus'], 'stage_id': actual_stage, 'stage_event': stage_event}

def _tile_topology(pin):
    if pin is None:
        return None
    if not isinstance(pin, dict) or set(pin) != {'path', 'sha256'}:
        raise RuntimeError('native tile topology needs a path and SHA-256 pin')
    artifact = Path(pin['path']).read_bytes()
    if hashlib.sha256(artifact).hexdigest() != pin['sha256']:
        raise RuntimeError('native tile topology SHA-256 changed')
    data = json.loads(artifact)
    return native_picker.Topology(data['layout'], data['neighbors'],
                                  {index: native_picker.Target(**identity)
                                   for index, identity in data['targets'].items()})


def tr_grid_step(cell, direction):
    if not isinstance(cell, int) or isinstance(cell, bool) or not 0 <= cell < 87:
        raise ValueError(f'invalid Training roster cell {cell!r}')
    if direction not in ('left', 'up', 'right', 'down'):
        raise ValueError(f'invalid Training roster direction {direction!r}')
    cols, rows = 13, 7
    row, index = divmod(cell, cols)
    length = min(cols, 87 - row * cols)
    if direction in ('left', 'right'):
        return row * cols + (index + (-1 if direction == 'left' else 1)) % length
    column = index + (cols - length) // 2
    next_row = (row + (-1 if direction == 'up' else 1)) % rows
    next_len = min(cols, 87 - next_row * cols)
    first = (cols - next_len) // 2
    return next_row * cols + min(max(column, first), first + next_len - 1) - first


def tr_grid_path(start, goal):
    tr_grid_step(start, 'left')
    tr_grid_step(goal, 'left')
    queue, visited = deque([(start, [])]), {start}
    while queue:
        cell, path = queue.popleft()
        if cell == goal:
            return path
        for direction in ('left', 'up', 'right', 'down'):
            next_cell = tr_grid_step(cell, direction)
            if next_cell not in visited:
                visited.add(next_cell)
                queue.append((next_cell, [*path, direction]))
    raise RuntimeError('Training roster target is unreachable')


def library_topology(name):
    try:
        entry = routes_index()['topologies'][name]
    except KeyError:
        raise RuntimeError(f'no library topology pin for {name!r}') from None
    path = ROUTES / entry['file']
    if _sha256(path) != entry['sha256']:
        raise RuntimeError(f'library topology SHA-256 changed: {name}')
    return {'path': str(path), 'sha256': entry['sha256']}


def _parking_tile(topology, start, target):
    queue, visited = deque([(start, 0)]), {start}
    while queue:
        tile, distance = queue.popleft()
        if distance >= 2 and tile in topology.targets and tile != target:
            return tile
        for dest in topology.neighbors.get(tile, {}).values():
            if dest not in visited:
                visited.add(dest)
                queue.append((dest, distance + 1))
    raise RuntimeError('no qualified parking tile at least two steps from P1')


def pick_cpu(ctx, fighter, *, topology=None, form='default'):
    """Place the CPU coin from a qualified Training fighter-select observation."""
    entry = fighter_entry(fighter)
    target = _native_target(entry, form)
    topology = _tile_topology(topology or library_topology('css-training'))
    io = BridgePickerIO(ctx)
    started = time.monotonic()
    timings = {}

    def phase(name, function, *args, **kwargs):
        begin = time.monotonic()
        result = function(*args, **kwargs)
        timings[name] = round((time.monotonic() - begin) * 1000)
        return result

    current, _ = native_picker._coin_baseline(io, 4, 'training')
    native_picker._p1_placed(current)
    if topology is None or topology.layout != current.values['layout']:
        raise RuntimeError('CPU placement needs the qualified active CSS topology')
    original = native_picker.p1_medal(current)
    if topology.targets.get(original[1]) != native_picker.Target.read(current, 'placed.'):
        raise RuntimeError('P1 medal identity disagrees with the qualified topology')
    goals = [tile for tile, identity in topology.targets.items() if identity == target]
    if len(goals) != 1:
        raise RuntimeError('CPU target identity is absent or ambiguous in the active topology')
    goal = goals[0]
    already = (current.values.get('cpu.token') == 'placed'
               and tuple(current.values.get('cpu.placed.' + key) for key in ('at', 'kind', 'name', 'form'))
               == (goal, target.kind, target.name, target.form))
    untangled, parked = False, None
    final = current
    skipped = []
    if already:
        skipped = ['reach', 'take', 'walk', 'place']
    else:
        try:
            phase('reach', native_picker.reach_token, io, 1)
        except native_picker.CoinBlocked as blocked:
            if blocked.pointed != '0':
                raise
            untangled = True
            phase('take_p1', native_picker.take_token, io, 0)
            parked = _parking_tile(topology, original[1], goal)
            prepared = phase('park_walk', native_picker.hover, io, 'css', topology.targets[parked], topology,
                             expected_context='training')
            phase('park_place', native_picker.confirm, io, 'css', prepared, expected_context='training')
            parked_state, _ = native_picker._coin_baseline(io, 4, 'training')
            native_picker._p1_placed(parked_state)
            parked_medal = native_picker.p1_medal(parked_state)
            if parked_medal[1] != parked:
                raise RuntimeError('P1 parking placement did not reach the chosen tile')
            phase('reach_after_parking', native_picker.reach_token, io, 1)
        else:
            parked_medal = original
        phase('take', native_picker.take_token, io, 1)
        _, reached = phase('walk', native_picker.walk_cpu, io, target, topology)
        final = phase('place', native_picker.place_cpu, io, target, reached, parked_medal)
        if untangled:
            phase('reach_p1', native_picker.reach_token, io, 0)
            phase('take_parked_p1', native_picker.take_token, io, 0)
            prepared = phase('restore_walk', native_picker.hover, io, 'css', topology.targets[original[1]], topology,
                             expected_context='training')
            phase('restore_place', native_picker.confirm, io, 'css', prepared, expected_context='training')
            final, _ = native_picker._coin_baseline(io, 4, 'training')
            native_picker._p1_placed(final)
            if native_picker.p1_medal(final) != original:
                raise RuntimeError('P1 medal did not return to its original identity and tile')
            if (final.values.get('cpu.token') != 'placed'
                    or tuple(final.values.get('cpu.placed.' + key) for key in ('at', 'kind', 'name', 'form'))
                    != (goal, target.kind, target.name, target.form)):
                raise RuntimeError('CPU placement changed while restoring P1')
    remember(ctx, cpu_kind=entry['kind'], cpu=entry['name'])
    receipt = {
        'block': 'pick_cpu', 'args': {'fighter': fighter, 'form': form},
        'verified': {'cpu': {'tile': goal, 'kind': target.kind, 'name': target.name, 'form': target.form},
                     'p1_medal': list(native_picker.p1_medal(final))},
        'inputs': getattr(io, 'input_count', len(getattr(io, 'actions', ()))),
        'bridge_requests': getattr(io, 'request_count', 0),
        'ms': {**timings, 'total': round((time.monotonic() - started) * 1000)},
        'bridge_boot': final.block['boot'], 'revisions': getattr(io, 'revisions', {}),
        'skipped': skipped, 'already': already, 'untangled': untangled, 'parked_at': parked,
    }
    return _save(ctx, 'routes-pick-cpu', receipt)


def _native_target(entry, form):
    return native_picker.Target(str(entry.get('kind', entry.get('id'))), entry['name'], form)


def _stage_pick_context(next_screen):
    return 'training' if next_screen == 'css.training' else None


def _native_prepare(ctx, screen, entry, topology, form, next_screen):
    # The checkpoint lives only in the current runner and console instance.
    pending = getattr(ctx.runner, 'native_picks', {})
    pending.pop(ctx.on, None)
    ctx.runner.native_picks = pending
    io = BridgePickerIO(ctx)
    prepared = native_picker.hover(io, screen, _native_target(entry, form), _tile_topology(topology),
                                   expected_context=_stage_pick_context(next_screen))
    pending[ctx.on] = (io, screen, prepared, entry, next_screen)
    return {'expect': entry['name'], 'kind': prepared[2].kind, 'tile': prepared[0].values['at']}


def _finish_native_pick(ctx, io, screen, prepared, entry, next_screen, transition_cap, *, expected_context=None):
    if screen == 'sss':
        expected_context = _stage_pick_context(next_screen)
    result = native_picker.confirm(io, screen, prepared, next_screen=next_screen, transition_cap=transition_cap,
                                   expected_context=expected_context)
    if screen == 'sss':
        remember(ctx, stage_id=entry['id'], stage=entry['name'], tile=int(result['tile']))
    elif result['seat'] == '0':
        remember(ctx, p1_kind=entry['kind'], p1=entry['name'])
    return _save(ctx, 'routes-confirm-pick', result)


def pick_stage_hw(ctx, stage, allow_derived=False, *, topology=None, form='normal', next_screen='css.training'):
    """Prepare a state-checked stage hover. confirm_pick rechecks it before A."""
    _need_switch(ctx, 'pick_stage_hw')
    return _native_prepare(ctx, 'sss', stage_entry(stage), topology, form, next_screen)


def pick_fighter_hw(ctx, p1, *, topology=None, form='default'):
    """Prepare a state-checked local fighter hover. confirm_pick places the local token."""
    _need_switch(ctx, 'pick_fighter_hw')
    return _native_prepare(ctx, 'css', fighter_entry(p1), topology, form, None)


def confirm_pick(ctx, expect, transition_cap=60):
    """Confirm the named tile prepared on this runner instance, from a fresh owner observation."""
    _need_switch(ctx, 'confirm_pick')
    pending = getattr(ctx.runner, 'native_picks', {}).pop(ctx.on, None)
    if pending is None or _norm(pending[3]['name']) != _norm(expect):
        raise RuntimeError(f'no pending native pick for {expect!r} on this instance')
    return _finish_native_pick(ctx, *pending, transition_cap)


def pick_local_fighter(ctx, fighter, *, topology=None, form='default', take_back=False, expected_context=None):
    """Pick and verify the local controller's fighter, including a Local Wireless guest's native P2."""
    entry = fighter_entry(fighter)
    io = BridgePickerIO(ctx)
    target = _native_target(entry, form)
    topology = _tile_topology(topology)
    if take_back:
        result = native_picker.acquire_local_token(io, target, topology, expected_context=expected_context)
        if result is not None:
            if result['seat'] == '0':
                remember(ctx, p1_kind=entry['kind'], p1=entry['name'])
            return _save(ctx, 'routes-confirm-pick', result)
    prepared = native_picker.hover(io, 'css', target, topology, expected_context=expected_context)
    return _finish_native_pick(ctx, io, 'css', prepared, entry, None, 60, expected_context=expected_context)


def ensure_local_fighter_placed(ctx, fighter=None, *, topology=None, expected_context=None):
    """Verify a retained local medal or confirm the settled native choice."""
    if fighter is not None:
        return pick_local_fighter(ctx, fighter, topology=topology, take_back=True,
                                  expected_context=expected_context)
    io = BridgePickerIO(ctx)
    until = time.monotonic() + 5
    ui = native_picker.wait_sample(io, native_picker.UI, until)
    native_picker.native_input_owner(ui, 'css', expected_context=expected_context)
    current = native_picker.wait_sample(io, native_picker.TILE, until)
    native_picker.same_epoch(ui, current, in_flight=True)
    native_picker.context(current, 'css')
    if current.values.get('local.token') == 'placed' and current.values.get('carried') == 'none':
        native_picker.number(current, 'placed.at')
        identity = native_picker.Target.read(current, 'placed.')
        return {'name': identity.name, 'kind': identity.kind, 'form': identity.form, 'placed': True}
    prepared = native_picker.hover(io, 'css', None, expected_context=expected_context)
    return native_picker.confirm(io, 'css', prepared, expected_context=expected_context)

def switch_to_training(ctx, p1=None, *, stage_topology=None, fighter_topology=None):
    """Games & More → Training through state-checked stage selection and local placement."""
    _need_switch(ctx, 'switch_to_training')
    out = {'stage': main_to_stage(ctx)}
    pick_stage_hw(ctx, 'Training', topology=stage_topology)
    confirm_pick(ctx, 'Training')
    out['fighter'] = ensure_local_fighter_placed(
        ctx, p1 or ctx.instance.get('native_training_fighter'),
        topology=fighter_topology or ctx.instance.get('native_css_topology'), expected_context='training')
    out['training'] = enter_training(ctx)
    return _save(ctx, 'routes-switch-training', out)

def _wait_tr_menu(io, until, opened, after=None, *, strict=False):
    while (remaining := until - time.monotonic()) > 0:
        state = io.read('tr.menu', remaining)
        block = state.block
        if block['schema'] != 'tr.menu/1' or block['rev'] <= 0 or block['filtered']:
            raise RuntimeError('Training Mod menu needs a complete tr.menu/1 reply')
        if after is not None:
            native_picker.same_epoch(after, state, in_flight=True)
            if block['rev'] <= after.block['rev']:
                continue
        if _fields_match(state, {'scene': 'training', 'keyboard': '0', 'open': str(int(opened))}):
            return state
        if strict:
            raise RuntimeError('Training Mod menu changed scene, keyboard, or open state')
    raise RuntimeError(f'Training Mod menu needs tr.menu scene=training, keyboard=0, open={int(opened)}')


def _tr_menu_gate(ctx, cap):
    io = BridgePickerIO(ctx)
    until = time.monotonic() + cap
    menu = _wait_tr_menu(io, until, opened=True)
    _wait_training_owner(io, menu, until, 'tr.menu')
    _save(ctx, 'gate-routes.tr-menu', {'gate': 'routes.tr-menu', 'pass': True, 'provider': 'tr.menu',
                                     'boot': menu.block['boot'], 'revision': menu.block['rev'],
                                     'focus': menu.values['focus']})
    return str(Path(ctx.row_dir) / 'gate-routes.tr-menu.json')


def _wait_training_owner(io, menu, until, owner):
    expected = {**TRAINING_SCREEN, 'input.owner': owner}
    ui = None
    while time.monotonic() < until:
        ui = native_picker.wait_sample(io, native_picker.UI, until, ui, in_flight=True)
        native_picker.same_epoch(menu, ui, in_flight=True)
        if _fields_match(ui, expected):
            return ui
    raise _missing_navigation_state('Training Mod menu', ui, str(expected))


def tr_menu(ctx):
    """Open and close the Training Mod menu using fenced native and TR state."""
    io = BridgePickerIO(ctx)
    cap = SWITCH_CAPS['training'] if on_switch(ctx) else ROUTE_GATES['training']['cap']
    for opened, macro in ((False, 'tr-menu-open'), (True, 'tr-menu-close')):
        until = time.monotonic() + (10 if opened else cap)
        owner = 'tr.menu' if opened else 'native'
        source = {**TRAINING_SCREEN, 'input.owner': owner}
        current = _wait_tr_menu(io, until, opened=opened)
        ui = _wait_training_owner(io, current, until, owner)
        before = _armed_native_sample(io, ui, until, 'Training Mod menu')
        if not _fields_match(before, source):
            raise _missing_navigation_state('Training Mod menu', before, str(source))
        native_picker.same_epoch(before, current, in_flight=True)
        io.send(macro)
        until = time.monotonic() + (cap if opened else 10)
        confirmed = _wait_tr_menu(io, until, not opened, current)
        _wait_training_owner(io, confirmed, until, 'native' if opened else 'tr.menu')
        io.finish()
        if not opened:
            opened_revision = confirmed.block['rev']
    return _save(ctx, 'routes-tr-menu', {'opened': opened_revision, 'closed': confirmed.block['rev'],
                                         'boot': confirmed.block['boot']})


def _spool_file(ctx, name, data):
    directory = instance_path(ctx, 'bridge_log').parent / 'bridge'
    if not directory.is_dir():
        raise RuntimeError('bridge request spool is not available')
    path = directory / name
    if path.exists():
        raise RuntimeError(f'bridge request is already pending: {name}')
    temporary = directory / f'.{name}.{os.getpid()}.{time.time_ns()}.tmp'
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
        metrics = getattr(ctx.runner, 'setup_metrics', None)
        if metrics is not None:
            metrics['bridge_requests'] += 1
    finally:
        if temporary.exists():
            temporary.unlink()


def _wait_bridge_lines(ctx, cursor, until, predicate, label):
    while (remaining := until - time.monotonic()) > 0:
        ctx.check_stop()
        for line in cursor.read():
            if state_gate.BOUNDARY.search(line) or cursor.rotations:
                raise RuntimeError(f'bridge continuity changed while waiting for {label}')
            result = predicate(line)
            if result is not None:
                return result
        time.sleep(min(.05, remaining))
    raise RuntimeError(f'no fresh bridge {label} before deadline')


def _wait_writer(ctx, cursor, until):
    marker = '[TrainingRooms] settings.json written by the writer thread'
    line = _wait_bridge_lines(ctx, cursor, until, lambda line: line if line.startswith(marker) else None,
                              'settings writer')
    return line


def _fetch_settings(ctx, cap=20):
    log = instance_path(ctx, 'bridge_log')
    cursor = LogCursor(log, hold=False)
    _spool_file(ctx, 'bridge-file.req', b'settings.json\n')
    pattern = re.compile(r'\[Bridge\] file_end name=settings\.json bytes=(\d+) sha256=([0-9a-f]{64}) '
                         r'result=ok.*\bmirrored=true(?: |$)')
    line = _wait_bridge_lines(ctx, cursor, time.monotonic() + cap,
                              lambda value: value if pattern.search(value) else None, 'settings file')
    match = pattern.search(line)
    data = (log.parent / 'bridge' / 'files' / 'settings.json').read_bytes()
    if len(data) != int(match[1]) or hashlib.sha256(data).hexdigest() != match[2]:
        raise RuntimeError('fresh settings file does not match the bridge receipt')
    return data


def _apply_settings(ctx, data, cap=30):
    log = instance_path(ctx, 'bridge_log')
    cursor = LogCursor(log, hold=False)
    _spool_file(ctx, 'diag-settings-next.json', data)
    until = time.monotonic() + cap
    reply = writer = None
    while time.monotonic() < until and not (reply and writer):
        ctx.check_stop()
        for line in cursor.read():
            if state_gate.BOUNDARY.search(line) or cursor.rotations:
                raise RuntimeError('bridge continuity changed while applying settings')
            if '[Bridge] req set result=' in line:
                if not re.search(r'\[Bridge\] req set result=ok(?: |$)', line):
                    raise RuntimeError(line)
                reply = line
            if reply and line.startswith('[TrainingRooms] settings.json written by the writer thread'):
                writer = line
        if not (reply and writer):
            time.sleep(min(.05, max(0, until - time.monotonic())))
    if not (reply and writer):
        raise RuntimeError('no fresh settings set reply and writer line before deadline')
    metrics = getattr(ctx.runner, 'setup_metrics', None)
    if metrics is not None:
        metrics['bridge_requests'] += 1
    return _fetch_settings(ctx, cap=max(.01, until - time.monotonic())), reply


def _tr_input(io, before, macro, until, *, opened=True):
    old_ack = native_picker.number(before, 'ack')
    if time.monotonic() >= until:
        raise RuntimeError(f'Training Mod deadline expired before {macro}')
    io.send(macro)
    latest = before
    while (remaining := until - time.monotonic()) > 0:
        after = io.read('tr.menu', remaining)
        block = after.block
        if block['schema'] != 'tr.menu/1' or block['rev'] <= 0 or block['filtered']:
            raise RuntimeError('Training Mod menu needs a complete tr.menu/1 reply')
        native_picker.same_epoch(latest, after, in_flight=True)
        if block['rev'] <= latest.block['rev']:
            continue
        latest = after
        ack = native_picker.number(after, 'ack')
        if ack == old_ack:
            continue
        if ack != old_ack + 1 or after.values.get('ack.result') != 'handled':
            raise RuntimeError(f'Training Mod input {macro} did not advance exactly one handled ACK')
        if not _fields_match(after, {'scene': 'training', 'keyboard': '0', 'open': str(int(opened))}):
            raise RuntimeError('Training Mod menu changed scene, keyboard, or open state')
        io.finish()
        return after
    raise RuntimeError(f'Training Mod input {macro} was not handled')


def _roster_value(settings, key, kind):
    catalog = tables()['roster_catalog']
    wanted = [int(item['kind'] == kind) for item in catalog]
    if sum(wanted) != 1:
        raise RuntimeError(f'fighter kind {kind} is absent or ambiguous in the TR roster catalog')
    actual = settings.get('menu', {}).get(key)
    if actual != wanted:
        raise RuntimeError(f'settings menu.{key} is not the required one-hot fighter')
    return actual


def _live_fighter(ctx, seat, kind, cap):
    state = wait_native(ctx, lambda value: (
        0 in value.get('roster', {}) and 1 in value.get('roster', {})
        and value['roster'][seat].get('kind') == kind
    ), cap=cap, fresh=('roster',), roster_entries=(0, 1))
    roster = state.get('roster', {})
    if (0 not in roster or 1 not in roster or roster[seat].get('kind') != kind):
        raise RuntimeError(f'live roster does not confirm entry {seat} kind {kind}: {roster}')
    return roster


def _with_setup_metrics(ctx, action):
    metrics = {'inputs': 0, 'bridge_requests': 0, 'bridge_boot': None, 'revisions': {}}
    previous = getattr(ctx.runner, 'setup_metrics', None)
    ctx.runner.setup_metrics = metrics
    try:
        return action(metrics)
    finally:
        ctx.runner.setup_metrics = previous
        if previous is not None:
            _merge_setup_metrics(previous, metrics)


TR_PAGES = ('cpu', 'saves', 'recording', 'fighters_stages', 'display_controls', 'developer')


def _tr_current(io, until):
    remaining = until - time.monotonic()
    if remaining <= 0:
        raise RuntimeError('Training Mod deadline expired before the menu read')
    menu = io.read('tr.menu', remaining)
    if (menu.block['schema'] != 'tr.menu/1' or menu.block['rev'] <= 0 or menu.block['filtered']
            or menu.values.get('scene') != 'training' or menu.values.get('keyboard') != '0'
            or menu.values.get('open') not in ('0', '1')):
        raise RuntimeError('Training Mod needs a complete menu observation in Training without a keyboard')
    return menu


def _tr_depth(menu):
    return native_picker.number(menu, 'depth')


def _tr_back_to_page(io, menu, until):
    while _tr_depth(menu) > 0:
        before = _tr_depth(menu)
        menu = _tr_input(io, menu, 'tap-b1', until)
        if _tr_depth(menu) >= before:
            raise RuntimeError('Training Mod depth did not decrease after B')
    return menu


def _open_menu(io, until, page):
    menu = _tr_current(io, until)
    opened = menu.values['open'] == '1'
    owner = 'tr.menu' if opened else 'native'
    ui = _wait_training_owner(io, menu, until, owner)
    if not opened:
        before = _armed_native_sample(io, ui, until, 'Training Mod menu')
        if not _fields_match(before, {**TRAINING_SCREEN, 'input.owner': 'native'}):
            raise RuntimeError('Training Mod menu lacks native Training input owner')
        native_picker.same_epoch(before, menu, in_flight=True)
        if time.monotonic() >= until:
            raise RuntimeError('Training Mod deadline expired before opening the menu')
        io.send('tr-menu-open')
        menu = _wait_tr_menu(io, until, opened=True, after=menu)
        _wait_training_owner(io, menu, until, 'tr.menu')
        io.finish()
    skipped = ['open'] if opened else []
    if page is not None:
        menu = _tr_back_to_page(io, menu, until)
        for _ in range(len(TR_PAGES)):
            current = menu.values.get('page')
            if current == page:
                break
            if current not in TR_PAGES:
                raise RuntimeError(f'Training Mod current page is unknown: {current!r}')
            menu = _tr_input(io, menu, 'tap-r1', until)
            if menu.values.get('page') == current or _tr_depth(menu) != 0:
                raise RuntimeError('Training Mod page did not change at depth 0 after R')
        if menu.values.get('page') != page:
            raise RuntimeError(f'Training Mod page {page!r} was not reached within the page ring')
    return menu, skipped


def _close_menu(io, until):
    menu = _tr_current(io, until)
    opened = menu.values['open'] == '1'
    _wait_training_owner(io, menu, until, 'tr.menu' if opened else 'native')
    if not opened:
        return menu, ['close']
    menu = _tr_back_to_page(io, menu, until)
    menu = _tr_input(io, menu, 'tap-b1', until, opened=False)
    _wait_training_owner(io, menu, until, 'native')
    return menu, []


def tr_open(ctx, page=None, cap=20):
    if page is not None and page not in TR_PAGES:
        raise RuntimeError(f'unknown Training Mod page {page!r}')
    return _with_setup_metrics(ctx, lambda metrics: _tr_open(ctx, page, cap, metrics))


def _tr_open(ctx, page, cap, metrics):
    started = time.monotonic()
    menu, skipped = _open_menu(BridgePickerIO(ctx), started + cap, page)
    if menu.values.get('page') not in TR_PAGES or not menu.values.get('focus'):
        raise RuntimeError('Training Mod open menu page or focus is missing')
    elapsed = round((time.monotonic() - started) * 1000)
    return _save(ctx, 'routes-tr-open', {
        'block': 'tr_open', 'args': {'page': page, 'cap': cap},
        'verified': {'open': menu.values['open'], 'page': menu.values.get('page'),
                     'depth': _tr_depth(menu), 'owner': 'tr.menu', 'focus': menu.values.get('focus')},
        **metrics, 'ms': {'menu': elapsed, 'total': elapsed}, 'skipped': skipped,
    })


def tr_close(ctx, cap=20):
    return _with_setup_metrics(ctx, lambda metrics: _tr_close(ctx, cap, metrics))


def _tr_close(ctx, cap, metrics):
    started = time.monotonic()
    menu, skipped = _close_menu(BridgePickerIO(ctx), started + cap)
    elapsed = round((time.monotonic() - started) * 1000)
    return _save(ctx, 'routes-tr-close', {
        'block': 'tr_close', 'args': {'cap': cap}, 'verified': {'open': menu.values['open'], 'owner': 'native'},
        **metrics, 'ms': {'menu': elapsed, 'total': elapsed}, 'skipped': skipped,
    })


def _tr_path(path):
    if not isinstance(path, list) or not path:
        raise RuntimeError('Training Mod path must be a nonempty list')
    result = []
    for item in path:
        if isinstance(item, str) and item:
            result.append({'action': item})
        elif (isinstance(item, dict) and item and set(item) <= {'action', 'row'}
              and all(isinstance(value, str) and value for value in item.values())):
            result.append(dict(item))
        else:
            raise RuntimeError('Training Mod path element needs action and/or row')
    return result


def _tr_identity(menu):
    fields = ('action', 'row', 'row_id')
    if any(not isinstance(menu.values.get(key), str) for key in fields) or not menu.values['action']:
        raise RuntimeError('Training Mod focus identity is missing')
    return tuple(menu.values[key] for key in fields)


def _navigate_menu(io, menu, page, path, until):
    for index, target in enumerate(path):
        depth = _tr_depth(menu)
        group = menu.values.get('group')
        nested = depth > 0
        if nested and (not isinstance(group, str) or not group.isdigit()):
            raise RuntimeError('Training Mod Rows group is missing')
        seen = set()
        while not all(menu.values.get(key) == value for key, value in target.items()):
            before = _tr_identity(menu) if nested else native_picker.number(menu, 'cursor')
            if before in seen:
                raise RuntimeError(f'Training Mod row not found before wrapping: {target!r}')
            seen.add(before)
            after = _tr_input(io, menu, 'main-menu-down', until)
            if after.values.get('page') != page or _tr_depth(after) != depth:
                raise RuntimeError('Training Mod page or depth changed while seeking row')
            if nested:
                if after.values.get('group') != group:
                    raise RuntimeError('Training Mod Rows group changed while seeking row')
                current = _tr_identity(after)
                if current == before:
                    raise RuntimeError('Training Mod focus identity did not change after Down')
            else:
                current = native_picker.number(after, 'cursor')
                if current == before:
                    raise RuntimeError('Training Mod cursor did not change after Down')
            if current in seen:
                raise RuntimeError(f'Training Mod row not found before wrapping: {target!r}')
            menu = after
        if index + 1 < len(path):
            after = _tr_input(io, menu, 'tap-a1', until)
            if after.values.get('page') != page or _tr_depth(after) != depth + 1:
                raise RuntimeError('Training Mod page or depth did not enter the requested sub-screen')
            if not isinstance(after.values.get('group'), str) or not after.values['group'].isdigit():
                raise RuntimeError('Training Mod Rows group was not published after A')
            _tr_identity(after)
            menu = after
    return menu


def tr_goto(ctx, page, path, cap=30):
    if page not in TR_PAGES:
        raise RuntimeError(f'unknown Training Mod page {page!r}')
    targets = _tr_path(path)
    if type(cap) not in (int, float) or not 0 < cap < float('inf'):
        raise RuntimeError('Training Mod path needs a positive cap')
    return _with_setup_metrics(ctx, lambda metrics: _tr_goto(ctx, page, path, targets, cap, metrics))


def _tr_goto(ctx, page, path, targets, cap, metrics):
    started = time.monotonic()
    until = started + cap
    io = BridgePickerIO(ctx)
    menu, skipped = _open_menu(io, until, page)
    menu = _navigate_menu(io, menu, page, targets, until)
    _tr_identity(menu)
    if not menu.values.get('focus'):
        raise RuntimeError('Training Mod focus is missing')
    elapsed = round((time.monotonic() - started) * 1000)
    return _save(ctx, 'routes-tr-goto', {
        'block': 'tr_goto', 'args': {'page': page, 'path': path, 'cap': cap},
        'verified': {key: menu.values.get(key) for key in ('focus', 'page', 'group', 'cursor', 'action', 'row')}
                    | {'depth': _tr_depth(menu)},
        **metrics, 'ms': {'menu': elapsed, 'total': elapsed}, 'skipped': skipped,
    })


def drill_observe(ctx, attempts, cap, allow_stopped=False):
    if type(attempts) is not int or attempts <= 0 or type(cap) not in (int, float) or not 0 < cap < float('inf'):
        raise RuntimeError('drill observation needs positive attempts and cap')
    if type(allow_stopped) is not bool:
        raise RuntimeError('allow_stopped must be a boolean')
    return _with_setup_metrics(ctx, lambda metrics: _drill_observe(ctx, attempts, cap, allow_stopped, metrics))


def _attempt_trace_status(ctx, path):
    status = ctx.call('trace.list', {})
    point = trace_attempts.point_id(status, 'edge_juggle_attempt')
    if (status.get('output_error') or status.get('boot_error') or status.get('output') != path
            or 'file_lost' not in status):
        raise RuntimeError('attempt trace output is unavailable or mismatched')
    initial_point = next(item for item in status['points'] if isinstance(item, dict) and str(item.get('id')) == point)
    if 'dropped' not in initial_point:
        raise RuntimeError('attempt trace loss counter is unavailable')
    try:
        baseline_loss = (trace_attempts._decimal(status['file_lost']), trace_attempts._decimal(initial_point['dropped']))
    except ValueError:
        raise RuntimeError('attempt trace loss counter is unavailable') from None
    identity = (initial_point.get('process_id'), initial_point.get('address'))
    try:
        if int(identity[0]) <= 0 or int(identity[1], 0) <= 0:
            raise ValueError('invalid identity')
    except (TypeError, ValueError):
        raise RuntimeError('attempt trace point lacks a live process and address') from None
    return point, baseline_loss, identity


def _drill_observe(ctx, attempts, cap, allow_stopped, metrics):
    started = time.monotonic()
    until = started + cap
    path = ctx.instance.get('trace_jsonl')
    if not isinstance(path, str) or not path:
        raise RuntimeError('drill observation needs a trace_jsonl')
    stream = trace_attempts.Stream(path, '')
    trace_identity = _attempt_trace_status(ctx, path)
    point, loss, identity = trace_identity
    if loss != (0, 0):
        raise RuntimeError('attempt trace loss counters are nonzero at observation start')
    stream.point = point
    if _attempt_trace_status(ctx, path) != trace_identity:
        raise RuntimeError('attempt trace loss or output changed')
    io = BridgePickerIO(ctx)
    baseline = _tr_current(io, until)
    marks = []
    counts = {}

    def collect():
        for mark in stream.read(until):
            if mark['process'] != str(identity[0]):
                raise RuntimeError('attempt marker came from a different trace process')
            marks.append(mark)
            terminal = mark['terminal']
            if terminal:
                counts[terminal] = counts.get(terminal, 0) + 1

    while time.monotonic() < until:
        ctx.check_stop()
        collect()
        if sum(counts.values()) >= attempts:
            break
        time.sleep(min(.05, max(0, until - time.monotonic())))
    if sum(counts.values()) < attempts:
        raise RuntimeError(f'{sum(counts.values())} terminals observed before deadline; wanted {attempts}')
    final = _tr_current(io, until)
    native_picker.same_epoch(baseline, final, in_flight=True)
    running = final.values.get('game.running')
    if running not in ('0', '1'):
        raise RuntimeError('drill running state is missing')
    if _attempt_trace_status(ctx, path) != trace_identity:
        raise RuntimeError('attempt trace loss or output changed')
    collect()
    if time.monotonic() >= until:
        raise RuntimeError(f'{sum(counts.values())} terminals observed but drill observation deadline expired')
    elapsed = round((time.monotonic() - started) * 1000)
    receipt = _save(ctx, 'routes-drill-observe', {
        'block': 'drill_observe', 'args': {'attempts': attempts, 'cap': cap, 'allow_stopped': allow_stopped},
        'verified': {'game': final.values.get('game'), 'running': running,
                     'releases': sum(mark['event'] == 0 for mark in marks), 'terminals': counts,
                     'first_sequence': marks[0]['sequence'], 'last_sequence': marks[-1]['sequence'],
                     'first_frame': marks[0]['frame'], 'last_frame': marks[-1]['frame']},
        **metrics, 'ms': {'observe': elapsed, 'total': elapsed}, 'skipped': [],
    })
    if running == '0' and not allow_stopped:
        raise RuntimeError('drill stopped while observing attempts')
    return receipt


def _set_fighter(ctx, fighter, seat, cap, metrics):
    started = time.monotonic()
    entry = fighter_entry(fighter)
    io = BridgePickerIO(ctx)
    until = time.monotonic() + cap
    ui = native_picker.wait_sample(io, native_picker.UI, until)
    if not _fields_match(ui, {'screen.context': 'training', 'dialog': 'none'}):
        raise RuntimeError('fighter setup needs the Training context without a modal dialog')
    if _fields_match(ui, TRAINING_CSS):
        nested = (pick_cpu(ctx, fighter) if seat == 1 else
                  pick_local_fighter(ctx, fighter, topology=library_topology('css-training'),
                                     take_back=True, expected_context='training'))
        return _save(ctx, f'routes-set-{"cpu" if seat else "player"}', {
            'block': 'set_cpu' if seat else 'set_player', 'args': {'fighter': fighter},
            'verified': nested, 'skipped': ['tr_menu'], **metrics,
            'ms': {'css': round((time.monotonic() - started) * 1000),
                   'total': round((time.monotonic() - started) * 1000)},
        })
    if ui.values.get('screen') != 'training' or ui.values.get('input.owner') not in ('native', 'tr.menu'):
        raise RuntimeError('fighter setup needs native Training or Training Mod menu ownership')
    menu = io.read('tr.menu', max(.01, until - time.monotonic()))
    if (menu.block['schema'] != 'tr.menu/1' or menu.block['filtered']
            or menu.values.get('scene') != 'training' or menu.values.get('keyboard') != '0'
            or menu.values.get('open') not in ('0', '1')):
        raise RuntimeError('fighter setup needs a complete Training Mod menu observation')
    owner = 'tr.menu' if menu.values['open'] == '1' else 'native'
    ui = _wait_training_owner(io, menu, until, owner)
    ui = _armed_native_sample(io, ui, until, 'Training Mod menu')
    if not _fields_match(ui, {**TRAINING_SCREEN, 'input.owner': owner}):
        raise RuntimeError('Training Mod menu lacks the expected input ownership')
    native_picker.same_epoch(ui, menu, in_flight=True)
    if menu.values['open'] == '0':
        if time.monotonic() >= until:
            raise RuntimeError('Training Mod deadline expired before opening the menu')
        io.send('tr-menu-open')
        menu = _wait_tr_menu(io, until, opened=True, after=menu)
        _wait_training_owner(io, menu, until, 'tr.menu')
        io.finish()

    def depth(state):
        value = state.values.get('depth')
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise RuntimeError(f'Training Mod menu depth is unknown: {value!r}') from None
        if parsed < 0:
            raise RuntimeError(f'Training Mod menu depth is unknown: {value!r}')
        return parsed

    while depth(menu) > 0:
        old_depth = depth(menu)
        menu = _tr_input(io, menu, 'tap-b1', until)
        if depth(menu) >= old_depth:
            raise RuntimeError('Training Mod grid did not close after B')
    if menu.values.get('depth') != '0':
        raise RuntimeError('Training Mod menu depth is unknown')
    for _ in range(6):
        if menu.values.get('page') == 'fighters_stages':
            break
        previous = menu.values.get('page')
        menu = _tr_input(io, menu, 'tap-r1', until)
        if menu.values.get('page') == previous or menu.values.get('depth') != '0':
            raise RuntimeError('Training Mod page did not change after R')
    if menu.values.get('page') != 'fighters_stages':
        raise RuntimeError('Training Mod fighters_stages page not reached in six R presses')
    action = 'cpu_character' if seat else 'player_character'
    visited = set()
    while menu.values.get('action') != action:
        cursor = menu.values.get('cursor')
        if cursor is None or cursor in visited:
            raise RuntimeError(f'Training Mod row {action} was not found before wrapping')
        visited.add(cursor)
        menu = _tr_input(io, menu, 'main-menu-down', until)
        if menu.values.get('cursor') == cursor or menu.values.get('page') != 'fighters_stages':
            raise RuntimeError('Training Mod cursor did not move down')
    menu = _tr_input(io, menu, 'tap-a1', until)
    if menu.values.get('depth') != '1':
        raise RuntimeError('Training Mod fighter grid did not open')
    named = 'grid' in menu.values
    if named and menu.values['grid'] != action:
        raise RuntimeError(f'Training Mod grid is {menu.values["grid"]!r}, not {action!r}')
    try:
        cell = int(menu.values['editor.sel'])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('Training Mod grid editor.sel is unknown') from None
    if not 0 <= cell < 87:
        raise RuntimeError(f'Training Mod grid editor.sel is outside the roster: {cell}')
    for direction in tr_grid_path(cell, entry['tile']):
        predicted = tr_grid_step(cell, direction)
        menu = _tr_input(io, menu, f'main-menu-{direction}', until)
        if menu.values.get('editor.sel') != str(predicted):
            raise RuntimeError(f'Training Mod grid cell did not reach predicted {predicted}')
        if named and menu.values.get('grid') != action:
            raise RuntimeError('Training Mod grid identity changed during movement')
        cell = predicted
    if ('grid.kind' in menu.values and menu.values['grid.kind'] != str(entry['kind'])
            or 'grid.name' in menu.values and menu.values['grid.name'] != entry['name']):
        raise RuntimeError('Training Mod grid kind/name does not match the fighter')
    writer = LogCursor(instance_path(ctx, 'bridge_log'), hold=False)
    menu = _tr_input(io, menu, 'tap-a1', until)
    menu = _tr_input(io, menu, 'tap-b1', until)
    if menu.values.get('depth') != '0':
        raise RuntimeError('Training Mod fighter grid did not close')
    menu = _tr_input(io, menu, 'tap-b1', until, opened=False)
    _wait_training_owner(io, menu, until, 'native')
    timings = {'menu': round((time.monotonic() - started) * 1000)}
    phase = time.monotonic()
    writer_line = _wait_writer(ctx, writer, until)
    settings = json.loads(_fetch_settings(ctx, cap=max(.01, until - time.monotonic())))
    picked = _roster_value(settings, action, entry['kind'])
    timings['settings'] = round((time.monotonic() - phase) * 1000)
    phase = time.monotonic()
    roster = _live_fighter(ctx, seat, entry['kind'], max(.01, until - time.monotonic()))
    timings['roster'] = round((time.monotonic() - phase) * 1000)
    remember(ctx, **{f'{"cpu" if seat else "p1"}_kind': entry['kind'],
                     'cpu' if seat else 'p1': entry['name']})
    return _save(ctx, f'routes-set-{"cpu" if seat else "player"}', {
        'block': 'set_cpu' if seat else 'set_player', 'args': {'fighter': fighter},
        'verified': {'kind': entry['kind'], 'roster': roster, 'one_hot': picked},
        'grid_named': named, 'writer': writer_line, **metrics,
        'ms': {**timings, 'total': round((time.monotonic() - started) * 1000)}, 'skipped': [],
    })


def set_cpu(ctx, fighter, cap=60):
    return _with_setup_metrics(ctx, lambda metrics: _set_fighter(ctx, fighter, 1, cap, metrics))


def set_player(ctx, fighter, cap=60):
    return _with_setup_metrics(ctx, lambda metrics: _set_fighter(ctx, fighter, 0, cap, metrics))


def tr_settings(ctx, values=None, file=None, cap=20, *, verify_roster=False):
    return _with_setup_metrics(ctx, lambda metrics: _tr_settings(
        ctx, values, file, cap, verify_roster, metrics,
    ))


def _tr_settings(ctx, values, file, cap, verify_roster, metrics):
    if (values is None) == (file is None):
        raise ValueError('supply exactly one of values or file')
    start = time.monotonic()
    io = BridgePickerIO(ctx)
    native_picker.wait_sample(io, native_picker.UI, start + cap)
    remaining = cap - (time.monotonic() - start)
    if remaining <= 0:
        raise RuntimeError('settings deadline expired before the initial read')
    original = json.loads(_fetch_settings(ctx, cap=remaining))
    timings = {'read': round((time.monotonic() - start) * 1000)}
    if values is not None:
        if not isinstance(values, dict) or not isinstance(original.get('menu'), dict):
            raise ValueError('values and the current settings menu must be objects')
        changed = json.loads(json.dumps(original))
        changed['menu'].update(values)
        keys = list(values)
    else:
        changed = json.loads(Path(file).read_bytes())
        if not isinstance(changed, dict):
            raise ValueError('settings file must contain a JSON object')
        keys = list(changed)
    phase = time.monotonic()
    remaining = cap - (time.monotonic() - start)
    if remaining <= 0:
        raise RuntimeError('settings deadline expired before set')
    readback_bytes, reply = _apply_settings(ctx, json.dumps(changed).encode(), cap=remaining)
    timings['apply'] = round((time.monotonic() - phase) * 1000)
    readback = json.loads(readback_bytes)
    differences = ([key for key in keys if readback.get('menu', {}).get(key) != changed['menu'][key]]
                   if values is not None else _settings_differences(changed, readback))
    if differences:
        raise RuntimeError(f'settings readback differs for {", ".join(differences)}')
    live = {}
    phase = time.monotonic()
    if verify_roster:
        for seat, key in ((0, 'player_character'), (1, 'cpu_character')):
            if key in changed.get('menu', {}):
                array = changed['menu'][key]
                catalog = tables()['roster_catalog']
                if len(array) != len(catalog) or sum(array) != 1 or any(x not in (0, 1) for x in array):
                    raise RuntimeError(f'menu.{key} is not one-hot')
                kind = catalog[array.index(1)]['kind']
                live[key] = _live_fighter(ctx, seat, kind, max(.01, cap - (time.monotonic() - start)))
    timings['roster'] = round((time.monotonic() - phase) * 1000)
    fighter_keys = {'player_character', 'cpu_character'}
    changed_fighters = fighter_keys.intersection(changed.get('menu', {}))
    if values is not None:
        changed_fighters.intersection_update(values)
    return _save(ctx, 'routes-tr-settings', {
        'block': 'tr_settings', 'args': {'values': values, 'file': str(file) if file else None},
        'verified': {'keys': keys, 'live_roster': live}, 'set_reply': reply,
        'live_fighter': ('checked' if verify_roster else 'not checked') if changed_fighters else 'not applicable',
        'settings_sha256': hashlib.sha256(readback_bytes).hexdigest(),
        **metrics,
        'ms': {**timings, 'total': round((time.monotonic() - start) * 1000)}, 'skipped': [],
    })


def _settings_differences(sent, readback, path=''):
    if isinstance(sent, dict):
        if not isinstance(readback, dict):
            return [path or '<root>']
        differences = []
        for key, value in sent.items():
            child = f'{path}.{key}' if path else key
            if key not in readback:
                differences.append(child)
            else:
                differences.extend(_settings_differences(value, readback[key], child))
        return differences
    if (type(sent) is not type(readback)
            or json.dumps(sent, sort_keys=True) != json.dumps(readback, sort_keys=True)):
        return [path]
    return []


def setup_state(ctx):
    started = time.monotonic()
    io = BridgePickerIO(ctx)
    ui = native_picker.wait_sample(io, native_picker.UI, time.monotonic() + 6)
    screen = ui.values.get('screen', 'unknown')
    p1 = cpu = 'unknown'
    stage = expected(ctx).get('stage_id', 'unknown')
    if screen == 'training':
        state = native_state(ctx, fresh=('roster',), roster_entries=(0, 1))
        roster = state.get('roster', {})
        names = {entry['kind']: entry['name'] for entry in tables()['roster_catalog']}
        p1_kind = roster.get(0, {}).get('kind', 'unknown')
        cpu_kind = roster.get(1, {}).get('kind', 'unknown')
        p1 = {'kind': p1_kind, 'name': names.get(p1_kind, 'unknown')}
        cpu = {'kind': cpu_kind, 'name': names.get(cpu_kind, 'unknown')}
        stage = state.get('stage', 'unknown')
    elif screen == 'css.training':
        tile = native_picker.wait_sample(io, native_picker.TILE, time.monotonic() + 6)
        native_picker.same_epoch(ui, tile, in_flight=True)
        p1 = {key: tile.values.get('placed.' + key, 'unknown') for key in ('at', 'kind', 'name', 'form')}
        cpu = {key: tile.values.get('cpu.placed.' + key, 'unknown') for key in ('at', 'kind', 'name', 'form')}
    settings = _fetch_settings(ctx)
    return _save(ctx, 'routes-setup-state', {
        'block': 'setup_state', 'args': {}, 'verified': {'screen': screen, 'context': ui.values.get('screen.context'),
                                                         'owner': ui.values.get('input.owner'), 'p1': p1, 'cpu': cpu,
                                                         'stage_id': stage,
                                                         'settings_sha256': hashlib.sha256(settings).hexdigest()},
        'inputs': 0, 'bridge_requests': getattr(io, 'request_count', 0) + 1 + int(screen == 'training'),
        'bridge_boot': ui.block['boot'], 'revisions': getattr(io, 'revisions', {}),
        'ms': {'total': round((time.monotonic() - started) * 1000)}, 'skipped': [],
    })


def reach_training(ctx, p1, cpu=None, stage='Final Destination', form='normal', cap=180):
    started = time.monotonic()
    until = started + cap
    wanted_stage = stage_entry(stage)['id']
    wanted_p1 = fighter_entry(p1)['kind']
    wanted_cpu = fighter_entry(cpu)['kind'] if cpu is not None else None
    metrics = {'inputs': 0, 'bridge_requests': 0, 'bridge_boot': None, 'revisions': {}}
    previous = getattr(ctx.runner, 'setup_metrics', None)
    ctx.runner.setup_metrics = metrics
    phases = ('to_main', 'back_to_main', 'main_to_stage', 'pick_stage', 'quit_training',
              'pick_fighters', 'enter_training')
    skipped, timings, receipts = list(phases), {}, {}

    def run(name, function, *args, **kwargs):
        if time.monotonic() >= until:
            raise RuntimeError(f'reach_training deadline expired before {name}')
        begin = time.monotonic()
        value = function(*args, **kwargs)
        timings[name] = round((time.monotonic() - begin) * 1000)
        receipts[name] = value
        if name in skipped:
            skipped.remove(name)
        return value

    try:
        io = BridgePickerIO(ctx)
        ui = native_picker.wait_sample(io, native_picker.UI, until)
        known_stage = expected(ctx).get('stage_id')
        if not _fields_match(ui, {'screen.context': 'training', 'dialog': 'none'}):
            if not _main_top(ui):
                run('to_main', to_main, ctx)
            screen = 'main'
        else:
            screen = ui.values.get('screen')
        if screen == 'training':
            if ui.values.get('input.owner') != 'native':
                raise RuntimeError('reach_training needs native Training input ownership')
            state = native_state(ctx, fresh=('roster',), cap=max(.01, until - time.monotonic()),
                                 roster_entries=(0, 1))
            known_stage = state.get('stage')
            roster = state.get('roster', {})
            stage_matches = state.get('stage') == wanted_stage
            fighters_match = (roster.get(0, {}).get('kind') == wanted_p1
                              and 1 in roster and (wanted_cpu is None or roster[1].get('kind') == wanted_cpu))
            if stage_matches and fighters_match:
                final = state
                screen = 'done'
            elif stage_matches:
                run('quit_training', quit_training, ctx, cap=max(.01, until - time.monotonic()))
                screen = 'css.training'
            else:
                run('back_to_main', back_to_main, ctx)
                screen = 'main'
        if screen == 'css.training':
            if known_stage != wanted_stage:
                run('back_to_main', back_to_main, ctx)
                screen = 'main'
        if screen not in ('done', 'main', 'stage_select', 'css.training'):
            run('back_to_main', back_to_main, ctx)
            screen = 'main'
        if screen == 'main':
            run('main_to_stage', main_to_stage, ctx)
            screen = 'stage_select'
        if screen == 'stage_select':
            run('pick_stage', pick_stage, ctx, stage, topology=library_topology('sss-training'), form=form)
            screen = 'css.training'
        if screen == 'css.training':
            run('pick_fighters', pick_fighters, ctx, p1, cpu=cpu, topology=library_topology('css-training'),
                take_back=True)
            run('enter_training', enter_training, ctx, cap=max(.01, until - time.monotonic()),
                stage_ids={wanted_stage}, fighters={'p1': p1, **({'cpu': cpu} if cpu is not None else {})})
            final = native_state(ctx, fresh=('roster',), cap=max(.01, until - time.monotonic()),
                                 roster_entries=(0, 1))
        final_ui = native_picker.wait_sample(io, native_picker.UI, until, ui, in_flight=True)
        if not _fields_match(final_ui, TRAINING_SCREEN):
            raise RuntimeError('final Training screen is not owned by native input')
        roster = final.get('roster', {})
        if (final.get('stage') != wanted_stage or roster.get(0, {}).get('kind') != wanted_p1
                or 1 not in roster or (wanted_cpu is not None and roster[1].get('kind') != wanted_cpu)):
            raise RuntimeError(f'live Training state does not match the requested setup: {final}')
    finally:
        ctx.runner.setup_metrics = previous
        if previous is not None:
            _merge_setup_metrics(previous, metrics)
    return _save(ctx, 'routes-reach-training', {
        'block': 'reach_training', 'args': {'p1': p1, 'cpu': cpu, 'stage': stage, 'form': form},
        'verified': {'stage_id': wanted_stage, 'roster': final['roster']}, **metrics,
        'phases': receipts, 'ms': {**timings, 'total': round((time.monotonic() - started) * 1000)}, 'skipped': skipped,
    })


def switch_close(ctx):
    """Close SSBU: HOME, X, the close dialog's A (switch-remote's `switch.terminate`, each step image-checked)."""
    _need_switch(ctx, 'switch_close')
    result = ctx.call('switch.terminate', {}, job_cap=60)
    return _save(ctx, 'routes-switch-close', {'terminate': result})
