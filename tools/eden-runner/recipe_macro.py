#!/usr/bin/env python3
"""Compile segment recipes into eden-input-macro files offline, so a plan is READY before any console or Eden.

    recipe_macro.py <rows.json> [--fps 60] [--min-frames 2]

For every segment that has a "recipe" and no file yet, this writes the segment's "file" (an .edenmacro.json the
Eden runner and the real-Switch pad both play: the pad uploads it with MBEGIN/MEV and plays it at 60 Hz of its own
clock) and its "sha256" back into rows.json. The runner then plays the file; it never records a recipe on hardware
(switch-remote's client refuses macro.record).

Recipe actions (the runner's recipe syntax):
  {"buttons": "b+up", "hold": 100, "gap": 0.5}   buttons pressed in ONE frame ("+" or "," between names), held
                                                  `hold` ms (or "frames"), then `gap` s (or "gap_frames") neutral
  {"buttons": "a", "with_stick": [0, -1]}         the stick held through the press
  {"stick": [x, y, ms], "gap": 0.3}                the left stick (x, y in -1..1; y up is positive)
Every hold is at least --min-frames frames (2 by default: a press shorter than one console poll may be missed), and
a frame count is rounded up from ms at --fps.
HOME and CAPTURE are refused: the pad refuses them inside a macro; use switch.launch / switch.terminate steps.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

BITS = {'a': 1, 'b': 2, 'x': 4, 'y': 8, 'lstick': 16, 'l3': 16, 'rstick': 32, 'r3': 32, 'l': 64, 'r': 128,
        'zl': 256, 'zr': 512, 'plus': 1024, 'start': 1024, '+': 1024, 'minus': 2048, '-': 2048,
        'left': 4096, 'dleft': 4096, 'up': 8192, 'dup': 8192, 'right': 16384, 'dright': 16384,
        'down': 32768, 'ddown': 32768}
SYSTEM = ('home', 'capture')
MOTION = [{'accel': [0, 0, -1], 'delta_us': 1000, 'gyro': [0, 0, 0]}] * 2
GAME_ID = '01006A800016E000'


def mask(names):
    """'b+up' or 'b,up' -> the Eden bit mask. 'plus' is the + button; a bare '+' separates names."""
    parts = [p for chunk in names.split(',') for p in chunk.split('+')] if names != '+' else ['plus']
    value = 0
    for raw in parts:
        name = raw.strip().lower()
        if not name:
            continue
        if name in SYSTEM:
            raise ValueError(f'{name} is a system button: the pad refuses it inside a macro (use switch.* steps)')
        if name not in BITS:
            raise ValueError(f'unknown button {raw!r}')
        value |= BITS[name]
    if not value:
        raise ValueError(f'no buttons in {names!r}')
    return value


def frames_of(ms, fps, minimum):
    return max(minimum, int(math.ceil(float(ms) * fps / 1000.0 - 1e-9)))


def compile_recipe(name, recipe, fps=60, min_frames=2):
    events, frame = [], 0

    def sample(at, buttons=0, sticks=(0, 0, 0, 0)):
        if events and events[-1]['frame'] == at:
            events[-1].update(buttons=buttons, sticks=list(sticks))
        else:
            events.append({'buttons': buttons, 'frame': at, 'motion': MOTION, 'sticks': list(sticks),
                           'triggers': [0, 0]})

    for action in recipe:
        if 'buttons' in action:
            bits = mask(action['buttons'])
            hold = int(action['frames']) if 'frames' in action else frames_of(action.get('hold', 150), fps, min_frames)
            hold = max(hold, min_frames)
            sticks = (0, 0, 0, 0)
            if 'with_stick' in action:
                sticks = (float(action['with_stick'][0]), float(action['with_stick'][1]), 0, 0)
            sample(frame, bits, sticks)
            frame += hold
            sample(frame, 0)
        elif 'stick' in action:
            x, y = float(action['stick'][0]), float(action['stick'][1])
            ms = action['stick'][2] if len(action['stick']) > 2 else 250
            hold = int(action['frames']) if 'frames' in action else frames_of(ms, fps, min_frames)
            sample(frame, 0, (x, y, 0, 0))
            frame += max(hold, min_frames)
            sample(frame, 0)
        else:
            raise ValueError(f'{name}: unknown recipe action {action}')
        gap = int(action['gap_frames']) if 'gap_frames' in action else frames_of(action.get('gap', 0.6) * 1000, fps, 1)
        frame += gap
    duration = max(frame, (events[-1]['frame'] + 1) if events else 1)
    return {'bindings': {'gamepad': '', 'keyboard': ''}, 'duration_frames': duration, 'events': events,
            'format': 'eden-input-macro', 'game_id': GAME_ID, 'markers': [], 'name': name,
            'parent': {'marker': '', 'name': ''}, 'player': 0, 'version': 1}


def compile_plan(path, fps=60, min_frames=2):
    path = Path(path)
    plan = json.loads(path.read_text())
    made = []
    for name, seg in plan.get('segments', {}).items():
        if not seg.get('recipe') or 'library' in seg:
            continue
        rel = seg.get('file') or f'segments/{name}.edenmacro.json'
        target = path.parent / rel
        if target.is_file() and seg.get('sha256'):
            continue
        doc = compile_recipe(name, seg['recipe'], fps, min_frames)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc, sort_keys=True, separators=(',', ':')))
        seg['file'] = rel
        seg['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
        seg['compiled'] = {'by': 'recipe_macro.py', 'fps': fps, 'min_frames': min_frames}
        made.append(name)
    path.write_text(json.dumps(plan, indent=2) + '\n')
    return made


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('rows')
    parser.add_argument('--fps', type=int, default=60)
    parser.add_argument('--min-frames', type=int, default=2)
    args = parser.parse_args(argv)
    made = compile_plan(args.rows, args.fps, args.min_frames)
    print(json.dumps({'compiled': made}))


if __name__ == '__main__':
    main()
