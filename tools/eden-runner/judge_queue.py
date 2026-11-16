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
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NO_EFFECT_DIFF = 3.0      # mean abs RGB diff (0-255, 320x180) below which two frames are "the same picture"
JUDGE_TIMEOUT_S = 600
FRAME_NAMES = ('start', 'mid', 'end')


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


def sample_frames(clip, folder):
    """{start, mid, end: png path} from the clip (full size); {} plus an error when the clip can't be read."""
    import cut_session
    folder.mkdir(parents=True, exist_ok=True)
    pipe = cut_session.Pipeline(clip)
    try:
        ok, duration = pipe.pipe.query_duration(pipe.Gst.Format.TIME)
        if not ok or duration <= 0:
            raise RuntimeError('clip has no duration')
        at = {'start': min(200_000_000, duration // 10), 'mid': duration // 2,
              'end': max(0, duration - 300_000_000)}
        out = {}
        for name in FRAME_NAMES:
            got = pipe.still(at[name])
            if got is None:
                continue
            path = folder / f'clip-{name}.png'
            got[1].save(path)
            out[name] = str(path)
        return out
    finally:
        pipe.close()


def clip_check(job, frames):
    """Deterministic: the expected-result shot exists, and the row changed something on screen."""
    shots = job.get('shots') or []
    taken = [s for s in shots if s.get('path') and Path(s['path']).is_file()]
    expected = [s for s in shots if s.get('phase') == 'after'] or [s for s in shots if s.get('phase') == 'step']
    if expected and not Path(expected[-1].get('path') or '').is_file():
        return {'ok': False, 'reason': f'expected-result shot missing: {expected[-1]["name"]}'}
    before = next((s['path'] for s in taken if s.get('phase') == 'before'), None) or frames.get('start')
    if not before:
        return {'ok': True, 'note': 'no before-shot and no clip start frame: change not measured'}
    later = [frames[k] for k in ('mid', 'end') if frames.get(k)] + \
        [s['path'] for s in taken if s.get('phase') in ('step', 'after')]
    if not later:
        return {'ok': True, 'note': 'nothing to compare with the before frame'}
    diffs, unreadable = {}, []
    for p in later:
        try:
            diffs[str(p)] = round(frame_diff(before, p), 2)
        except Exception:  # noqa: BLE001 - an unreadable still is listed, not a verdict
            unreadable.append(str(p))
    if not diffs:
        return {'ok': True, 'note': 'no readable frame pair: change not measured', 'unreadable': unreadable}
    best = max(diffs.values())
    if best <= NO_EFFECT_DIFF:
        return {'ok': False, 'reason': 'no-visible-effect', 'max_diff': best, 'diffs': diffs, 'before': before}
    return {'ok': True, 'max_diff': best, 'before': before}


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


def apply_verdict(receipt, check, verdict, frames):
    status, reason, review = final_status(receipt.get('gates_status', receipt.get('status')),
                                          receipt.get('effects_passed') or [], check, verdict)
    receipt['judge'] = {'state': 'done', **(verdict or {}), 'frames': frames, 'landed': time.time()}
    receipt['clip_check'] = check
    receipt['status'] = status
    receipt['status_reason'] = reason
    if review:
        receipt['review'] = True
    else:
        receipt.pop('review', None)
    return receipt


# ---- the worker ----------------------------------------------------------------------------------------------------
def run_judge(job_path, job):
    out = Path(job['dir']) / 'verdict.json'
    cmd = job.get('judge_cmd')
    script = Path(job['plan_dir']) / cmd if cmd else None
    if not script or not script.is_file():
        return {'verdict': 'UNSURE', 'reason': f'judge unavailable: {script} not found', 'shots_cited': [],
                'model': None}
    try:
        proc = subprocess.run([sys.executable, '-I', str(script), '--job', str(job_path), '--out', str(out)],
                              capture_output=True, text=True, timeout=job.get('timeout_s', JUDGE_TIMEOUT_S))
    except subprocess.TimeoutExpired:
        return {'verdict': 'UNSURE', 'reason': 'judge timed out', 'shots_cited': [], 'model': None}
    try:
        verdict = json.loads(out.read_text())
    except (OSError, ValueError):
        return {'verdict': 'UNSURE', 'reason': f'judge gave no verdict (exit {proc.returncode}): '
                f'{(proc.stderr or "")[-300:]}', 'shots_cited': [], 'model': None}
    if verdict.get('verdict') not in ('PASS', 'FAIL', 'UNSURE'):
        verdict = {**verdict, 'verdict': 'UNSURE', 'reason': f'invalid verdict {verdict.get("verdict")!r}'}
    return verdict


def work(job_path):
    job_path = Path(job_path)
    job = json.loads(job_path.read_text())
    folder = Path(job['dir'])
    frames, frame_error = {}, None
    if job.get('clip'):
        try:
            frames = sample_frames(job['clip'], folder)
        except Exception as error:  # noqa: BLE001 - an unreadable clip is recorded, the judge still gets the shots
            frame_error = repr(error)[:300]
    job['frames'] = frames
    if frame_error:
        job['frame_error'] = frame_error
    job_path.write_text(json.dumps(job, indent=2) + '\n')
    try:
        check = clip_check(job, frames)
        verdict = run_judge(job_path, job) if check.get('ok', True) else \
            {'verdict': 'FAIL', 'reason': f'clip check: {check["reason"]}', 'shots_cited': [], 'model': 'clip-check'}
    except Exception as error:  # noqa: BLE001 - a worker error is UNSURE (RAN-UNVERIFIED), never a PASS
        check = {'ok': True, 'error': repr(error)[:300]}
        verdict = {'verdict': 'UNSURE', 'reason': f'judge worker error: {error!r}'[:300], 'shots_cited': [],
                   'model': None}
    merge_receipt(job['receipt'], lambda r: apply_verdict(r, check, verdict, frames))
    (folder / 'done.json').write_text(json.dumps({'verdict': verdict, 'clip_check': check}, indent=2) + '\n')
    return verdict


# ---- the runner side -----------------------------------------------------------------------------------------------
def write_job(out, row, receipt_path, plan_dir, judge_cfg, clip):
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
        shots.append({**spec, 'path': path})
    job = {'row': row['id'], 'name': row.get('name'), 'expected': row.get('expected'),
           'verdict_hint': row.get('verdict_hint'), 'covers': row.get('covers', []), 'shots': shots,
           'clip': clip, 'frames_wanted': list(FRAME_NAMES), 'receipt': str(receipt_path), 'dir': str(folder),
           'plan_dir': str(plan_dir), 'judge_cmd': judge_cfg.get('cmd'), 'timeout_s': judge_cfg.get('timeout_s',
                                                                                                    JUDGE_TIMEOUT_S),
           'effects_passed': receipt.get('effects_passed') or [], 'queued': time.time()}
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
    failing, pending = [], []
    for row in plan.get('rows', []):
        path = out / f'{row["id"]}.json'
        if not path.is_file():
            continue
        receipt = json.loads(path.read_text())
        status = receipt.get('status')
        if (receipt.get('judge') or {}).get('state') == 'pending':
            pending.append(row['id'])
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
