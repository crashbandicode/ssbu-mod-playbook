"""The asynchronous row judge of the runner (plan "judge"): the console never waits for a verdict.

After a row's clip file is closed (per-row OBS files: at the next row's split, or the session stop), the runner writes
<out>/judge/jobs/<row>.json and starts one background worker process for it (`judge_queue.py work <job>`). The worker:
  1. samples three clip frames, start / mid / end (cut_session.Pipeline, full size) into <out>/judge/<row>/;
  2. runs the deterministic clip check: the expected-result shot (the row's last "after" shot, else its last shot) must
     exist, and something must have changed: max diff of the mid/end frames and the step/after shots against the start
     frame / before-shot > NO_EFFECT_DIFF, else FAIL "no-visible-effect";
  3. calls the judge: `<python> -I <plan dir>/<judge cmd> --job <job.json> --out <verdict.json>` -> {verdict: PASS|FAIL|
     UNSURE, reason, shots_cited, model}; a judge that is missing, crashes or times out is UNSURE (never a PASS);
  4. merges the verdict into the row's receipt (atomic replace under a lock) and sets the final status (final_status).

Final status (the operator's rule, 2026-11-16): deterministic gates failed -> FAILED (no judge job); clip check failed ->
FAILED; judge FAIL -> FAILED, unless a deterministic effect gate passed (a disagreement) -> RAN-UNVERIFIED; judge PASS ->
PASS; UNSURE -> RAN-UNVERIFIED. Without a judge: PASS needs a passed effect gate when the plan has require_effect.
RAN-UNVERIFIED rows carry "review": true and a reason; FAILING.md lists them with the failures.
"""
import fcntl
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

try:
    from PIL import Image, ImageFile
except ImportError:
    Image = None
    ImageFile = None

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import still_fresh  # noqa: E402


def effect_evidence(value):
    if isinstance(value, dict):
        marker = value.get('unverified')
        return (bool(value) and not (isinstance(marker, str) and marker)
                and value.get('grade') != 'NEEDS VISUAL READ' and value.get('visual_verdict') != 'pending'
                and ('verdict' not in value or str(value['verdict']).upper() == 'PASS')
                and ('pass' not in value or value['pass'] is True))
    return ((isinstance(value, str) and bool(value))
            or (isinstance(value, (int, float)) and not isinstance(value, bool)))


NO_EFFECT_DIFF = 3.0      # mean abs RGB diff (0-255, 320x180) below which two frames are "the same picture"
JUDGE_TIMEOUT_S = 600
FRAME_NAMES = ('start', 'mid', 'end')
_IMAGE_VERIFY_LOCK = threading.RLock()


# ---- status rules (pure; host-tested) ------------------------------------------------------------------------------
def gates_only_status(effects_passed, require_effect):
    """A row whose steps all passed, with no judge: PASS only with a passed effect gate (when required)."""
    if effects_passed or not require_effect:
        return 'PASS', None
    return 'RAN-UNVERIFIED', 'no checked effect gate passed (only start/navigation gates)'


def final_status(gates_status, effects_passed, clip_check, verdict):
    """(status, reason, review) from the deterministic result, the clip check and the judge verdict."""
    if gates_status != 'PASS':
        return 'FAILED', 'deterministic gate failed', False
    if clip_check and not clip_check.get('ok', True):
        return 'FAILED', clip_check.get('reason') or 'clip check failed', False
    v = (verdict or {}).get('verdict')
    reason = (verdict or {}).get('reason') or ''
    if v == 'PASS':
        return 'PASS', reason, False
    if v == 'FAIL':
        if effects_passed:
            return 'RAN-UNVERIFIED', f'disagreement: effect gate(s) {effects_passed} passed, judge FAIL: {reason}', True
        return 'FAILED', f'judge FAIL: {reason}', False
    return 'RAN-UNVERIFIED', f'judge {v or "missing"}: {reason}'.strip(), True


# ---- image helpers -------------------------------------------------------------------------------------------------
def frame_diff(a, b):
    """Mean absolute RGB difference of two images (paths or PIL images) at 320x180."""
    from PIL import Image, ImageChops, ImageStat

    def load(x):
        im = Image.open(x) if isinstance(x, (str, Path)) else x
        return im.convert('RGB').resize((320, 180), Image.Resampling.BOX)
    diff = ImageChops.difference(load(a), load(b))
    return sum(ImageStat.Stat(diff).mean) / 3


def clip_span_ms(receipt):
    """The row's length in its clip file's own time (ms): the row's last mark (receipt rec_ms[1]) minus
    clip_rec_ms_base, or None. GStreamer's duration of a per-row OBS file can be short (2026-11-18: CPU-F001 8.3 s
    against an 11.4 s row, REC-F005f 38.9 s against 41.5 s), which put "clip end" BEFORE the row's effect stills."""
    base, span = receipt.get('clip_rec_ms_base'), receipt.get('rec_ms')
    if isinstance(base, (int, float)) and isinstance(span, list) and len(span) == 2 \
            and isinstance(span[1], (int, float)) and span[1] > base:
        return span[1] - base
    return None


def frame_positions_ns(total_ns):
    """Where start / mid / end are cut in a clip of `total_ns`."""
    return {'start': min(200_000_000, total_ns // 10), 'mid': total_ns // 2, 'end': max(0, total_ns - 300_000_000)}


def sample_frames(clip, folder, span_ms=None):
    """({start, mid, end: png path}, {name: clip ms it was cut at}) from the clip (full size). The positions use the
    row's real length `span_ms` (clip_span_ms of the receipt) when known, else GStreamer's duration; a position past
    the decodable end falls back to the GStreamer-duration position. Raises when the clip can't be read."""
    import cut_session
    folder.mkdir(parents=True, exist_ok=True)
    pipe = cut_session.Pipeline(clip)
    try:
        ok, duration = pipe.pipe.query_duration(pipe.Gst.Format.TIME)
        if (not ok or duration <= 0) and not span_ms:
            raise RuntimeError('clip has no duration')
        fallback = frame_positions_ns(duration) if ok and duration > 0 else {}
        at = frame_positions_ns(int(span_ms * 1_000_000)) if span_ms else fallback
        out, at_ms = {}, {}
        for name in FRAME_NAMES:
            got = pipe.still(at[name])
            if got is None and fallback.get(name) is not None and fallback[name] != at[name]:
                got = pipe.still(fallback[name])
            if got is None:
                continue
            path = folder / f'clip-{name}.png'
            got[1].save(path)
            out[name], at_ms[name] = str(path), round(got[0] / 1_000_000)
        return out, at_ms
    finally:
        pipe.close()


DENSE_MAX = 6          # effect-window frames per row
DENSE_MIN_GAP_MS = 600  # a gap between stills shorter than this needs no extra frame


def dense_positions(receipt, n=DENSE_MAX, min_gap_ms=DENSE_MIN_GAP_MS):
    """Clip ms of up to `n` effect-window frames: the midpoints of the longest gaps between consecutive stills of the
    row (in clip time, rec_ms - clip_rec_ms_base) that hold no menu hook ('run' item) between them, i.e. gameplay the
    stills skip over. Sorted; [] without times."""
    base = receipt.get('clip_rec_ms_base')
    if not isinstance(base, (int, float)):
        return []
    gaps, prev, menu = [], None, False
    for item in receipt.get('results') or []:
        if not isinstance(item, dict):
            continue
        if item.get('shot') and isinstance(item.get('rec_ms'), (int, float)):
            t = item['rec_ms'] - base
            if prev is not None and not menu and t - prev >= min_gap_ms:
                gaps.append((t - prev, round((prev + t) / 2)))
            prev, menu = t, False
        elif 'run' in item:
            menu = True
    return sorted(mid for _, mid in sorted(gaps, reverse=True)[:n])


def sample_dense(clip, folder, positions_ms):
    """{clip ms: png path} for the effect-window frames (full size); positions past the decodable end are skipped."""
    import cut_session
    folder.mkdir(parents=True, exist_ok=True)
    pipe = cut_session.Pipeline(clip)
    try:
        out = {}
        for ms in positions_ms:
            got = pipe.still(int(ms) * 1_000_000)
            if got is None:
                continue
            path = folder / f'dense-{int(ms):07d}.png'
            got[1].save(path)
            out[round(got[0] / 1_000_000)] = str(path)
        return out
    finally:
        pipe.close()


def _png_decodes(path):
    with open(path, 'rb') as stream:
        if stream.read(8) != b'\x89PNG\r\n\x1a\n':
            return False
        header = None
        inflater = None
        seen_end = False
        seen_palette = False
        palette_entries = 0
        seen_metadata = set()
        idat_closed = False
        passes = []
        remaining_rows = row_bytes = offset = 0

        def consume(data):
            nonlocal remaining_rows, row_bytes, offset
            position = 0
            while position < len(data):
                if not remaining_rows:
                    if not passes:
                        return False
                    remaining_rows, row_bytes = passes.pop(0)
                    offset = 0
                if offset == 0 and data[position] > 4:
                    return False
                take = min(len(data) - position, row_bytes - offset)
                offset += take
                position += take
                if offset == row_bytes:
                    remaining_rows -= 1
                    offset = 0
            return True

        while True:
            prefix = stream.read(8)
            if len(prefix) != 8:
                return False
            length, kind = struct.unpack('>I4s', prefix)
            if length >= 2 ** 31 or any(not (65 <= c <= 90 or 97 <= c <= 122) for c in kind) or kind[2] & 32:
                return False
            if header is None and (kind != b'IHDR' or length != 13):
                return False
            if kind == b'IHDR' and header is not None:
                return False
            if kind[0] & 32:
                lengths = {b'gAMA': 4, b'cHRM': 32, b'sRGB': 1, b'pHYs': 9, b'tIME': 7}
                if kind not in (*lengths, b'tRNS', b'tEXt'):
                    return False
                if kind != b'tEXt' and kind in seen_metadata:
                    return False
                if (kind in lengths and length != lengths[kind]) or (kind == b'tEXt' and length > 1024 * 1024):
                    return False
                if kind in (b'gAMA', b'cHRM', b'sRGB') and seen_palette:
                    return False
                if kind not in (b'tEXt', b'tIME') and inflater is not None:
                    return False
                if kind == b'tRNS':
                    colour = header[3]
                    if ((colour == 0 and length != 2) or (colour == 2 and length != 6)
                            or (colour == 3 and not (seen_palette and 1 <= length <= palette_entries))
                            or colour in (4, 6)):
                        return False
                seen_metadata.add(kind)
            if kind == b'IDAT':
                if idat_closed or (header[3] == 3 and not seen_palette):
                    return False
                if inflater is None:
                    inflater = zlib.decompressobj()
            elif inflater is not None:
                idat_closed = True
            crc = zlib.crc32(kind)
            value = b''
            left = length
            while left:
                block = stream.read(min(left, 65536))
                if not block:
                    return False
                left -= len(block)
                crc = zlib.crc32(block, crc)
                if kind == b'IHDR' or kind[0] & 32:
                    value += block
                if kind == b'IDAT':
                    while block:
                        decoded = inflater.decompress(block, 65536)
                        if not consume(decoded) or inflater.unused_data:
                            return False
                        block = inflater.unconsumed_tail
            checksum = stream.read(4)
            if len(checksum) != 4 or struct.unpack('>I', checksum)[0] != crc:
                return False
            if kind == b'IHDR':
                width, height, depth, colour, compression, filtering, interlace = struct.unpack('>IIBBBBB', value)
                modes = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
                         4: (8, 16), 6: (8, 16)}
                if (not 0 < width < 2 ** 31 or not 0 < height < 2 ** 31
                        or colour not in modes or depth not in modes[colour]
                        or compression or filtering or interlace not in (0, 1)):
                    return False
                header = (width, height, depth, colour)
                bits = depth * {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour]
                grids = ((0, 0, 1, 1),) if not interlace else (
                    (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                    (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
                for x, y, dx, dy in grids:
                    w = max(0, (width - x + dx - 1) // dx)
                    h = max(0, (height - y + dy - 1) // dy)
                    if w and h:
                        passes.append((h, 1 + (w * bits + 7) // 8))
            elif kind == b'PLTE':
                if (seen_palette or inflater is not None or not length or length > 768 or length % 3
                        or header[3] in (0, 4) or (header[3] == 3 and length // 3 > 2 ** header[2])):
                    return False
                seen_palette = True
                palette_entries = length // 3
            elif kind == b'IEND':
                if inflater is not None:
                    while decoded := inflater.decompress(b'', 65536):
                        if not consume(decoded):
                            return False
                seen_end = length == 0 and inflater is not None and inflater.eof
                break
            elif kind == b'gAMA' and struct.unpack('>I', value)[0] == 0:
                return False
            elif kind == b'sRGB' and value[0] > 3:
                return False
            elif kind == b'pHYs' and value[-1] > 1:
                return False
            elif kind == b'tRNS' and header[3] in (0, 2):
                if any(sample >= 2 ** header[2] for sample in struct.unpack('>' + 'H' * (length // 2), value)):
                    return False
            elif kind == b'tEXt':
                keyword, separator, text = value.partition(b'\x00')
                if (not separator or not 1 <= len(keyword) <= 79 or b'\x00' in text
                        or keyword.startswith(b' ') or keyword.endswith(b' ') or b'  ' in keyword
                        or any(not (32 <= c <= 126 or 161 <= c <= 255) for c in keyword)):
                    return False
            elif kind == b'tIME':
                _, month, day, hour, minute, second = struct.unpack('>H5B', value)
                if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59 and second <= 60):
                    return False
            elif kind not in (b'IDAT',) and not (kind[0] & 32):
                return False
        return seen_end and not remaining_rows and not offset and not passes and not stream.read(1)


def image_issue(path, *, previews=None):
    try:
        if Image is None:
            return None if _png_decodes(path) else 'not a decodable image'
        with _IMAGE_VERIFY_LOCK:
            truncated = ImageFile.LOAD_TRUNCATED_IMAGES
            try:
                ImageFile.LOAD_TRUNCATED_IMAGES = False
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    image.load()
                    if previews is not None:
                        previews[str(Path(path))] = image.convert('RGB').resize((320, 180), Image.Resampling.BOX)
            finally:
                ImageFile.LOAD_TRUNCATED_IMAGES = truncated
        return None
    except Exception:  # noqa: BLE001 - any unreadable or unsupported image is excluded
        return 'not a decodable image'


def _cached_image_issue(path, cache):
    if cache is None:
        return image_issue(path)
    key = str(Path(path))
    if key not in cache:
        preview = {}
        issue = image_issue(path, previews=preview)
        cache[key] = (issue, preview.get(key))
    return cache[key][0]


def shot_issue(shot, image_cache=None):
    path = shot.get('path')
    if not path:
        return 'still was not taken'
    try:
        path = Path(path)
        if not path.is_file():
            return 'still file is missing or unreadable'
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
    except (OSError, TypeError, ValueError):
        return 'still file is missing or unreadable'
    if _cached_image_issue(path, image_cache):
        return 'still file is not a decodable image'
    record = shot.get('fresh')
    if not still_fresh.usable(record):
        return still_fresh.unusable_reason(record)
    expected = record.get('sha256')
    if isinstance(expected, str) and re.fullmatch(r'[0-9a-fA-F]{64}', expected) and digest.hexdigest() != expected.lower():
        return 'still bytes differ from the capture record'
    return None


def clip_check(job, frames, image_cache=None):
    """Deterministic: the expected-result shot exists, and the row changed something on screen."""
    if image_cache is None:
        image_cache = {}
    for path in frames.values():
        if isinstance(path, (str, Path)) and path:
            _cached_image_issue(path, image_cache)
    shots = job.get('shots') or []
    taken = [s for s in shots if s.get('phase') in ('before', 'step', 'after') and s.get('path')]
    checked = [(s, shot_issue(s, image_cache)) for s in taken]
    excluded = [{'path': s['path'],
                 **({'label': s['label']} if s.get('label') else
                    {'name': s.get('name') or Path(s['path']).name}),
                 'reason': issue} for s, issue in checked if issue]
    comparable = [s for s, issue in checked if not issue]

    def result(values):
        return {**values, **({'excluded': excluded} if excluded else {})}

    def unmeasured(note):
        if excluded:
            return result({'ok': True, 'unmeasured': True,
                           'note': f'change not measured: {len(excluded)} unusable still(s) excluded'})
        return {'ok': True, 'note': note}

    expected = [s for s in shots if s.get('phase') == 'after'] or [s for s in shots if s.get('phase') == 'step']
    if expected and not Path(expected[-1].get('path') or '').is_file():
        return result({'ok': False, 'reason': f'expected-result shot missing: {expected[-1]["name"]}'})
    before = next((s['path'] for s in comparable if s.get('phase') == 'before'), None) or frames.get('start')
    if not before:
        return unmeasured('no before-shot and no clip start frame: change not measured')
    later = [frames[k] for k in ('mid', 'end') if frames.get(k)] + \
        [s['path'] for s in comparable if s.get('phase') in ('step', 'after')]
    if not later:
        return unmeasured('nothing to compare with the before frame')
    diffs, unreadable = {}, []
    def preview(path):
        if not isinstance(path, (str, Path)):
            return path
        cached = image_cache.get(str(Path(path)))
        if cached and cached[0]:
            raise ValueError(cached[0])
        return cached[1] if cached and cached[1] is not None else path
    for p in later:
        try:
            diffs[str(p)] = round(frame_diff(preview(before), preview(p)), 2)
        except Exception:  # noqa: BLE001 - an unreadable still is listed, not a verdict
            unreadable.append(str(p))
    if not diffs:
        if excluded:
            return unmeasured('no readable frame pair: change not measured')
        return {'ok': True, 'note': 'no readable frame pair: change not measured', 'unreadable': unreadable}
    best = max(diffs.values())
    if best <= NO_EFFECT_DIFF:
        return result({'ok': False, 'reason': 'no-visible-effect', 'max_diff': best, 'diffs': diffs, 'before': before})
    return result({'ok': True, 'max_diff': best, 'before': before})


# ---- receipt merge -------------------------------------------------------------------------------------------------
def merge_receipt(receipt_path, update):
    """Read-modify-write the receipt under an exclusive lock; atomic replace."""
    receipt_path = Path(receipt_path)
    lock = receipt_path.with_suffix('.json.lock')
    with open(lock, 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
        update(receipt)
        tmp = receipt_path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(receipt, indent=2) + '\n')
        os.replace(tmp, receipt_path)
    return receipt


def known_issue(known, verdict):
    """{ids, titles, judge_reason} when a judge FAIL of this row belongs to a known product issue (plan known_issues,
    already narrowed to the row's families): only the issues whose reason regex (the issue's exact signature) matches
    the judge reason. No match → None: the row stays FAILED (switch-hw-session-2: the old fallback "else all" labelled
    a clean-state failure and a possible product finding KNOWN-PRODUCT-ISSUE)."""
    if not known or (verdict or {}).get('verdict') != 'FAIL':
        return None
    reason = verdict.get('reason') or ''
    ids = [k for k, v in known.items() if v.get('reason') and re.search(v['reason'], reason, re.I)]
    if not ids:
        return None
    return {'ids': ids, 'titles': [known[k]['title'] for k in ids], 'judge_reason': reason}


def _shot_blockers(shots, image_cache=None):
    result = []
    for shot in shots:
        issue = shot_issue(shot, image_cache)
        if issue:
            path = shot.get('path')
            source = shot.get('label') or shot.get('name') or (Path(path).name if path else 'still')
            result.append({'kind': 'evidence', 'source': source,
                           'text': f"still {path} is not usable evidence: {issue}"})
    return result


def evidence_blockers(shots, verdict, all_shots=None, image_cache=None):
    row_shots = [shot for shot in shots or [] if isinstance(shot, dict)
                 and shot.get('phase') in ('before', 'step', 'after')]
    available = [shot for shot in (all_shots or row_shots) if isinstance(shot, dict) and shot.get('path')]
    citations = (verdict or {}).get('shots_cited')
    citations = citations if isinstance(citations, list) else []
    matched = [shot for shot in row_shots if any((shot.get('path') and citation == shot['path'])
                                                  or (shot.get('label') and citation == shot['label'])
                                                  for citation in citations)]
    unresolved = any(not any(citation == shot.get('path') or citation == shot.get('label')
                             for shot in available) for citation in citations)
    if not citations or unresolved:
        expected = [shot for shot in row_shots if shot.get('phase') == 'after'] or \
            [shot for shot in row_shots if shot.get('phase') == 'step']
        before = next((shot for shot in row_shots if shot.get('phase') == 'before'), None)
        for shot in ([before] if before else []) + expected[-1:]:
            if shot not in matched:
                matched.append(shot)
    return _shot_blockers(matched, image_cache)


def retire_evidence(receipt, attempt=None):
    blockers = receipt.get('blockers') or []
    removed = [item for item in blockers if item.get('kind') == 'evidence']
    if removed:
        previous = attempt if isinstance(attempt, dict) else receipt.get('judge')
        previous = previous if isinstance(previous, dict) else {}
        receipt['blockers'] = [item for item in blockers if item.get('kind') != 'evidence']
        receipt.setdefault('evidence_attempts', []).append(
            {'verdict': previous.get('verdict'), 'landed': previous.get('landed'), 'blockers': removed})


def apply_verdict(receipt, check, verdict, frames, known=None, shots=None, all_shots=None, image_cache=None):
    retire_evidence(receipt)
    gates_status = receipt.get('gates_status', receipt.get('status'))
    status, reason, review = final_status(gates_status, receipt.get('effects_passed') or [], check, verdict)
    if gates_status != 'PASS':
        evidence = evidence_blockers(shots, verdict, all_shots, image_cache)
    elif check and not check.get('ok', True):
        compared = check.get('diffs') or {}
        evidence = _shot_blockers([shot for shot in shots or [] if isinstance(shot, dict)
                                   and shot.get('phase') in ('before', 'step', 'after') and shot.get('path')
                                   and (shot['path'] == check.get('before') or shot['path'] in compared)], image_cache)
    else:
        evidence = (evidence_blockers(shots, verdict, all_shots, image_cache)
                    if (verdict or {}).get('verdict') in ('PASS', 'FAIL') else [])
        if (verdict or {}).get('verdict') in ('PASS', 'FAIL') and (check or {}).get('unmeasured'):
            for shot in check.get('excluded') or []:
                item = {'kind': 'evidence', 'source': shot.get('label') or shot.get('name') or Path(shot['path']).name,
                        'text': f"still {shot['path']} is not usable evidence: {shot['reason']}"}
                if item not in evidence:
                    evidence.append(item)
    issue = (known_issue(known, verdict)
             if gates_status == 'PASS' and not evidence and status == 'FAILED' and (check or {}).get('ok', True)
             else None)
    if issue:  # not chased, not counted by the consecutive-failure stop; listed apart in FAILING.md
        status, reason, review = 'KNOWN-PRODUCT-ISSUE', f'known issue {",".join(issue["ids"])}: {issue["judge_reason"]}', False
        receipt['known_issue'] = issue
    landed = time.time()
    receipt['judge'] = {'state': 'done', **(verdict or {}), 'frames': frames, 'landed': landed}
    if evidence:
        blockers = list(receipt.get('blockers') or [])
        blockers.extend(item for item in evidence if item not in blockers)
        receipt['blockers'] = blockers
        if gates_status == 'PASS':
            receipt.pop('known_issue', None)
            status, reason, review = 'RAN-UNVERIFIED', blockers[0]['text'], True
            receipt['review_kind'] = 'unverified'
    if status == 'PASS' and 'blockers' in receipt:
        blockers = receipt['blockers'] or []
        visual = [blocker for blocker in blockers if blocker.get('kind') == 'visual']
        if visual:
            receipt['cleared'] = list(receipt.get('cleared') or []) + [
                {**blocker, 'by': 'judge', 'landed': landed} for blocker in visual]
        remaining = [blocker for blocker in blockers if blocker.get('kind') != 'visual']
        receipt['blockers'] = remaining
        if remaining:
            status, reason, review = 'RAN-UNVERIFIED', remaining[0]['text'], True
            receipt['review_kind'] = 'unverified'
        elif visual:
            receipt.pop('review_kind', None)
    receipt['clip_check'] = check
    receipt['status'] = status
    receipt['status_reason'] = reason
    if status == 'PASS':
        receipt.pop('review_kind', None)
    if review:
        receipt['review'] = True
    else:
        receipt.pop('review', None)
    return receipt


# ---- the worker ----------------------------------------------------------------------------------------------------
def archive_verdict(folder, name='verdict'):
    out = Path(folder) / f'{name}.json'
    if out.exists():
        attempt = 1
        while (Path(folder) / f'{name}.attempt-{attempt}.json').exists():
            attempt += 1
        out.rename(Path(folder) / f'{name}.attempt-{attempt}.json')


def mark_retry(receipt_path):
    try:
        current = json.loads(Path(receipt_path).read_text())
    except (OSError, ValueError):
        return
    judge = current.get('judge') if isinstance(current, dict) else None
    if not isinstance(judge, dict) or judge.get('state') != 'done':
        return

    def update(receipt):
        previous = receipt.get('judge')
        if not isinstance(previous, dict) or previous.get('state') != 'done':
            return
        retire_evidence(receipt, previous)
        receipt['judge'] = {'state': 'pending', 'retry': True,
                            'previous': {'verdict': previous.get('verdict'), 'landed': previous.get('landed')}}
        if receipt.get('gates_status') == 'PASS':
            receipt.update(status='RAN-UNVERIFIED', status_reason='judge pending (retry)', review=True)

    merge_receipt(receipt_path, update)


def run_judge(job_path, job):
    out = Path(job['dir']) / 'verdict.json'
    cmd = job.get('judge_cmd')
    script = Path(job['plan_dir']) / cmd if cmd else None
    if not script or not script.is_file():
        return {'verdict': 'UNSURE', 'reason': f'judge unavailable: {script} not found', 'shots_cited': [],
                'model': None}
    try:
        proc = subprocess.run([sys.executable, '-I', str(script), '--job', str(job_path), '--out', str(out),
                               *[str(a) for a in job.get('judge_args') or []]],
                              capture_output=True, text=True, timeout=job.get('timeout_s', JUDGE_TIMEOUT_S))
    except subprocess.TimeoutExpired:
        return {'verdict': 'UNSURE', 'reason': 'judge timed out', 'shots_cited': [], 'model': None}
    if proc.returncode:
        return {'verdict': 'UNSURE', 'reason': f'judge failed (exit {proc.returncode}): {(proc.stderr or "")[-300:]}',
                'shots_cited': [], 'model': None}
    try:
        verdict = json.loads(out.read_text())
    except (OSError, ValueError):
        return {'verdict': 'UNSURE', 'reason': f'judge gave no verdict (exit {proc.returncode}): '
                f'{(proc.stderr or "")[-300:]}', 'shots_cited': [], 'model': None}
    if not isinstance(verdict, dict):
        return {'verdict': 'UNSURE', 'reason': f'invalid verdict {verdict!r}', 'shots_cited': [], 'model': None}
    if verdict.get('verdict') not in ('PASS', 'FAIL', 'UNSURE'):
        verdict = {**verdict, 'verdict': 'UNSURE', 'reason': f'invalid verdict {verdict.get("verdict")!r}'}
    return verdict


def work(job_path):
    job_path = Path(job_path)
    job = json.loads(job_path.read_text())
    folder = Path(job['dir'])
    initial_shots = job.get('shots') or []
    job['shots'], job['judge_shots'], job['excluded_shots'] = [], [], []
    frames = {}
    image_cache = {}
    try:
        try:
            mark_retry(job['receipt'])
        finally:
            archive_verdict(folder)
            archive_verdict(folder, 'done')
        row_shots_path = folder / 'row-shots.json'
        snapshot_error = None
        if job.get('row_shots_saved'):
            try:
                row_shots = json.loads(row_shots_path.read_text())
                if not isinstance(row_shots, list):
                    raise ValueError('expected a list')
            except (OSError, ValueError) as error:
                snapshot_error = f'row shots snapshot unreadable: {error!r}'
        else:
            row_shots = initial_shots
            row_shots_path.write_text(json.dumps(row_shots, indent=2) + '\n')
            job['row_shots_saved'] = True
        if snapshot_error:
            check = {'ok': True, 'error': snapshot_error}
            verdict = {'verdict': 'UNSURE', 'reason': snapshot_error, 'shots_cited': [], 'model': None}
        else:
            frames_at, dense, frame_error = {}, {}, None
            if job.get('clip'):
                try:
                    receipt = json.loads(Path(job['receipt']).read_text()) if job.get('receipt') else {}
                    frames, frames_at = sample_frames(job['clip'], folder, clip_span_ms(receipt))
                    dense = sample_dense(job['clip'], folder, dense_positions(receipt))
                except Exception as error:  # noqa: BLE001 - an unreadable clip is recorded, the judge still gets the shots
                    frame_error = repr(error)[:300]
            # judge.py's job format: shots [{label, path}] (taken ones, in order), frames [start, mid, end] paths
            # the evidence shots first, then the clip frames as shots labelled "clip start/mid/end" (an overlay left on screen
            # at the clip's end, e.g. a Break the Targets HUD, is a FAIL: session-1 LEG-04)
            # evidence-only labels: the row's own still labels (row id prefix dropped); a label that would give the verdict
            # away (FAIL/PASS) is neutral (judge/make_jobs.py rule)
            def label(s, i):
                name = s['name'][len(job['row']) + 1:] if s['name'].startswith(job['row'] + '-') else s['name']
                return f'shot-{i}' if re.search(r'FAIL|PASS', name) else f'{s.get("phase", "step")}:{name}'
            job['shots'] = [{**s, 'label': label(s, i)} for i, s in enumerate(row_shots, 1)]
            checked = [(s, shot_issue(s, image_cache)) for s in job['shots']]
            job['excluded_shots'] = [
                {'label': s['label'], 'name': s.get('name'), 'phase': s.get('phase'), 'path': s.get('path'),
                 'reason': issue} for s, issue in checked if issue]
            clip_shots = \
                [{'label': f'clip {k}', 'path': frames[k], 'phase': 'clip',
                  **({'clip_ms': frames_at[k]} if k in frames_at else {})} for k in FRAME_NAMES if frames.get(k)] + \
                [{'label': f'dense {ms / 1000:.1f}s', 'path': p, 'phase': 'clip', 'clip_ms': ms}
                 for ms, p in sorted(dense.items())]
            checked_clip = [(shot, 'clip frame file is missing' if not Path(shot['path']).is_file()
                             else 'clip frame is not a decodable image'
                             if _cached_image_issue(shot['path'], image_cache) else None) for shot in clip_shots]
            for shot, issue in checked_clip:
                if issue:
                    job['excluded_shots'].append({'label': shot['label'], 'name': None, 'phase': 'clip',
                                                  'path': shot['path'], 'reason': issue})
            job['judge_shots'] = [s for s, issue in checked if not issue] + \
                [s for s, issue in checked_clip if not issue]
            available_clip_labels = {s['label'] for s, issue in checked_clip if not issue}
            frames = {k: p for k, p in frames.items() if f'clip {k}' in available_clip_labels}
            job['frames_by_name'] = frames
            job['frames_at_ms'] = frames_at  # where each clip frame was cut (clip time, ms): the judge times them from this
            job['frames'] = []
            if frame_error:
                job['frame_error'] = frame_error
            job_path.write_text(json.dumps({**job, 'shots': job['judge_shots']}, indent=2) + '\n')
            check = clip_check(job, frames, image_cache)
            image_cache = {path: (issue, None) for path, (issue, _) in image_cache.items()}
            if not check.get('ok', True):
                verdict = {'verdict': 'FAIL', 'reason': f'clip check: {check["reason"]}', 'shots_cited': [],
                           'model': 'clip-check'}
            elif not job['judge_shots']:
                reason = (f"{len(job['excluded_shots'])} unusable still(s) excluded" if job['excluded_shots']
                          else 'no row still and no clip frame'
                               + (f"; clip: {job['frame_error']}" if job.get('frame_error') else ''))
                verdict = {'verdict': 'UNSURE',
                           'reason': f'no usable image for the judge: {reason}',
                           'shots_cited': [], 'model': None}
            else:
                verdict = run_judge(job_path, job)
    except Exception as error:  # noqa: BLE001 - a worker error is UNSURE (RAN-UNVERIFIED), never a PASS
        image_cache = {path: (issue, None) for path, (issue, _) in image_cache.items()}
        check = {'ok': True, 'error': repr(error)[:300]}
        verdict = {'verdict': 'UNSURE', 'reason': f'judge worker error: {error!r}'[:300], 'shots_cited': [],
                   'model': None}
    def update(receipt):
        apply_verdict(receipt, check, verdict, frames, job.get('known_issues'), job['shots'], job['judge_shots'],
                      image_cache)
        if job['excluded_shots']:
            receipt['judge']['excluded_shots'] = job['excluded_shots']

    merge_receipt(job['receipt'], update)
    (folder / 'done.json').write_text(json.dumps(
        {'verdict': verdict, 'clip_check': check, 'excluded_shots': job['excluded_shots']}, indent=2) + '\n')
    return verdict


# ---- the runner side -----------------------------------------------------------------------------------------------
def write_job(out, row, receipt_path, plan_dir, judge_cfg, clip, known=None, still_records=None):
    """The job file of one row: what the judge sees (expected line, labelled full-size shots, clip frames to come)."""
    receipt = json.loads(Path(receipt_path).read_text())
    folder = Path(out) / 'judge' / row['id']
    folder.mkdir(parents=True, exist_ok=True)
    # shot files are <on>-<HHMMSS>Z-<us>-<name>.png (Runner.shot); a name repeated in a row maps in order
    taken = [item['shot'] for item in receipt.get('results') or [] if isinstance(item, dict) and item.get('shot')]
    shots, used = [], set()
    for spec in row.get('shots') or []:
        path = next((p for p in taken if p not in used and Path(p).stem.endswith('-' + spec['name'])), None)
        used.add(path)
        shot = {**spec, 'path': path}
        if path and still_records is not None:
            record = still_records.get(str(path))
            shot['fresh'] = ({key: value for key, value in record.items()
                              if isinstance(value, (str, int, float, bool, type(None)))}
                             if isinstance(record, dict) else None)
        shots.append(shot)
    job = {'row': row['id'], 'name': row.get('name'), 'expected': row.get('expected'),
           'verdict_hint': row.get('verdict_hint'), 'covers': row.get('covers', []), 'shots': shots,
           'clip': clip, 'frames_wanted': list(FRAME_NAMES), 'receipt': str(receipt_path), 'dir': str(folder),
           'notes': row.get('name'), 'judge_args': judge_cfg.get('args') or [],
           'plan_dir': str(plan_dir), 'judge_cmd': judge_cfg.get('cmd'), 'timeout_s': judge_cfg.get('timeout_s',
                                                                                                    JUDGE_TIMEOUT_S),
           'effects_passed': receipt.get('effects_passed') or [], 'queued': time.time(),
           'known_issues': {k: v for k, v in (known or {}).items() if row['id'] in v.get('rows', [])}}
    path = Path(out) / 'judge' / 'jobs' / f'{row["id"]}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, indent=2) + '\n')
    return path


def start_worker(job_path, log_path):
    """One detached worker process per job (the runner never waits on it)."""
    with open(log_path, 'a') as log:  # the child keeps its own copy of the descriptor
        return subprocess.Popen([sys.executable, '-I', str(HERE / 'judge_queue.py'), 'work', str(job_path)],
                                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=str(HERE))


# ---- the end-of-run report -----------------------------------------------------------------------------------------
def failing_report(out, plan, manifest_path):
    """FAILING.md + rerun.json: the FAILED and RAN-UNVERIFIED rows, their reasons and stills, and an --only rerun."""
    out = Path(out)
    lines = ['# FAILING: rows to look at after the run', '',
             f'Plan `{manifest_path}`; receipts in `{out}`. FAILED and RAN-UNVERIFIED rows (judge pending = no verdict '
             'landed before the report).', '', '| Row | Status | Reason | Stills |', '|---|---|---|---|']
    failing, pending, known = [], [], []
    for row in plan.get('rows', []):
        path = out / f'{row["id"]}.json'
        if not path.is_file():
            continue
        receipt = json.loads(path.read_text())
        status = receipt.get('status')
        if (receipt.get('judge') or {}).get('state') == 'pending':
            pending.append(row['id'])
        if status == 'KNOWN-PRODUCT-ISSUE':
            issue = receipt.get('known_issue') or {}
            known.append(f'| {row["id"]} | {",".join(issue.get("ids", []))} | '
                         f'{(issue.get("judge_reason") or "")[:300].replace("|", "/")} | `{receipt.get("clip", "")}` |')
            continue
        if status not in ('FAILED', 'RAN-UNVERIFIED'):
            continue
        failing.append(row['id'])
        reason = receipt.get('status_reason') or receipt.get('error') or ''
        judge = receipt.get('judge') or {}
        if judge.get('reason') and judge.get('reason') not in reason:
            reason += f' (judge: {judge["reason"]})'
        stills = [r['shot'] for r in receipt.get('results') or [] if isinstance(r, dict) and r.get('shot')][-3:]
        stills += [p for p in receipt.get('evidence_paths') or [] if str(p).endswith('.png')][:1]
        cited = judge.get('shots_cited') or []
        lines.append(f'| {row["id"]} | {status} | {reason[:300].replace("|", "/")} | '
                     f'{"<br>".join(f"`{s}`" for s in (cited or stills))} |')
    command = (f'{sys.executable} -I {HERE / "runner.py"} run {manifest_path} --instances <instances.json> '
               f'--out {out} --rows {",".join(failing)}')
    lines += ['', f'{len(failing)} rows. Judge pending: {pending or "none"}.', '', 'Rerun just these rows:', '',
              f'    {command}', '']
    if known:
        lines += ['## KNOWN-PRODUCT-ISSUE (not chased, not rerun; the fix lane has the clips)', '',
                  '| Row | Issue | Judge reason | Clip |', '|---|---|---|---|', *known, '']
    (out / 'FAILING.md').write_text('\n'.join(lines))
    (out / 'rerun.json').write_text(json.dumps({'rows': failing, 'judge_pending': pending, 'command': command},
                                               indent=2) + '\n')
    return failing


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == 'work':
        sys.path.insert(0, str(HERE))
        print(json.dumps(work(argv[1])))
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
