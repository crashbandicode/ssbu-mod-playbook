"""Shared Eden setup routes: proven building blocks every lane reuses instead of hand-building a boot route.

Each public function is a runner step hook (rows.json: {"run": "routes:<function>", "args": {...}, "check": true})
that reaches a named game state, proves it with a game-state or image gate, and returns a small receipt. Waits poll
game state (Eden status, a plugin log line, a tile-cursor line, an image gate) with a cap; there is no fixed sleep
where a state exists. Every block fails closed: a missing state raises with what it saw, and nothing guesses.

Blocks (docs/ROUTES.md has what each guarantees on exit):
  host_network     select and read back a non-loopback interface and LAN mode before game.load
  frontend_room    host or join a connected Eden frontend room
  wireless_room    create a Local Wireless room and reach its waiting room as host
  bridge_alive     verify bridge replies after the frontend room connects
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
import math
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

import closed_loop
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


def _bridge_tail(ctx):
    try:
        with instance_path(ctx, 'bridge_log').open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            position, tail = stream.tell(), b''
            while position:
                size = min(position, 8192)
                position -= size
                stream.seek(position)
                tail = stream.read(size) + tail
                lines = tail.split(b'\n')
                for line in reversed(lines if position == 0 else lines[1:]):
                    if line.strip():
                        return line.decode('utf-8', errors='replace').rstrip('\r')
    except (AttributeError, KeyError, OSError):
        return None
    return None


def _stall_result(ok, stalls):
    result = 'PASS' if ok else 'STOPPED'
    count = len(stalls)
    return f'{result} with {count} {"stall" if count == 1 else "stalls"}' if count else result


def _append_stall(ctx, block, event):
    with (Path(ctx.row_dir) / 'stall-events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({**event, 'block': block}, default=str) + '\n')


def _stall_utc():
    return datetime.now(timezone.utc).isoformat()


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


def host_network(ctx, interface=None, lan_online=False, ldn_mitm=False):
    if (ctx.call('status') or {}).get('running'):
        raise RuntimeError('host_network runs before game.load: core.network_interface is not changeable at run time')
    if not interface:
        config = Path(ctx.runner.out).parent / 'config.json'
        if config.is_file():
            interface = json.loads(config.read_text()).get('network_interface')
    if not interface:
        raise RuntimeError('host_network: no interface: pass "interface" or use a base that names "network_interface"')
    if interface == 'lo':
        raise RuntimeError('host_network: Eden never lists loopback, so "lo" means no network and no local wireless room; '
                           'name a real interface')
    readback = {'core.network_interface': _setting(ctx, 'core.network_interface', interface),
                'core.lan_online': _setting(ctx, 'core.lan_online', lan_online)}
    try:
        ctx.call('settings.get', {'key': 'core.ldn_mitm'})
    except RuntimeError as error:
        if not re.search(r"""['"]code['"]:\s*['"]unknown_setting['"]""", str(error)):
            raise
        readback['core.ldn_mitm'] = 'absent'
    else:
        readback['core.ldn_mitm'] = _setting(ctx, 'core.ldn_mitm', ldn_mitm)
    return _save(ctx, 'routes-host-network', {'block': 'host_network', 'interface': interface,
                                              'lan_online': lan_online, 'ldn_mitm': ldn_mitm, 'readback': readback})


def _frontend_title(node):
    title = node.get('title')
    if title in (None, ''):
        props = node.get('properties') or {}
        for key in ('windowTitle', 'title'):
            value = props.get(key)
            title = value.get('value') if isinstance(value, dict) else value
            if title not in (None, ''):
                break
    return '' if title is None else str(title)


def _frontend_text(node):
    text = node.get('text')
    if text in (None, ''):
        entry = (node.get('properties') or {}).get('text')
        text = entry.get('value') if isinstance(entry, dict) else entry
    return '' if text is None else str(text)


def _frontend_connected(ctx, tree, members):
    for node in tree:
        if not ctx.runner.ui_matches(node, {'class': 'ClientRoomWindow'}):
            continue
        title = _frontend_title(node)
        count = re.search(r'\((\d+)/\d+ members\).* - connected$', title)
        if count and int(count.group(1)) >= members:
            return {k: node.get(k) for k in ('id', 'class', 'name', 'text')}, title, int(count.group(1))
    return None


def _frontend_room_head(title):
    match = re.fullmatch(r'(.+) \(\d+/\d+ members\) - connected', title, flags=re.DOTALL)
    return match.group(1) if match else None


def _frontend_room_matches(title, room_name):
    """Eden: "<room> - <game> (n/m members) - connected" (client_room.cpp:102)."""
    head = _frontend_room_head(title)
    if head is None:
        return False
    i = head.find(' - ')
    return i != -1 and i == head.rfind(' - ') and head[:i] == room_name and bool(head[i + 3:])


def _frontend_confirmable_room_name(room_name):
    if isinstance(room_name, str) and (' - ' in room_name or room_name.endswith(' -')):
        raise RuntimeError(f'frontend_room: room_name "{room_name}" contains " - ", so Eden\'s room window title '
                           '("<room> - <game> (n/m members) - connected") could not confirm it; '
                           'use a name without " - "')


def _frontend_name(field, value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9._ -]{4,20}', value):
        raise RuntimeError(f'frontend_room: {field} "{value}" is not valid for Eden: 4 to 20 characters of '
                           'letters, digits, ".", "_", " " or "-" (yuzu/multiplayer/validation.h)')


def _frontend_wait(ctx, match, cap, members=None, confirmation=None, room_name=None):
    until = time.monotonic() + cap
    titles = []
    while time.monotonic() < until:
        ctx.check_stop()
        tree = ctx.call('ui.tree', {}) or []
        for node in tree:
            if ctx.runner.ui_matches(node, {'class': 'QMessageBox'}):
                text = _frontend_text(node)
                if confirmation and confirmation['game_running'] and 'game is already running' in text:
                    if confirmation['text'] is not None:
                        raise RuntimeError(f'frontend_room: Eden showed the running-game warning again: "{text}"')
                    _, confirmation['text'] = _frontend_dismiss(ctx, tree, max(0, until - time.monotonic()),
                                                                 ok_pattern='^&?OK$')
                    break
                raise RuntimeError(f'frontend_room: Eden showed "{_frontend_text(node)}" while waiting for {match}')
        else:
            titles = [_frontend_title(node) for node in tree if node.get('class') == 'ClientRoomWindow']
            for node in tree:
                if not ctx.runner.ui_matches(node, match):
                    continue
                if members is not None:
                    title = _frontend_title(node)
                    count = re.search(r'\((\d+)/\d+ members\).* - connected$', title)
                    if not count or int(count.group(1)) < members:
                        continue
                    if room_name is not None and not _frontend_room_matches(title, room_name):
                        continue
                    return {k: node.get(k) for k in ('id', 'class', 'name', 'text')}, title, int(count.group(1))
                return {k: node.get(k) for k in ('id', 'class', 'name', 'text')}
            time.sleep(min(.5, max(0, until - time.monotonic())))
    if members is not None:
        seen = ', '.join(repr(title) for title in titles) if titles else 'no room window'
        room = f' for room "{room_name}"' if room_name is not None else ''
        raise RuntimeError(f'frontend_room: expected a connected room window{room} with at least {members} member(s); '
                           f'saw: {seen}')
    raise RuntimeError(f'frontend_room: widget {match} did not appear within {cap} s')


def _frontend_dismiss(ctx, tree, cap, ok_pattern='^OK$'):
    boxes = [n for n in tree if ctx.runner.ui_matches(n, {'class': 'QMessageBox'})]
    if not boxes:
        return tree, None
    if len(boxes) != 1:
        raise RuntimeError('frontend_room: multiple visible message boxes; cannot choose an OK button')
    box = boxes[0]
    buttons = [n for n in tree if n.get('window') == box.get('id')
               and ctx.runner.ui_matches(n, {'class': 'QPushButton', 'text': ok_pattern, 'enabled': True})]
    if len(buttons) != 1 or not buttons[0].get('id'):
        raise RuntimeError('frontend_room: message box has no uniquely identifiable OK button')
    text = _frontend_text(box)
    ctx.runner.ui(ctx.on, {'ui': 'click', 'target': {'class': 'QPushButton', 'id': buttons[0]['id'],
                                                     'text': ok_pattern}})
    until = time.monotonic() + cap
    while time.monotonic() < until:
        ctx.check_stop()
        tree = ctx.call('ui.tree', {}) or []
        if not any(n.get('id') == box.get('id') and ctx.runner.ui_matches(n, {'class': 'QMessageBox'}) for n in tree):
            return tree, text
        time.sleep(min(.5, max(0, until - time.monotonic())))
    raise RuntimeError(f'frontend_room: message box "{text}" did not close after OK')


def _frontend_dialog_value(ctx, tree, anchor_name, field_name):
    anchors = [n for n in tree if ctx.runner.ui_matches(n, {'class': 'QLineEdit', 'name': anchor_name})]
    if len(anchors) != 1 or not anchors[0].get('window'):
        return None
    fields = [n for n in tree if n.get('window') == anchors[0]['window']
              and ctx.runner.ui_matches(n, {'class': 'QLineEdit', 'name': field_name})]
    return _frontend_text(fields[0]) if len(fields) == 1 else None


def frontend_room(ctx, role='host', port=None, nickname=None, room_name=None, ip='127.0.0.1', members=1, cap=30):
    game_running = bool((ctx.call('status') or {}).get('running'))
    if role not in ('host', 'join'):
        raise RuntimeError(f'frontend_room: unknown role {role!r}')
    nickname = ('TRHost' if role == 'host' else 'TRGuest') if nickname is None else nickname
    _frontend_name('nickname', nickname)
    if role == 'host' and room_name is not None:
        _frontend_name('room_name', room_name)
    room_file = Path(ctx.runner.out) / 'frontend-room.json'
    saved = json.loads(room_file.read_text()) if room_file.is_file() else {}
    expected_name = room_name if room_name is not None else saved.get('room_name')
    _frontend_confirmable_room_name(expected_name)
    requested_name = room_name
    requested_port = port

    def reuse(connected, dismissed):
        widget, title, count = connected
        if expected_name is None:
            raise RuntimeError(f'frontend_room: this Eden is already connected to "{title}", but no room_name was '
                               'given and this run has no frontend-room.json to check it against')
        if not _frontend_room_matches(title, expected_name):
            head = _frontend_room_head(title)
            ambiguous = ('; the title has more than one " - ", so its room name is ambiguous'
                         if head is not None and head.find(' - ') != head.rfind(' - ') else '')
            raise RuntimeError(f'frontend_room: this Eden is connected to another room: expected "{expected_name}", '
                               f'saw "{title}"{ambiguous}')
        return _save(ctx, 'routes-frontend-room', {
            'role': role, 'port': requested_port if requested_port is not None else saved.get('port'),
            'nickname': nickname, 'room_name': expected_name, 'ip': ip,
            'evidence': {'frontend_connected': widget}, 'window_title': title, 'member_count': count,
            'already_connected': True, 'reused_dialog': False, 'dismissed': dismissed, 'game_running': game_running,
            'confirmed_running_game': None, 'room_matched': True})

    tree = ctx.call('ui.tree', {}) or []
    confirmation = {'game_running': game_running, 'text': None}
    boxes = [n for n in tree if ctx.runner.ui_matches(n, {'class': 'QMessageBox'})]
    warning = next((n for n in boxes if 'game is already running' in _frontend_text(n)), None)
    if warning and not game_running:
        raise RuntimeError(f'frontend_room: Eden showed "{_frontend_text(warning)}" while waiting for connected room')
    connected = _frontend_connected(ctx, tree, members)
    if connected and not warning:
        return reuse(connected, None)
    if warning:
        anchor = 'username' if role == 'host' else 'nickname'
        actual_port = _frontend_dialog_value(ctx, tree, anchor, 'port')
        port = int(actual_port) if actual_port and actual_port.isascii() and actual_port.isdecimal() else None
        dialog_name = _frontend_dialog_value(ctx, tree, anchor, 'room_name') if role == 'host' else None
        comparable_port = (int(requested_port) if isinstance(requested_port, str) and requested_port.isascii()
                           and requested_port.isdecimal() else requested_port)
        if ((role == 'host' and requested_name is not None and dialog_name is not None
             and requested_name != dialog_name)
                or (requested_port is not None and port is not None and comparable_port != port)):
            if role == 'join':
                raise RuntimeError(f'frontend_room: the open Direct Connect dialog is on port {port}, '
                                   f'not the requested port {requested_port}')
            raise RuntimeError(f'frontend_room: the open Create Room dialog is for room "{dialog_name}" on port '
                               f'{port}, not the requested room "{requested_name}" on port {requested_port}')
        nickname = _frontend_dialog_value(ctx, tree, anchor, anchor) or nickname
        _frontend_name('nickname', nickname)
        if role == 'host':
            room_name = requested_name if requested_name is not None else dialog_name or saved.get('room_name')
        else:
            room_name = expected_name
        if role == 'join':
            ip = _frontend_dialog_value(ctx, tree, anchor, 'ip') or ip
        if role == 'host':
            if room_name is None:
                raise RuntimeError('frontend_room: cannot read the room name of the open Create Room dialog')
            _frontend_name('room_name', room_name)
        _frontend_confirmable_room_name(room_name)
        tree, confirmation['text'] = _frontend_dismiss(ctx, tree, cap, ok_pattern='^&?OK$')
        dismissed = None
    else:
        if port is None:
            port = free_udp_port() if role == 'host' else saved.get('port')
        if port is None:
            raise RuntimeError('frontend_room: join needs a port or the frontend-room.json of this run')
        if role == 'host' and room_name is None:
            room_name = f'TR{port}'
        elif role == 'join' and room_name is None:
            room_name = saved.get('room_name')
        if role == 'host':
            _frontend_name('room_name', room_name)
        _frontend_confirmable_room_name(room_name)

        tree, dismissed = _frontend_dismiss(ctx, tree, cap)
    connected = _frontend_connected(ctx, tree, members)
    if connected and not warning:
        return reuse(connected, dismissed)

    evidence = {}
    dialog = {'class': 'QLineEdit', 'name': 'username' if role == 'host' else 'nickname'}
    reused_dialog = any(ctx.runner.ui_matches(n, dialog) for n in tree)
    if not warning and role == 'host':
        action = {'class': 'QAction', 'name': 'action_Start_Room'}
        if not reused_dialog:
            evidence['host_action'] = _frontend_wait(ctx, action, cap, confirmation=confirmation)
            ctx.runner.ui(ctx.on, {'ui': 'trigger', 'target': action})
        evidence['host_dialog'] = _frontend_wait(ctx, dialog, cap, confirmation=confirmation)
        for name, value in (('username', nickname), ('room_name', room_name), ('port', str(port))):
            ctx.runner.ui(ctx.on, {'ui': 'set', 'target': {'class': 'QLineEdit', 'name': name},
                                   'property': 'text', 'value': value})
        ctx.runner.ui(ctx.on, {'ui': 'set', 'target': {'class': 'QComboBox', 'name': 'host_type'},
                               'property': 'currentIndex', 'value': 1})
        ctx.runner.ui(ctx.on, {'ui': 'click', 'target': {'class': 'QPushButton', 'name': 'host'}})
        evidence['frontend_leave'] = _frontend_wait(
            ctx, {'class': 'QPushButton', 'name': 'disconnect', 'text': '^Leave Room$'}, cap,
            confirmation=confirmation)
    elif not warning:
        action = {'class': 'QAction', 'name': 'action_Connect_To_Room'}
        if not reused_dialog:
            evidence['peer_action'] = _frontend_wait(ctx, action, cap, confirmation=confirmation)
            ctx.runner.ui(ctx.on, {'ui': 'trigger', 'target': action})
        evidence['peer_dialog'] = _frontend_wait(ctx, dialog, cap, confirmation=confirmation)
        for name, value in (('nickname', nickname), ('ip', ip), ('port', str(port))):
            ctx.runner.ui(ctx.on, {'ui': 'set', 'target': {'class': 'QLineEdit', 'name': name},
                                   'property': 'text', 'value': value})
        ctx.runner.ui(ctx.on, {'ui': 'click', 'target': {'class': 'QPushButton', 'name': 'connect'}})
    evidence['frontend_connected'], title, count = _frontend_wait(
        ctx, {'class': 'ClientRoomWindow', 'title': r'\(\d+/\d+ members\).*connected'}, cap, members,
        confirmation=confirmation, room_name=room_name)
    if role == 'host' and port is not None and room_name is not None:
        room_file.write_text(json.dumps({'port': port, 'room_name': room_name, 'instance': ctx.on}, indent=1) + '\n')
    return _save(ctx, 'routes-frontend-room', {'role': role, 'port': port, 'nickname': nickname,
                                               'room_name': room_name, 'ip': ip, 'evidence': evidence,
                                               'window_title': title, 'member_count': count,
                                               'already_connected': False, 'reused_dialog': reused_dialog,
                                               'dismissed': dismissed, 'game_running': game_running,
                                               'confirmed_running_game': confirmation['text'],
                                               'room_matched': True if room_name is not None else None})


def _wireless_read(ctx, io, provider, cap, screens, *, on_sample=None):
    ctx.check_stop()
    try:
        sample = io.read(provider, cap)
    except RuntimeError as error:
        if any(word in str(error).lower() for word in ('continuity', 'boot changed', 'malformed', 'overlapping')):
            raise
        return {'error': str(error)}
    block = sample.block
    if (block.get('name') != provider or block.get('schema') != provider + '/1'
            or block.get('filtered') or block.get('rev', 0) <= 0):
        raise RuntimeError(f'{provider}: incomplete bridge state: {block}')
    if on_sample:
        on_sample(sample)
    values = dict(block['values'])
    if provider == 'ssbu.ui':
        screen = values.get('screen', 'unknown')
        if not screens or screens[-1] != screen:
            screens.append(screen)
    return values


def _wireless_placed(tile):
    return tile.get('screen') == 'css' and (tile.get('p1.token') == 'placed'
                                             or tile.get('local.token') == 'placed')


def wireless_room(ctx, ready_cap=60, unpublished_create='refuse'):
    """Reach the Local Wireless waiting room as host from an observed route screen."""
    if unpublished_create not in ('refuse', 'allow'):
        raise RuntimeError(f'wireless_room: invalid unpublished_create {unpublished_create!r}')
    if not isinstance(ready_cap, (int, float)) or not math.isfinite(ready_cap) or ready_cap <= 0:
        raise RuntimeError(f'wireless_room: ready_cap must be positive, got {ready_cap!r}')
    started, screens = time.monotonic(), []
    io = BridgePickerIO(ctx)
    state = {'last_ui': {}, 'last_tile': None, 'last_error': None, 'last_read_was_error': False,
             'form_pending': False, 'form_ready': False, 'form_settling': False, 'ready_published': False,
             'form_away': 0, 'form_left_list': False, 'create_form': None, 'confirm_at': None, 'confirm_presses': 0,
             'stage_presses': 0, 'place_presses': 0, 'chord_sent': False, 'chord_fence': None,
             'prefinished': False, 'current': None, 'ui_clock': {}}

    def remember_ui(sample):
        state['ui_clock'] = {key: sample.block.get(key) for key in ('frame', 'wall', 'rev')}
        state['ready_published'] |= 'input.ready' in sample.block['values']

    def observe():
        ui = _wireless_read(ctx, io, 'ssbu.ui', 2, screens, on_sample=remember_ui)
        if 'error' in ui:
            state['last_error'] = ui['error']
            state['last_read_was_error'] = True
            return closed_loop.NO_READ
        state['last_read_was_error'] = False
        state['last_ui'] = ui
        tile = None
        if (ui.get('screen') in ('stage_select', 'css.smash') and ui.get('screen.context') == 'local_wireless'
                and ui.get('observation.active') == 'yes'):
            tile = _wireless_read(ctx, io, 'tile.cursor', 2, screens)
            state['last_tile'] = tile
        return {'ui': ui, 'tile': tile}

    def active(obs, screen):
        ui = obs['ui']
        return ui.get('screen') == screen and ui.get('observation.active') == 'yes'

    def accepting(ui):
        return 'input.ready' not in ui or ui['input.ready'] == 'yes'

    def press_gate():
        return ('input.ready + tile.cursor' if state['ready_published']
                else 'tile.cursor (no input.ready published)')

    def local(obs, screen):
        return active(obs, screen) and obs['ui'].get('screen.context') == 'local_wireless'

    def available():
        return not state['form_pending']

    def hovered(tile):
        at = tile.get('at')
        return isinstance(at, str) and at.isdigit() and tile.get('hovered') == at

    def begin_create(_obs):
        state['form_pending'] = True
        state['form_ready'] = False
        state['form_settling'] = False
        state['form_away'] = 0

    def create_done(obs):
        if not state['form_pending']:
            return False
        screen = obs['ui'].get('screen')
        if state['form_settling']:
            if screen not in ('unknown', 'ldn.create'):
                raise closed_loop.RouteStopped('wireless_room', 'open-create',
                                              f'expected the Create form (ldn.create, or unpublished) after A; '
                                              f'saw {obs["ui"]}', {})
            state['create_form'] = 'ldn.create' if screen == 'ldn.create' else 'unpublished'
            state['form_ready'] = True
            state['form_pending'] = False
            state['form_settling'] = False
            return True
        if screen is not None and screen != 'ldn.room_list':
            state['form_left_list'] = True
        if unpublished_create == 'refuse':
            if not active(obs, 'ldn.create'):
                return False
            state['create_form'] = 'ldn.create'
            state['form_ready'] = True
            state['form_pending'] = False
            return True
        state['form_away'] = state['form_away'] + 1 if screen is not None and screen != 'ldn.room_list' else 0
        if state['form_away'] < 3:
            return False
        io.finish()
        state['prefinished'] = True
        time.sleep(1.5)
        state['form_settling'] = True
        return False

    def confirm_ready(obs):
        ui = obs['ui']
        screen = ui.get('screen')
        if unpublished_create == 'refuse':
            form = active(obs, 'ldn.create')
        else:
            form = screen == 'ldn.create' or (screen == 'unknown' and state['form_ready']
                                              and state['current'] == 'confirm-create')
        return (available() and form and accepting(ui)
                and (state['confirm_at'] is None or time.monotonic() - state['confirm_at'] >= 20))

    def confirm_done(obs):
        ui = obs['ui']
        if local(obs, 'stage_select') or local(obs, 'css.smash'):
            return True
        if state['confirm_at'] is not None and ui.get('screen') not in (
                None, 'unknown', 'ldn.create', 'stage_select', 'css.smash'):
            raise closed_loop.RouteStopped('wireless_room', 'confirm-create',
                                          f'expected stage_select or css.smash in local_wireless after '
                                          f'{state["confirm_presses"]} confirm press(es); saw {ui}, screens {screens}',
                                          {})
        return False

    def begin_confirm(_obs):
        state['confirm_at'] = time.monotonic()

    def fence_chord(obs):
        state['chord_fence'] = {key: obs['tile'].get(key)
                                for key in ('screen', 'p1.token', 'local.token', 'at', 'hovered')}

    def css(obs):
        return available() and local(obs, 'css.smash')

    def stage_at(obs):
        tile = obs['tile'] or {}
        return (available() and local(obs, 'stage_select') and tile.get('screen') == 'sss'
                and accepting(obs['ui']) and tile.get('settled') == '1'
                and tile.get('p1.token') == 'held' and tile.get('local.token') == 'held' and hovered(tile))

    def hand_at(obs):
        tile = obs['tile'] or {}
        return (css(obs) and accepting(obs['ui']) and tile.get('screen') == 'css' and tile.get('p1.token') == 'held'
                and not hovered(tile) and not _wireless_placed(tile))

    def place_at(obs):
        tile = obs['tile'] or {}
        return (css(obs) and accepting(obs['ui']) and tile.get('screen') == 'css' and tile.get('p1.token') == 'held'
                and hovered(tile) and not _wireless_placed(tile))

    def placed(obs):
        return css(obs) and _wireless_placed(obs['tile'] or {})

    def waiting(obs):
        return (active(obs, 'ldn.waiting') and obs['ui'].get('ldn.role') == 'host')

    ready_wait_cap = max(ready_cap + 10, 45)
    steps = [
        closed_loop.Step('open-room-list',
                         lambda o: (available() and active(o, 'main_menu') and accepting(o['ui'])
                                    and o['ui'].get('input.owner') == 'native'),
                         lambda o: active(o, 'ldn.room_list'), 'lobby-open-room-list', cap=25, lost_after=6,
                         target=lambda o: (o['ui'].get('focus'), o['ui'].get('screen.layer'))),
        closed_loop.Step('open-create', lambda o: (not state['form_settling'] and active(o, 'ldn.room_list')
                                                   and accepting(o['ui'])), create_done, 'tap-a1',
                         cap=15, lost_after=5, before_send=begin_create),
        closed_loop.Step('confirm-create', confirm_ready, confirm_done, 'tap-plus1',
                         cap=20, lost_after=0, sends=2, before_send=begin_confirm),
        closed_loop.Step('stage-a', stage_at, lambda o: local(o, 'css.smash'), 'lobby-a',
                         cap=60, ready_cap=20, lost_after=6, target=lambda o: (o['tile'] or {}).get('at')),
        closed_loop.Step('css-hand', hand_at, lambda o: placed(o) or place_at(o), 'lobby-first-tile',
                         cap=5, ready_cap=30, lost_after=3, before_send=fence_chord,
                         target=lambda o: ((o['tile'] or {}).get('at'), (o['tile'] or {}).get('hovered'))),
        closed_loop.Step('place', place_at, placed, 'lobby-a', cap=8, lost_after=4,
                         target=lambda o: (o['tile'] or {}).get('at')),
        closed_loop.Step('ready', lambda o: placed(o) and accepting(o['ui']), waiting, 'lobby-ready', cap=ready_wait_cap,
                         lost_after=ready_wait_cap + 1, sends=1,
                         target=lambda o: ((o['tile'] or {}).get('p1.placed.at'),
                                           (o['tile'] or {}).get('local.placed.at'))),
    ]

    def send(macro):
        io.send(macro)
        if state['current'] == 'confirm-create':
            state['confirm_presses'] += 1
        elif state['current'] == 'stage-a':
            state['stage_presses'] += 1
        elif state['current'] == 'css-hand':
            state['chord_sent'] = True
        elif state['current'] == 'place':
            state['place_presses'] += 1

    def finish():
        if state['prefinished']:
            state['prefinished'] = False
        else:
            io.finish()

    def on_step(name):
        if state['current'] == 'confirm-create' and name != 'confirm-create':
            state['form_ready'] = False
            state['confirm_at'] = None
        state['current'] = name
        if name == 'confirm-create' and state['create_form'] is None:
            state['create_form'] = 'ldn.create'

    def on_stall(event, obs):
        result = {
            **event, 'kind': 'resend', 'screen': obs['ui'].get('screen'), **state['ui_clock'],
            'tile': obs['tile'], 'last_bridge_line': _bridge_tail(ctx), 'eden_frame_ms': None, 'utc': _stall_utc(),
        }
        try:
            status = ctx.call('status') or {}
            fps = status.get('game_fps') if isinstance(status, dict) else None
            if isinstance(fps, (int, float)) and math.isfinite(fps) and fps > 0:
                result['eden_fps'] = fps
                result['eden_frame_ms'] = 1000 / fps
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass
        _append_stall(ctx, 'wireless_room', result)
        return result

    try:
        outcome = closed_loop.run('wireless_room', steps, observe, send, finish,
                                  clock=time.monotonic, sleep=time.sleep, on_step=on_step, on_stall=on_stall)
    except closed_loop.RouteStopped as error:
        reason = error.reason
        if (error.step == 'confirm-create' and state['confirm_presses'] == 1
                and (state['last_read_was_error'] or state['last_ui'].get('screen') is None)):
            seen = state['last_error'] if state['last_read_was_error'] else f'screen missing from {state["last_ui"]}'
            reason = ('no ssbu.ui reply after the Create confirm, so a second press would '
                      f'have no screen witness; last read: {seen}; {reason}')
        elif error.step == 'confirm-create' and reason.startswith('no transition'):
            reason = (f'expected stage_select or css.smash in local_wireless after '
                      f'{state["confirm_presses"]} confirm press(es); saw {state["last_ui"]}, screens {screens}; '
                      f'{reason}')
        elif error.step == 'locate':
            reason += '; expected main_menu with native input or a later route screen'
        elif error.step == 'open-room-list':
            reason += f'; expected ldn.room_list; saw {state["last_ui"]}'
        elif error.step == 'open-create':
            if state['form_settling'] and state['last_read_was_error']:
                reason = f'expected the Create form after A; last read error: {state["last_error"]}; {reason}'
            elif (unpublished_create == 'refuse' and state['form_left_list']
                    and reason.startswith('no transition within cap')):
                reason = ('unsupported reader: the Create form is not published as ldn.create; '
                          f'last read {state["last_ui"]}; {reason}')
            else:
                reason += f'; expected ssbu.ui to leave ldn.room_list; saw {state["last_ui"]}'
        elif error.step == 'stage-a':
            reason += f'; expected css.smash in local_wireless; saw {state["last_ui"]}'
        elif error.step == 'css-hand':
            if state['chord_sent']:
                reason += f'; expected the hand on a tile before placement; saw {state["last_tile"]}'
            else:
                reason += ('; expected css.smash before tile-up; expected tile.cursor screen css with '
                           f'p1.token held before the chord; saw {state["last_tile"]}')
        elif error.step == 'place':
            reason += (f'; expected p1.token or local.token placed; saw {state["last_tile"]}; '
                       'expected css.smash before Ready')
        elif error.step == 'ready':
            reason += f'; expected ldn.waiting; expected ldn.role host; saw {state["last_ui"]}'
        error.reason = reason
        error.receipt['reason'] = reason
        error.args = (f'wireless_room: stopped on step {error.step!r}: {reason}',)
        sends = next((entry['sends'] for entry in error.receipt['steps'] if entry['name'] == error.step), 0)
        stop_receipt = {
            'block': 'wireless_room', 'ok': False, 'step': error.step, 'reason': reason, 'sends': sends,
            'screens': screens, 'last_ui': state['last_ui'], 'last_tile': state['last_tile'],
            'steps': error.receipt['steps'], 'ms': round((time.monotonic() - started) * 1000),
            'stalls': error.receipt['stalls'], 'result': _stall_result(False, error.receipt['stalls']),
            'last_error': state['last_error'], 'press_gate': press_gate(),
        }
        dispatch = next((entry.get('dispatch') for entry in error.receipt['steps'] if entry['name'] == error.step),
                        None)
        if dispatch is not None:
            stop_receipt['dispatch'] = error.receipt['dispatch'] = dispatch
        if reason.startswith('unsupported reader: the Create form is not published as ldn.create'):
            stop_receipt['unsupported_reader'] = 'ldn.create'
        _save(ctx, 'routes-wireless-room-stop', stop_receipt)
        raise

    tile, waiting_ui = state['last_tile'], state['last_ui']
    if state['place_presses']:
        placed_by = 'chord + A' if state['chord_sent'] else 'A'
    elif state['chord_sent']:
        placed_by = 'chord'
    else:
        placed_by = 'already placed'
    return _save(ctx, 'routes-wireless-room', {
        'block': 'wireless_room', 'screens': screens, 'create_form': state['create_form'],
        'create_witness': {'unpublished': 'none: unpublished Create form allowed by the caller',
                           None: 'not observed: the route entered after the Create form'}.get(
                               state['create_form'], 'ldn.create'),
        'confirm_presses': state['confirm_presses'], 'stage_presses': state['stage_presses'], 'tile': tile,
        'placed_by': placed_by, 'chord_sent': state['chord_sent'], 'chord_fence': state['chord_fence'],
        'waiting': {key: waiting_ui.get(key, 'unknown')
                    for key in ('screen', 'ldn.role', 'ldn.members', 'observation.active')},
        'ldn.members': waiting_ui.get('ldn.members', 'unknown'), 'ms': round((time.monotonic() - started) * 1000),
        'entered_at': outcome['entered_at'], 'steps': outcome['steps'],
        'stalls': outcome['stalls'], 'result': _stall_result(True, outcome['stalls']),
        'press_gate': press_gate(),
    })


def bridge_alive(ctx, cap=10):
    """Prove the bridge answers: UI observations advance; two fenced tr.menu replies may have equal revisions."""
    until = time.monotonic() + cap
    try:
        io = BridgePickerIO(ctx)
        first = native_picker.wait_sample(io, native_picker.UI, until)
        second = native_picker.wait_sample(io, native_picker.UI, until, first)
        if not second.values.get('screen'):
            raise RuntimeError('missing screen')
    except RuntimeError as error:
        raise RuntimeError(f'bridge_alive: ssbu.ui did not provide two fresh replies within {cap} s: {error}') from error
    try:
        replies = []
        for _ in range(2):
            remaining = until - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('deadline reached')
            sample = io.read('tr.menu', remaining)
            block = sample.block
            if (block['name'] != 'tr.menu' or block['schema'] != 'tr.menu/1' or block['filtered']
                    or block['rev'] <= 0 or block['values'].get('schema') != 'tr.menu/1'):
                raise RuntimeError('incomplete tr.menu/1 reply')
            native_picker.same_epoch(second if not replies else replies[-1], sample, in_flight=False)
            replies.append(sample)
    except RuntimeError as error:
        raise RuntimeError(f'bridge_alive: tr.menu did not provide two fresh replies within {cap} s: {error}') from error
    menu = {'rev': [sample.block['rev'] for sample in replies]}
    for key in ('frame', 'wall'):
        if any(key in sample.block for sample in replies):
            menu[key] = [sample.block.get(key) for sample in replies]
    with instance_path(ctx, 'bridge_log').open(errors='replace') as log:
        reconnects = sum('[Adapter] reconnect' in line for line in log)
    return _save(ctx, 'routes-bridge-alive', {
        'block': 'bridge_alive',
        'ssbu.ui': {'rev': [first.block['rev'], second.block['rev']], 'screen': second.values['screen']},
        'tr.menu': menu, 'reconnects': reconnects})


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
    out = {}
    runner_out = getattr(getattr(ctx, 'runner', None), 'out', None)
    config_path = Path(runner_out).parent / 'config.json' if runner_out is not None else None
    if config_path is not None and config_path.is_file():
        check = json.loads(config_path.read_text()).get('network_check')
        if isinstance(check, dict) and check.get('no_lobby') is False and check.get('interface'):
            live = (ctx.call('settings.get', {'key': 'core.network_interface'}) or {}).get('value')
            interface = check['interface']
            if live != interface:
                raise RuntimeError(f'load_game: core.network_interface is "{live}" but this run\'s checked interface '
                                   f'is "{interface}"; a step changed it after host_network. No game was loaded.')
            out['network_interface'] = live
    t0 = time.monotonic()
    ctx.call('game.load', {'path': ctx.instance['game']}, job_cap=240)
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
        kinds[seat] = fighter_entry(name)['kind'] if name is not None else None
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
        **result, 'block': 'enter_training', 'args': {
            'cap': cap, 'stage_ids': sorted(stage_ids) if stage_ids is not None else None, 'fighters': fighters},
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
    before = _remember(io, native_picker.wait_sample(io, native_picker.UI, until, initial, loading_ui=settling))
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
    # Setup blocks set the CPU in Training; this remains for Tile Cursor rows.
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


def take_cpu(ctx):
    """Take the CPU's coin at Training fighter select."""
    started = time.monotonic()
    io = BridgePickerIO(ctx)
    before, _ = native_picker._coin_baseline(io, 4, 'training')
    native_picker.take_lines(before)
    ack = native_picker.number(before, 'take.ack')
    applied = native_picker.number(before, 'take.applied')
    after = native_picker.take_cpu(io)
    if (native_picker.number(after, 'take.ack'), native_picker.number(after, 'take.applied')) != (ack + 1, applied + 1):
        raise RuntimeError('L3 + B counters changed before the take')
    return _save(ctx, 'routes-take-cpu', {
        'block': 'take_cpu', 'args': {},
        'verified': {'take.ack': [ack, ack + 1], 'take.applied': [applied, applied + 1],
                     **{key: after.values[key] for key in ('p1.carried', 'cpu.token', 'p1.coin.at', 'at')}},
        'inputs': getattr(io, 'input_count', len(getattr(io, 'actions', ()))),
        'ms': {'total': round((time.monotonic() - started) * 1000)},
    })


def take_noop(ctx, holding, hold=False):
    """Check that L3 + B does nothing while the specified coin is held."""
    if holding not in ('cpu', 'p1'):
        raise ValueError(f'holding must be cpu or p1, not {holding!r}')
    started = time.monotonic()
    io = BridgePickerIO(ctx)
    current, ui = native_picker._coin_baseline(io, 4, 'training')
    native_picker.take_lines(current)
    carried = current.values.get('p1.carried')
    wanted = '1' if holding == 'cpu' else '0'
    if carried == wanted:
        reached_by = 'already'
    elif carried in ('none', 'unknown') and current.values.get('carried') == 'none':
        native_picker._p1_placed(current)
        if holding == 'cpu':
            native_picker.take_cpu(io)
            reached_by = 'take_cpu'
        else:
            native_picker.take_back_local_token(io, current, ui, expected_context='training')
            reached_by = 'b'
    else:
        raise RuntimeError(f'{holding.upper()} no-op requires its coin or an empty hand; p1.carried={carried!r}')
    verified = native_picker.take_noop(io, wanted, hold=hold)
    verified['reached_by'] = reached_by
    args = {'holding': holding}
    if hold:
        args['hold'] = True
    return _save(ctx, 'routes-take-noop', {
        'block': 'take_noop', 'args': args, 'verified': verified,
        'inputs': getattr(io, 'input_count', len(getattr(io, 'actions', ()))),
        'ms': {'total': round((time.monotonic() - started) * 1000)},
    })


def place_cpu(ctx, fighter, must_move=False):
    """Place the CPU's coin on a qualified fighter tile."""
    started = time.monotonic()
    entry = fighter_entry(fighter)
    target = _native_target(entry, 'default')
    topology = _tile_topology(library_topology('css-training'))
    io = BridgePickerIO(ctx)
    current, _ = native_picker._coin_baseline(io, 4, 'training')
    native_picker.take_lines(current)
    if topology is None or topology.layout != current.values.get('layout'):
        raise RuntimeError('CPU placement needs the qualified active CSS topology')
    goals = [tile for tile, identity in topology.targets.items() if identity == target]
    if len(goals) != 1:
        raise RuntimeError('CPU target identity is absent or ambiguous in the active topology')
    goal = goals[0]
    p1_at = str(native_picker.number(current, 'p1.coin.at'))
    p1_identity = topology.targets.get(p1_at)
    if current.values.get('p1.coin') != 'placed' or p1_identity is None:
        raise RuntimeError('P1 coin is not placed on a qualified tile')
    p1_before = ('placed', p1_at, p1_identity.kind, p1_identity.name, p1_identity.form)
    already = (current.values.get('cpu.token') == 'placed'
               and tuple(current.values.get('cpu.placed.' + key) for key in ('at', 'kind', 'name', 'form'))
               == (goal, target.kind, target.name, target.form))
    taken_here = False
    steps = 0
    if already:
        native_picker._p1_placed(current)
        if native_picker.p1_medal(current) != p1_before:
            raise RuntimeError('P1 medal identity disagrees with the qualified topology')
        if must_move:
            raise RuntimeError(f"the CPU's coin is already on {fighter}: this row cannot show a move")
        final = current
        skipped = ['take', 'walk', 'place']
    else:
        carried = current.values.get('p1.carried')
        if carried == '1' and current.values.get('cpu.token') == 'held':
            skipped = ['take']
        elif carried in ('none', 'unknown') and current.values.get('carried') == 'none':
            native_picker._p1_placed(current)
            if native_picker.p1_medal(current) != p1_before:
                raise RuntimeError('P1 medal identity disagrees with the qualified topology')
            current = native_picker.take_cpu(io)
            taken_here = True
            skipped = []
        else:
            raise RuntimeError(f"CPU placement requires its coin held or an empty hand: {carried!r}")
        ack, applied = native_picker.number(current, 'ack'), native_picker.number(current, 'applied')
        walked, reached = native_picker.walk_cpu(io, target, topology)
        steps = native_picker.number(walked, 'ack') - ack
        if steps < 0 or native_picker.number(walked, 'applied') - applied != steps:
            raise RuntimeError('CPU tile step counters disagree')
        if must_move and steps < 1:
            raise RuntimeError(f"the CPU's coin is already on {fighter}: this row cannot show a move")
        final = native_picker.place_cpu(io, target, reached, p1_before)
    remember(ctx, cpu_kind=entry['kind'], cpu=entry['name'])
    return _save(ctx, 'routes-place-cpu', {
        'block': 'place_cpu', 'args': {'fighter': fighter, 'must_move': must_move},
        'verified': {'cpu': {'tile': goal, 'kind': target.kind, 'name': target.name, 'form': target.form},
                     'p1_medal': list(native_picker.p1_medal(final)), 'steps': steps},
        'inputs': getattr(io, 'input_count', len(getattr(io, 'actions', ()))),
        'ms': {'total': round((time.monotonic() - started) * 1000)},
        'skipped': skipped, 'already': already, 'taken_here': taken_here,
    })


def expect_fighters(ctx, p1=None, cpu=None):
    """Confirm the requested Training fighters against roster and live bodies."""
    started = time.monotonic()
    io = BridgePickerIO(ctx)
    ui = native_picker.wait_sample(io, native_picker.UI, started + 6)
    if not _fields_match(ui, TRAINING_SCREEN):
        raise RuntimeError('expect_fighters needs Training with native input')
    state = native_state(ctx, fresh=('roster',), roster_entries=(0, 1))
    bodies = _switch_hw().fighters_now(instance_path(ctx, 'bridge_log').parent)
    roster = state.get('roster', {})
    for seat, name in ((0, p1), (1, cpu)):
        if name is not None:
            wanted = fighter_entry(name)['kind']
            roster_kind = roster.get(seat, {}).get('kind')
            body_kind = bodies.get(seat, {}).get('kind')
            if roster_kind != wanted or body_kind != wanted:
                raise RuntimeError(f'entry {seat} wanted {name} kind {wanted}: '
                                   f'roster kind {roster_kind}, body kind {body_kind}')
    return _save(ctx, 'routes-expect-fighters', {
        'block': 'expect_fighters', 'args': {'p1': p1, 'cpu': cpu},
        'verified': {'roster': {seat: value.get('kind') for seat, value in roster.items()},
                     'bodies': {seat: value.get('kind') for seat, value in bodies.items()}},
        'inputs': 0, 'ms': {'total': round((time.monotonic() - started) * 1000)},
    })


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
        state = _remember(io, io.read('tr.menu', remaining))
        block = state.block
        if block['schema'] != 'tr.menu/1' or block['rev'] <= 0 or block['filtered']:
            raise RuntimeError(f'expected complete tr.menu/1 reply; saw {block!r}')
        if after is not None:
            native_picker.same_epoch(after, state, in_flight=True)
            if block['rev'] <= after.block['rev']:
                continue
        if _fields_match(state, {'scene': 'training', 'keyboard': '0', 'open': str(int(opened))}):
            return state
        if strict:
            raise RuntimeError(f'expected Training Mod scene, keyboard and open state; saw {state.values!r}')
    raise RuntimeError(f'expected tr.menu scene=training, keyboard=0, open={int(opened)}; saw no matching menu')


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
        ui = _remember(io, native_picker.check_sample(
            io.read(native_picker.UI, until - time.monotonic()), native_picker.UI))
        native_picker.same_epoch(menu, ui, in_flight=True)
        if _fields_match(ui, expected):
            return ui
    raise RuntimeError(f'expected input owner {owner}; saw {ui.values.get("input.owner") if ui else "unknown"}')


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


def _current_settings_observation(ctx, cap):
    data = _fetch_settings(ctx, cap=cap)
    log = instance_path(ctx, 'bridge_log')
    lines = log.read_text(errors='replace').splitlines() if log.is_file() else []
    marker = next((line for line in reversed(lines)
                   if re.search(r'\[Bridge\] req set result=ok(?: |$)', line)
                   or '[TrainingRooms] settings.json written by the writer thread' in line), None)
    if marker is None or '[TrainingRooms] settings.json written by the writer thread' in marker:
        return json.loads(data), 'file', data
    try:
        record = json.loads((log.parent / 'live-settings.json').read_text())
        if record.get('set_reply') != marker or not isinstance(record.get('settings'), dict):
            raise ValueError('missing matching payload')
    except (OSError, ValueError, AttributeError, TypeError):
        raise RuntimeError('settings.json is stale: a bridge set was applied after the last file write '
                           'and its payload is not on record: ' + marker) from None
    return record['settings'], 'live-set', json.dumps(record['settings']).encode()


def _current_settings(ctx, cap):
    return _current_settings_observation(ctx, cap)[0]


_BRIDGE_ON_TITLE_MOVIE_READ = 0x1865104
_BRIDGE_ON_TITLE_LAYOUT = "[[x19+0xe0]+0x80]"
_BRIDGE_ON_TITLE_CONTEXT = "x19+0xc4"


def _bridge_on_capture(row, expression):
    for capture in row.get("captures", ()):
        if capture["expression"] == expression and not capture["error"]:
            return bytes.fromhex(capture["hex"])
    return b""


def _bridge_on_number(row, expression, width):
    data = _bridge_on_capture(row, expression)
    return int.from_bytes(data, "little") if len(data) == width else None


def _bridge_on_settled_phase(data, phase):
    if len(data) != 17:
        return False
    depth, current, age = (int.from_bytes(data[i:i + 4], "little") for i in (0, 4, 8))
    return depth == 0 and current == phase and age >= 2 and data[16] == 0


def _bridge_on_opening_owner(row):
    registers = row["registers"]
    pc = int(registers["pc"], 16)
    context = int(registers["x19"], 16)
    movie_layout = int(registers["x8"], 16)
    base = pc - _BRIDGE_ON_TITLE_MOVIE_READ
    context_bytes = _bridge_on_capture(row, _BRIDGE_ON_TITLE_CONTEXT)
    return (
        row["locations"].get(registers["pc"]) == f"main+0x{_BRIDGE_ON_TITLE_MOVIE_READ:x}"
        and bool(context and movie_layout)
        and len(context_bytes) == 45 and context_bytes[44] == 1
        and _bridge_on_number(row, "[x19+0xe0]", 8) == base + 0x50ACBA0
        and _bridge_on_number(row, "[x19+0xe8]", 8) == base + 0x50AC910
        and _bridge_on_number(row, "[x8+0xf8]", 8) == base + 0x5186398
        and _bridge_on_settled_phase(context_bytes[:17], 3)
        and _bridge_on_settled_phase(_bridge_on_capture(row, _BRIDGE_ON_TITLE_LAYOUT + "+0xd4"), 1)
        and _bridge_on_settled_phase(_bridge_on_capture(row, "x8+0xd4"), 2)
        and _bridge_on_capture(row, "[[x8+0xf8]+8]+0x110") == bytes((0, 1, 1, 0))
    )


def _bridge_on_arm_opening(ctx):
    expressions = (
        (_BRIDGE_ON_TITLE_CONTEXT, "bytes", 45),
        ("[x19+0xe0]", "u64", 0), ("[x19+0xe8]", "u64", 0),
        (_BRIDGE_ON_TITLE_LAYOUT + "+0xd4", "bytes", 17),
        ("x8+0xd4", "bytes", 17), ("[x8+0xf8]", "u64", 0),
        ("[[x8+0xf8]+8]+0x110", "bytes", 4),
    )
    return ctx.call("trace.arm", {
        "target": f"main+0x{_BRIDGE_ON_TITLE_MOVIE_READ:x}", "registers": ["pc", "x19", "x8"],
        "condition": "thread == main", "max_hits": 256, "rate_limit": 120,
        "captures": [{"expression": field, "type": type_, **({"size": size} if size else {})}
                     for field, type_, size in expressions],
    })


def _bridge_on_events(ctx):
    path = instance_path(ctx, "bridge_events")
    return path.read_text().splitlines() if path.is_file() else []


class BridgeOnChordHoldError(RuntimeError):
    pass


def _bridge_on_chord(ctx):
    buttons = ("lstick", "rstick", "zl", "zr")
    snapshot_ns = time.monotonic_ns()
    before = _bridge_on_events(ctx)
    press_wall = None
    event = None
    first_event_seen_s = None
    try:
        for button in buttons:
            ctx.call("input.button", {"button": button, "state": "press",
                                      "duration_ms": 10000, "lease_ms": 10000})
        started = time.monotonic_ns()
        press_wall = time.time()
        last_no_event_ns = snapshot_ns - started
        while True:
            ctx.check_stop()
            poll_ns = time.monotonic_ns()
            after = _bridge_on_events(ctx)
            if after[:len(before)] != before:
                raise RuntimeError("Toggle event history changed")
            elapsed_ns = time.monotonic_ns() - started
            if event is None and elapsed_ns <= 6_000_000_000:
                event = next((line for line in after[len(before):]
                              if "[Bridge] toggle phase=" in line), None)
                if event is not None:
                    first_event_seen_s = elapsed_ns / 1_000_000_000
                else:
                    last_no_event_ns = poll_ns - started
            if elapsed_ns >= 6_000_000_000 or (event is not None and elapsed_ns >= 3_200_000_000):
                break
            time.sleep(min(0.005, (6_000_000_000 - elapsed_ns) / 1_000_000_000))
        release_wall = time.time()
        held_s = (time.monotonic_ns() - started) / 1_000_000_000
        for button in buttons:
            ctx.call("input.button", {"button": button, "state": "release",
                                      "duration_ms": 3200, "lease_ms": 1000})
    except BaseException:
        try:
            ctx.call("input.reset", {})
        except Exception:
            pass
        raise
    last_no_event_s = last_no_event_ns / 1_000_000_000
    suffix = 1
    while (Path(ctx.row_dir) / f"chord-hold-{suffix}.json").exists():
        suffix += 1
    result = _save(ctx, f"chord-hold-{suffix}", {
        "press_wall": press_wall, "release_wall": release_wall, "held_s": round(held_s, 3),
        "last_no_event_s": last_no_event_s, "first_event_seen_s": first_event_seen_s,
        "latency_bound_s": None if event is None else [last_no_event_s, first_event_seen_s], "event": event,
    })
    if event is None:
        raise BridgeOnChordHoldError("no toggle within 6 s of the chord")
    if last_no_event_ns < 3_000_000_000:
        raise BridgeOnChordHoldError(f"chord toggle may have come before 3.0 s: no-event poll at {last_no_event_s:.3f} s, event seen at {first_event_seen_s:.3f} s")
    return result


def _bridge_on_cold_enable(ctx, trace):
    before = _bridge_on_events(ctx)
    existing = next((line for line in reversed(before) if "[Bridge] toggle phase=on " in line), None)
    if existing is not None:
        return {'chord': 'already on', 'event': existing}
    armed = _bridge_on_arm_opening(ctx)
    point = str(armed.get("id", armed.get("point_id")))
    if point == "None":
        raise RuntimeError(f"Opening owner trace did not arm: {armed!r}")
    until = time.monotonic() + 54
    witness = None
    while time.monotonic() < until:
        ctx.check_stop()
        before = _bridge_on_events(ctx)
        boot_off = any("toggle=off reason=boot_default" in line for line in before)
        ready = any(re.fullmatch(r"\d+ \[Bridge\] chord ready", line) for line in before)
        if any("[Bridge] toggle phase=on " in line for line in before):
            event = next(line for line in reversed(before) if "[Bridge] toggle phase=on " in line)
            return {'chord': 'already on', 'event': event}
        if boot_off and ready and trace.is_file():
            for line in reversed(trace.read_text().splitlines()):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("point_id")) == point:
                    age = time.time_ns() - int(row["wall_time_ns"])
                    if 0 <= age <= 500_000_000 and _bridge_on_opening_owner(row):
                        witness = row
                        break
            if witness is not None:
                break
        time.sleep(0.05)
    if witness is None:
        raise RuntimeError("No live stock opening owner and boot-default chord readiness")
    _bridge_on_chord(ctx)
    until = time.monotonic() + 10
    while time.monotonic() < until:
        ctx.check_stop()
        after = _bridge_on_events(ctx)
        if after[:len(before)] != before:
            raise RuntimeError("Cold-start toggle event history changed")
        transitions = [line for line in after[len(before):] if "[Bridge] toggle phase=" in line]
        if transitions:
            if len(transitions) != 1 or not re.search(r"phase=on gen=1 .*\bsource=pad_chord\b", transitions[0]):
                raise RuntimeError(f"Unexpected cold chord transition: {transitions!r}")
            return _save(ctx, "cold-opening-chord", {"stock": witness, "event": transitions[0]})
        time.sleep(0.05)
    raise RuntimeError("No fresh pad-chord On event within 10 seconds")


def free_port(first=6980, last=7079, host='0.0.0.0'):
    for port in range(first, last + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
                candidate.bind((host, port))
            return port
        except OSError:
            continue
    raise RuntimeError(f'no free TCP port on {host} in range {first}-{last}')


def free_udp_port(first=24872, last=24971):
    for port in range(first, last + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as candidate:
                candidate.bind(('0.0.0.0', port))
            return port
        except OSError:
            continue
    raise RuntimeError(f'no free UDP port on 0.0.0.0 in range {first}-{last}')


def bridge_on(ctx, port=None, cap=60):
    """Turn the bridge on at the title screen and start its detached playbook capture."""
    try:
        if not ctx.instance.get('trace_jsonl'):
            raise KeyError('trace_jsonl')
        trace = instance_path(ctx, 'trace_jsonl')
    except KeyError:
        raise RuntimeError('bridge_on needs a trace run: the title-screen witness is a trace point '
                           '(start Eden with a trace config and the GDB stub off)') from None
    if port is None:
        try:
            port = int(instance_path(ctx, 'sdmc:ultimate/ssbu-hw-bridge/port.txt').read_text().strip())
        except (KeyError, OSError, ValueError):
            raise RuntimeError('bridge_on needs a port argument or an integer in profile port.txt') from None
    chord = _bridge_on_cold_enable(ctx, trace)
    until = time.monotonic() + 5
    listening = last_listen = None
    pattern = re.compile(r'\[Bridge\] listening port=' + re.escape(str(port)) + r'\b')
    while time.monotonic() < until:
        ctx.check_stop()
        events = _bridge_on_events(ctx)
        last_listen = next((line for line in reversed(events) if '[Bridge] listen' in line), None)
        listening = next((line for line in reversed(events) if pattern.search(line)), None)
        if listening is not None:
            break
        time.sleep(0.05)
    if listening is None:
        raise RuntimeError(f'bridge_on: this Eden\'s bridge is not listening on port {port}; '
                           f'expected "[Bridge] listening port={port}" in its own events log, '
                           f'saw: {last_listen or "no listen line"}. Another process holds the port: '
                           'give the run its own free port (port.txt) and boot again. No capture was started.')
    run = Path(ctx.row_dir).parent / 'bridge-run'
    (run / 'bridge' / 'files').mkdir(parents=True, exist_ok=True)
    log = run / 'bridge.log'
    log.touch()
    if fcntl is None:
        raise RuntimeError('bridge_on requires flock support for the detached capture')
    with (run / 'bridge.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            reused = True
        else:
            reused = False
            fcntl.flock(lock, fcntl.LOCK_UN)
    capture_path = run / 'capture.json'
    if reused:
        capture = json.loads(capture_path.read_text()) if capture_path.is_file() else {}
        pid = capture.get('pid')
    else:
        with (run / 'capture.stderr.log').open('ab') as stderr:
            process = subprocess.Popen(
                [sys.executable, '-I', str(HERE / 'switch_hw.py'), 'capture-bridge', str(run),
                 '--bridge-host', f'127.0.0.1:{port}', '--console', str(run / 'bridge-console.log')],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr, start_new_session=True)
        pid = process.pid
        capture_path.write_text(json.dumps({'pid': pid, 'port': port, 'instance': ctx.on,
                                            'bridge_log': str(log), 'started': time.time()}) + '\n')
    ctx.runner.instances.setdefault(ctx.on, {})['bridge_log'] = str(log)
    text = ''
    until = time.monotonic() + cap
    while 'plugin=ssbu-hw-bridge/' not in text or 'tr_export=' not in text:
        if time.monotonic() > until:
            raise RuntimeError(f'no bridge greeting within {cap} s: {text[-400:]!r}')
        time.sleep(0.2)
        text = log.read_text(errors='replace')
    hello = next(l for l in text.splitlines() if '[Bridge] hello' in l)
    install = next(l for l in text.splitlines() if 'plugin=ssbu-hw-bridge/' in l and 'result=' in l)
    guard = next((field for field in install.split() if field.startswith('guards=')), '')
    if not guard or int(guard.split('=', 1)[1], 16) & 0x3fffff != 0x3fffff:
        raise RuntimeError(f'bridge core guards incomplete: {install}')
    fields = set(install.split())
    for want in ('tr_export=available', 'toggle=off', 'reason=boot_default', 'chord=deferred',
                 'server=started', 'result=installed', 'h1=installed',
                 'h2=installed', 'h3=installed'):
        if want not in fields:
            raise RuntimeError(f'bridge install line lacks {want}: {install}')
    if not re.search(r'\btoggle_gen=[1-9][0-9]*\b', hello):
        raise RuntimeError(f'bridge greeting lacks a positive On generation: {hello}')
    backup = None
    until = time.monotonic() + 15
    while time.monotonic() < until:
        ctx.check_stop()
        lines = log.read_text(errors='replace').splitlines()
        backup = next((line for line in reversed(lines) if '[Bridge] req backup result=' in line), None)
        if backup is not None:
            break
        time.sleep(0.1)
    return _save(ctx, 'routes-bridge-on', {
        'block': 'bridge_on', 'chord': chord, 'listening': listening, 'hello': hello, 'install': install, 'backup': backup,
        'backup_open': bool(backup and re.search(r'\[Bridge\] req backup result=ok(?: |$)', backup)),
        'capture': {'pid': pid, 'port': port, 'bridge_log': str(log)}, 'reused': reused,
    })


def _apply_settings(ctx, data, cap=30):
    log = instance_path(ctx, 'bridge_log')
    cursor = LogCursor(log, hold=False)
    _spool_file(ctx, 'diag-settings-next.json', data)
    until = time.monotonic() + cap
    reply = reload = None
    pattern = re.compile(r'\[TrainingRooms\] diag_settings_reload rerolls=\S+ issues=(\d+)(?: |$)')
    while time.monotonic() < until and not (reply and reload):
        ctx.check_stop()
        for line in cursor.read():
            if state_gate.BOUNDARY.search(line) or cursor.rotations:
                raise RuntimeError('bridge continuity changed while applying settings')
            if '[Bridge] req set result=' in line:
                if not re.search(r'\[Bridge\] req set result=ok(?: |$)', line):
                    if re.search(r'\[Bridge\] req set result=refused reason=no_backup(?: |$)', line):
                        raise RuntimeError('settings set refused: the bridge has no open fixture backup. '
                                           'Expected the capture to have opened one at connect '
                                           '("[Bridge] req backup result=ok"; routes:bridge_on does). Saw: ' + line)
                    raise RuntimeError(line)
                reply = line
            if match := pattern.search(line):
                if int(match[1]) != 0:
                    raise RuntimeError(f'settings set: expected reload issues=0, saw: {line}')
                reload = line
        if not (reply and reload):
            time.sleep(min(.05, max(0, until - time.monotonic())))
    if not (reply and reload):
        raise RuntimeError('no fresh settings set reply and reload line before deadline')
    settings = json.loads(data)
    if not isinstance(settings, dict):
        raise RuntimeError('settings set: expected a JSON object')
    live = log.parent / 'live-settings.json'
    temporary = live.with_suffix('.json.tmp')
    try:
        temporary.write_text(json.dumps({'set_reply': reply, 'reload': reload, 'settings': settings}) + '\n')
        os.replace(temporary, live)
    finally:
        temporary.unlink(missing_ok=True)
    return reply, reload


INPUT_LAG_S = 3.0


def _send_with_retry(io, macro, until, wait):
    io.send(macro)
    result = wait(min(until, time.monotonic() + INPUT_LAG_S))
    if result is None and time.monotonic() < until:
        io.send(macro)
        metrics = getattr(io.ctx.runner, 'setup_metrics', None)
        if metrics is not None:
            metrics['retries'] = metrics.get('retries', 0) + 1
        result = wait(until)
    if result is not None:
        io.finish()
    return result


def _tr_input(io, before, macro, until, *, opened=True):
    old_ack = native_picker.number(before, 'ack')
    if time.monotonic() >= until:
        raise RuntimeError(f'Training Mod deadline expired before {macro}')
    latest = before

    def wait(deadline):
        nonlocal latest
        while (remaining := deadline - time.monotonic()) > 0:
            after = _remember(io, io.read('tr.menu', remaining))
            block = after.block
            if block['schema'] != 'tr.menu/1' or block['rev'] <= 0 or block['filtered']:
                raise RuntimeError(f'expected complete tr.menu/1 reply; saw {block!r}')
            native_picker.same_epoch(latest, after, in_flight=True)
            if block['rev'] <= latest.block['rev']:
                continue
            latest = after
            ack = native_picker.number(after, 'ack')
            if ack == old_ack:
                continue
            if ack > old_ack + 1:
                raise RuntimeError(f'input handled twice: expected ACK {old_ack + 1}; saw {ack}')
            if ack != old_ack + 1 or after.values.get('ack.result') != 'handled':
                raise RuntimeError(f'expected one handled ACK {old_ack + 1}; saw {ack} {after.values.get("ack.result")}')
            if not _fields_match(after, {'scene': 'training', 'keyboard': '0', 'open': str(int(opened))}):
                raise RuntimeError(f'expected Training Mod open={int(opened)}; saw {after.values}')
            return after
        return None

    after = _send_with_retry(io, macro, until, wait)
    if after is None:
        raise RuntimeError(f'expected {macro} handled ACK; saw input was not handled')
    return after


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


def _live_body(ctx, seat, kind, cap):
    """Wait for a fresh live body matching the requested seat and kind."""
    until = time.monotonic() + cap
    last = None
    while time.monotonic() < until:
        ctx.check_stop()
        metrics = getattr(ctx.runner, 'setup_metrics', None)
        if metrics is not None:
            metrics['bridge_requests'] += 1
        try:
            bodies = _switch_hw().fighters_now(instance_path(ctx, 'bridge_log').parent,
                                               cap=max(.01, min(6.0, until - time.monotonic())))
            last = bodies.get(seat)
            if last is not None and last.get('kind') == kind:
                return last
        except RuntimeError:
            last = None
        remaining = until - time.monotonic()
        if remaining > 0:
            time.sleep(min(.25, remaining))
    raise RuntimeError(f'live fighter does not confirm entry {seat} kind {kind}: {last!r}')


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


def _remember(io, state):
    metrics = getattr(getattr(getattr(io, 'ctx', None), 'runner', None), 'setup_metrics', None)
    if isinstance(metrics, dict) and 'last_seen' in metrics:
        fields = ('open', 'page', 'depth', 'focus', 'action', 'row', 'game', 'game.running',
                  'notice.header', 'notice.body', 'editor', 'editor.sel')
        if state.block['name'] == 'tr.menu':
            metrics['last_seen'].update({key: state.values[key] for key in fields if key in state.values})
        elif state.block['name'] == native_picker.UI:
            metrics['last_seen']['input.owner'] = state.values.get('input.owner', 'unknown')
    return state


def _block(ctx, block, name, args, action):
    started = time.monotonic()
    metrics = {'inputs': 0, 'bridge_requests': 0, 'bridge_boot': None, 'revisions': {},
               'retries': 0, 'last_seen': {}}
    previous = getattr(ctx.runner, 'setup_metrics', None)
    ctx.runner.setup_metrics = metrics
    try:
        try:
            receipt = action(metrics)
            receipt.pop('observed', None)
            receipt['ok'] = True
        except RuntimeError as error:
            last = dict(metrics['last_seen'])
            seen = ' '.join(f'{key}={value}' for key, value in last.items()) or 'none'
            observed = metrics.pop('observed', None)
            elapsed = round((time.monotonic() - started) * 1000)
            receipt = {'block': block, 'args': args, 'ok': False, 'error': str(error),
                       'last_seen': last, **metrics, 'ms': {'total': elapsed}}
            if observed is not None:
                receipt['observed'] = observed
            _save_block(ctx, name, receipt)
            raise RuntimeError(f'{error}; last seen: {seen}') from error
        return _save_block(ctx, name, receipt)
    finally:
        ctx.runner.setup_metrics = previous
        if previous is not None:
            _merge_setup_metrics(previous, metrics)


def _save_block(ctx, name, receipt):
    directory = Path(ctx.row_dir)
    number = 1
    while True:
        suffix = '' if number == 1 else f'-{number}'
        try:
            with (directory / f'{name}{suffix}.json').open('x') as target:
                target.write(json.dumps(receipt, indent=1, default=str) + '\n')
            return receipt
        except FileExistsError:
            number += 1


TR_PAGES = ('cpu', 'saves', 'recording', 'fighters_stages', 'display_controls', 'developer')


def _tr_current(io, until):
    remaining = until - time.monotonic()
    if remaining <= 0:
        raise RuntimeError('Training Mod deadline expired before the menu read')
    menu = _remember(io, io.read('tr.menu', remaining))
    if (menu.block['schema'] != 'tr.menu/1' or menu.block['rev'] <= 0 or menu.block['filtered']
            or menu.values.get('scene') != 'training' or menu.values.get('keyboard') != '0'
            or menu.values.get('open') not in ('0', '1')):
        raise RuntimeError(f'expected complete menu observation in Training without a keyboard; saw {menu.values!r}')
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


def _open_menu(io, until, page, *, after=None):
    menu = _tr_current(io, until)
    if after is not None:
        native_picker.same_epoch(after, menu, in_flight=True)
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
        before = menu
        menu = _send_with_retry(io, 'tr-menu-open', until,
                                lambda deadline: _try_tr_condition(
                                    io, before, deadline,
                                    lambda state: _fields_match(state, {'scene': 'training', 'keyboard': '0', 'open': '1'})))
        if menu is None:
            raise RuntimeError('expected open Training Mod menu; saw no new open menu')
        _wait_training_owner(io, menu, until, 'tr.menu')
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


def _close_menu(io, until, *, after=None):
    menu = _tr_current(io, until)
    if after is not None:
        native_picker.same_epoch(after, menu, in_flight=True)
    opened = menu.values['open'] == '1'
    _wait_training_owner(io, menu, until, 'tr.menu' if opened else 'native')
    if not opened:
        return menu, ['close']
    menu = _tr_back_to_page(io, menu, until)
    menu = _tr_input(io, menu, 'tap-b1', until, opened=False)
    _wait_training_owner(io, menu, until, 'native')
    return menu, []


def tr_open(ctx, page=None, cap=20):
    args = {'page': page, 'cap': cap}

    def action(metrics):
        if page is not None and page not in TR_PAGES:
            raise RuntimeError(f'unknown Training Mod page {page!r}')
        return _tr_open(ctx, page, cap, metrics)

    return _block(ctx, 'tr_open', 'routes-tr-open', args, action)


def _tr_open(ctx, page, cap, metrics):
    started = time.monotonic()
    io = BridgePickerIO(ctx)
    menu, skipped = _open_menu(io, started + cap, page)
    if menu.values.get('page') not in TR_PAGES or not menu.values.get('focus'):
        raise RuntimeError('Training Mod open menu page or focus is missing')
    owner = _wait_training_owner(io, menu, started + cap, 'tr.menu')
    elapsed = round((time.monotonic() - started) * 1000)
    return {
        'block': 'tr_open', 'args': {'page': page, 'cap': cap},
        'verified': {'open': menu.values['open'], 'page': menu.values.get('page'),
                     'depth': _tr_depth(menu), 'owner': owner.values['input.owner'], 'focus': menu.values.get('focus')},
        **metrics, 'ms': {'menu': elapsed, 'total': elapsed}, 'skipped': skipped,
    }


def tr_close(ctx, cap=20):
    return _block(ctx, 'tr_close', 'routes-tr-close', {'cap': cap},
                  lambda metrics: _tr_close(ctx, cap, metrics))


def _tr_close(ctx, cap, metrics):
    started = time.monotonic()
    io = BridgePickerIO(ctx)
    menu, skipped = _close_menu(io, started + cap)
    owner = _wait_training_owner(io, menu, started + cap, 'native')
    elapsed = round((time.monotonic() - started) * 1000)
    return {
        'block': 'tr_close', 'args': {'cap': cap},
        'verified': {'open': menu.values['open'], 'owner': owner.values['input.owner']},
        **metrics, 'ms': {'menu': elapsed, 'total': elapsed}, 'skipped': skipped,
    }


def tr_idle(ctx, cap=20):
    return _block(ctx, 'tr_idle', 'routes-tr-idle', {'cap': cap},
                  lambda metrics: _tr_idle(ctx, cap, metrics))


def _tr_idle(ctx, cap, metrics):
    if type(cap) not in (int, float) or not 0 < cap < float('inf'):
        raise RuntimeError('tr_idle needs a positive cap')
    started = time.monotonic()
    until = started + cap
    io = BridgePickerIO(ctx)
    steps = []
    waited_notices = set()
    last = {}
    input_steps = 0
    while time.monotonic() < until:
        ctx.check_stop()
        menu = _tr_current(io, until)
        ui = _remember(io, io.read(native_picker.UI, max(.001, until - time.monotonic())))
        native_picker.same_epoch(menu, ui, in_flight=True)
        last = {'open': menu.values.get('open'), 'game.running': menu.values.get('game.running'),
                'notice.header': menu.values.get('notice.header'), 'input.owner': ui.values.get('input.owner')}
        if last == {'open': '0', 'game.running': '0', 'notice.header': '', 'input.owner': 'native'}:
            return {'block': 'tr_idle', 'args': {'cap': cap}, 'steps': steps, 'verified': last,
                    **metrics, 'ms': {'total': round((time.monotonic() - started) * 1000)}, 'skipped': []}
        if input_steps >= 12 and (_edge_wizard(menu) or last['open'] == '1' or last['game.running'] == '1'):
            break
        if isinstance(last['notice.header'], str) and _edge_wizard(menu):
            header = last['notice.header']
            input_steps += 1
            try:
                io.send('tap-b1')
                after = _try_tr_condition(
                    io, menu, until,
                    lambda state: any(state.values.get(key) != menu.values.get(key)
                                      for key in ('notice.header', 'open', 'game.running')))
            except RuntimeError as error:
                io.finish()
                if str(error) == 'no fenced bridge reply for state tr.menu':
                    raise RuntimeError(f'tr_idle: expected the wizard page to change after B; saw {last!r}') from error
                raise
            if after is None:
                io.finish()
                raise RuntimeError(f'tr_idle: expected the wizard page to change after B; saw {last!r}')
            io.finish()
            steps.append(f'B out of wizard page {header}')
            continue
        if last['open'] == '1':
            input_steps += 1
            _close_menu(io, until, after=menu)
            steps.append('close menu')
        elif last['game.running'] == '1':
            input_steps += 1
            game = menu.values.get('game')
            if game != 'Guard the Edge':
                raise RuntimeError(f'tr_idle: expected stoppable Guard the Edge; saw {last!r}, game={game!r}')
            _drill_stop(ctx, game, max(.001, until - time.monotonic()), metrics)
            steps.append('stop Guard the Edge')
        elif last['open'] == '0' and last['game.running'] == '0':
            header = last['notice.header']
            if header not in waited_notices:
                steps.append(f'waited for notice "{header}" to clear')
                waited_notices.add(header)
            time.sleep(min(.2, max(0, until - time.monotonic())))
        else:
            raise RuntimeError(f'tr_idle: expected plain idle Training; saw {last!r}')
    raise RuntimeError(f'tr_idle: expected plain idle Training within {cap} s and 12 steps; saw {last!r}')


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
        def matches(key, value):
            observed = menu.values.get(key)
            if key == 'row' and isinstance(observed, str):
                normalize = lambda text: ' '.join(unicodedata.normalize('NFKC', text).casefold().split())
                return normalize(observed) == normalize(value)
            return observed == value

        while not all(matches(key, value) for key, value in target.items()):
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
                raise RuntimeError('Training Mod Rows group was not published after activation')
            _tr_identity(after)
            menu = after
    return menu


def tr_goto(ctx, page, path, cap=30):
    args = {'page': page, 'path': path, 'cap': cap}

    def action(metrics):
        if page not in TR_PAGES:
            raise RuntimeError(f'unknown Training Mod page {page!r}')
        targets = _tr_path(path)
        if type(cap) not in (int, float) or not 0 < cap < float('inf'):
            raise RuntimeError('Training Mod path needs a positive cap')
        return _tr_goto(ctx, page, path, targets, cap, metrics)

    return _block(ctx, 'tr_goto', 'routes-tr-goto', args, action)


def _tr_goto(ctx, page, path, targets, cap, metrics):
    started = time.monotonic()
    until = started + cap
    io = BridgePickerIO(ctx)
    menu, skipped = _open_menu(io, until, page)
    menu = _navigate_menu(io, menu, page, targets, until)
    _tr_identity(menu)
    if not menu.values.get('focus'):
        raise RuntimeError('Training Mod focus is missing')
    owner = _wait_training_owner(io, menu, until, 'tr.menu')
    elapsed = round((time.monotonic() - started) * 1000)
    return {
        'block': 'tr_goto', 'args': {'page': page, 'path': path, 'cap': cap},
        'verified': {key: menu.values.get(key) for key in ('focus', 'page', 'group', 'cursor', 'action', 'row')}
                    | {'depth': _tr_depth(menu), 'owner': owner.values['input.owner']},
        **metrics, 'ms': {'menu': elapsed, 'total': elapsed}, 'skipped': skipped,
    }


def _settings_menu(data):
    class Pairs(list):
        pass

    def unique(pairs, label):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError(f'duplicate settings key {label}{key}')
            result[key] = value
        return result

    try:
        root = json.loads(data, object_pairs_hook=Pairs)
        if not isinstance(root, Pairs):
            raise ValueError('top-level settings is not an object')
        top = unique(root, '')
        for name in ('menu', 'defaults_menu'):
            if isinstance(top.get(name), Pairs):
                top[name] = unique(top[name], name + '.')
        menu = top['menu']
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f'settings menu is missing or malformed: {error}') from error
    if not isinstance(menu, dict):
        raise RuntimeError('settings menu is not an object')
    return menu


def _settings_row(menu, key):
    value = menu.get(key)
    if not isinstance(value, list) or any(type(item) is not int or item not in (0, 1) for item in value):
        raise RuntimeError(f'expected a 0/1 integer array for menu.{key}; saw {value!r}')
    return value


def _stored_one_hot(size, selected):
    return [int(index == selected) for index in range(size)]


def _tr_editor_move(io, menu, wanted, count, depth, until):
    current = native_picker.number(menu, 'editor.sel')
    if not 0 <= current < count or not 0 <= wanted < count:
        raise RuntimeError('Training Mod editor selection is outside its options')
    for _ in range(count):
        if current == wanted:
            return menu
        after = _tr_input(io, menu, 'main-menu-down', until)
        expected = (current + 1) % count
        if _tr_depth(after) != depth or native_picker.number(after, 'editor.sel') != expected:
            raise RuntimeError('Training Mod editor selection did not move by one')
        menu, current = after, expected
    raise RuntimeError('Training Mod editor option not found before wrapping')


def _practice_target(row_id, value, before):
    if row_id not in ('edge_difficulty', 'juggle_difficulty'):
        raise RuntimeError('practice_cpu kind needs a supported practice CPU row')
    if not isinstance(value, dict) or not {'fighter', 'mode'} <= set(value) or set(value) - {'fighter', 'mode', 'level'}:
        raise RuntimeError('practice_cpu value needs fighter, mode and optional level')
    entry = fighter_entry(value['fighter'])
    catalog = tables()['roster_catalog']
    indices = [index for index, item in enumerate(catalog) if item['kind'] == entry['kind']]
    if len(indices) != 1:
        raise RuntimeError('practice fighter is absent or ambiguous in the source catalog')
    prefix = 'edge' if row_id == 'edge_difficulty' else 'juggle'
    fighter_key, level_key = prefix + '_cpu_fighter', prefix + '_cpu_level'
    mode = value['mode']
    match = re.fullmatch(r'CPU ([1-9])', mode) if isinstance(mode, str) else None
    if mode not in ('Perfect', 'Zoom') and not match:
        raise RuntimeError('practice CPU mode is not a supported mode key')
    if mode == 'Zoom' and entry['kind'] != 0x53:
        raise RuntimeError('practice Zoom is available only for Hero')
    old_level = before.get(level_key)
    if len(old_level) != 9 or sum(old_level) != 1:
        raise RuntimeError(f'settings menu.{level_key} is not a one-hot level')
    level = value.get('level', int(match[1]) if match else old_level.index(1) + 1)
    if type(level) is not int or not 1 <= level <= 9 or match and int(match[1]) != level:
        raise RuntimeError('practice CPU level contradicts the requested mode')
    mode_index = 0 if match else 1 if mode == 'Perfect' else 2
    target = {fighter_key: _stored_one_hot(len(catalog) + 1, indices[0] + 1),
              row_id: _stored_one_hot(3, mode_index), level_key: _stored_one_hot(9, level - 1)}
    return entry, level, mode, target


def tr_set(ctx, page, path, kind, value, cap=45):
    args = {'page': page, 'path': path, 'kind': kind, 'value': value, 'cap': cap}

    def action(metrics):
        if page not in TR_PAGES:
            raise RuntimeError(f'unknown Training Mod page {page!r}')
        targets = _tr_path(path)
        if kind not in ('list', 'toggle', 'checklist', 'practice_cpu'):
            raise RuntimeError(f'unknown Training Mod setting kind {kind!r}')
        if type(cap) not in (int, float) or not 0 < cap < float('inf'):
            raise RuntimeError('Training Mod setting needs a positive cap')
        return _tr_set(ctx, page, path, targets, kind, value, cap, metrics)

    return _block(ctx, 'tr_set', 'routes-tr-set', args, action)


def _tr_set(ctx, page, path, targets, kind, value, cap, metrics):
    started = time.monotonic()
    until = started + cap
    io = BridgePickerIO(ctx)
    initial = _tr_current(io, until)
    closed, _ = _close_menu(io, until, after=initial)
    _, before_source, before_bytes = _current_settings_observation(ctx, max(.001, until - time.monotonic()))
    before_menu = _settings_menu(before_bytes)
    menu, _ = _open_menu(io, until, page, after=closed)
    menu = _navigate_menu(io, menu, page, targets, until)
    row_id = menu.values.get('row_id')
    if not isinstance(row_id, str) or not row_id:
        raise RuntimeError('Training Mod focused row has no row_id')
    kinds = json.loads((ROUTES / 'tr-menu-kinds.json').read_text())
    if row_id not in kinds[kind]:
        raise RuntimeError(f'Training Mod row {row_id!r} is not the requested kind {kind!r}')
    if kind == 'practice_cpu':
        prefix = 'edge' if row_id == 'edge_difficulty' else 'juggle'
        for key in (row_id, prefix + '_cpu_fighter', prefix + '_cpu_level'):
            _settings_row(before_menu, key)
        entry, level, mode, wanted = _practice_target(row_id, value, before_menu)
        before = {key: before_menu.get(key) for key in wanted}
    else:
        before = _settings_row(before_menu, row_id)
        widths = json.loads((ROUTES / 'tr-setting-widths.json').read_text())['widths']
        width = widths.get(row_id)
        if width is None:
            raise RuntimeError(f'unsupported: no product width for menu.{row_id}')
        if len(before) != width:
            raise RuntimeError(f'unsupported: settings file menu.{row_id} has {len(before)} slots; '
                               f'product schema has {width}')
        original_value = value
        if kind == 'toggle' and (type(value) is bool or isinstance(value, str) and value.casefold() in ('on', 'off')):
            on = value if type(value) is bool else value.casefold() == 'on'
            value = [int(on), int(not on)]
        elif kind == 'list' and type(value) is int:
            if not 0 <= value < width:
                raise RuntimeError(f'unsupported list index {value} for menu.{row_id}: '
                                   f'product schema has {width} options')
            value = _stored_one_hot(width, value)
        if (not isinstance(value, list) or not value
                or any(type(item) is not int or item not in (0, 1) for item in value)):
            raise RuntimeError(f'expected a 0/1 integer array for value; saw {value!r}')
        if len(value) != width:
            raise RuntimeError(f'expected settings menu.{row_id} array of length {width}; saw {value!r}')
        if kind == 'toggle' and value not in ([1, 0], [0, 1]) or kind == 'list' and sum(value) != 1:
            raise RuntimeError(f'invalid {kind} value encoding')
        wanted = {row_id: value}
    if time.monotonic() >= until:
        raise RuntimeError('Training Mod setting deadline expired before applying')
    writer = None

    def changing(state, macro):
        nonlocal writer
        if writer is None:
            writer = LogCursor(instance_path(ctx, 'bridge_log'), hold=False)
        return _tr_input(io, state, macro, until)

    already = all(before_menu.get(key) == target for key, target in wanted.items())
    original_depth = _tr_depth(menu)
    if not already and kind == 'toggle':
        menu = changing(menu, 'tap-a1')
        if _tr_depth(menu) != original_depth:
            raise RuntimeError('toggle kind unexpectedly entered an editor')
    elif not already and kind in ('list', 'checklist'):
        menu = _tr_input(io, menu, 'tap-a1', until)
        depth = original_depth + 1
        if _tr_depth(menu) != depth or menu.values.get('editor') not in ('', None):
            raise RuntimeError(f'{kind} kind did not enter its list editor')
        if kind == 'list':
            desired = value.index(1)
            count = len(value)
            if row_id in kinds['retired']:
                if desired == 0:
                    raise RuntimeError('retired practice Off option cannot be written from the list')
                desired -= 1
                count -= 1
            if row_id in kinds['yes_no_random']:
                count = 3
            menu = _tr_editor_move(io, menu, desired, count, depth, until)
            menu = changing(menu, 'tap-a1')
            if _tr_depth(menu) != original_depth:
                raise RuntimeError('list kind did not commit and leave its editor')
        else:
            if any(type(item) is not int or item not in (0, 1) for item in before):
                raise RuntimeError('checklist before value has non-binary weights')
            if row_id == 'hitbox_vis' and len(value) > 3 and before[3] != value[3]:
                raise RuntimeError('retired hitbox checklist slot cannot be edited')
            count = len(value) - (1 if row_id == 'hitbox_vis' else 0)
            for index, (old, target) in enumerate(zip(before, value)):
                if old != target:
                    cursor = index - (1 if row_id == 'hitbox_vis' and index > 3 else 0)
                    menu = _tr_editor_move(io, menu, cursor, count, depth, until)
                    menu = changing(menu, 'tap-a1')
                    if _tr_depth(menu) != depth or native_picker.number(menu, 'editor.sel') != cursor:
                        raise RuntimeError('checklist kind did not stay on its selected entry')
            menu = _tr_input(io, menu, 'tap-b1', until)
            if _tr_depth(menu) != original_depth:
                raise RuntimeError('checklist editor did not close')
    elif not already:
        menu = _tr_input(io, menu, 'tap-a1', until)
        depth = original_depth + 1
        game_name = 'GuardEdge' if row_id == 'edge_difficulty' else 'Juggler'
        editor = f'PracticeCpu({game_name})'
        if _tr_depth(menu) != depth or menu.values.get('editor') != editor:
            raise RuntimeError('practice_cpu kind did not open its roster editor')
        cell = native_picker.number(menu, 'editor.sel')
        for direction in tr_grid_path(cell, entry['tile']):
            cell = tr_grid_step(cell, direction)
            menu = _tr_input(io, menu, f'main-menu-{direction}', until)
            if menu.values.get('editor') != editor or native_picker.number(menu, 'editor.sel') != cell:
                raise RuntimeError('practice CPU roster did not reach the predicted cell')
        menu = changing(menu, 'tap-a1')
        if _tr_depth(menu) != depth or menu.values.get('editor') != editor or not menu.values.get('editor.sel_key'):
            raise RuntimeError('practice CPU mode popup did not open')
        mode_count = 3 if entry['kind'] == 0x53 else 2
        current_level = before_menu[('edge' if row_id == 'edge_difficulty' else 'juggle') + '_cpu_level'].index(1) + 1

        def popup_key(state):
            choice = native_picker.number(state, 'editor.sel')
            keys = (f'CPU {current_level}', 'Perfect', 'Zoom')[:mode_count]
            if choice >= mode_count or state.values.get('editor.sel_key') != keys[choice]:
                raise RuntimeError('practice CPU popup mode and selection disagree')
            return choice

        def cycle_to(state, predicate):
            for _ in range(mode_count):
                choice = popup_key(state)
                old = state.values.get('editor.sel_key')
                if predicate(old):
                    return state
                state = changing(state, 'main-menu-down')
                if (state.values.get('editor') != editor or state.values.get('editor.sel_key') == old
                        or popup_key(state) != (choice + 1) % mode_count):
                    raise RuntimeError('practice CPU mode did not advance by one')
            if predicate(state.values.get('editor.sel_key')):
                return state
            raise RuntimeError('practice CPU requested mode was not found before wrapping')

        if current_level != level:
            menu = cycle_to(menu, lambda key: isinstance(key, str) and key.startswith('CPU '))
            for _ in range(9):
                key = menu.values.get('editor.sel_key')
                if key == f'CPU {level}':
                    break
                menu = changing(menu, 'tap-x1')
                current_level = current_level % 9 + 1
                if menu.values.get('editor') != editor or popup_key(menu) != 0:
                    raise RuntimeError('practice CPU level did not advance by one')
            else:
                raise RuntimeError('practice CPU level not reached')
        menu = cycle_to(menu, lambda key: key == mode)
        menu = _tr_input(io, menu, 'tap-b1', until)
        if _tr_depth(menu) != depth or menu.values.get('editor') != editor or menu.values.get('editor.sel_key'):
            raise RuntimeError('practice CPU popup did not close')
        menu = _tr_input(io, menu, 'tap-b1', until)
        if _tr_depth(menu) != original_depth:
            raise RuntimeError('practice CPU roster did not close')
    closed, _ = _close_menu(io, until, after=menu)
    line = _wait_writer(ctx, writer, until) if writer is not None else None
    if time.monotonic() >= until:
        raise RuntimeError('Training Mod setting deadline expired before read-back')
    if line is None:
        _, _, after_bytes = _current_settings_observation(ctx, max(.001, until - time.monotonic()))
    else:
        after_bytes = _fetch_settings(ctx, cap=max(.001, until - time.monotonic()))
    after_menu = _settings_menu(after_bytes)
    current = _tr_current(io, until)
    native_picker.same_epoch(closed, current, in_flight=True)
    if current.values.get('open') != '0':
        raise RuntimeError('Training Mod menu reopened during settings read-back')
    for key, target in wanted.items():
        current_value = _settings_row(after_menu, key)
        if current_value != target:
            raise RuntimeError(f'settings read-back mismatch for menu.{key}: expected {target!r}, '
                               f'saw {current_value!r}')
    after = {key: after_menu[key] for key in wanted} if kind == 'practice_cpu' else after_menu[row_id]
    verified = {'row_id': row_id, 'before': before, 'after': after,
                'settings_sha256': hashlib.sha256(after_bytes).hexdigest(), 'writer': line}
    if kind == 'practice_cpu':
        verified['mode'] = value['mode']
        verified['mode_stored'] = wanted[row_id]
        verified['mode_witness'] = 'stored encoding' if already else 'editor.sel_key'
    else:
        verified['value_stored'] = value
        verified['width'] = width
    elapsed = round((time.monotonic() - started) * 1000)
    return {
        'block': 'tr_set', 'args': {'page': page, 'path': path, 'kind': kind,
                                    'value': value if kind == 'practice_cpu' else original_value, 'cap': cap},
        'verified': verified, 'before_source': before_source, **metrics, 'ms': {'setting': elapsed, 'total': elapsed},
        'skipped': ['change', 'writer'] if already else [],
    }


def _edge_wizard(menu):
    header = menu.values.get('notice.header')
    if not isinstance(header, str):
        raise RuntimeError(f'expected practice wizard notice header; saw {header!r}')
    match = re.fullmatch(r'Edge · (\d+)/(\d+)', header)
    return bool(match and 1 <= int(match[1]) <= int(match[2]))


def _wizard_body(menu, game):
    header = menu.values.get('notice.header')
    body = menu.values.get('notice.body')
    if (menu.values.get('open') != '0' or menu.values.get('game') != game
            or menu.values.get('game.running') != '1' or not _edge_wizard(menu)):
        raise RuntimeError(f'expected {game} running wizard; saw game={menu.values.get("game")!r} '
                           f'running={menu.values.get("game.running")!r} header={header!r} body={body!r}')
    if isinstance(body, str):
        pair = r'\((-?\d+),\s*(-?\d+)\)'
        preview = re.search(rf'CPU\s*{pair}.*?(\d+)%.*?P1\s*{pair}', body)
        if preview:
            return {'header': header, 'body': body, 'kind': 'Preview',
                    'cpu': list(preview.group(1, 2)), 'percent': int(preview.group(3)),
                    'p1': list(preview.group(4, 5))}
        cpu = re.match(rf'CPU\s*{pair}', body)
        if cpu and not re.search(r'P1\s*\(', body):
            return {'header': header, 'body': body, 'kind': 'CPU', 'cpu': list(cpu.groups())}
        p1 = re.match(rf'P1\s*{pair}', body)
        if p1:
            return {'header': header, 'body': body, 'kind': 'P1', 'p1': list(p1.groups())}
        area = re.match(rf'(Lower left|Lower right|Upper left|Upper right)\s*{pair}', body)
        if area:
            return {'header': header, 'body': body, 'kind': 'Area', 'area': area.group(1)}
    raise RuntimeError(f'expected an Area, CPU, P1 or Preview page; saw header={header!r} body={body!r}')


def _wizard_owner(io, menu, until):
    state = _remember(io, io.read(native_picker.UI, max(.001, until - time.monotonic())))
    native_picker.same_epoch(menu, state, in_flight=True)
    return state.values.get('input.owner', 'unknown')


def _wizard_next(io, menu, game, until):
    before = _wizard_body(menu, game)
    match = re.fullmatch(r'Edge · (\d+)/(\d+)', before['header'])
    wanted = f'Edge · {int(match[1]) + 1}/{match[2]}'
    if time.monotonic() >= until:
        raise RuntimeError(f'wizard deadline before advancing: {before}')

    def changed(state):
        page = _wizard_body(state, game)
        if page['header'] not in (before['header'], wanted):
            raise RuntimeError(f'expected wizard header {wanted}; saw {page["header"]!r} body={page["body"]!r}')
        return page['header'] == wanted

    after = _send_with_retry(io, 'tap-a1', until,
                             lambda deadline: _try_tr_condition(io, menu, deadline, changed))
    if after is None:
        raise RuntimeError(f'expected wizard header {wanted}; saw {before["header"]!r} body={before["body"]!r}')
    return after


def _wizard_pulse(io, menu, game, macro, until, pulse_cap):
    page = _wizard_body(menu, game)
    io.send(macro)
    deadline = min(until, time.monotonic() + pulse_cap)

    def read(cap):
        try:
            return io.read('tr.menu', cap)
        except RuntimeError as error:
            if str(error) != 'no fenced bridge reply for state tr.menu':
                raise
            io.finish()
            return None

    while (remaining := deadline - time.monotonic()) > 0:
        state = read(remaining)
        if state is None:
            return menu, False
        state = _remember(io, state)
        if state.block['schema'] != 'tr.menu/1' or state.block['rev'] <= 0 or state.block['filtered']:
            raise RuntimeError(f'expected complete wizard tr.menu/1 reply; saw {state.block!r}')
        native_picker.same_epoch(menu, state, in_flight=True)
        current = _wizard_body(state, game)
        if current['header'] != page['header'] or current['kind'] != page['kind']:
            raise RuntimeError(f'expected wizard page {page["header"]}; saw {current!r}')
        if state.block['rev'] > menu.block['rev']:
            io.finish()
            return state, True
        time.sleep(min(.01, max(0, deadline - time.monotonic())))
    if time.monotonic() >= until:
        raise RuntimeError(f'wizard deadline after pulse; last shown {page["body"]!r}')
    state = read(min(pulse_cap, until - time.monotonic()))
    if state is None:
        return menu, False
    state = _remember(io, state)
    if state.block['schema'] != 'tr.menu/1' or state.block['rev'] <= 0 or state.block['filtered']:
        raise RuntimeError(f'expected complete wizard tr.menu/1 reply; saw {state.block!r}')
    native_picker.same_epoch(menu, state, in_flight=True)
    current = _wizard_body(state, game)
    if current['header'] != page['header'] or current['kind'] != page['kind']:
        raise RuntimeError(f'expected wizard page {page["header"]}; saw {current!r}')
    if state.block['rev'] == menu.block['rev'] and current['body'] == page['body']:
        io.finish()
        return state, False
    if state.block['rev'] > menu.block['rev']:
        io.finish()
        return state, True
    raise RuntimeError(f'expected unchanged or newer wizard notice; saw {current!r}')


def _wizard_move(io, menu, game, label, target, until, pulse_cap):
    pulses = []
    for _ in range(60):
        shown = _wizard_body(menu, game)[label.lower()]
        if shown == target:
            return menu, shown, pulses
        axis = 0 if shown[0] != target[0] else 1
        distance = int(target[axis]) - int(shown[axis])
        if distance == 0:
            distance = -1 if target[axis] == '-0' else 1
        size = 'coarse' if abs(distance) >= 24 else 'medium' if abs(distance) >= 3 else 'fine'
        direction = (('left' if distance < 0 else 'right') if axis == 0
                     else ('down' if distance < 0 else 'up'))
        macro = f'wizard-{direction}-{size}'
        for _ in range(3):
            try:
                menu, published = _wizard_pulse(io, menu, game, macro, until, pulse_cap)
            except RuntimeError as error:
                io.finish()
                raise RuntimeError(f'wizard {label} unreachable: last {shown}, target {target}, '
                                   f'pulses {pulses + [macro]}: {error}') from error
            current = _wizard_body(menu, game)[label.lower()]
            published = bool(published and current != shown)
            pulses.append({'axis': 'x' if axis == 0 else 'y', 'macro': macro, 'published': published})
            if published:
                break
        else:
            io.finish()
            raise RuntimeError(f"wizard {label} unreachable: expected {target}, shown {current} "
                               f"after 3 unpublished '{macro}' pulses")
    shown = _wizard_body(menu, game)[label.lower()]
    raise RuntimeError(f'wizard {label} unreachable after 60 pulses: last {shown}, target {target}, pulses {pulses}')


def wizard_place(ctx, game, area=None, cpu=None, p1=None, cap=120, pulse_cap=2.0):
    args = {'game': game, 'area': area, 'cpu': cpu, 'p1': p1, 'cap': cap,
            'pulse_cap': pulse_cap}

    def action(metrics):
        if game != 'Guard the Edge':
            raise RuntimeError(f'unsupported wizard game {game!r}')
        if area is not None and area not in ('Lower left', 'Lower right', 'Upper left', 'Upper right'):
            raise RuntimeError(f'unknown wizard area {area!r}')
        targets = []
        for label, point in (('cpu', cpu), ('p1', p1)):
            if point is None:
                targets.append(None)
            elif isinstance(point, list) and len(point) == 2:
                targets.append([_shown_coordinate(value) for value in point])
            else:
                raise RuntimeError(f'wizard {label} requires two shown coordinates')
        if any(type(value) not in (int, float) or not 0 < value < float('inf') for value in (cap, pulse_cap)):
            raise RuntimeError('wizard caps must be positive')
        return _wizard_place(ctx, args, targets, metrics)

    return _block(ctx, 'wizard_place', 'routes-wizard-place', args, action)


def _wizard_place(ctx, args, targets, metrics):
    started = time.monotonic()
    game, area = args['game'], args['area']
    until = started + args['cap']
    io = BridgePickerIO(ctx)
    menu = _tr_current(io, until)
    skipped = []
    if not _edge_wizard(menu):
        menu, skipped = _open_menu(io, until, 'fighters_stages', after=menu)
        menu = _navigate_menu(io, menu, 'fighters_stages',
                              _tr_path(['game.6', {'action': 'row', 'row': 'Set up…'}]), until)
        menu = _tr_input(io, menu, 'tap-a1', until, opened=False)
        if not _edge_wizard(menu):
            menu = _wait_tr_condition(io, menu, until,
                                      lambda state: state.values.get('open') == '0'
                                      and _edge_wizard(state), 'wizard header')
    owners = {}
    pages = []
    area_body = None
    moves = {'cpu': [], 'p1': []}
    area_seen = False
    while True:
        page = _wizard_body(menu, game)
        pages.append({'header': page['header'], 'kind': page['kind']})
        owners[page['header']] = _wizard_owner(io, menu, until)
        if area is not None and not area_seen and page['kind'] != 'Area':
            raise RuntimeError(f'expected an Area page to choose {area}; saw header={page["header"]!r} body={page["body"]!r}')
        if page['kind'] == 'Area':
            area_seen = True
            for _ in range(4):
                page = _wizard_body(menu, game)
                area_body = page['body']
                if area is None or page['area'] == area:
                    break
                before = menu

                def changed(state):
                    current = _wizard_body(state, game)
                    if current['header'] != page['header']:
                        raise RuntimeError(f'expected wizard header {page["header"]}; saw {current["header"]!r}')
                    return current['body'] != page['body']

                menu = _send_with_retry(io, 'main-menu-right', until,
                                        lambda deadline: _try_tr_condition(io, before, deadline, changed))
                if menu is None:
                    raise RuntimeError(f'expected wizard area change; saw {page["body"]!r}')
            else:
                raise RuntimeError(f'expected wizard area {area!r}; saw {page["body"]!r}')
        elif page['kind'] in ('CPU', 'P1'):
            index = 0 if page['kind'] == 'CPU' else 1
            if targets[index] is not None:
                menu, _, moves[page['kind'].lower()] = _wizard_move(
                    io, menu, game, page['kind'], targets[index], until, args['pulse_cap'])
        else:
            break
        menu = _wizard_next(io, menu, game, until)
    cpu_shown, p1_shown = page['cpu'], page['p1']
    if (targets[0] is not None and cpu_shown != targets[0]
            or targets[1] is not None and p1_shown != targets[1]):
        raise RuntimeError(f'expected Preview coordinates {targets!r}; saw {page["body"]!r}, pulses {moves!r}')
    elapsed = round((time.monotonic() - started) * 1000)
    return {
        'block': 'wizard_place', 'args': args,
        'verified': {'area_body': area_body, 'cpu_shown': cpu_shown, 'p1_shown': p1_shown,
                     'percent': page['percent'], 'preview_body': page['body'], 'pulses': moves,
                     'pages': pages, 'owners': owners,
                     'scope': 'published coordinates only; input readiness is not certified',
                     'limitations': [f'{header}: input.owner=native.pause observed while this page was published'
                                     for header, owner in owners.items() if owner == 'native.pause'],
                     'unconfirmed_pulses': sum(not pulse['published'] for group in moves.values() for pulse in group),
                     'note': 'an unpublished pulse may have moved the point by less than a shown unit; '
                             "only the shown coordinates are verified here; exact position words are checked by "
                             "drill_start's first release"},
        **metrics, 'ms': {'wizard': elapsed, 'total': elapsed}, 'skipped': skipped,
    }


def _try_tr_condition(io, before, until, predicate):
    latest = before
    while (remaining := until - time.monotonic()) > 0:
        state = _remember(io, io.read('tr.menu', remaining))
        if state.block['schema'] != 'tr.menu/1' or state.block['rev'] <= 0 or state.block['filtered']:
            raise RuntimeError(f'expected complete practice tr.menu/1 reply; saw {state.block!r}')
        native_picker.same_epoch(latest, state, in_flight=True)
        if state.block['rev'] <= latest.block['rev']:
            continue
        latest = state
        if state.values.get('scene') != 'training' or state.values.get('keyboard') != '0':
            raise RuntimeError(f'expected Training scene without keyboard; saw {state.values!r}')
        if predicate(state):
            return state
    return None


def _wait_tr_condition(io, before, until, predicate, label):
    state = _try_tr_condition(io, before, until, predicate)
    if state is not None:
        return state
    metrics = getattr(io.ctx.runner, 'setup_metrics', None) or {}
    raise RuntimeError(f'expected practice {label}; saw {metrics.get("last_seen", {})!r}')


def drill_stop(ctx, game, cap=30):
    args = {'game': game, 'cap': cap}

    def action(metrics):
        if game != 'Guard the Edge':
            raise RuntimeError(f'unsupported practice game {game!r}')
        if type(cap) not in (int, float) or not 0 < cap < float('inf'):
            raise RuntimeError('practice stop needs a positive cap')
        return _drill_stop(ctx, game, cap, metrics)

    return _block(ctx, 'drill_stop', 'routes-drill-stop', args, action)


def _drill_stop(ctx, game, cap, metrics):
    started = time.monotonic()
    until = started + cap
    io = BridgePickerIO(ctx)
    initial = _tr_current(io, until)
    if _edge_wizard(initial):
        raise RuntimeError('practice wizard is up; cannot stop it through the menu')
    if initial.values.get('game.running') != '1':
        raise RuntimeError('practice game is not running')
    if initial.values.get('game') != game:
        raise RuntimeError('running practice game does not match the requested game')
    menu, skipped = _open_menu(io, until, 'fighters_stages', after=initial)
    menu = _navigate_menu(io, menu, 'fighters_stages', _tr_path(['game.6', 'start.6']), until)
    if (_edge_wizard(menu) or menu.values.get('game') != game
            or menu.values.get('game.running') != '1'):
        raise RuntimeError('practice game or wizard state changed before stop')
    menu = _tr_input(io, menu, 'tap-a1', until)
    if menu.values.get('game.running') != '0':
        menu = _wait_tr_condition(io, menu, until,
                                  lambda state: state.values.get('open') == '1'
                                  and state.values.get('game.running') == '0', 'running=0')
    _, close_skipped = _close_menu(io, until, after=menu)
    elapsed = round((time.monotonic() - started) * 1000)
    return {
        'block': 'drill_stop', 'args': {'game': game, 'cap': cap},
        'verified': {'game': game, 'running': '0'},
        **metrics, 'ms': {'stop': elapsed, 'total': elapsed}, 'skipped': skipped + close_skipped,
    }


def drill_observe(ctx, attempts, cap, game='Guard the Edge', perfect=None, allow_stopped=False, count='terminals'):
    args = {'attempts': attempts, 'cap': cap, 'game': game, 'perfect': perfect,
            'allow_stopped': allow_stopped, 'count': count}

    def action(metrics):
        if type(attempts) is not int or attempts <= 0 or type(cap) not in (int, float) or not 0 < cap < float('inf'):
            raise RuntimeError('drill observation needs positive attempts and cap')
        if game not in ('Guard the Edge', 'Juggler'):
            raise RuntimeError(f'unsupported practice game {game!r}')
        if perfect is not None and type(perfect) is not bool or type(allow_stopped) is not bool:
            raise RuntimeError('perfect and allow_stopped must be booleans')
        if count not in ('terminals', 'releases'):
            raise RuntimeError('drill observation count must be terminals or releases')
        return _drill_observe(ctx, args, metrics)

    return _block(ctx, 'drill_observe', 'routes-drill-observe', args, action)


def _trace_snapshot(ctx, path, needs_plan=False, *, expected=None):
    status = ctx.call('trace.list', {})
    if (status.get('output_error') or status.get('boot_error') or status.get('output') != path
            or 'file_lost' not in status):
        actual = {key: status.get(key) for key in ('output', 'output_error', 'boot_error', 'file_lost')}
        raise RuntimeError(f'expected available attempt trace output {path!r}; saw {actual!r}')
    try:
        points = status['points']
        sequence = sum(trace_attempts._decimal(point['recorded']) for point in points)
        loss = trace_attempts._decimal(status['file_lost'])

        def selected(symbol):
            point_id = trace_attempts.point_id(status, symbol)
            point = next(item for item in points if str(item.get('id')) == point_id)
            identity = (point.get('process_id'), point.get('address'))
            if int(identity[0]) <= 0 or int(identity[1], 0) <= 0:
                raise ValueError('invalid identity')
            counters = {key: trace_attempts._decimal(point[key]) for key in ('hits', 'matched', 'recorded', 'dropped')}
            return {'id': point_id, 'identity': identity, 'counters': counters}

        attempt = selected('edge_juggle_attempt')
        planner = selected('planner_chose') if needs_plan else None
        if planner and planner['identity'][0] != attempt['identity'][0]:
            raise RuntimeError('planner and attempt trace processes differ')
        snapshot = {'sequence': sequence, 'file_lost': loss, 'attempt': attempt, 'planner': planner}
        if expected is not None:
            _same_trace_points(expected, snapshot)
        return snapshot
    except (KeyError, TypeError, ValueError, StopIteration) as error:
        raise RuntimeError(f'attempt trace counter or point identity is unavailable: {error}') from error


def _trace_delta(start, end, *, require_hits=True):
    if start['file_lost'] != end['file_lost']:
        raise RuntimeError(f'expected unchanged trace loss {start["file_lost"]}; saw {end["file_lost"]}')
    result = {}
    for name in ('attempt', 'planner'):
        before, after = start[name], end[name]
        if before is None:
            continue
        if after is None or (before['id'], before['identity']) != (after['id'], after['identity']):
            raise RuntimeError(f'expected unchanged {name} trace point; saw {after!r}')
        a, b = before['counters'], after['counters']
        if b['dropped'] != a['dropped']:
            raise RuntimeError(f'expected unchanged {name} trace loss {a["dropped"]}; saw {b["dropped"]}')
        delta = {key: b[key] - a[key] for key in a}
        if any(value < 0 for value in delta.values()):
            raise RuntimeError(f'expected monotonic {name} trace counters; saw {a!r} then {b!r}')
        result[name] = delta
    attempt = result['attempt']
    if require_hits and attempt['hits'] != attempt['recorded']:
        raise RuntimeError(f'expected every attempt hit recorded; saw hits +{attempt["hits"]}, '
                           f'recorded +{attempt["recorded"]}')
    return result


def _same_trace_points(start, end):
    for name in ('attempt', 'planner'):
        before, after = start[name], end[name]
        if before is not None and (after is None or
                                   (before['id'], before['identity']) != (after['id'], after['identity'])):
            raise RuntimeError(f'expected unchanged {name} trace point; saw {after!r}')


def _shown_coordinate(value):
    if isinstance(value, str) and re.fullmatch(r'-?\d+', value):
        return value
    if type(value) is int:
        return str(value)
    if type(value) is float and math.isfinite(value):
        return format(value, '.0f')
    raise RuntimeError(f'invalid shown coordinate {value!r}')


def _start_expect(expect):
    if expect is None:
        return None
    keys = {'cpu_kind', 'stage_form', 'stage_id', 'perfect', 'juggler', 'damage',
            'cpu', 'cpu_words', 'p1', 'p1_words', 'plan'}
    if not isinstance(expect, dict) or not set(expect) <= keys:
        raise RuntimeError('invalid first-release expect keys')
    for key, value in expect.items():
        if key in ('perfect', 'juggler'):
            valid = type(value) is bool
        elif key in ('cpu_kind', 'stage_form', 'stage_id'):
            valid = type(value) is int and value >= 0
        elif key == 'damage':
            valid = type(value) is int or type(value) is float and math.isfinite(value)
        elif key in ('cpu', 'p1'):
            valid = isinstance(value, list) and len(value) == 2
            if valid:
                for coordinate in value:
                    _shown_coordinate(coordinate)
        elif key in ('cpu_words', 'p1_words'):
            valid = (isinstance(value, list) and len(value) == 2
                     and all(isinstance(word, str) and re.fullmatch(r'0x[0-9a-f]{8}', word) for word in value))
        else:
            valid = (value in ('none', 'any') or (isinstance(value, list) and len(value) == 2
                                                  and all(type(item) is int and item >= 0 for item in value)))
        if not valid:
            raise RuntimeError(f'invalid first-release expect {key}')
    return expect


def drill_start(ctx, game, expect=None, cap=60):
    args = {'game': game, 'expect': expect, 'cap': cap}

    def action(metrics):
        if game != 'Guard the Edge':
            raise RuntimeError(f'unsupported practice game {game!r}')
        if type(cap) not in (int, float) or not 0 < cap < float('inf'):
            raise RuntimeError('practice start needs a positive cap')
        _start_expect(expect)
        return _drill_start(ctx, game, expect, cap, metrics)

    return _block(ctx, 'drill_start', 'routes-drill-start', args, action)


def _drill_start(ctx, game, expect, cap, metrics):
    started = time.monotonic()
    until = started + cap
    path = ctx.instance.get('trace_jsonl')
    if path is not None and (not isinstance(path, str) or not path):
        raise RuntimeError('practice trace path is invalid')
    if path is None and expect is not None:
        raise RuntimeError('first-release expect requires a trace in this boot')
    stream = fence = None
    needs_plan = expect is not None and 'plan' in expect
    observed = {'markers_before_start': [], 'markers_before_release': []}
    metrics['observed'] = observed
    if path is not None:
        stream = trace_attempts.Stream(path, '')
        preflight = _trace_snapshot(ctx, path, needs_plan)
        stream.point = preflight['attempt']['id']
        if needs_plan:
            stream.planner = preflight['planner']['id']
    io = BridgePickerIO(ctx)
    initial = _tr_current(io, until)
    wizard = _edge_wizard(initial)
    skipped = []

    def running(state):
        return (state.values.get('open') == '0' and state.values.get('game') == game
                and state.values.get('game.running') == '1'
                and (not wizard or not _edge_wizard(state)))

    def fail_running(values):
        observed.update({'game': values.get('game'), 'running': values.get('game.running'),
                         'header': values.get('notice.header')})
        raise RuntimeError(f'expected {game} running after start; saw game={values.get("game")!r} '
                           f'running={values.get("game.running")!r} header={values.get("notice.header")!r}')

    if wizard:
        page = _wizard_body(initial, game)
        if page['kind'] != 'Preview':
            raise RuntimeError(f'expected Preview; saw header={page["header"]!r} body={page["body"]!r}')
        if time.monotonic() >= until:
            raise RuntimeError('practice start deadline expired before wizard input')
        if stream is not None:
            fence = _trace_snapshot(ctx, path, needs_plan, expected=preflight)
        menu = _send_with_retry(io, 'tap-a1', until,
                                lambda deadline: _try_tr_condition(
                                    io, initial, deadline,
                                    lambda state: state.values.get('open') == '0' and not _edge_wizard(state)))
        if menu is None:
            raise RuntimeError(f'expected wizard Preview dismissal; saw header={page["header"]!r} body={page["body"]!r}')
    else:
        if initial.values.get('game.running') == '1':
            raise RuntimeError('a practice game is already running; call drill_stop')
        if initial.values.get('game.running') != '0':
            raise RuntimeError(f'expected practice running=0 before start; saw {initial.values.get("game.running")!r}')
        menu, skipped = _open_menu(io, until, 'fighters_stages', after=initial)
        menu = _navigate_menu(io, menu, 'fighters_stages', _tr_path(['game.6', 'start.6']), until)
        if _edge_wizard(menu) or menu.values.get('game.running') != '0':
            raise RuntimeError(f'expected stopped practice without wizard before start; saw {menu.values!r}')
        if stream is not None:
            fence = _trace_snapshot(ctx, path, needs_plan, expected=preflight)
        menu = _tr_input(io, menu, 'tap-a1', until, opened=False)
    if not running(menu):
        changed = _try_tr_condition(io, menu, until, running)
        if changed is None:
            fail_running(metrics['last_seen'])
        menu = changed
    release = None
    selected_plan = None
    terminal = None
    if stream is not None:
        observed['fence'] = {'sequence': fence['sequence'], 'taken': 'immediately before the start input',
                             'state': 'wizard Preview' if wizard else 'not running'}
        while time.monotonic() < until:
            ctx.check_stop()
            for mark in stream.read(until):
                if mark['process'] != str(fence['attempt']['identity'][0]):
                    raise RuntimeError('practice trace record came from a different process')
                if mark['sequence'] <= fence['sequence']:
                    keys = ('sequence', 'frame', 'event', 'terminal') if mark['record'] == 'attempt' else (
                        'sequence', 'frame', 'record')
                    observed['markers_before_start'].append({key: mark[key] for key in keys})
                    continue
                if mark['record'] == 'plan':
                    if release is not None and selected_plan is None:
                        selected_plan = mark
                        observed['plan'] = mark
                elif release is None:
                    if mark['event'] == 0:
                        release = mark
                        observed['first_release'] = mark
                        wanted = game == 'Juggler'
                        if release['juggler'] != wanted:
                            raise RuntimeError(f'release sequence {release["sequence"]}: expected juggler={wanted}; '
                                               f'saw {release["juggler"]}')
                    else:
                        observed['markers_before_release'].append(
                            {key: mark[key] for key in ('sequence', 'frame', 'event', 'terminal')})
                elif mark['event'] == 0:
                    raise RuntimeError('next release arrived before first-attempt terminal')
                else:
                    terminal = mark
                    observed['plan_terminal'] = terminal
                    for field in ('ordinal', 'juggler', 'perfect'):
                        if terminal[field] != release[field]:
                            raise RuntimeError(f'terminal sequence {terminal["sequence"]}: expected terminal '
                                               f'{field} {release[field]}; saw {terminal[field]}')
                    break
                if release is not None and not needs_plan:
                    break
            if release is not None and (not needs_plan or terminal is not None):
                break
            time.sleep(min(.05, max(0, until - time.monotonic())))
        if release is None or needs_plan and terminal is None:
            raise RuntimeError('first release or first-attempt terminal not observed before deadline')
        end = _trace_snapshot(ctx, path, needs_plan)
        if stream.sequence is not None and stream.sequence > end['sequence']:
            raise RuntimeError(f'trace counters do not account for file sequences: expected <= {end["sequence"]}; '
                               f'saw {stream.sequence}')
        delta = _trace_delta(fence, end)
        if time.monotonic() >= until:
            raise RuntimeError('practice start deadline expired during trace fence')
    plan_value = selected_plan if selected_plan is not None else 'none'
    if needs_plan and selected_plan is None and delta['planner']['hits']:
        planner = delta['planner']
        plan_value = f'not observed (planner hit {planner["hits"]} times, recorded {planner["recorded"]})'
    final = _tr_current(io, until)
    native_picker.same_epoch(menu, final, in_flight=True)
    menu = final
    if not running(menu):
        fail_running(menu.values)
    verified = {'game': menu.values.get('game'), 'running': menu.values.get('game.running'),
                'first_release': release if release is not None else 'not checked: no trace in this boot',
                'markers_before_start': observed['markers_before_start'],
                'markers_before_release': observed['markers_before_release']}
    if needs_plan:
        verified.update({'plan': plan_value, 'plan_terminal': terminal, 'planner': delta['planner']})
    if fence:
        verified['fence'] = observed['fence']
        verified['trace_loss_start'] = {'file_lost': fence['file_lost'],
                                        'point_dropped': fence['attempt']['counters']['dropped']}
        verified['trace_loss_end'] = {'file_lost': end['file_lost'],
                                      'point_dropped': end['attempt']['counters']['dropped']}
        if needs_plan:
            verified['trace_loss_start']['planner_dropped'] = fence['planner']['counters']['dropped']
            verified['trace_loss_end']['planner_dropped'] = end['planner']['counters']['dropped']
    observed.update(verified)
    elapsed = round((time.monotonic() - started) * 1000)
    receipt = {'block': 'drill_start', 'args': {'game': game, 'expect': expect, 'cap': cap},
               'verified': verified, **metrics, 'ms': {'start': elapsed, 'total': elapsed}, 'skipped': skipped}
    if expect is not None:
        for key, wanted in expect.items():
            if key == 'plan':
                if wanted == 'any':
                    continue
                actual = ([selected_plan['route'], selected_plan['row']] if selected_plan is not None else plan_value)
            elif key in ('cpu', 'p1'):
                actual = [_shown_coordinate(value) for value in release[key]]
                wanted = [_shown_coordinate(value) for value in wanted]
            else:
                actual = release['stage' if key == 'stage_id' else key]
            if actual != wanted:
                raise RuntimeError(f'first-release mismatch for {key}: expected {wanted!r}; saw {actual!r}')
    return receipt


def _drill_observe(ctx, args, metrics):
    started = time.monotonic()
    until = started + args['cap']
    path = ctx.instance.get('trace_jsonl')
    if not isinstance(path, str) or not path:
        raise RuntimeError('drill observation needs a trace_jsonl')
    stream = trace_attempts.Stream(path, '')
    trace_start = _trace_snapshot(ctx, path)
    stream.point = trace_start['attempt']['id']
    _trace_delta(trace_start, _trace_snapshot(ctx, path), require_hits=False)
    io = BridgePickerIO(ctx)
    baseline = _tr_current(io, until)
    if baseline.values.get('game') != args['game']:
        raise RuntimeError(f'expected game {args["game"]}; saw {baseline.values.get("game")!r}')
    marks = []
    counts = {}
    kinds = set()
    expected_perfect = args['perfect']
    before_observation = []
    observed = {'game': baseline.values.get('game'), 'running': baseline.values.get('game.running'),
                'markers': marks, 'markers_before_observation': before_observation,
                'fence': {'sequence': trace_start['sequence'], 'taken': 'at the start of the observation'}}
    metrics['observed'] = observed

    def collect():
        nonlocal expected_perfect
        for mark in stream.read(until):
            if mark['sequence'] <= trace_start['sequence']:
                before_observation.append({key: mark[key] for key in ('sequence', 'frame', 'event', 'terminal')})
                continue
            if mark['process'] != str(trace_start['attempt']['identity'][0]):
                raise RuntimeError('attempt marker came from a different trace process')
            marks.append(mark)
            expected_juggler = args['game'] == 'Juggler'
            if mark['juggler'] != expected_juggler:
                raise RuntimeError(f'marker sequence {mark["sequence"]}: expected juggler={expected_juggler}; '
                                   f'saw {mark["juggler"]}')
            if expected_perfect is None:
                expected_perfect = mark['perfect']
            elif mark['perfect'] != expected_perfect:
                raise RuntimeError(f'marker sequence {mark["sequence"]}: expected perfect={expected_perfect}; '
                                   f'saw {mark["perfect"]}')
            kinds.add(mark['cpu_kind'])
            terminal = mark['terminal']
            if terminal:
                counts[terminal] = counts.get(terminal, 0) + 1

    def reached():
        return (sum(mark['event'] == 0 for mark in marks) if args['count'] == 'releases' else sum(counts.values()))

    while time.monotonic() < until:
        ctx.check_stop()
        collect()
        if reached() >= args['attempts']:
            break
        time.sleep(min(.05, max(0, until - time.monotonic())))
    if reached() < args['attempts']:
        note = ' (a Zoom arm emits no attempt markers)' if not marks else ''
        raise RuntimeError(f'{reached()} {args["count"]} observed in {args["cap"]} s; '
                           f'wanted {args["attempts"]}{note}')
    final = _tr_current(io, until)
    native_picker.same_epoch(baseline, final, in_flight=True)
    running = final.values.get('game.running')
    observed.update({'game': final.values.get('game'), 'running': running})
    if final.values.get('game') != args['game']:
        raise RuntimeError(f'expected game {args["game"]}; saw {final.values.get("game")!r}')
    if running not in ('0', '1'):
        raise RuntimeError(f'expected drill running state 0 or 1; saw {running!r}')
    collect()
    trace_end = _trace_snapshot(ctx, path)
    if stream.sequence is not None and stream.sequence > trace_end['sequence']:
        raise RuntimeError(f'trace counters do not account for file sequences: expected <= {trace_end["sequence"]}; '
                           f'saw {stream.sequence}')
    _trace_delta(trace_start, trace_end)
    if time.monotonic() >= until:
        raise RuntimeError(f'{reached()} {args["count"]} observed but drill observation deadline expired')
    if running == '0' and not args['allow_stopped']:
        raise RuntimeError('expected running drill; saw stopped')
    elapsed = round((time.monotonic() - started) * 1000)
    return {
        'block': 'drill_observe', 'args': args,
        'verified': {'game': final.values.get('game'), 'running': running,
                     'releases': sum(mark['event'] == 0 for mark in marks), 'terminals': counts,
                     'cpu_kinds': sorted(kinds), 'perfect': expected_perfect,
                     'fence': observed['fence'], 'markers_before_observation': before_observation,
                     'trace_loss_start': {'file_lost': trace_start['file_lost'],
                                          'point_dropped': trace_start['attempt']['counters']['dropped']},
                     'trace_loss_end': {'file_lost': trace_end['file_lost'],
                                        'point_dropped': trace_end['attempt']['counters']['dropped']},
                     'first_sequence': marks[0]['sequence'], 'last_sequence': marks[-1]['sequence'],
                     'first_frame': marks[0]['frame'], 'last_frame': marks[-1]['frame']},
        **metrics, 'ms': {'observe': elapsed, 'total': elapsed}, 'skipped': [],
    }


def _set_fighter(ctx, fighter, seat, cap, metrics):
    started = time.monotonic()
    entry = fighter_entry(fighter)
    io = BridgePickerIO(ctx)
    until = time.monotonic() + cap
    ui = native_picker.wait_sample(io, native_picker.UI, until)
    if not _fields_match(ui, {'screen.context': 'training', 'dialog': 'none'}):
        raise RuntimeError('fighter setup needs the Training context without a modal dialog')
    if _fields_match(ui, TRAINING_CSS):
        if seat == 1:
            raise RuntimeError('set_cpu works in Training: the CPU is set through the Training Rooms menu; '
                               'call reach_training first')
        nested = pick_local_fighter(ctx, fighter, topology=library_topology('css-training'),
                                    take_back=True, expected_context='training')
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
    phase = time.monotonic()
    fighter_body = _live_body(ctx, seat, entry['kind'], max(.01, until - time.monotonic()))
    timings['fighter'] = round((time.monotonic() - phase) * 1000)
    remember(ctx, **{f'{"cpu" if seat else "p1"}_kind': entry['kind'],
                     'cpu' if seat else 'p1': entry['name']})
    return _save(ctx, f'routes-set-{"cpu" if seat else "player"}', {
        'block': 'set_cpu' if seat else 'set_player', 'args': {'fighter': fighter},
        'verified': {'kind': entry['kind'], 'roster': roster, 'one_hot': picked, 'fighter': fighter_body},
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
    original = _current_settings(ctx, remaining)
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
    sent = json.dumps(changed).encode()
    reply, reload = _apply_settings(ctx, sent, cap=remaining)
    timings['apply'] = round((time.monotonic() - phase) * 1000)
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
        'verified': {'keys': keys, 'values': ({key: changed['menu'][key] for key in keys}
                                               if values is not None else {key: changed[key] for key in keys}),
                     'live_roster': live}, 'set_reply': reply, 'reload': reload,
        'applied': 'live; settings.json is rewritten only by the next menu write',
        'live_fighter': ('checked' if verify_roster else 'not checked') if changed_fighters else 'not applicable',
        'sent_sha256': hashlib.sha256(sent).hexdigest(),
        **metrics,
        'ms': {**timings, 'total': round((time.monotonic() - start) * 1000)}, 'skipped': [],
    })


def setup_state(ctx):
    started = time.monotonic()
    io = BridgePickerIO(ctx)
    ui = native_picker.wait_sample(io, native_picker.UI, time.monotonic() + 6)
    screen = ui.values.get('screen', 'unknown')
    p1 = cpu = 'unknown'
    stage = 'unknown'
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
    _, source, settings = _current_settings_observation(ctx, 20)
    return _save(ctx, 'routes-setup-state', {
        'block': 'setup_state', 'args': {}, 'verified': {'screen': screen, 'context': ui.values.get('screen.context'),
                                                         'owner': ui.values.get('input.owner'), 'p1': p1, 'cpu': cpu,
                                                         'stage_id': stage,
                                                         'settings_sha256': hashlib.sha256(settings).hexdigest(),
                                                         'settings_source': source},
        'inputs': 0, 'bridge_requests': getattr(io, 'request_count', 0) + 1 + int(screen == 'training'),
        'remembered': {'stage_id': expected(ctx).get('stage_id')},
        'bridge_boot': ui.block['boot'], 'revisions': getattr(io, 'revisions', {}),
        'ms': {'total': round((time.monotonic() - started) * 1000)}, 'skipped': [],
    })


def _reach_training_screen(ui):
    values = ui.values
    if (values.get('observation.active') != 'yes' or values.get('input.owner') != 'native'
            or values.get('dialog') != 'none'):
        return None
    screen = values.get('screen')
    if screen == 'main_menu':
        return screen
    if screen in ('stage_select', 'css.training', 'training') and values.get('screen.context') == 'training':
        return screen
    return None


def _reach_training_main_checkpoint(ui):
    if _main_top(ui):
        return True
    values = ui.values
    focus = values.get('focus.id')
    return focus in ('classic_mode', 'mob_smash', 'training') and _fields_match(ui, {
        'screen': 'main_menu', 'screen.tab': 'games_and_more', 'screen.layer': 'submenu',
        'input.owner': 'native', 'dialog': 'none', 'focus.source': 'native', 'focus.id': focus,
        'focus': f'main_menu.submenu.{focus}',
    })


def reach_training(ctx, p1, cpu=None, stage='Final Destination', form='normal', cap=240):
    started = time.monotonic()
    until = started + cap
    wanted_stage = stage_entry(stage)['id']
    wanted_p1 = fighter_entry(p1)['kind']
    wanted_cpu = fighter_entry(cpu)['kind'] if cpu is not None else None
    metrics = {'inputs': 0, 'bridge_requests': 0, 'bridge_boot': None, 'revisions': {}}
    previous = getattr(ctx.runner, 'setup_metrics', None)
    ctx.runner.setup_metrics = metrics
    phases = ('to_main', 'back_to_main', 'main_to_stage', 'pick_stage', 'quit_training',
              'pick_fighters', 'enter_training', 'set_cpu')
    skipped, timings, receipts = list(phases), {}, {}
    starts, recovered, errors = {}, [], {}
    stalls, retry_evidence, phase_elapsed = [], {}, {}
    io, ui, last_ui = None, None, None
    known_stage = expected(ctx).get('stage_id')
    css_picked, retry_same = False, None

    def pending():
        entries = getattr(io, 'pending', None) if io is not None else None
        if entries is None:
            entries = getattr(ctx.runner, 'native_ui_pending', {})
        value = entries.get(ctx.on)
        return value[0] if isinstance(value, tuple) else value

    def stop(step, reason):
        receipt = {
            'block': 'reach_training', 'ok': False, 'step': step, 'reason': reason,
            'starts': dict(starts), 'recovered': list(recovered), 'last_ui': last_ui,
            'phases': dict(receipts), 'ms': {**timings, 'total': round((time.monotonic() - started) * 1000)},
            'pending': pending(), 'stalls': list(stalls), 'result': _stall_result(False, stalls),
        }
        _save(ctx, 'routes-reach-training-stop', receipt)
        raise closed_loop.RouteStopped('reach_training', step, reason, receipt)

    def deadline(reason):
        if time.monotonic() >= until:
            stop('deadline', reason)

    def phase_stall(kind, name, evidence):
        event = {'kind': kind, 'step': name, **evidence,
                 'last_bridge_line': _bridge_tail(ctx), 'utc': _stall_utc()}
        _append_stall(ctx, 'reach_training', event)
        stalls.append(event)

    def phase(name):
        nonlocal css_picked, known_stage
        deadline(f'deadline expired before {name}')
        if starts.get(name, 0) >= 3:
            stop(name, f'no transition after 3 starts; last error: {errors.get(name, "none")}')
        if name in retry_evidence:
            phase_stall('phase-retry', name, retry_evidence.pop(name))
        recovered[:] = [item for item in recovered if item != name]
        starts[name] = starts.get(name, 0) + 1
        begin = time.monotonic()
        try:
            if name == 'to_main':
                value = to_main(ctx)
            elif name == 'back_to_main':
                value = back_to_main(ctx)
            elif name == 'main_to_stage':
                value = main_to_stage(ctx)
            elif name == 'pick_stage':
                value = pick_stage(ctx, stage, topology=library_topology('sss-training'), form=form)
            elif name == 'quit_training':
                value = quit_training(ctx, cap=max(.01, until - time.monotonic()))
            elif name == 'pick_fighters':
                value = pick_fighters(ctx, p1, topology=library_topology('css-training'), take_back=True)
            elif name == 'enter_training':
                value = enter_training(ctx, cap=max(.01, until - time.monotonic()),
                                       stage_ids={wanted_stage}, fighters={'p1': p1, 'cpu': None})
            else:
                value = _set_fighter(ctx, cpu, 1, max(.01, until - time.monotonic()), metrics)
        finally:
            phase_elapsed[name] = time.monotonic() - begin
            timings[name] = timings.get(name, 0) + round(phase_elapsed[name] * 1000)
        receipts[name] = value
        if name in skipped:
            skipped.remove(name)
        if name == 'pick_fighters':
            css_picked = True
        elif name == 'pick_stage':
            known_stage = expected(ctx).get('stage_id')

    def fatal(error):
        return isinstance(error, closed_loop.RouteStopped) or any(
            word in str(error).lower() for word in
            ('boot changed', 'connection', 'continuity', 'rotated', 'truncated', 'malformed', 'overlapping'))

    def settle(name, original, before):
        nonlocal ui, last_ui
        settle_until = min(until, time.monotonic() + 30)
        previous, matching, count = ui, None, 0
        if pending() is not None and hasattr(io, 'in_flight'):
            io.in_flight = True
        while True:
            deadline(f'deadline expired; settle after {name} was cut short: {original}')
            if time.monotonic() >= settle_until:
                stop(name, f'{original}; no two consecutive qualified screens within settle cap')
            try:
                sample = native_picker.check_sample(
                    io.read(native_picker.UI, 2.0),
                    native_picker.UI, loading_ui=True)
                deadline(f'deadline expired; settle after {name} was cut short: {original}')
                if time.monotonic() > settle_until:
                    stop(name, f'{original}; no two consecutive qualified screens within settle cap')
                if previous is not None:
                    native_picker.same_epoch(previous, sample, in_flight=True)
                    _, revision, observation = sample.clock
                    _, old_revision, old_observation = previous.clock
                    if revision < old_revision or observation < old_observation:
                        raise RuntimeError('bridge observation continuity changed during settle')
                    if revision == old_revision and sample.values != previous.values:
                        raise RuntimeError('malformed contradictory bridge observation during settle')
                    fresh = revision > old_revision and observation > old_observation
                else:
                    fresh = True
                if fresh:
                    previous = sample
                last_ui = dict(sample.values)
                current = _reach_training_screen(sample) if fresh else None
                if current is None:
                    matching, count = None, 0
                elif current == matching:
                    count += 1
                else:
                    matching, count = current, 1
                if count == 2:
                    deadline(f'deadline expired; settle after {name} was cut short: {original}')
                    ui = sample
                    io.finish()
                    return current
            except RuntimeError as error:
                if isinstance(error, closed_loop.RouteStopped):
                    raise
                if fatal(error):
                    stop(name, f'{original}; settle failed: {error}')
                matching, count = None, 0
            ctx.check_stop()
            time.sleep(min(.5, max(0, settle_until - time.monotonic())))

    def later(name, before, after):
        if after == before:
            return False
        if name in ('to_main', 'back_to_main'):
            return after == 'main_menu'
        if name == 'quit_training':
            return after == 'css.training'
        rank = {'main_menu': 0, 'stage_select': 1, 'css.training': 2, 'training': 3}
        target = {'main_to_stage': 1, 'pick_stage': 2, 'pick_fighters': 3, 'enter_training': 3}
        return name in target and rank.get(after, -1) >= target[name]

    try:
        try:
            io = BridgePickerIO(ctx)
        except RuntimeError as error:
            stop('locate', str(error))
        next_phase = 'locate'
        while True:
            deadline(f'deadline expired before {next_phase}')
            try:
                ui = native_picker.wait_sample(io, native_picker.UI, until, ui, in_flight=True)
                last_ui = dict(ui.values)
            except RuntimeError as error:
                if time.monotonic() >= until:
                    stop('deadline', f'deadline expired before {next_phase}: {error}')
                stop('locate', str(error))
            screen = _reach_training_screen(ui)
            if screen != 'css.training':
                css_picked = False
            if screen == 'main_menu':
                name = 'main_to_stage' if _reach_training_main_checkpoint(ui) else 'back_to_main'
            elif _fields_match(ui, {'screen.context': 'training', 'dialog': 'none'}):
                raw_screen = ui.values.get('screen')
                if raw_screen == 'training':
                    if ui.values.get('input.owner') != 'native':
                        stop('locate', 'reach_training needs native Training input ownership')
                    try:
                        final = native_state(ctx, fresh=('roster',), cap=max(.01, until - time.monotonic()),
                                             roster_entries=(0, 1))
                    except RuntimeError as error:
                        stop('locate', str(error))
                    known_stage = final.get('stage')
                    roster = final.get('roster', {})
                    if known_stage != wanted_stage:
                        name = 'back_to_main'
                    elif roster.get(0, {}).get('kind') != wanted_p1 or 1 not in roster:
                        name = 'quit_training'
                    elif wanted_cpu is not None and roster[1].get('kind') != wanted_cpu:
                        name = 'set_cpu'
                    else:
                        name = None
                elif raw_screen == 'css.training':
                    name = ('back_to_main' if known_stage != wanted_stage else
                            'enter_training' if css_picked else 'pick_fighters')
                elif raw_screen == 'stage_select':
                    name = 'pick_stage'
                else:
                    name = 'back_to_main'
            else:
                name = 'to_main'
            if retry_same is not None:
                retry_name, retry_screen = retry_same
                retry_same = None
                if screen == retry_screen:
                    name = retry_name
            if name is None:
                deadline('deadline expired before final verification')
                try:
                    final_ui = native_picker.wait_sample(io, native_picker.UI, until, ui, in_flight=True)
                    last_ui = dict(final_ui.values)
                except RuntimeError as error:
                    stop('locate', str(error))
                if not _fields_match(final_ui, TRAINING_SCREEN):
                    stop('locate', 'final Training screen is not owned by native input')
                roster = final.get('roster', {})
                if (final.get('stage') != wanted_stage or roster.get(0, {}).get('kind') != wanted_p1
                        or 1 not in roster or (wanted_cpu is not None and roster[1].get('kind') != wanted_cpu)):
                    stop('locate', f'live Training state does not match the requested setup: {final}')
                break
            next_phase = name
            try:
                phase(name)
            except closed_loop.RouteStopped as error:
                if error.route == 'reach_training' and error.receipt.get('block') == 'reach_training':
                    raise
                errors[name] = str(error)
                stop(name, str(error))
            except RuntimeError as error:
                errors[name] = str(error)
                if fatal(error):
                    stop(name, str(error))
                after = settle(name, str(error), screen)
                block = getattr(ui, 'block', {})
                retry_evidence[name] = {
                    'start': starts[name], 'error': str(error), 'screen': after, 'seconds': phase_elapsed[name],
                    **{key: block.get(key) for key in ('frame', 'wall', 'rev')},
                }
                if later(name, screen, after):
                    recovered.append(name)
                    phase_stall('phase-recovered', name, retry_evidence[name])
                elif after == screen:
                    retry_same = (name, screen)
                if after != 'css.training':
                    css_picked = False
            next_phase = {'to_main': 'main_to_stage', 'back_to_main': 'main_to_stage',
                          'main_to_stage': 'pick_stage', 'pick_stage': 'pick_fighters',
                          'quit_training': 'pick_fighters', 'pick_fighters': 'enter_training',
                          'enter_training': 'set_cpu', 'set_cpu': 'final verification'}[name]
    finally:
        ctx.runner.setup_metrics = previous
        if previous is not None:
            _merge_setup_metrics(previous, metrics)
    return _save(ctx, 'routes-reach-training', {
        'block': 'reach_training', 'args': {'p1': p1, 'cpu': cpu, 'stage': stage, 'form': form},
        'verified': {'stage_id': wanted_stage, 'roster': final['roster']}, **metrics,
        'phases': receipts, 'ms': {**timings, 'total': round((time.monotonic() - started) * 1000)}, 'skipped': skipped,
        'starts': starts, 'recovered': recovered, 'retries': {key: count - 1 for key, count in starts.items()
                                                            if count > 1},
        'stalls': stalls, 'result': _stall_result(True, stalls),
    })


def switch_close(ctx):
    """Close SSBU: HOME, X, the close dialog's A (switch-remote's `switch.terminate`, each step image-checked)."""
    _need_switch(ctx, 'switch_close')
    result = ctx.call('switch.terminate', {}, job_cap=60)
    return _save(ctx, 'routes-switch-close', {'terminate': result})
