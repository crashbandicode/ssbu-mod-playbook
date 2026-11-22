#!/usr/bin/python3
"""Offline cutter for a hardware-runbook session recording.

During a hardware run the playbook runner keeps ONE continuous OBS recording and appends marks to
`<out>/recording.jsonl` (event start | row-start | row-end | shot | gif-start | gif-end | stop; row, label, wall,
rec_ms, frame, active, and path on stop). This turns those marks into GIFs and stills, after the run or, with
--partial, while the recording is still being written.

  cut_session.py OUT [--rows ID,ID] [--only gifs|rows|stills] [--recording PATH] [--partial] [--offset-ms N]

Outputs (all under OUT unless a gif label names its own `out`, which is taken relative to OUT):
  gifs    one GIF per gif-start/gif-end pair at the label's `out` (default OUT/<row>/<gif>.gif) plus a receipt
          `<gif>.gif.json` (frames, duration_s, bytes, sha256, path, source recording and rec_ms span).
          Use preview (1280 wide, 12+ fps, <= 10 s) for gameplay images, preview-menu (10+ fps) for menus.
          Legacy evidence and docs presets still cut GIFs for existing runs. A byte cap never drops frames.
  rows    OUT/clips/<row>.gif, a review clip per row-start/row-end pair, with the same receipt.
          Keep the original 1080p recording and a 2–3× zoom crop as evidence. Encode documentation and tutorial
          clips as source-rate MP4s from that recording using the recipe in docs/GIFS.md.
  stills  OUT/stills/<row>-<label>.png, full size, one per `shot` mark.

Per-row files (plan session_recording.per_row_files): the runner split the recording at each row start and logged
a `clip` event per closed file (row, path, clip = the <out>/<row>/clip.* link, file_rec_ms = rec_ms at which that
file began, rows = every row the file holds). Each row's GIFs and stills are then cut from its own clip at
rec_ms - file_rec_ms, never by scrubbing one giant file; a row without a closed clip is skipped with a reason.
--recording PATH turns this off (one file, global rec_ms).

A JSON summary goes to stdout. Exit 0 ok, 1 when a cut failed, 2 when recording.jsonl has no `start` event
(the recording was unavailable), 3 when the recording file cannot be found.

Decode path: GStreamer through PyGObject (filesrc ! decodebin, avdec_h264 for OBS's H.264) into Pillow. The file is
seeked by timestamp (flush + accurate) and only the needed span is decoded. rec_ms is OBS's record duration, i.e.
the file's own timeline from 0; --offset-ms shifts it if a recording turns out to start elsewhere.
Run it with `/usr/bin/python3 -I`; it imports nothing from its sibling modules except, to find an unfinished
recording, obsws (read-only GetRecordDirectory).
"""
import argparse
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path

EXIT_FAILED, EXIT_NO_START, EXIT_NO_RECORDING = 1, 2, 3

GIF_PREVIEW_FPS = {'preview': 12, 'preview-menu': 10}
PRESETS = {
    **{name: {'width': 1280, 'fps': fps, 'max_s': 10.0, 'max_bytes': None}
       for name, fps in GIF_PREVIEW_FPS.items()},
    'evidence': {'width': 640, 'fps': 15, 'max_s': None, 'max_bytes': 10 * 1024 * 1024},
    'docs': {'width': 1280, 'fps': 15, 'max_s': 10.0, 'max_bytes': None},
}
MAX_FPS = 15
FRAME_CAP = 900          # decoded frames held in memory per clip (640x360 RGB ~ 0.7 MB each)
FRAME_NS = 1_000_000_000 // 60
EVENTS = ('start', 'row-start', 'row-end', 'shot', 'gif-start', 'gif-end', 'stop', 'clip', 'recording-restarted')
STILL_TOLERANCE_NS = 500_000_000   # a still whose nearest decoded frame is further than this is not trusted


# --------------------------------------------------------------------------------------------------------------
# Marks: parsing and pairing (pure; no GStreamer)

def parse_marks(path):
    """(marks, bad_lines) from recording.jsonl. A truncated or garbled line (the file is still being appended to,
    or a crash cut it) is skipped and counted, never fatal."""
    marks, bad = [], []
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        raise FileNotFoundError(f'cannot read {path}: {e}') from e
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            bad.append(n)
            continue
        if not isinstance(record, dict) or record.get('event') not in EVENTS:
            bad.append(n)
            continue
        marks.append(record)
    return marks, bad


def label_spec(label):
    """The compact-JSON label of a gif mark as a dict ({} when the label is plain text)."""
    if isinstance(label, str) and label.lstrip().startswith('{'):
        try:
            spec = json.loads(label)
        except ValueError:
            return {}
        return spec if isinstance(spec, dict) else {}
    return {}


def _ms(mark):
    value = mark.get('rec_ms')
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def safe_name(text):
    """A file-name-safe form of a row id or label."""
    return re.sub(r'[^A-Za-z0-9._-]+', '_', str(text)).strip('_') or 'x'


def pair_marks(marks):
    """Pair the start/end marks.

    Returns {'rows': [...], 'gifs': [...], 'shots': [...], 'incomplete': [...], 'unusable': [...]}.
    A span is {'kind', 'row', 'name', 'start_ms', 'end_ms', 'spec', 'attempts'}. A start met again before its end
    (an aborted attempt) is incomplete; of several complete spans for one key the last wins (a retry replaces
    the earlier attempt). A mark without a numeric rec_ms (the backend's mark failed) makes its span unusable."""
    open_, done, incomplete, unusable = {}, {}, [], []
    attempts = {}
    shots = []
    for mark in marks:
        event = mark.get('event')
        row = mark.get('row')
        if event == 'shot':
            if _ms(mark) is None or not row:
                unusable.append({'kind': 'still', 'row': row, 'name': mark.get('label'),
                                 'reason': 'shot mark has no rec_ms or no row'})
            else:
                shots.append({'row': str(row), 'label': str(mark.get('label') or 'shot'), 'ms': _ms(mark)})
            continue
        if event in ('row-start', 'gif-start'):
            kind = 'rows' if event == 'row-start' else 'gifs'
            spec = label_spec(mark.get('label')) if kind == 'gifs' else {}
            name = str(spec.get('gif') or mark.get('label') or '') if kind == 'gifs' else str(row)
            key = (kind, str(row), name)
            if key in open_:
                incomplete.append({'kind': kind, 'row': str(row), 'name': name,
                                   'reason': 'start repeated before an end mark (aborted attempt)'})
            open_[key] = (mark, spec)
            attempts[key] = attempts.get(key, 0) + 1
        elif event in ('row-end', 'gif-end'):
            kind = 'rows' if event == 'row-end' else 'gifs'
            spec = label_spec(mark.get('label')) if kind == 'gifs' else {}
            name = str(spec.get('gif') or mark.get('label') or '') if kind == 'gifs' else str(row)
            key = (kind, str(row), name)
            if key not in open_:
                incomplete.append({'kind': kind, 'row': str(row), 'name': name, 'reason': 'end mark without a start'})
                continue
            start, start_spec = open_.pop(key)
            s, e = _ms(start), _ms(mark)
            if s is None or e is None or e < s:
                unusable.append({'kind': kind, 'row': str(row), 'name': name,
                                 'reason': 'start/end mark has no usable rec_ms'})
                continue
            done[key] = {'kind': kind, 'row': str(row), 'name': name, 'start_ms': s, 'end_ms': e,
                         'spec': start_spec or spec, 'attempts': attempts[key]}
    for (kind, row, name), (mark, _spec) in open_.items():
        incomplete.append({'kind': kind, 'row': row, 'name': name, 'reason': 'no end mark yet'})
    return {'rows': [s for s in done.values() if s['kind'] == 'rows'],
            'gifs': [s for s in done.values() if s['kind'] == 'gifs'],
            'shots': shots, 'incomplete': incomplete, 'unusable': unusable}


def span_ns(start_ms, end_ms, offset_ms=0.0):
    """(start_ns, end_ns) in the file's timeline for a rec_ms span."""
    return (max(0, round((start_ms + offset_ms) * 1_000_000)), max(0, round((end_ms + offset_ms) * 1_000_000)))


def gif_preview_minimum(spec):
    if spec.get('preset') == 'evidence' and not spec.get('out'):
        return None
    scene = spec.get('scene', 'menu' if spec.get('preset') == 'preview-menu' else 'gameplay')
    if scene not in ('gameplay', 'menu'):
        raise ValueError('GIF scene must be gameplay or menu')
    return 10 if scene == 'menu' else 12


def clip_plan(kind, spec, start_ms, end_ms):
    """Width, fps, byte limit and duration cap for a clip: the label's values within the preset's limits."""
    preset_name = spec.get('preset') if spec.get('preset') in PRESETS else 'evidence'
    preset = PRESETS[preset_name]
    width = spec.get('width') if isinstance(spec.get('width'), int) and spec['width'] > 0 else preset['width']
    fps = spec.get('fps') if isinstance(spec.get('fps'), (int, float)) and spec['fps'] > 0 else preset['fps']
    preview_minimum = gif_preview_minimum(spec) if kind == 'gifs' else None
    if preview_minimum is not None and fps < preview_minimum:
        raise ValueError(f'GIF {spec.get("preset", preset_name)} needs at least {preview_minimum} fps')
    fps = float(min(fps, MAX_FPS if preview_minimum is not None else preset['fps']))
    max_s = spec.get('max_duration_s') if isinstance(spec.get('max_duration_s'), (int, float)) \
        and spec['max_duration_s'] > 0 else preset['max_s']
    if preset['max_s'] is not None and max_s is not None:
        max_s = min(max_s, preset['max_s'])
    max_bytes = spec.get('max_bytes') if isinstance(spec.get('max_bytes'), int) and spec['max_bytes'] > 0 \
        else preset['max_bytes']
    if preset['max_bytes'] is not None and max_bytes is not None:
        max_bytes = min(max_bytes, preset['max_bytes'])
    duration = (end_ms - start_ms) / 1000.0
    truncated = False
    if max_s is not None and duration > max_s:
        duration, truncated = max_s, True
    if duration > FRAME_CAP:                      # even 1 fps would exceed the frame budget
        duration, truncated = float(FRAME_CAP), True
    if duration * fps > FRAME_CAP:
        if preview_minimum is not None:
            duration, truncated = FRAME_CAP / fps, True
        else:                                    # Long internal review spans may use a lower rate.
            fps = max(1.0, FRAME_CAP / max(duration, 1e-9))
    return {'preset': preset_name, 'width': width, 'fps': fps, 'max_bytes': max_bytes,
            'duration_s': duration, 'truncated': truncated, 'preview_minimum': preview_minimum}


class SlotPicker:
    """Streaming frame picker: slots lie on a grid of 1/fps from the first frame, and each slot shows the newest
    source frame at or before it (a gap repeats the last good frame, so the clip keeps real time)."""

    def __init__(self, fps, max_frames=None):
        self.step = 1e9 / fps
        self.tol = self.step / 8
        self.max_frames = max_frames
        self.t0 = None
        self.slot = 0
        self.latest = None
        self.picked = []
        self.last_pts = None
        self.source_frames = 0

    def source_fps(self):
        if self.source_frames < 2:
            return 0.0
        return round((self.source_frames - 1) * 1e9 / (self.last_pts - self.t0), 3)

    def full(self):
        return self.max_frames is not None and len(self.picked) >= self.max_frames

    def add(self, pts, image_factory):
        """Offer a source frame; `image_factory()` builds its image only if some slot ends up using it."""
        if self.full():
            return
        if self.t0 is None:
            self.t0 = pts
        if self.last_pts is not None and pts <= self.last_pts:
            return
        self.last_pts = pts
        self.source_frames += 1
        while not self.full() and self.latest is not None and self.t0 + self.slot * self.step + self.tol < pts:
            self.picked.append(self._image())
            self.slot += 1
        self.latest = (pts, image_factory, None)

    def _image(self):
        pts, factory, image = self.latest
        if image is None:
            image = factory()
            self.latest = (pts, factory, image)
        return image

    def finish(self):
        """Emit the slots up to the last source frame; return the picked images."""
        while not self.full() and self.latest is not None and self.t0 + self.slot * self.step <= self.last_pts + self.tol:
            self.picked.append(self._image())
            self.slot += 1
        return self.picked


# --------------------------------------------------------------------------------------------------------------
# GIF encoding (Pillow)

def gif_durations(n, fps):
    """Per-frame delays in ms, multiples of 10 (GIF's unit), whose running total tracks n/fps."""
    out, prev = [], 0
    for i in range(1, n + 1):
        cumulative = round(i * 1000.0 / fps / 10) * 10
        out.append(max(20, cumulative - prev))
        prev = cumulative
    return out


def gif_bytes(images, fps, colours):
    """One shared palette from frames spread over the clip, no dithering (playbook encode.py's method)."""
    from PIL import Image
    n = len(images)
    picks = sorted({round(i * (n - 1) / 7) for i in range(8)}) if n > 1 else [0]
    w, h = images[0].size
    sheet = Image.new('RGB', (w, h * len(picks)))
    for slot, i in enumerate(picks):
        sheet.paste(images[i], (0, h * slot))
    palette = sheet.quantize(colors=colours, method=Image.Quantize.MEDIANCUT)
    frames = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
    buf = io.BytesIO()
    frames[0].save(buf, format='GIF', save_all=True, append_images=frames[1:], duration=gif_durations(n, fps),
                   loop=0, optimize=False, disposal=1)
    return buf.getvalue()


def encode_fitting(images, fps, max_bytes):
    """Try smaller palettes against the byte cap while keeping the sampled frames and timing."""
    smallest = None
    for colours in (128, 64, 32, 16):
        data = gif_bytes(images, fps, colours)
        if max_bytes is None or len(data) <= max_bytes:
            return data, {'frames': len(images), 'fps': round(fps, 3), 'colours': colours, 'frames_dropped': 0}
        smallest = len(data) if smallest is None else min(smallest, len(data))
    raise ValueError(f'smallest attempt is {smallest} bytes, over the {max_bytes} limit; trim or crop the source and retry')


# --------------------------------------------------------------------------------------------------------------
# GStreamer decode

def _gst():
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init(None)
    return Gst


class Pipeline:
    """filesrc ! decodebin ! videoconvert [! videoscale] ! RGB caps ! appsink, held in PAUSED.
    Audio (and any further video) streams go to fakesinks."""

    def __init__(self, path, width=None):
        self.Gst = Gst = _gst()
        caps = 'video/x-raw,format=RGB' + (f',width={int(width)},pixel-aspect-ratio=1/1' if width else '')
        self.pipe = Gst.parse_launch(
            f'filesrc name=src ! decodebin name=dec  videoconvert name=vc ! videoscale ! {caps} ! '
            'appsink name=sink sync=false max-buffers=8 drop=false')
        self.pipe.get_by_name('src').set_property('location', str(path))
        self.sink = self.pipe.get_by_name('sink')
        vc_sink = self.pipe.get_by_name('vc').get_static_pad('sink')
        pipe = self.pipe

        def on_pad(_dec, pad):
            name = (pad.get_current_caps() or pad.query_caps(None)).get_structure(0).get_name()
            if name.startswith('video/') and not vc_sink.is_linked():
                pad.link(vc_sink)
            else:
                fake = Gst.ElementFactory.make('fakesink', None)
                fake.set_property('sync', False)
                pipe.add(fake)
                fake.sync_state_with_parent()
                pad.link(fake.get_static_pad('sink'))

        self.pipe.get_by_name('dec').connect('pad-added', on_pad)
        self.pipe.set_state(Gst.State.PAUSED)
        self._settle(f'open {path}')

    def _error(self):
        msg = self.pipe.get_bus().timed_pop_filtered(0, self.Gst.MessageType.ERROR)
        if msg is None:
            return None
        err, debug = msg.parse_error()
        return f'{err.message} ({(debug or "").strip()[:160]})'

    def _settle(self, what, timeout_s=20):
        Gst = self.Gst
        result, _state, _pending = self.pipe.get_state(timeout_s * Gst.SECOND)
        error = self._error()
        if error or result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f'{what}: {error or "state change failed"}')
        if result == Gst.StateChangeReturn.ASYNC:
            raise RuntimeError(f'{what}: no video frame within {timeout_s} s (no video stream, or an unreadable file)')

    def seek(self, start_ns, stop_ns=None):
        Gst = self.Gst
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
        stop_type = Gst.SeekType.SET if stop_ns is not None else Gst.SeekType.NONE
        if not self.pipe.seek(1.0, Gst.Format.TIME, flags, Gst.SeekType.SET, int(start_ns), stop_type,
                              int(stop_ns) if stop_ns is not None else -1):
            raise RuntimeError(f'seek to {start_ns / 1e9:.3f} s refused')
        self._settle(f'seek to {start_ns / 1e9:.3f} s')

    def _image(self, sample):
        from PIL import Image
        structure = sample.get_caps().get_structure(0)
        w, h = structure.get_value('width'), structure.get_value('height')
        buf = sample.get_buffer()
        ok, info = buf.map(self.Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError('cannot map a decoded buffer')
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        return Image.frombuffer('RGB', (w, h), data, 'raw', 'RGB', len(data) // h, 1)

    def still(self, at_ns):
        """(pts_ns, image) of the frame at `at_ns`, or None when the file ends before it."""
        self.seek(at_ns)
        sample = self.sink.emit('try-pull-preroll', 5 * self.Gst.SECOND)
        if sample is None:
            return None
        return sample.get_buffer().pts, self._image(sample).copy()

    def frames(self, start_ns, stop_ns, picker):
        """Decode [start_ns, stop_ns) into `picker`; stops early once the picker is full."""
        Gst = self.Gst
        self.seek(start_ns, stop_ns)
        self.pipe.set_state(Gst.State.PLAYING)
        while not picker.full():
            sample = self.sink.emit('try-pull-sample', 5 * Gst.SECOND)
            if sample is None:
                error = self._error()
                if error:
                    raise RuntimeError(error)
                break                                   # EOS (or nothing more within 5 s)
            pts = sample.get_buffer().pts
            if pts is None or pts == Gst.CLOCK_TIME_NONE:
                continue
            picker.add(pts, lambda s=sample: self._image(s).copy())
        return picker.finish()

    def close(self):
        self.pipe.set_state(self.Gst.State.NULL)


def cut_clip(recording, start_ns, plan):
    """Decode and resample the span. Return the picked images, last PTS and actual source cadence."""
    max_frames = int(plan['duration_s'] * plan['fps'] + 0.5) or 1
    stop_ns = start_ns + int(plan['duration_s'] * 1e9)
    picker = SlotPicker(plan['fps'], max_frames)
    pipeline = Pipeline(recording, plan['width'])
    try:
        images = pipeline.frames(start_ns, stop_ns, picker)
    finally:
        pipeline.close()
    return images, picker.last_pts, picker.source_fps()


# --------------------------------------------------------------------------------------------------------------
# Cutting

def write_gif(images, out, fps, max_bytes, receipt_extra, minimum=None):
    """Write the GIF and its `<out>.json` receipt; returns the receipt."""
    data, info = encode_fitting(images, fps, max_bytes)
    out = Path(out)
    # Pillow folds runs of identical frames into one longer frame, so count what the file holds.
    from PIL import Image
    with Image.open(io.BytesIO(data)) as written:
        stored = written.n_frames
        total_ms = 0
        for i in range(stored):
            written.seek(i)
            total_ms += written.info.get('duration', 0)
    if minimum is not None and (fps < minimum or stored < 2):
        raise ValueError(f'preview needs at least {minimum:g} fps and more than one distinct image')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    receipt = {'path': str(out), 'frames': stored, 'frames_sampled': info['frames'], 'fps': info['fps'],
               'duration_s': round(total_ms / 1000.0, 3), 'bytes': len(data),
               'sha256': hashlib.sha256(data).hexdigest(), 'colours': info['colours'],
               'frames_dropped': info['frames_dropped'], 'width': images[0].size[0], **receipt_extra}
    Path(str(out) + '.json').write_text(json.dumps(receipt, indent=1) + '\n', encoding='utf-8')
    return receipt


def cut_span(span, recording, out_path, offset_ms, partial):
    """Cut one row/gif span to a GIF. Returns (receipt, None) or (None, reason) for a skip; raises on failure."""
    plan = clip_plan(span['kind'], span['spec'], span['start_ms'], span['end_ms'])
    start_ns, end_ns = span_ns(span['start_ms'], span['end_ms'], offset_ms)
    images, last_pts, source_fps = cut_clip(recording, start_ns, plan)
    if not images:
        if partial:
            return None, 'no frames decoded (recording not yet written that far)'
        raise RuntimeError(f'no frames decoded from {recording} at {start_ns / 1e9:.3f} s')
    wanted_end = start_ns + int(plan['duration_s'] * 1e9)
    short_ms = max(0.0, (wanted_end - last_pts) / 1e6)
    if short_ms > 3 * FRAME_NS / 1e6:
        if partial:
            return None, f'truncated tail: recording ends {short_ms:.0f} ms before the span end'
    extra = {'source': {'recording': str(recording), 'rec_ms': [span['start_ms'], span['end_ms']],
                        'start_s': round(start_ns / 1e9, 3), 'end_s': round(end_ns / 1e9, 3),
                        'offset_ms': offset_ms},
             'preset': plan['preset'], 'row': span['row'], 'truncated': plan['truncated'],
             'attempts': span['attempts'], 'short_ms': round(short_ms) if short_ms > 3 * FRAME_NS / 1e6 else 0}
    minimum = plan['preview_minimum']
    fps = plan['fps']
    if minimum is not None:
        # Measured source timestamps can lower the floor. Filled slots cannot.
        if source_fps <= 0:
            raise ValueError('preview needs at least two source frames with distinct timestamps')
        extra['source_fps'] = source_fps
        if source_fps < minimum:
            fps = minimum = source_fps
            images, _, _ = cut_clip(recording, start_ns, plan | {'fps': fps})
            if len(images) < 2:
                raise ValueError('preview needs at least two sampled source frames')
    if span['kind'] == 'gifs':
        extra['gif'] = span['name']
    return write_gif(images, out_path, fps, plan['max_bytes'], extra, minimum), None


def gif_out_path(out_dir, span):
    spec_out = span['spec'].get('out')
    if isinstance(spec_out, str) and spec_out.strip():
        p = Path(spec_out)
        return p if p.is_absolute() else out_dir / p
    return out_dir / safe_name(span['row']) / f'{safe_name(span["name"])}.gif'


def still_name(shot, taken):
    label = shot['label']
    prefix = f'{shot["row"]}:'
    if label.startswith(prefix):
        label = label[len(prefix):]
    base = f'{safe_name(shot["row"])}-{safe_name(label)}'
    name, n = base, 1
    while name in taken:
        n += 1
        name = f'{base}-{n}'
    taken.add(name)
    return name + '.png'


def find_recording(marks, explicit):
    """The recording file: --recording, else the stop mark's path, else any mark's path, else (an unfinished
    recording) the newest file in OBS's record directory modified since the start mark (read-only request)."""
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    paths = [m.get('path') for m in marks if m.get('path')]
    for path in reversed(paths):
        if Path(path).is_file():
            return Path(path)
    start = next((m for m in marks if m.get('event') == 'start'), {})
    since = (start.get('wall') or 0) - 10
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import obsws
        client = obsws.ObsClient()
        try:
            folder = Path(client.request('GetRecordDirectory').get('recordDirectory') or '')
        finally:
            client.close()
        files = [f for f in folder.iterdir()
                 if f.suffix.lower() in ('.mp4', '.mkv', '.mov') and f.stat().st_mtime >= since]
        return max(files, key=lambda f: f.stat().st_mtime) if files else None
    except Exception:  # noqa: BLE001 - OBS down or no directory: the caller reports "no recording"
        return None


def clip_sources(marks):
    """Per-row files: {row: {'path', 'base_ms'}} from the `clip` events. The linked clip is preferred, else the OBS
    file; a file that also held rows whose own split failed maps those rows too. base_ms None (a file that began
    without an acknowledged split) cannot place marks."""
    out = {}
    for mark in marks:
        if mark.get('event') != 'clip':
            continue
        clip = mark.get('clip')
        path = clip if clip and Path(clip).is_file() else mark.get('path')
        if not path:
            continue
        base = mark.get('file_rec_ms')
        base = float(base) if isinstance(base, (int, float)) and not isinstance(base, bool) else None
        rows = [r for r in (mark.get('rows') or []) if r is not None]
        if mark.get('row') is not None and mark['row'] not in rows:
            rows.insert(0, mark['row'])
        for row in rows:
            out[str(row)] = {'path': str(path), 'base_ms': base}
    return out


def unavailable_reason(out_dir):
    """Why the runner could not start the session recording, from its record in <out>/actions.jsonl
    ({"session_recording": "unavailable", "error"|"reason": ...}); None when it logged none."""
    reason = None
    try:
        lines = (Path(out_dir) / 'actions.jsonl').read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get('session_recording') == 'unavailable':
            reason = str(record.get('reason') or record.get('error') or 'unavailable')
    return reason


def _no_start(out_dir, what):
    reason = unavailable_reason(out_dir)
    message = f'{what}: the session recording was unavailable (nothing to cut)'
    summary = {'ok': False, 'error': message}
    if reason:
        summary['error'] = f'{message}; runner reason: {reason}'
        summary['session_unavailable'] = reason
    return summary, EXIT_NO_START


def run(out_dir, rows=None, only=None, recording=None, partial=False, offset_ms=0.0):
    """Cut everything; returns (summary, exit_code)."""
    t0 = time.monotonic()
    out_dir = Path(out_dir)
    log = out_dir / 'recording.jsonl'
    if not log.is_file():
        return _no_start(out_dir, f'{log} not found: no session recording was marked')
    marks, bad = parse_marks(log)
    if not any(m.get('event') == 'start' for m in marks):
        return _no_start(out_dir, f'{log} has no start event')
    clips = {} if recording else clip_sources(marks)
    source = None if clips else find_recording(marks, recording)
    if source is None and not clips:
        return {'ok': False, 'error': 'the session recording file was not found: pass --recording PATH '
                                      '(the stop mark names none that exists here)'}, EXIT_NO_RECORDING
    want = set(only) if only else {'gifs', 'rows', 'stills'}
    row_filter = set(rows) if rows else None
    paired = pair_marks(marks)
    summary = {'out': str(out_dir), 'recording': str(source) if source else 'per-row files',
               'per_row_files': bool(clips), 'partial': bool(partial), 'offset_ms': offset_ms,
               'gifs': [], 'rows': [], 'stills': [], 'skipped': [], 'failed': [], 'warnings': []}

    def source_of(row):
        """(file, offset_ms, None) for a row's marks, or (None, None, reason)."""
        if not clips:
            return source, offset_ms, None
        clip = clips.get(str(row))
        if clip is None:
            return None, None, 'no per-row clip for this row (its file is not closed yet, or its split failed)'
        if clip['base_ms'] is None:
            return None, None, 'its file began without an acknowledged split (no rec_ms origin)'
        if not Path(clip['path']).is_file():
            return None, None, f'per-row clip {clip["path"]} not found'
        return Path(clip['path']), offset_ms - clip['base_ms'], None
    if bad:
        summary['warnings'].append(f'recording.jsonl: {len(bad)} unreadable line(s) skipped (first: {bad[0]})')
    if not any(m.get('event') == 'stop' for m in marks):
        summary['warnings'].append('no stop mark: the recording may still be written or the run was cut short')

    def wanted(row):
        return row_filter is None or row in row_filter

    for item in paired['incomplete'] + paired['unusable']:
        group = 'stills' if item['kind'] == 'still' else item['kind']
        if group in want and (item['row'] is None or wanted(str(item['row']))):
            summary['skipped'].append(item)

    for kind in ('gifs', 'rows'):
        if kind not in want:
            continue
        for span in paired[kind]:
            if not wanted(span['row']):
                continue
            target = gif_out_path(out_dir, span) if kind == 'gifs' \
                else out_dir / 'clips' / f'{safe_name(span["row"])}.gif'
            span_source, span_offset, why = source_of(span['row'])
            if span_source is None:
                summary['skipped'].append({'kind': kind, 'row': span['row'], 'name': span['name'], 'reason': why})
                continue
            try:
                receipt, reason = cut_span(span, span_source, target, span_offset, partial)
            except Exception as e:  # noqa: BLE001 - one bad span must not stop the rest
                summary['failed'].append({'kind': kind, 'row': span['row'], 'name': span['name'],
                                          'error': f'{type(e).__name__}: {e}'[:300]})
                continue
            if receipt is None:
                summary['skipped'].append({'kind': kind, 'row': span['row'], 'name': span['name'], 'reason': reason})
            else:
                summary[kind].append(receipt)

    if 'stills' in want:
        shots = [s for s in paired['shots'] if wanted(s['row'])]
        taken, pipeline, pipeline_source = set(), None, None
        for shot in shots:
            shot_source, shot_offset, why = source_of(shot['row'])
            if shot_source is None:
                summary['skipped'].append({'kind': 'still', 'row': shot['row'], 'name': shot['label'], 'reason': why})
                continue
            if str(shot_source) != pipeline_source:
                # Marks are chronological: finish this file before opening the next.
                # Keeping one decoder per row exhausts resources on long sessions.
                if pipeline is not None:
                    pipeline.close()
                pipeline_source, pipeline = str(shot_source), None
                try:
                    pipeline = Pipeline(shot_source)
                except Exception as e:  # noqa: BLE001
                    summary['failed'].append({'kind': 'stills', 'row': shot['row'], 'source': str(shot_source),
                                              'error': f'{type(e).__name__}: {e}'[:300]})
                    pipeline = None
            if pipeline is None:
                continue
            name = still_name(shot, taken)
            at_ns = span_ns(shot['ms'], shot['ms'], shot_offset)[0]
            try:
                got = pipeline.still(at_ns)
            except Exception as e:  # noqa: BLE001
                summary['failed'].append({'kind': 'still', 'row': shot['row'], 'name': shot['label'],
                                          'error': f'{type(e).__name__}: {e}'[:300]})
                continue
            if got is None or abs(got[0] - at_ns) > STILL_TOLERANCE_NS:
                reason = 'recording does not reach this mark yet' if got is None \
                    else f'nearest decoded frame is {abs(got[0] - at_ns) / 1e6:.0f} ms away'
                summary['skipped'].append({'kind': 'still', 'row': shot['row'], 'name': shot['label'],
                                           'reason': reason})
                continue
            path = out_dir / 'stills' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            got[1].save(path, format='PNG')
            summary['stills'].append({'path': str(path), 'row': shot['row'], 'label': shot['label'],
                                      'rec_ms': shot['ms'], 'size': list(got[1].size),
                                      'bytes': path.stat().st_size, 'source': str(shot_source),
                                      'at_s': round(at_ns / 1e9, 3)})
        if pipeline is not None:
            pipeline.close()

    summary['counts'] = {k: len(summary[k]) for k in ('gifs', 'rows', 'stills', 'skipped', 'failed')}
    summary['ok'] = not summary['failed']
    summary['elapsed_s'] = round(time.monotonic() - t0, 2)
    return summary, (0 if summary['ok'] else EXIT_FAILED)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('out', help='the run output directory holding recording.jsonl')
    ap.add_argument('--rows', help='comma-separated row ids to cut (default all)')
    ap.add_argument('--only', action='append', choices=('gifs', 'rows', 'stills'),
                    help='cut only this kind (repeatable)')
    ap.add_argument('--recording', help='the session recording file (default: the stop mark\'s path)')
    ap.add_argument('--partial', action='store_true',
                    help='the recording is still being written: cut only spans whose end mark exists and whose '
                         'footage is on disk, skip a truncated tail without failing')
    ap.add_argument('--offset-ms', type=float, default=0.0, help='added to every rec_ms (default 0)')
    a = ap.parse_args(argv)
    rows = [r.strip() for r in a.rows.split(',') if r.strip()] if a.rows else None
    summary, code = run(a.out, rows, a.only, a.recording, a.partial, a.offset_ms)
    print(json.dumps(summary, indent=1))
    if code in (EXIT_NO_START, EXIT_NO_RECORDING):
        print(summary['error'], file=sys.stderr)
    return code


if __name__ == '__main__':
    sys.exit(main())
