"""Checked effects, the async judge, the clip check and the consecutive-failure stop rule (judge_queue.py + runner)."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness  # noqa: E402
import judge_queue as jq  # noqa: E402
import runner  # noqa: E402

HOOK = '''
def ok(path, gate):
    return {"ok": True}
'''
# A stand-in judge: the verdict word is in the row's "expected" line.
JUDGE = '''
import argparse, json
ap = argparse.ArgumentParser(); ap.add_argument("--job"); ap.add_argument("--out"); a = ap.parse_args()
job = json.load(open(a.job))
word = next(w for w in ("PASS", "FAIL", "UNSURE") if w in (job.get("expected") or "UNSURE"))
json.dump({"verdict": word, "reason": "stand-in judge: " + word, "shots_cited": [s["path"] for s in job["shots"]],
           "model": "stand-in", "saw_expected": job["expected"]}, open(a.out, "w"))
'''

INPUT_JUDGE = '''
import argparse, json
from pathlib import Path
from PIL import Image
ap = argparse.ArgumentParser(); ap.add_argument("--job"); ap.add_argument("--out"); a = ap.parse_args()
job = json.loads(Path(a.job).read_text())
Path(job["plan_dir"], "judge-ran.json").write_text(json.dumps([s["label"] for s in job["shots"]]))
mode = job["expected"]
if mode == "PERSIST":
    bright = sum(Image.open(s["path"]).convert("RGB").getpixel((0, 0))[0] >= 128 for s in job["shots"])
    word, cited = ("PASS" if bright >= 2 else "UNSURE"), ["step:bright-step"]
else:
    word = "PASS"
    cited = [] if mode == "NO-CITE" else ["not-an-image"] if mode == "UNRESOLVED" else [mode]
json.dump({"verdict": word, "reason": "stand-in input judge", "shots_cited": cited, "model": "stand-in"},
          open(a.out, "w"))
'''


class PngClient:
    """Fake backend: per-row OBS files; screenshots are real PNGs, coloured by shot name ("same" = one grey)."""

    def __init__(self, folder):
        self.folder, self.n, self.cur, self.calls = folder, 0, None, []

    def new_file(self):
        self.n += 1
        path = self.folder / f'rec-{self.n}.mp4'
        path.write_bytes(b'not an mp4')
        return str(path)

    def request(self, method, params=None):
        from PIL import Image
        params = params or {}
        self.calls.append(method)
        if method == 'recording.session_start':
            self.cur = self.new_file()
            return {'ok': True, 'result': {'started_wall': 1.0, 'path': self.cur}}
        if method == 'recording.session_ensure':
            return {'ok': True, 'result': {'active_before': True, 'restarted': False, 'path': self.cur}}
        if method == 'recording.session_split':
            prev, self.cur = self.cur, self.new_file()
            return {'ok': True, 'result': {'active': True, 'rec_ms': 1000, 'wall': 1.0, 'split_ok': True,
                                           'new_path': self.cur, 'prev_path': prev, 'file_rec_ms': 0,
                                           'closed': [{'path': prev, 'finalized': True}], 'late_paths': []}}
        if method in ('recording.session_mark',):
            return {'ok': True, 'result': {'active': True, 'rec_ms': 1000, 'wall': 1.0}}
        if method == 'recording.session_stop':
            return {'ok': True, 'result': {'path': self.cur, 'finalized': True, 'late_paths': []}}
        if method == 'ui.tree':
            return {'ok': True, 'result': [{'id': 'w', 'class': 'GRenderWindow'}]}
        if method == 'status':
            return {'ok': True, 'result': {'frame': 321, 'state': 'running', 'nested': {'ignored': True}}}
        if method == 'ui.screenshot':
            name = Path(params['path']).stem.split('-', 3)[-1]
            colour = (90, 90, 90) if 'same' in name else tuple(hashlib.md5(name.encode()).digest()[:3])
            Image.new('RGB', (128, 72), colour).save(params['path'])
        return {'ok': True, 'result': {}}


def make_plan(folder, rows, **plan_keys):
    (folder / 'hooks').mkdir(parents=True, exist_ok=True)
    (folder / 'hooks' / 'ok.py').write_text(HOOK)
    (folder / 'judge.py').write_text(JUDGE)
    gates = {'start': {'kind': 'hook', 'module': 'hooks/ok.py', 'function': 'ok', 'cap': 2},
             'nav': {'kind': 'hook', 'module': 'hooks/ok.py', 'function': 'ok', 'cap': 2},
             'fx': {'kind': 'hook', 'module': 'hooks/ok.py', 'function': 'ok', 'cap': 2, 'effect': True},
             'never': {'kind': 'log', 'source': 'console_log', 'pattern': 'never', 'cap': 0.3}}
    plan = {'start': 'start', 'gates': gates, 'recover': [], 'rows': [], **plan_keys}
    for row in rows:
        plan['rows'].append({'start': 'start', 'end': 'start', **row})
    (folder / 'rows.json').write_text(json.dumps(plan))
    log = folder / 'console.log'
    log.write_text('')
    return runner.Runner(folder / 'rows.json', {'sw': {'socket': 'x', 'console_log': str(log)}}, folder / 'out', 15,
                         client_factory=lambda s: PngClient(folder))


def receipt(folder, row):
    return json.loads((folder / 'out' / f'{row}.json').read_text())


class WorkerJudgeInput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.workdir = self.folder / 'judge'
        self.workdir.mkdir()
        self.job_path = self.folder / 'job.json'
        self.receipt_path = self.folder / 'receipt.json'
        (self.folder / 'judge.py').write_text(INPUT_JUDGE)

    def shot(self, name, phase, level, fresh):
        from PIL import Image
        path = self.folder / f'{name}.png'
        Image.new('RGB', (32, 18), (level, level, level)).save(path)
        shot = {'name': name, 'phase': phase, 'path': str(path)}
        if fresh is not None:
            shot['fresh'] = fresh
        return shot

    def run_worker(self, shots, expected, frames=None, dense=None):
        marker = self.folder / 'judge-ran.json'
        marker.unlink(missing_ok=True)
        self.receipt_path.write_text(json.dumps({'gates_status': 'PASS', 'status': 'RAN-UNVERIFIED',
                                                 'blockers': []}))
        job = {'row': 'a', 'shots': shots, 'expected': expected, 'clip': 'fake.mp4' if frames is not None else None,
               'dir': str(self.workdir), 'receipt': str(self.receipt_path),
               'plan_dir': str(self.folder), 'judge_cmd': 'judge.py'}
        self.job_path.write_text(json.dumps(job))
        with mock.patch.object(jq, 'sample_frames', return_value=(frames or {}, {'start': 0, 'end': 2000})), \
             mock.patch.object(jq, 'sample_dense', return_value=dense or {}):
            verdict = jq.work(self.job_path)
        return (json.loads(self.job_path.read_text()), json.loads(self.receipt_path.read_text()),
                json.loads((self.workdir / 'done.json').read_text()), verdict, marker)

    def test_persistence_judge_does_not_see_refused_after(self):
        good = {'status': 'fresh'}
        refused = {'status': 'refused', 'reason': 'unchanged across 200 game frames'}
        shots = [self.shot('base', 'before', 20, good), self.shot('bright-step', 'step', 220, good),
                 self.shot('bright-after', 'after', 220, refused)]
        job, result, done, verdict, marker = self.run_worker(shots, 'PERSIST')
        self.assertEqual((verdict['verdict'], result['status']), ('UNSURE', 'RAN-UNVERIFIED'))
        self.assertTrue(marker.is_file())
        self.assertEqual(json.loads(marker.read_text()), ['before:base', 'step:bright-step'])
        self.assertEqual([s['path'] for s in job['shots']], [shots[0]['path'], shots[1]['path']])
        excluded = [{'label': 'after:bright-after', 'name': 'bright-after', 'phase': 'after',
                     'path': shots[2]['path'], 'reason': refused['reason']}]
        self.assertEqual(job.get('excluded_shots'), excluded)
        self.assertEqual(done.get('excluded_shots'), excluded)
        self.assertEqual(result['judge'].get('excluded_shots'), excluded)

        shots[2]['fresh'] = good
        job, result, done, verdict, marker = self.run_worker(shots, 'PERSIST')
        self.assertEqual((verdict['verdict'], result['status']), ('PASS', 'PASS'))
        self.assertEqual(json.loads(marker.read_text()), ['before:base', 'step:bright-step', 'after:bright-after'])
        self.assertEqual(len(job['shots']), 3)
        self.assertEqual(job.get('excluded_shots'), [])
        self.assertEqual(done.get('excluded_shots'), [])
        self.assertNotIn('excluded_shots', result['judge'])

    def test_all_unusable_stills_skip_judge_and_are_audited(self):
        shots = [self.shot('base', 'before', 20, {'status': 'error', 'reason': 'journal failed'}),
                 self.shot('after', 'after', 220, None)]
        job, result, done, verdict, marker = self.run_worker(shots, 'PERSIST')
        self.assertFalse(marker.exists())
        self.assertEqual(verdict, {'verdict': 'UNSURE',
                                   'reason': 'no usable image for the judge: 2 unusable still(s) excluded',
                                   'shots_cited': [], 'model': None})
        self.assertEqual((result['status'], result['review']), ('RAN-UNVERIFIED', True))
        self.assertEqual(job['shots'], [])
        excluded = [{'label': 'before:base', 'name': 'base', 'phase': 'before',
                     'path': shots[0]['path'], 'reason': 'journal failed'},
                    {'label': 'after:after', 'name': 'after', 'phase': 'after',
                     'path': shots[1]['path'], 'reason': 'no freshness record'}]
        self.assertEqual(job.get('excluded_shots'), excluded)
        self.assertEqual(done.get('excluded_shots'), excluded)
        self.assertEqual(result['judge'].get('excluded_shots'), excluded)

    def test_unrelated_excluded_diagnostic_preserves_pass_and_labels(self):
        good = {'status': 'fresh'}
        shots = [self.shot('base', 'before', 20, good), self.shot('diagnostic', 'step', 200, good),
                 self.shot('result', 'after', 220, good)]
        full, result, _, _, marker = self.run_worker(shots, 'after:result')
        self.assertEqual(result['status'], 'PASS')
        full_labels = json.loads(marker.read_text())
        self.assertEqual(full_labels, ['before:base', 'step:diagnostic', 'after:result'])
        shots[1]['fresh'] = {'status': 'error', 'reason': 'diagnostic journal failed'}
        job, result, done, _, marker = self.run_worker(shots, 'after:result')
        self.assertEqual((result['status'], result['blockers']), ('PASS', []))
        self.assertEqual(json.loads(marker.read_text()), [full_labels[0], full_labels[2]])
        self.assertEqual(job.get('excluded_shots'), [
            {'label': full_labels[1], 'name': 'diagnostic', 'phase': 'step', 'path': shots[1]['path'],
             'reason': 'diagnostic journal failed'}])
        self.assertEqual(done.get('excluded_shots'), job['excluded_shots'])
        self.assertEqual(result['judge'].get('excluded_shots'), job['excluded_shots'])

    def test_clip_only_and_no_images_without_exclusions_keep_running_judge(self):
        start = self.shot('clip-start', 'clip', 20, None)['path']
        end = self.shot('clip-end', 'clip', 220, None)['path']
        dense = self.shot('clip-dense', 'clip', 120, None)['path']
        job, result, done, _, marker = self.run_worker([], 'clip end',
                                                       frames={'start': start, 'end': end}, dense={1000: dense})
        self.assertEqual((result['status'], json.loads(marker.read_text())),
                         ('PASS', ['clip start', 'clip end', 'dense 1.0s']))
        self.assertEqual(job.get('excluded_shots'), [])
        self.assertEqual(done.get('excluded_shots'), [])
        self.assertNotIn('excluded_shots', result['judge'])
        job, result, _, _, marker = self.run_worker([], 'NO-CITE')
        self.assertEqual((result['status'], json.loads(marker.read_text())), ('PASS', []))
        self.assertEqual(job['shots'], [])

    def test_uncited_or_unresolved_verdict_still_checks_expected_after(self):
        good = {'status': 'fresh'}
        shots = [self.shot('base', 'before', 20, good), self.shot('bright-step', 'step', 220, good),
                 self.shot('result', 'after', 220, {'status': 'refused', 'reason': 'frozen'})]
        for mode in ('NO-CITE', 'UNRESOLVED'):
            with self.subTest(mode=mode):
                job, result, _, verdict, marker = self.run_worker(shots, mode)
                self.assertTrue(marker.is_file())
                self.assertEqual((verdict['verdict'], result['status']), ('PASS', 'RAN-UNVERIFIED'))
                self.assertEqual(result['blockers'], [
                    {'kind': 'evidence', 'source': 'after:result',
                     'text': f"still {shots[2]['path']} is not usable evidence: frozen"}])
                self.assertEqual(job.get('excluded_shots', [{}])[0].get('label'), 'after:result')

    def test_missing_path_and_phaseless_row_shots_are_audited(self):
        good = {'status': 'fresh'}
        diagnostic = self.shot('diagnostic', 'step', 100, {'status': 'error', 'reason': 'bad'})
        diagnostic.pop('phase')
        shots = [self.shot('base', 'before', 20, good), diagnostic,
                 self.shot('result', 'after', 220, good)]
        job, result, _, _, _ = self.run_worker(shots, 'after:result')
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(job.get('excluded_shots'), [
            {'label': 'step:diagnostic', 'name': 'diagnostic', 'phase': None,
             'path': diagnostic['path'], 'reason': 'bad'}])
        missing = [{'name': 'missing', 'phase': 'before', 'path': None}]
        job, result, _, verdict, marker = self.run_worker(missing, 'NO-CITE')
        self.assertFalse(marker.exists())
        self.assertEqual((verdict['verdict'], result['status']), ('UNSURE', 'RAN-UNVERIFIED'))
        self.assertEqual(job.get('excluded_shots'), [
            {'label': 'before:missing', 'name': 'missing', 'phase': 'before', 'path': None,
             'reason': 'still was not taken'}])

    def test_saved_job_replay_preserves_exclusions_in_fresh_worker_processes(self):
        shots = [self.shot('base', 'before', 20, {'status': 'error', 'reason': 'journal failed'}),
                 self.shot('after', 'after', 220, None)]
        self.receipt_path.write_text(json.dumps({'gates_status': 'PASS', 'status': 'RAN-UNVERIFIED',
                                                 'blockers': []}))
        self.job_path.write_text(json.dumps({
            'row': 'a', 'shots': shots, 'expected': 'NO-CITE', 'clip': None, 'dir': str(self.workdir),
            'receipt': str(self.receipt_path), 'plan_dir': str(self.folder), 'judge_cmd': 'judge.py'}))
        marker = self.folder / 'judge-ran.json'
        exclusions = None
        for _ in range(2):
            worker = subprocess.run([sys.executable, '-I', '-B', str(HERE / 'judge_queue.py'), 'work',
                                     str(self.job_path)], capture_output=True, text=True, timeout=30)
            self.assertEqual(worker.returncode, 0, worker.stderr)
            verdict = json.loads(worker.stdout)
            job = json.loads(self.job_path.read_text())
            result = json.loads(self.receipt_path.read_text())
            done = json.loads((self.workdir / 'done.json').read_text())
            self.assertFalse(marker.exists())
            self.assertEqual((verdict['verdict'], result['status']), ('UNSURE', 'RAN-UNVERIFIED'))
            self.assertEqual(verdict['reason'], 'no usable image for the judge: 2 unusable still(s) excluded')
            if exclusions is None:
                exclusions = job.get('excluded_shots')
                self.assertEqual(len(exclusions), 2)
            self.assertEqual(job.get('excluded_shots'), exclusions)
            self.assertEqual(done.get('excluded_shots'), exclusions)
            self.assertEqual(result['judge'].get('excluded_shots'), exclusions)
            self.assertEqual(json.loads((self.workdir / 'row-shots.json').read_text()), shots)


class StatusRules(unittest.TestCase):
    def test_freshness_selection_uses_citations_or_before_and_expected_fallback(self):
        select = getattr(jq, 'evidence_blockers', None)
        self.assertTrue(callable(select))
        good = {'status': 'unverified', 'reason': 'no game frame source'}
        bad = {'status': 'error', 'reason': 'journal failed'}
        shots = [{'phase': 'before', 'path': '/before.png', 'label': 'before:base', 'fresh': bad},
                 {'phase': 'step', 'path': '/diagnostic.png', 'label': 'step:diagnostic', 'fresh': bad},
                 {'phase': 'after', 'path': '/after.png', 'label': 'after:result', 'fresh': good}]
        for cited, expected in ((['/after.png'], []), (['step:diagnostic'], ['step:diagnostic']),
                                ([], ['before:base']), (['not-a-shot'], ['before:base']),
                                (['/after.png', 'not-a-shot'], ['before:base'])):
            with self.subTest(cited=cited):
                blockers = select(shots, {'verdict': 'PASS', 'shots_cited': cited})
                self.assertEqual([item['source'] for item in blockers], expected)
        old = [{key: value for key, value in shot.items() if key != 'fresh'} for shot in shots]
        missing = select(old, {'verdict': 'PASS', 'shots_cited': []})
        self.assertEqual([item['source'] for item in missing], ['before:base', 'after:result'])
        self.assertTrue(all('no freshness record' in item['text'] for item in missing))
        mixed = [shots[0], {key: value for key, value in shots[-1].items() if key != 'fresh'}]
        blockers = select(mixed, {'verdict': 'PASS', 'shots_cited': ['/after.png']})
        self.assertEqual(len(blockers), 1)
        self.assertIn('no freshness record', blockers[0]['text'])
        mixed[-1]['fresh'] = None
        self.assertIn('no freshness record', select(mixed, {'shots_cited': ['/after.png']})[0]['text'])
        shots[-1]['fresh'] = bad
        fallback = select(shots, {'shots_cited': []})
        self.assertEqual(fallback[0]['text'], 'still /before.png is not usable evidence: journal failed')
        self.assertEqual([item['source'] for item in fallback],
                         ['before:base', 'after:result'])
        self.assertEqual([item['source'] for item in select(shots[:2], {'shots_cited': []})],
                         ['before:base', 'step:diagnostic'])

    def test_failed_gate_remains_authoritative_with_bad_verdict_stills(self):
        shot = {'name': 'before', 'phase': 'before', 'path': '/before.png', 'label': 'before:base',
                'fresh': {'status': 'error', 'reason': 'journal failed'}}
        for word in ('PASS', 'FAIL', 'UNSURE'):
            with self.subTest(verdict=word):
                receipt = {'gates_status': 'FAILED', 'status': 'RAN-UNVERIFIED', 'review_kind': 'existing',
                           'blockers': [{'kind': 'effect', 'source': 'gate', 'text': 'gate evidence'}]}
                verdict = {'verdict': word, 'reason': 'known failure', 'shots_cited': ['/before.png']}
                known = {'known': {'title': 'known', 'reason': 'known failure'}}
                jq.apply_verdict(receipt, {'ok': True}, verdict, {}, known=known, shots=[shot])
                self.assertEqual((receipt['status'], receipt['status_reason']),
                                 ('FAILED', 'deterministic gate failed'))
                self.assertNotIn('review', receipt)
                self.assertEqual(receipt['review_kind'], 'existing')
                self.assertEqual(receipt['judge']['verdict'], word)
                self.assertNotIn('known_issue', receipt)
                self.assertEqual(receipt['blockers'], [
                    {'kind': 'effect', 'source': 'gate', 'text': 'gate evidence'},
                    {'kind': 'evidence', 'source': 'before:base',
                     'text': 'still /before.png is not usable evidence: journal failed'}])
                if word == 'FAIL':
                    clean = {'gates_status': 'FAILED', 'status': 'RAN-UNVERIFIED'}
                    usable = {**shot, 'fresh': {'status': 'unverified', 'reason': 'no game frame source'}}
                    jq.apply_verdict(clean, {'ok': True}, verdict, {}, known=known, shots=[usable])
                    self.assertEqual((clean['status'], clean['status_reason']),
                                     ('FAILED', 'deterministic gate failed'))
                    self.assertNotIn('known_issue', clean)

    def test_failed_clip_only_checks_freshness_of_compared_stills(self):
        good = {'status': 'unverified', 'reason': 'no game frame source'}
        refused = {'status': 'refused', 'reason': 'unchanged across 200 game frames'}
        error = {'status': 'error', 'reason': 'journal failed'}
        cases = (
            ('missing expected', error, refused,
             {'ok': False, 'reason': 'expected-result shot missing: after'}, 'FAILED', []),
            ('all usable', good, good,
             {'ok': False, 'reason': 'no-visible-effect', 'before': '/before.png',
              'diffs': {'/after.png': 0}}, 'FAILED', []),
            ('refused after', good, refused,
             {'ok': False, 'reason': 'no-visible-effect', 'before': '/before.png',
              'diffs': {'/after.png': 0}}, 'RAN-UNVERIFIED', ['after:result']),
            ('error before', error, good,
             {'ok': False, 'reason': 'no-visible-effect', 'before': '/before.png',
              'diffs': {'/after.png': 0}}, 'RAN-UNVERIFIED', ['before:base']),
            ('both unusable', error, refused,
             {'ok': False, 'reason': 'no-visible-effect', 'before': '/before.png',
              'diffs': {'/after.png': 0}}, 'RAN-UNVERIFIED', ['before:base', 'after:result']),
            ('clip frames only', error, refused,
             {'ok': False, 'reason': 'no-visible-effect', 'before': '/clip-start.png',
              'diffs': {'/clip-end.png': 0}}, 'FAILED', []),
        )
        for name, before, after, check, status, sources in cases:
            with self.subTest(case=name):
                shots = [{'name': 'before', 'phase': 'before', 'path': '/before.png',
                          'label': 'before:base', 'fresh': before},
                         {'name': 'diagnostic', 'phase': 'step', 'path': '/diagnostic.png',
                          'label': 'step:diagnostic', 'fresh': refused},
                         {'name': 'after', 'phase': 'after', 'path': '/after.png',
                          'label': 'after:result', 'fresh': after}]
                receipt = {'gates_status': 'PASS', 'status': 'RAN-UNVERIFIED', 'blockers': []}
                verdict = {'verdict': 'FAIL', 'reason': 'clip check: ' + check['reason'], 'shots_cited': []}
                jq.apply_verdict(receipt, check, verdict, {}, shots=shots)
                self.assertEqual(receipt['status'], status)
                self.assertEqual(receipt['judge']['verdict'], 'FAIL')
                self.assertEqual([blocker['source'] for blocker in receipt['blockers']], sources)
                if sources:
                    self.assertEqual((receipt['review'], receipt['review_kind']), (True, 'unverified'))
                    self.assertEqual(receipt['status_reason'], receipt['blockers'][0]['text'])
                else:
                    self.assertEqual(receipt['status_reason'], check['reason'])
                    self.assertNotIn('review', receipt)

    def test_real_clip_check_failure_uses_compared_still_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            before, after = Path(tmp) / 'before.png', Path(tmp) / 'after.png'
            before.write_bytes(b'before')
            after.write_bytes(b'after')
            shots = [{'name': 'before', 'phase': 'before', 'path': str(before), 'label': 'before:base',
                      'fresh': {'status': 'same'}},
                     {'name': 'after', 'phase': 'after', 'path': str(after), 'label': 'after:result',
                      'fresh': {'status': 'same'}}]
            with mock.patch.object(jq, 'frame_diff', return_value=0):
                check = jq.clip_check({'shots': shots}, {})
            self.assertEqual((check['reason'], check['before'], set(check['diffs'])),
                             ('no-visible-effect', str(before), {str(after)}))
            shots[0]['fresh'] = {'status': 'error', 'reason': 'journal failed'}
            receipt = {'gates_status': 'PASS', 'status': 'RAN-UNVERIFIED', 'blockers': []}
            jq.apply_verdict(receipt, check, {'verdict': 'FAIL', 'shots_cited': []}, {}, shots=shots)
            self.assertEqual((receipt['status'], receipt['review_kind']), ('RAN-UNVERIFIED', 'unverified'))
            self.assertEqual([item['source'] for item in receipt['blockers']], ['before:base'])

    def test_clip_only_job_without_row_stills_keeps_verdict(self):
        clip = {'phase': 'clip', 'path': '/clip.png', 'label': 'clip end'}
        for word, status in (('PASS', 'PASS'), ('FAIL', 'FAILED')):
            with self.subTest(verdict=word):
                receipt = {'gates_status': 'PASS', 'status': 'RAN-UNVERIFIED', 'blockers': []}
                verdict = {'verdict': word, 'reason': 'visible', 'shots_cited': ['clip end']}
                jq.apply_verdict(receipt, {'ok': True}, verdict, {}, shots=[], all_shots=[clip])
                self.assertEqual(receipt['status'], status)
                self.assertEqual(receipt['judge']['verdict'], word)
                self.assertEqual(receipt['blockers'], [])

    def test_clip_frames_use_the_rows_real_length(self):
        # 2026-11-18: GStreamer reported CPU-F001's per-row file as 8.3 s against an 11.4 s row, so "clip end" fell
        # before the effect stills; the positions now come from the receipt's span in the file's own time
        self.assertEqual(jq.clip_span_ms({'clip_rec_ms_base': 49, 'rec_ms': [49, 11433]}), 11384)
        self.assertIsNone(jq.clip_span_ms({'rec_ms': [49, 11433]}))
        self.assertIsNone(jq.clip_span_ms({'clip_rec_ms_base': 49, 'rec_ms': [49, None]}))
        self.assertEqual(jq.frame_positions_ns(11_384_000_000),
                         {'start': 200_000_000, 'mid': 5_692_000_000, 'end': 11_084_000_000})
        self.assertEqual(jq.frame_positions_ns(1_000_000_000)['start'], 100_000_000)

    def test_dense_frames_fill_the_longest_gameplay_gaps_between_stills(self):
        results = [{'shot': 'a', 'rec_ms': 1049}, {'play': 'op-lra'}, {'shot': 'b', 'rec_ms': 3049},
                   {'shot': 'c', 'rec_ms': 3349}, {'run': 'hooks/x.py:nav'}, {'shot': 'd', 'rec_ms': 9049},
                   {'shot': 'e', 'rec_ms': 10049}, {'shot': 'f', 'rec_ms': 13049}]
        r = {'clip_rec_ms_base': 49, 'results': results}
        # a->b 2000 (play only), b->c 300 (too short), c->d menu (skipped), d->e 1000, e->f 3000
        self.assertEqual(jq.dense_positions(r), [2000, 9500, 11500])
        self.assertEqual(jq.dense_positions(r, n=1), [11500])
        self.assertEqual(jq.dense_positions({'results': results}), [])

    def test_final_status(self):
        self.assertEqual(jq.final_status('FAILED', [], None, {'verdict': 'PASS'})[0], 'FAILED')
        self.assertEqual(jq.final_status('PASS', [], {'ok': False, 'reason': 'no-visible-effect'},
                                         {'verdict': 'PASS'})[:2], ('FAILED', 'no-visible-effect'))
        self.assertEqual(jq.final_status('PASS', [], {'ok': True}, {'verdict': 'PASS'})[0], 'PASS')
        self.assertEqual(jq.final_status('PASS', [], {'ok': True}, {'verdict': 'FAIL'})[0], 'FAILED')
        status, reason, review = jq.final_status('PASS', ['g'], {'ok': True}, {'verdict': 'FAIL', 'reason': 'x'})
        self.assertEqual((status, review), ('RAN-UNVERIFIED', True))
        self.assertIn('disagreement', reason)
        self.assertEqual(jq.final_status('PASS', ['g'], {'ok': True}, {'verdict': 'UNSURE'})[0], 'RAN-UNVERIFIED')
        self.assertEqual(jq.final_status('PASS', [], {'ok': True}, None)[0], 'RAN-UNVERIFIED')
        self.assertEqual(jq.gates_only_status([], True)[0], 'RAN-UNVERIFIED')
        self.assertEqual(jq.gates_only_status(['g'], True)[0], 'PASS')
        self.assertEqual(jq.gates_only_status([], False)[0], 'PASS')  # old plans keep their meaning

    def test_judge_verdict_keeps_legacy_receipts_without_blockers(self):
        old = {'gates_status': 'PASS', 'effects_passed': [], 'status': 'RAN-UNVERIFIED'}
        result = jq.apply_verdict(old, {'ok': True}, {'verdict': 'PASS', 'reason': 'visible'}, {})
        self.assertEqual((result['status'], result['status_reason']), ('PASS', 'visible'))
        self.assertNotIn('blockers', result)
        self.assertNotIn('cleared', result)
        self.assertNotIn('review', result)

    def test_clip_check_no_visible_effect_and_missing_shot(self):
        from PIL import Image
        d = Path(tempfile.mkdtemp())
        for name, colour in (('a', (90, 90, 90)), ('b', (91, 90, 90)), ('c', (200, 30, 30))):
            Image.new('RGB', (64, 36), colour).save(d / f'{name}.png')
        good = {'status': 'unverified', 'reason': 'no game frame source'}
        same = {'shots': [{'name': 'x-a', 'phase': 'before', 'path': str(d / 'a.png'), 'fresh': good},
                          {'name': 'x-b', 'phase': 'after', 'path': str(d / 'b.png'), 'fresh': good}]}
        self.assertEqual(jq.clip_check(same, {})['reason'], 'no-visible-effect')
        changed = {'shots': [same['shots'][0], {'name': 'x-c', 'phase': 'after', 'path': str(d / 'c.png'),
                                               'fresh': good}]}
        self.assertTrue(jq.clip_check(changed, {})['ok'])
        missing = {'shots': [same['shots'][0], {'name': 'x-d', 'phase': 'after', 'path': None}]}
        self.assertIn('expected-result shot missing', jq.clip_check(missing, {})['reason'])
        # the clip frames count too: start vs end
        self.assertEqual(jq.clip_check({'shots': []}, {'start': str(d / 'a.png'), 'end': str(d / 'b.png')})['reason'],
                         'no-visible-effect')

    def test_clip_check_excludes_refused_step_from_visible_change(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            paths = {name: str(Path(tmp) / f'{name}.png') for name in ('before', 'step', 'after')}
            for name, level in (('before', 20), ('step', 220), ('after', 20)):
                Image.new('RGB', (32, 18), (level, level, level)).save(paths[name])
            good = {'status': 'fresh'}
            refused = {'status': 'refused', 'reason': 'unchanged across 200 game frames'}
            for step_record, expected in ((refused, 'FAILED'), (good, 'PASS')):
                with self.subTest(step=step_record['status']):
                    shots = [{'name': name, 'phase': phase, 'path': paths[name], 'label': f'{phase}:{name}',
                              'fresh': step_record if name == 'step' else good}
                             for name, phase in (('before', 'before'), ('step', 'step'), ('after', 'after'))]
                    check = jq.clip_check({'shots': shots}, {})
                    verdict = {'verdict': 'PASS', 'reason': 'visible', 'shots_cited': [paths['after']]}
                    receipt = {'gates_status': 'PASS', 'status': 'RAN-UNVERIFIED', 'blockers': []}
                    jq.apply_verdict(receipt, check, verdict, {}, shots=shots)
                    self.assertEqual(receipt['status'], expected)
                    if step_record is refused:
                        self.assertEqual((check['ok'], check['reason'], check['max_diff']),
                                         (False, 'no-visible-effect', 0.0))
                        self.assertEqual(check['excluded'], [{'path': paths['step'], 'label': 'step:step',
                                                             'reason': refused['reason']}])
                        self.assertEqual(receipt['status_reason'], 'no-visible-effect')
                        self.assertEqual(receipt['blockers'], [])
                    else:
                        self.assertTrue(check['ok'])
                        self.assertEqual(check['max_diff'], 200.0)
                        self.assertNotIn('excluded', check)

    def test_clip_check_unmeasured_blocks_pass_and_fail_without_duplicates(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            before, after = (str(Path(tmp) / f'{name}.png') for name in ('before', 'after'))
            for path, level in ((before, 20), (after, 220)):
                Image.new('RGB', (32, 18), (level, level, level)).save(path)
            shots = [{'name': 'before', 'phase': 'before', 'path': before, 'label': 'before:base',
                      'fresh': {'status': 'fresh'}},
                     {'name': 'after', 'phase': 'after', 'path': after, 'label': 'after:result',
                      'fresh': {'status': 'refused', 'reason': 'frozen'}}]
            check = jq.clip_check({'shots': shots}, {})
            self.assertEqual(check, {'ok': True, 'unmeasured': True,
                                     'note': 'change not measured: 1 unusable still(s) excluded',
                                     'excluded': [{'path': after, 'label': 'after:result', 'reason': 'frozen'}]})
            for word, cited in (('PASS', after), ('PASS', before), ('FAIL', after), ('FAIL', before)):
                with self.subTest(verdict=word, cited=cited):
                    receipt = {'gates_status': 'PASS', 'status': 'RAN-UNVERIFIED', 'blockers': []}
                    jq.apply_verdict(receipt, check, {'verdict': word, 'shots_cited': [cited]}, {}, shots=shots)
                    self.assertEqual((receipt['status'], receipt['review'], receipt['review_kind']),
                                     ('RAN-UNVERIFIED', True, 'unverified'))
                    self.assertEqual(receipt['judge']['verdict'], word)
                    self.assertEqual(receipt['blockers'], [{'kind': 'evidence', 'source': 'after:result',
                                                            'text': f'still {after} is not usable evidence: frozen'}])
            missing = {'shots': [shots[0], {'name': 'missing', 'phase': 'after', 'path': str(Path(tmp) / 'missing')}]}
            self.assertEqual(jq.clip_check(missing, {})['reason'], 'expected-result shot missing: missing')

    def test_clip_check_uses_clip_baseline_and_old_jobs_do_not_gain_evidence(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            paths = {name: str(Path(tmp) / f'{name}.png')
                     for name in ('before', 'after', 'clip-start', 'clip-end')}
            for name, level in (('before', 220), ('after', 20), ('clip-start', 20), ('clip-end', 220)):
                Image.new('RGB', (32, 18), (level, level, level)).save(paths[name])
            shots = [{'name': 'before', 'phase': 'before', 'path': paths['before'], 'label': 'before:base',
                      'fresh': {'status': 'error', 'reason': 'journal failed'}},
                     {'name': 'after', 'phase': 'after', 'path': paths['after'], 'label': 'after:result',
                      'fresh': {'status': 'fresh'}}]
            frames = {'start': paths['clip-start'], 'end': paths['clip-end']}
            check = jq.clip_check({'shots': shots}, frames)
            self.assertEqual((check['ok'], check['before'], check['max_diff']), (True, frames['start'], 200.0))
            self.assertEqual(check['excluded'][0]['label'], 'before:base')
            old = [{key: value for key, value in shot.items() if key != 'fresh'} for shot in shots]
            check = jq.clip_check({'shots': old}, frames)
            self.assertEqual((check['ok'], check['before'], check['max_diff']), (True, frames['start'], 200.0))
            self.assertEqual(len(check['excluded']), 2)
            self.assertTrue(all(item['reason'] == 'no freshness record' for item in check['excluded']))
            unmeasured = jq.clip_check({'shots': old}, {})
            self.assertTrue(unmeasured['unmeasured'])
            self.assertEqual(len(unmeasured['excluded']), 2)
            clips_only = jq.clip_check({'shots': []}, frames)
            self.assertEqual(clips_only, {'ok': True, 'max_diff': 200.0, 'before': frames['start']})

    def test_clip_check_unreadable_pair_with_exclusion_is_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {name: str(Path(tmp) / name) for name in ('before', 'step', 'after')}
            for path in paths.values():
                Path(path).write_bytes(b'exists')
            shots = [{'name': name, 'phase': phase, 'path': paths[name],
                      'fresh': {'status': 'error', 'reason': 'bad'} if name == 'step' else {'status': 'fresh'}}
                     for name, phase in (('before', 'before'), ('step', 'step'), ('after', 'after'))]
            with mock.patch.object(jq, 'frame_diff', side_effect=OSError('unreadable')):
                check = jq.clip_check({'shots': shots}, {})
            self.assertEqual(check, {'ok': True, 'unmeasured': True,
                                     'note': 'change not measured: 1 unusable still(s) excluded',
                                     'excluded': [{'path': paths['step'], 'name': 'step', 'reason': 'bad'}]})


class RunnerEffects(unittest.TestCase):
    def hook_run(self, value, *, target=False, effect=False, role='test', outcome=None, capture=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        step = {'run': 'hooks/result.py:check'}
        if effect:
            step['effect'] = True
        row = {'id': 'a', 'role': role, 'steps': [step]}
        if outcome:
            row['outcome'] = outcome
        r = make_plan(folder, [row], **({'target': {}} if target else {}))
        shot = '    ctx.shot("visual")\n' if capture else ''
        (folder / 'hooks/result.py').write_text(f'def check(ctx):\n{shot}    return {value!r}\n')
        self.assertEqual(r.run(), 'DONE')
        return folder, r

    def record_read(self, folder, still, verdict='PASS', text='the expected text is visible', row='a', capture=None):
        command = [sys.executable, '-I', '-B', str(HERE / 'runner.py'), 'record-read',
                   str(folder / 'rows.json'), '--out', str(folder / 'out'), '--rows', row,
                   '--reader', 'reviewer', '--verdict', verdict, '--text', text, '--still', str(still)]
        if capture is not None:
            command.extend(['--capture', capture])
        return subprocess.run(command, capture_output=True, text=True)

    def captured(self, folder, row='a'):
        captures = receipt(folder, row).get('captures')
        self.assertTrue(captures)
        return folder / 'out' / row / captures[0]['file']

    def visual_fresh_run(self, state):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        count = 2 if state in ('same', 'refused') else 1
        engine = make_plan(folder, [{'id': 'a', 'outcome': 'visual',
                                     'steps': [{'shot': 'same'} for _ in range(count)]}])
        if state in ('fresh', 'same', 'refused'):
            bridge = folder / 'bridge.log'
            engine.instances['sw']['bridge_log'] = str(bridge)
            frames = iter([1000, 1200 if state == 'refused' else 1060])
            client = engine.clients['sw']
            request = client.request

            def call(method, params=None):
                if method == 'ui.screenshot':
                    frame = next(frames)
                    with bridge.open('a') as stream:
                        stream.write(f'[Bridge] boot=0xabc frame={frame} wall={frame * 17}\n')
                return request(method, params)

            client.request = call
        if state == 'error':
            (engine.out / 'stills.jsonl').mkdir()
        self.assertEqual(engine.run(), 'DONE')
        capture = receipt(folder, 'a')['captures'][-1]
        return folder, engine.out / 'a' / capture['file']

    def test_record_read_requires_usable_freshness_for_pass_and_fail(self):
        for state in ('fresh', 'same', 'unverified', 'refused', 'error', 'missing'):
            with self.subTest(state=state):
                folder, still = self.visual_fresh_run(state)
                before = receipt(folder, 'a')
                capture = before['captures'][-1]
                if state == 'missing':
                    capture.pop('fresh', None)
                    (folder / 'out/a.json').write_text(json.dumps(before))
                else:
                    self.assertEqual((capture.get('fresh') or {}).get('status'), state)
                before = receipt(folder, 'a')
                if state in ('refused', 'error', 'missing'):
                    for verdict in ('PASS', 'FAIL'):
                        result = self.record_read(folder, still, verdict=verdict)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(f'the capture a/{still.name} is not usable evidence:', result.stderr)
                        self.assertIn('no freshness record' if state == 'missing'
                                      else runner.still_fresh.unusable_reason(capture['fresh']), result.stderr)
                        self.assertEqual(receipt(folder, 'a'), before)
                else:
                    result = self.record_read(folder, still)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(receipt(folder, 'a')['visual_read']['capture']['fresh'], capture['fresh'])

    def test_visual_read_and_judge_pass_preserve_evidence_blockers(self):
        folder, still = self.visual_fresh_run('unverified')
        current = receipt(folder, 'a')
        blocker = {'kind': 'evidence', 'source': 'after', 'text': 'still cannot be trusted'}
        current['blockers'].append(blocker)
        (folder / 'out/a.json').write_text(json.dumps(current))
        result = self.record_read(folder, still)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = receipt(folder, 'a')
        self.assertEqual((after['status'], after['review_kind'], after['blockers']),
                         ('RAN-UNVERIFIED', 'unverified', [blocker]))
        refused = self.record_read(folder, still)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('the row has no pending visual read', refused.stderr)
        jq.apply_verdict(after, {'ok': True}, {'verdict': 'PASS', 'reason': 'visible'}, {})
        self.assertEqual((after['status'], after['blockers']), ('RAN-UNVERIFIED', [blocker]))

    def test_async_verdict_cannot_use_errored_stills(self):
        for broken, word in ((False, 'PASS'), (True, 'PASS'), (True, 'FAIL')):
            with self.subTest(broken=broken, word=word):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                folder = Path(tmp.name)
                row = {'id': 'a', 'role': 'test', 'outcome': 'visual', 'expected': f'say {word}',
                       'steps': [{'shot': 'before'}, {'shot': 'after'}],
                       'shots': [{'name': 'before', 'phase': 'before'}, {'name': 'after', 'phase': 'after'}]}
                if broken:
                    row['steps'].append({'shot': 'witness'})
                    row['shots'].append({'name': 'witness', 'phase': 'diagnostic'})
                engine = make_plan(folder, [row], target={}, judge={'cmd': 'judge.py', 'wait_s': 60},
                                   known_issues={'known': {'title': 'known', 'reason': 'stand-in judge: FAIL',
                                                           'rows': ['a']}})
                if broken:
                    (engine.out / 'stills.jsonl').mkdir()
                    record = engine.stills.record

                    def record_with_witness(on, path, reply, frame):
                        if 'witness' in path.name:
                            return {'status': 'unverified', 'reason': 'no game frame source'}
                        return record(on, path, reply, frame)

                    engine.stills.record = record_with_witness
                self.assertEqual(engine.run(), 'DONE')
                result = receipt(folder, 'a')
                self.assertEqual(result['judge']['verdict'], word)
                job = json.loads((engine.out / 'judge/jobs/a.json').read_text())
                self.assertEqual([shot.get('fresh', {}).get('status') for shot in job['shots']],
                                 ['unverified'] if broken else ['unverified', 'unverified'])
                if broken:
                    self.assertEqual((result['status'], result['review'], result['review_kind']),
                                     ('RAN-UNVERIFIED', True, 'unverified'))
                    blockers = [item for item in result['blockers'] if item['kind'] == 'evidence']
                    self.assertEqual(len(blockers), 2)
                    excluded = job.get('excluded_shots') or []
                    self.assertEqual([shot['name'] for shot in excluded], ['before', 'after'])
                    for shot in excluded:
                        self.assertTrue(any(shot['path'] in item['text'] for item in blockers))
                    self.assertNotIn('known_issue', result)
                else:
                    self.assertEqual(result['status'], 'PASS')
                    self.assertEqual(result['blockers'], [])

    def test_async_judge_refuses_a_frozen_after_and_ignores_uncited_diagnostic(self):
        for stale in (True, False):
            with self.subTest(stale=stale):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                folder = Path(tmp.name)
                names = ('before', 'same-step', 'same-after') if stale else ('before', 'diagnostic', 'after')
                row = {'id': 'a', 'role': 'test', 'outcome': 'visual', 'expected': 'say PASS',
                       'steps': [{'shot': name} for name in names],
                       'shots': [{'name': name, 'phase': phase}
                                 for name, phase in zip(names, ('before', 'step', 'after'))]}
                engine = make_plan(folder, [row], target={}, judge={'cmd': 'judge.py', 'wait_s': 60})
                if stale:
                    bridge = folder / 'bridge.log'
                    engine.instances['sw']['bridge_log'] = str(bridge)
                    frames = iter((1000, 1000, 1200))
                    client = engine.clients['sw']
                    request = client.request

                    def call(method, params=None):
                        if method == 'ui.screenshot':
                            frame = next(frames)
                            with bridge.open('a') as stream:
                                stream.write(f'[Bridge] boot=0xabc frame={frame} wall={frame * 17}\n')
                        return request(method, params)

                    client.request = call
                    (folder / 'judge.py').write_text(JUDGE.replace(
                        '[s["path"] for s in job["shots"]]', '[]'))
                else:
                    record = engine.stills.record

                    def record_without_diagnostic(on, path, reply, frame):
                        if 'diagnostic' in path.name:
                            raise OSError('diagnostic journal failure')
                        return record(on, path, reply, frame)

                    engine.stills.record = record_without_diagnostic
                    (folder / 'judge.py').write_text(JUDGE.replace(
                        '[s["path"] for s in job["shots"]]',
                        '[s["path"] for s in job["shots"] if s["phase"] in ("before", "after")]'))
                self.assertEqual(engine.run(), 'DONE')
                result = receipt(folder, 'a')
                self.assertEqual(result['judge']['verdict'], 'PASS')
                job = json.loads((engine.out / 'judge/jobs/a.json').read_text())
                if stale:
                    excluded = job.get('excluded_shots') or []
                    self.assertEqual([shot['name'] for shot in excluded], ['same-after'])
                    self.assertIn('unchanged across 200 game frames', excluded[0]['reason'])
                    self.assertEqual(result['status'], 'RAN-UNVERIFIED')
                    self.assertTrue(any(item['kind'] == 'evidence' and excluded[0]['path'] in item['text']
                                        for item in result['blockers']))
                else:
                    excluded = job.get('excluded_shots') or []
                    self.assertEqual([shot['name'] for shot in excluded], ['diagnostic'])
                    self.assertIn('diagnostic journal failure', excluded[0]['reason'])
                    self.assertEqual(result['status'], 'PASS')
                    self.assertEqual(result['blockers'], [])

    def test_legacy_job_without_freshness_records_is_unverified(self):
        for word in ('PASS', 'FAIL'):
            with self.subTest(verdict=word):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                folder = Path(tmp.name)
                row = {'id': 'a', 'role': 'test', 'outcome': 'visual', 'expected': f'say {word}',
                       'steps': [{'shot': 'before'}, {'shot': 'after'}],
                       'shots': [{'name': 'before', 'phase': 'before'}, {'name': 'after', 'phase': 'after'}]}
                engine = make_plan(folder, [row], target={}, judge={'cmd': 'judge.py', 'wait_s': 60})
                self.assertEqual(engine.run(), 'DONE')
                job_path = engine.out / 'judge/jobs/a.json'
                job = json.loads(job_path.read_text())
                for shot in job['shots']:
                    shot.pop('fresh', None)
                job.pop('row_shots_saved', None)
                clip = folder / 'clip-start.png'
                clip.write_bytes(Path(job['shots'][0]['path']).read_bytes())
                job['clip'] = str(clip)
                job_path.write_text(json.dumps(job))
                current = receipt(folder, 'a')
                current.update(status='RAN-UNVERIFIED', judge={'state': 'pending'},
                               blockers=[{'kind': 'visual', 'source': 'row', 'text': 'visual read pending'}])
                (engine.out / 'a.json').write_text(json.dumps(current))
                with mock.patch.object(jq, 'sample_frames', return_value=({'start': str(clip)}, {'start': 0})), \
                     mock.patch.object(jq, 'sample_dense', return_value={}):
                    jq.work(job_path)
                result = receipt(folder, 'a')
                self.assertEqual((result['status'], result['judge']['verdict'], result.get('review_kind')),
                                 ('RAN-UNVERIFIED', word, 'unverified'))
                evidence = [item for item in result['blockers'] if item['kind'] == 'evidence']
                self.assertEqual(len(evidence), 2)
                self.assertTrue(all('no freshness record' in item['text'] for item in evidence))

    def test_unverified_hook_markers_never_become_pass_without_a_judge(self):
        for value, marker in (({'file': None, 'grade': 'NEEDS VISUAL READ'}, 'NEEDS VISUAL READ'),
                              ({'unverified': 'look at the bar'}, 'look at the bar'),
                              ({'visual_verdict': 'pending'}, 'pending')):
            with self.subTest(value=value):
                folder, _ = self.hook_run(value)
                r = receipt(folder, 'a')
                self.assertEqual(r['status'], 'RAN-UNVERIFIED')
                kind = 'unverified' if 'unverified' in value else 'visual'
                self.assertEqual((r['gates_status'], r['review'], r['review_kind']), ('PASS', True, kind))
                self.assertEqual(r['status_reason'], f'hook hooks/result.py:check: {marker}')

    def test_admitted_test_rows_require_a_checked_effect(self):
        for value, effect, role, status, effects, reason in (
            (True, False, 'test', 'RAN-UNVERIFIED', [], None),
            ({'frames': 300}, True, 'test', 'PASS', ['run:hooks/result.py:check'], None),
            ('a checked result', True, 'test', 'PASS', ['run:hooks/result.py:check'], None),
            (0, True, 'test', 'PASS', ['run:hooks/result.py:check'], None),
            (True, True, 'test', 'RAN-UNVERIFIED', [], 'effect hook hooks/result.py:check returned no evidence'),
            ({}, True, 'test', 'RAN-UNVERIFIED', [], 'effect hook hooks/result.py:check returned no evidence'),
            ([], True, 'test', 'RAN-UNVERIFIED', [], 'effect hook hooks/result.py:check returned no evidence'),
            (True, False, 'setup', 'PASS', [], None),
        ):
            with self.subTest(value=value, effect=effect, role=role):
                folder, _ = self.hook_run(value, target=True, effect=effect, role=role)
                r = receipt(folder, 'a')
                self.assertEqual((r['status'], r['effects_passed']), (status, effects))
                if reason:
                    self.assertEqual(r['status_reason'], reason)

    def test_unverified_effect_hook_is_not_evidence(self):
        folder, _ = self.hook_run({'unverified': 'read this', 'frames': 300}, target=True, effect=True)
        r = receipt(folder, 'a')
        self.assertEqual((r['status'], r['effects_passed']), ('RAN-UNVERIFIED', []))
        self.assertEqual(r['status_reason'], 'hook hooks/result.py:check: read this')

    def test_an_effect_hook_without_evidence_is_a_blocker_without_a_target(self):
        folder, _ = self.hook_run(True, effect=True)
        result = receipt(folder, 'a')
        self.assertEqual(result['status'], 'RAN-UNVERIFIED')
        self.assertEqual([b['kind'] for b in result.get('blockers', [])], ['effect'])
        self.assertEqual(result['status_reason'], 'effect hook hooks/result.py:check returned no evidence')

    def test_setup_with_require_effect_keeps_its_existing_unverified_status(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        engine = make_plan(folder, [{'id': 'a', 'role': 'setup', 'steps': [{'gate': 'nav'}]}],
                           require_effect=True)
        self.assertEqual(engine.run(), 'DONE')
        result = receipt(folder, 'a')
        self.assertEqual(result['status'], 'RAN-UNVERIFIED')
        self.assertEqual([b['kind'] for b in result.get('blockers', [])], ['effect'])
        self.assertEqual(result['status_reason'], 'no checked effect gate passed (only start/navigation gates)')

    def test_effect_hook_verdict_and_pass_values_are_checked(self):
        cases = (
            ({'verdict': 'FAIL', 'reason': 'x'}, 'RAN-UNVERIFIED', 'verdict FAIL'),
            ({'verdict': 'UNVERIFIED'}, 'RAN-UNVERIFIED', 'verdict UNVERIFIED'),
            ({'pass': False, 'samples': 3}, 'RAN-UNVERIFIED', 'pass=False'),
            ({'pass': 1, 'samples': 3}, 'RAN-UNVERIFIED', 'pass=1'),
            ({'verdict': 'PASS', 'frames': 300}, 'PASS', None),
            ({'verdict': 'pass', 'frames': 300}, 'PASS', None),
            ({'pass': True, 'samples': 300}, 'PASS', None),
        )
        for value, status, reason in cases:
            with self.subTest(value=value):
                folder, _ = self.hook_run(value, target=True, effect=True)
                result = receipt(folder, 'a')
                self.assertEqual(result['status'], status)
                self.assertEqual(result['effects_passed'], [] if reason else ['run:hooks/result.py:check'])
                if reason:
                    self.assertEqual(result['status_reason'], f'effect hook hooks/result.py:check returned {reason}')
                    self.assertNotEqual(result.get('review_kind'), 'visual')

    def test_failed_verdict_receipt_cannot_be_promoted_by_record_read(self):
        folder, _ = self.hook_run({'verdict': 'FAIL', 'reason': 'x'}, target=True, effect=True)
        still = folder / 'out/a/still.png'
        still.write_bytes(b'fixture image')
        before = receipt(folder, 'a')
        self.assertEqual(before['status'], 'RAN-UNVERIFIED')
        self.assertNotEqual(self.record_read(folder, still).returncode, 0)
        self.assertEqual(receipt(folder, 'a'), before)

    def test_a_skipped_judge_cannot_promote_an_unverified_hook(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        r = make_plan(folder, [{'id': 'a', 'judge': 'skip', 'steps': [{'run': 'hooks/result.py:check'}]}],
                      judge={'cmd': 'judge.py'})
        (folder / 'hooks/result.py').write_text('def check(ctx): return {"grade": "NEEDS VISUAL READ"}\n')
        self.assertEqual(r.run(), 'DONE')
        self.assertEqual(receipt(folder, 'a')['status'], 'RAN-UNVERIFIED')

    def test_a_skipped_judge_cannot_promote_an_effect_hook_without_evidence(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        r = make_plan(folder, [{'id': 'a', 'role': 'test', 'judge': 'skip',
                                'steps': [{'run': 'hooks/result.py:check', 'effect': True}]}],
                      judge={'cmd': 'judge.py'}, target={})
        (folder / 'hooks/result.py').write_text('def check(ctx): return True\n')
        self.assertEqual(r.run(), 'DONE')
        result = receipt(folder, 'a')
        self.assertEqual((result['status'], result['effects_passed']), ('RAN-UNVERIFIED', []))
        self.assertEqual(result['status_reason'], 'effect hook hooks/result.py:check returned no evidence')
        self.assertEqual(result['judge'], {'state': 'skipped'})

    def test_untargeted_skipped_judge_keeps_failed_effect_hook_blocker(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        rows = [{'id': name, 'judge': 'skip', 'steps': [{'run': f'hooks/result.py:{name}', 'effect': True}]}
                for name in ('bad', 'good')]
        engine = make_plan(folder, rows, require_effect=True, judge={'cmd': 'judge.py'})
        (folder / 'hooks/result.py').write_text(
            'def bad(ctx): return {"verdict": "FAIL"}\n'
            'def good(ctx): return {"frames": 300}\n')
        self.assertEqual(engine.run(), 'DONE')
        bad = receipt(folder, 'bad')
        self.assertEqual(bad['status'], 'RAN-UNVERIFIED')
        self.assertEqual((bad['status'], bad['review'], bad['review_kind']), ('RAN-UNVERIFIED', True, 'unverified'))
        self.assertEqual(bad['status_reason'], 'effect hook hooks/result.py:bad returned verdict FAIL')
        self.assertEqual(bad['blockers'], [{'kind': 'effect', 'source': 'hooks/result.py:bad',
                                           'text': 'effect hook hooks/result.py:bad returned verdict FAIL'}])
        self.assertEqual(bad['judge'], {'state': 'skipped'})
        good = receipt(folder, 'good')
        self.assertEqual((good['status'], good['judge']), ('PASS', {'state': 'skipped'}))
        self.assertEqual(good['blockers'], [])
        self.assertEqual(good['status_reason'], 'setup row: judge skipped')

    def test_skipped_queue_preserves_visual_and_mixed_blockers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        engine = make_plan(folder, [{'id': 'a', 'judge': 'skip', 'steps': []}], judge={'cmd': 'judge.py'})
        visual = {'kind': 'visual', 'source': 'row', 'text': 'read pending'}
        deterministic = {'kind': 'unverified', 'source': 'hook', 'text': 'count missing'}
        for blockers, kind, reason in (([visual], 'visual', 'read pending'),
                                       ([visual, deterministic], 'unverified', 'count missing')):
            with self.subTest(kind=kind):
                path = engine.out / 'a.json'
                path.write_text(json.dumps({'gates_status': 'PASS', 'status': 'RAN-UNVERIFIED',
                                            'judge': {'state': 'pending'}, 'blockers': blockers}))
                engine.queue_judge('a')
                result = receipt(folder, 'a')
                self.assertEqual(result['status'], 'RAN-UNVERIFIED')
                self.assertEqual((result['status'], result['review'], result['review_kind'], result['status_reason']),
                                 ('RAN-UNVERIFIED', True, kind, reason))
                self.assertEqual(result['judge'], {'state': 'skipped'})
                self.assertEqual(result['blockers'], blockers)
                self.assertEqual(result['visual_read'], {'state': 'pending'})
                self.assertNotIn('cleared', result)

    def test_failed_judge_start_keeps_recorded_blockers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        engine = make_plan(folder, [{'id': 'a', 'outcome': 'visual',
                                     'steps': [{'run': 'hooks/result.py:check', 'effect': True}]}],
                           target={}, judge={'cmd': 'judge.py'})
        (folder / 'hooks/result.py').write_text('def check(ctx): return {"unverified": "count missing"}\n')
        with mock.patch.object(jq, 'start_worker', side_effect=RuntimeError('cannot start')):
            self.assertEqual(engine.run(), 'DONE')
        result = receipt(folder, 'a')
        self.assertEqual((result['status'], result['review'], result['judge']['state']),
                         ('RAN-UNVERIFIED', True, 'error'))
        self.assertEqual([item['kind'] for item in result.get('blockers', [])], ['visual', 'unverified'])
        self.assertIn('judge job could not start', result['status_reason'])
        self.assertNotIn('cleared', result)

    def test_unread_visual_row_stays_unverified_on_a_second_run(self):
        folder, r = self.hook_run({'frames': 300}, target=True, effect=True, outcome='visual')
        still = folder / 'out/a/still.png'
        still.write_bytes(b'fixture image')
        first = receipt(folder, 'a')
        self.assertEqual((first['status'], first['gates_status'], first.get('review_kind')),
                         ('RAN-UNVERIFIED', 'PASS', 'visual'))
        self.assertEqual(first['status_reason'], 'visual read pending')
        self.assertEqual(first['visual_read'], {'state': 'pending'})
        self.assertEqual(r.run(), 'DONE')
        self.assertEqual(receipt(folder, 'a')['status'], 'RAN-UNVERIFIED')
        self.assertEqual(r.outcomes['a']['status'], 'unverified')

    def test_wrong_text_visual_read_fails_and_cannot_be_promoted(self):
        folder, _ = self.hook_run(True, outcome='visual', capture=True)
        still = self.captured(folder)
        self.assertEqual(receipt(folder, 'a')['status'], 'RAN-UNVERIFIED')
        result = self.record_read(folder, still, 'FAIL', 'shows 30..127, expected 30..97')
        self.assertEqual(result.returncode, 0, result.stderr)
        failed = receipt(folder, 'a')
        self.assertEqual(failed['status'], 'FAILED')
        self.assertEqual(failed['visual_read']['text'], 'shows 30..127, expected 30..97')
        self.assertNotEqual(self.record_read(folder, still).returncode, 0)
        self.assertEqual(receipt(folder, 'a'), failed)

    def test_record_read_accepts_only_a_pending_visual_receipt_and_owned_still(self):
        folder, _ = self.hook_run(True, outcome='visual', capture=True)
        still = self.captured(folder)
        before = receipt(folder, 'a')
        self.assertEqual(before['status'], 'RAN-UNVERIFIED')
        outside = folder / 'outside.png'
        outside.write_bytes(b'outside')
        self.assertNotEqual(self.record_read(folder, outside).returncode, 0)
        self.assertNotEqual(self.record_read(folder, still, text='   ').returncode, 0)
        self.assertEqual(receipt(folder, 'a'), before)
        result = self.record_read(folder, still)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = receipt(folder, 'a')
        self.assertEqual(after['status'], 'PASS')
        self.assertFalse(after['review'])
        self.assertEqual(after['visual_read']['still_sha256'], hashlib.sha256(still.read_bytes()).hexdigest())
        self.assertEqual(after['visual_reads'], [{'state': 'pending'}])
        changed = {'visual_read', 'visual_reads', 'status', 'status_reason', 'review', 'review_kind',
                   'blockers', 'cleared'}
        self.assertEqual({key: value for key, value in after.items() if key not in changed},
                         {key: value for key, value in before.items() if key not in changed})
        self.assertNotEqual(self.record_read(folder, still).returncode, 0)
        self.assertEqual(receipt(folder, 'a'), after)
        plan = json.loads((folder / 'rows.json').read_text())
        plan['judge'] = {'cmd': 'judge.py'}
        (folder / 'rows.json').write_text(json.dumps(plan))
        (folder / 'out/a.json').write_text(json.dumps(before))
        self.assertNotEqual(self.record_read(folder, still).returncode, 0)
        self.assertEqual(receipt(folder, 'a'), before)

    def test_missing_delivery_is_not_a_visual_read(self):
        marker = 'P1 delivery count not observed'
        folder, _ = self.hook_run({'unverified': marker}, target=True, effect=True, capture=True)
        before = receipt(folder, 'a')
        self.assertEqual((before['status'], before.get('review_kind')), ('RAN-UNVERIFIED', 'unverified'))
        self.assertEqual(sum(item['kind'] == 'unverified' for item in before.get('blockers', [])), 1)
        still = self.captured(folder)
        result = self.record_read(folder, still)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('the row has no pending visual read', result.stderr)
        self.assertEqual(receipt(folder, 'a'), before)

    def test_a_visual_read_does_not_clear_missing_delivery(self):
        marker = 'P1 delivery count not observed'
        folder, _ = self.hook_run({'unverified': marker}, target=True, effect=True,
                                  outcome='visual', capture=True)
        before = receipt(folder, 'a')
        self.assertEqual([item['kind'] for item in before.get('blockers', [])], ['visual', 'unverified'])
        result = self.record_read(folder, self.captured(folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        after = receipt(folder, 'a')
        self.assertEqual((after['status'], after.get('review_kind')), ('RAN-UNVERIFIED', 'unverified'))
        self.assertEqual(after['status_reason'], f'hook hooks/result.py:check: {marker}')
        self.assertEqual([item['kind'] for item in after['blockers']], ['unverified'])
        self.assertEqual(after['cleared'][0]['kind'], 'visual')
        self.assertEqual(after['cleared'][0]['reader'], 'reviewer')
        self.assertNotEqual(self.record_read(folder, self.captured(folder)).returncode, 0)

    def test_a_visual_read_records_the_actual_capture_and_clears_only_visual(self):
        folder, _ = self.hook_run({'frames': 300}, target=True, effect=True, outcome='visual', capture=True)
        still = self.captured(folder)
        capture = receipt(folder, 'a')['captures'][0]
        (folder / 'hooks/result.py').write_text('def check(ctx): return {"changed": True}\n')
        result = self.record_read(folder, still)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = receipt(folder, 'a')
        self.assertEqual(after['status'], 'PASS')
        self.assertEqual(after['blockers'], [])
        self.assertEqual(after['visual_read']['capture'], {'row': 'a', **capture})
        self.assertEqual(capture['name'], 'visual')
        self.assertEqual(capture['sha256'], hashlib.sha256(still.read_bytes()).hexdigest())
        self.assertRegex(capture['utc'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(capture['status'], {'frame': 321, 'state': 'running'})

    def test_a_failed_status_read_does_not_fail_the_screenshot(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        engine = make_plan(folder, [{'id': 'a', 'outcome': 'visual', 'steps': [{'shot': 'visual'}]}])
        client = engine.clients['sw']
        request = client.request

        def call(method, params=None):
            if method == 'status':
                return {'ok': False, 'error': 'unavailable'}
            return request(method, params)

        client.request = call
        self.assertEqual(engine.run(), 'DONE')
        captures = receipt(folder, 'a').get('captures')
        self.assertTrue(captures)
        self.assertIsNone(captures[0]['status'])

    def test_visual_read_refuses_unrecorded_json_and_png(self):
        folder, _ = self.hook_run({'frames': 300}, target=True, effect=True, outcome='visual', capture=True)
        before = receipt(folder, 'a')
        for name in ('hook.json', 'other.png'):
            with self.subTest(name=name):
                path = folder / 'out/a' / name
                path.write_bytes(b'not a runner capture')
                result = self.record_read(folder, path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('the still is not a capture recorded by the runner in this run', result.stderr)
                self.assertEqual(receipt(folder, 'a'), before)

    def test_a_copied_capture_needs_the_same_packet_and_up(self):
        for changed in (None, 'plan_identity', 'instances_sha256', 'inputs_identity',
                        'source_missing', 'reader_missing', 'both_missing'):
            with self.subTest(changed=changed):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                folder = Path(tmp.name)
                rows = [{'id': 'a', 'role': 'setup', 'steps': [{'run': 'hooks/capture.py:check'}]},
                        {'id': 'b', 'role': 'test', 'outcome': 'visual',
                         'steps': [{'run': 'hooks/copy.py:check', 'effect': True}]}]
                engine = make_plan(folder, rows, target={})
                (folder / 'hooks/capture.py').write_text('def check(ctx):\n    ctx.shot("source")\n    return 1\n')
                (folder / 'hooks/copy.py').write_text(
                    'import json\nfrom pathlib import Path\n'
                    'def check(ctx):\n'
                    '    out = Path(ctx.runner.out)\n'
                    '    captures = json.loads((out / "a.json").read_text()).get("captures") or []\n'
                    '    data = (out / "a" / captures[0]["file"]).read_bytes() if captures else b"no capture"\n'
                    '    (Path(ctx.row_dir) / "copy.png").write_bytes(data)\n'
                    '    return 1\n')
                self.assertEqual(engine.run(), 'DONE')
                a = receipt(folder, 'a')
                self.assertTrue(a.get('captures'))
                b = receipt(folder, 'b')
                self.assertEqual(a['inputs_identity'], b['inputs_identity'])
                if changed in ('plan_identity', 'instances_sha256', 'inputs_identity'):
                    a[changed] = 'another'
                elif changed in ('source_missing', 'both_missing'):
                    a.pop('inputs_identity')
                if changed in ('reader_missing', 'both_missing'):
                    b.pop('inputs_identity')
                    (folder / 'out/b.json').write_text(json.dumps(b))
                if changed:
                    (folder / 'out/a.json').write_text(json.dumps(a))
                else:
                    (folder / 'hooks/copy.py').write_text('def check(ctx): return "changed since capture"\n')
                still = folder / 'out/b/copy.png'
                before = receipt(folder, 'b')
                result = self.record_read(folder, still, row='b')
                if changed:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('the still is not a capture recorded by the runner in this run', result.stderr)
                    self.assertEqual(receipt(folder, 'b'), before)
                    if changed in ('inputs_identity', 'source_missing', 'reader_missing', 'both_missing'):
                        capture = f"a:{a['captures'][0]['file']}"
                        declared = self.record_read(folder, still, row='b', capture=capture)
                        self.assertNotEqual(declared.returncode, 0)
                        self.assertIn(f'--capture {capture} is not a capture of this run with these bytes',
                                      declared.stderr)
                        self.assertEqual(receipt(folder, 'b'), before)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(receipt(folder, 'b')['visual_read']['capture']['row'], 'a')
                    self.assertEqual(receipt(folder, 'b')['status'], 'PASS')

    def identical_captures(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        rows = [{'id': row, 'outcome': 'visual', 'steps': [{'shot': 'same'}] if row != 'c' else []}
                for row in ('a', 'b', 'c')]
        engine = make_plan(folder, rows)
        client = engine.clients['sw']
        request = client.request

        def call(method, params=None):
            if method == 'status':
                return {'ok': True, 'result': {'frame': {'a': 100, 'b': 700}[engine.current_row['id']]}}
            return request(method, params)

        client.request = call
        self.assertEqual(engine.run(), 'DONE')
        a, b = self.captured(folder, 'a'), self.captured(folder, 'b')
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(receipt(folder, 'a')['captures'][0]['status']['frame'], 100)
        self.assertEqual(receipt(folder, 'b')['captures'][0]['status']['frame'], 700)
        return folder, a, b

    def test_declared_capture_rejects_a_different_inputs_identity(self):
        folder, a, _ = self.identical_captures()
        copy = folder / 'out/c/copied.png'
        copy.write_bytes(a.read_bytes())
        source = receipt(folder, 'a')
        source['inputs_identity'] = 'another'
        (folder / 'out/a.json').write_text(json.dumps(source))
        before = receipt(folder, 'c')
        capture = f'a:{a.name}'
        result = self.record_read(folder, copy, row='c', capture=capture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f'--capture {capture} is not a capture of this run with these bytes', result.stderr)
        self.assertEqual(receipt(folder, 'c'), before)

    def test_own_capture_without_inputs_identity_works_in_all_resolution_modes(self):
        for mode in ('path', 'declared', 'unique'):
            with self.subTest(mode=mode):
                folder, _ = self.hook_run({'frames': 300}, target=True, effect=True, outcome='visual', capture=True)
                original = self.captured(folder)
                current = receipt(folder, 'a')
                current.pop('inputs_identity')
                (folder / 'out/a.json').write_text(json.dumps(current))
                still = original
                if mode != 'path':
                    still = folder / 'out/a/copied.png'
                    still.write_bytes(original.read_bytes())
                declared = f'a:{original.name}' if mode == 'declared' else None
                result = self.record_read(folder, still, capture=declared)
                self.assertEqual(result.returncode, 0, result.stderr)
                selected = receipt(folder, 'a')['visual_read']['capture']
                self.assertEqual((selected['row'], selected['file']), ('a', original.name))

    def test_identical_stills_are_credited_to_their_own_paths(self):
        folder, a, b = self.identical_captures()
        result = self.record_read(folder, b, row='b')
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = receipt(folder, 'b')['visual_read']['capture']
        self.assertEqual((selected['row'], selected['file'], selected['status']['frame']), ('b', b.name, 700))
        before = receipt(folder, 'a')
        conflict = self.record_read(folder, a, row='a', capture=f'b:{b.name}')
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("--capture names another capture than the still's own path", conflict.stderr)
        self.assertEqual(receipt(folder, 'a'), before)
        result = self.record_read(folder, a, row='a', capture=f'a:{a.name}')
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = receipt(folder, 'a')['visual_read']['capture']
        self.assertEqual((selected['row'], selected['file'], selected['status']['frame']), ('a', a.name, 100))

    def test_copied_identical_stills_require_a_valid_capture_declaration(self):
        folder, a, b = self.identical_captures()
        copy = folder / 'out/c/copied.png'
        copy.write_bytes(a.read_bytes())
        before = receipt(folder, 'c')
        ambiguous = self.record_read(folder, copy, row='c')
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn(f'the still matches 2 captures (a/{a.name}, b/{b.name}); '
                      'name one with --capture <row>:<file>', ambiguous.stderr)
        self.assertEqual(receipt(folder, 'c'), before)
        wrong = self.record_read(folder, copy, row='c', capture='b:wrong.png')
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn('--capture b:wrong.png is not a capture of this run with these bytes', wrong.stderr)
        copy.write_bytes(b'different bytes')
        wrong = self.record_read(folder, copy, row='c', capture=f'b:{b.name}')
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn(f'--capture b:{b.name} is not a capture of this run with these bytes', wrong.stderr)
        self.assertEqual(receipt(folder, 'c'), before)
        copy.write_bytes(b.read_bytes())
        result = self.record_read(folder, copy, row='c', capture=f'b:{b.name}')
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = receipt(folder, 'c')['visual_read']['capture']
        self.assertEqual((selected['row'], selected['file'], selected['status']['frame']), ('b', b.name, 700))

    def test_a_still_at_its_recorded_path_cannot_be_replaced(self):
        folder, _, b = self.identical_captures()
        before = receipt(folder, 'b')
        b.write_bytes(b'replaced')
        result = self.record_read(folder, b, row='b', capture=f'b:{b.name}')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"the still's bytes differ from the capture recorded for b/{b.name}", result.stderr)
        self.assertEqual(receipt(folder, 'b'), before)

    def test_a_declared_capture_must_be_from_the_same_packet_and_up(self):
        for changed in ('plan_identity', 'instances_sha256'):
            with self.subTest(changed=changed):
                folder, _, b = self.identical_captures()
                copy = folder / 'out/c/copied.png'
                copy.write_bytes(b.read_bytes())
                source = receipt(folder, 'b')
                source[changed] = 'another'
                (folder / 'out/b.json').write_text(json.dumps(source))
                before = receipt(folder, 'c')
                result = self.record_read(folder, copy, row='c', capture=f'b:{b.name}')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f'--capture b:{b.name} is not a capture of this run with these bytes', result.stderr)
                self.assertEqual(receipt(folder, 'c'), before)

    def test_a_visual_hook_marker_can_be_cleared_without_a_target(self):
        folder, _ = self.hook_run({'grade': 'NEEDS VISUAL READ'}, capture=True)
        self.assertEqual([b['kind'] for b in receipt(folder, 'a').get('blockers', [])], ['visual'])
        result = self.record_read(folder, self.captured(folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt(folder, 'a')['status'], 'PASS')

    def test_without_an_effect_gate_a_row_is_ran_unverified_never_pass(self):
        folder = Path(tempfile.mkdtemp())
        r = make_plan(folder, [{'id': 'a', 'steps': [{'gate': 'nav'}, {'gate': 'fx'}]},
                               {'id': 'b', 'steps': [{'gate': 'nav'}]},
                               {'id': 'c', 'steps': [{'gate': 'nav', 'effect': True}]}], require_effect=True)
        self.assertEqual(r.run(), 'DONE')
        self.assertEqual((receipt(folder, 'a')['status'], receipt(folder, 'a')['effects_passed']), ('PASS', ['fx']))
        b = receipt(folder, 'b')
        self.assertEqual((b['status'], b['gates_status'], b['review']), ('RAN-UNVERIFIED', 'PASS', True))
        self.assertEqual(receipt(folder, 'c')['status'], 'PASS')  # "effect": true on the step counts too
        failing = (folder / 'out' / 'FAILING.md').read_text()
        self.assertIn('| b | RAN-UNVERIFIED |', failing)
        self.assertNotIn('| a |', failing)
        self.assertEqual(json.loads((folder / 'out' / 'rerun.json').read_text())['rows'], ['b'])
        # a resume skips the RAN-UNVERIFIED row unless it is named
        r2 = make_plan(folder, [{'id': 'b', 'steps': [{'gate': 'nav'}]}], require_effect=True)
        r2.run()
        self.assertEqual(r2.outcomes['b']['status'], 'unverified')
        r3 = make_plan(folder, [{'id': 'b', 'steps': [{'gate': 'fx'}]}], require_effect=True)
        r3.run(only={'b'})
        self.assertEqual(receipt(folder, 'b')['status'], 'PASS')

    def test_async_judge_verdicts_merge_into_the_receipts(self):
        folder = Path(tempfile.mkdtemp())

        def row(rid, expected, before, after, effect=False):
            steps = [{'shot': f'{rid}-{before}'}, {'gate': 'fx' if effect else 'nav'}, {'shot': f'{rid}-{after}'}]
            return {'id': rid, 'expected': expected, 'steps': steps,
                    'shots': [{'name': f'{rid}-{before}', 'phase': 'before'},
                              {'name': f'{rid}-{after}', 'phase': 'after'}]}
        r = make_plan(folder, [row('p', 'say PASS', 'before', 'after'), row('f', 'say FAIL', 'before', 'after'),
                               row('u', 'say UNSURE', 'before', 'after'),
                               row('e', 'say FAIL', 'before', 'after', effect=True),
                               row('n', 'say PASS', 'same-before', 'same-after'), row('k', 'say FAIL', 'before', 'after')],
                      require_effect=True, judge={'cmd': 'judge.py', 'wait_s': 60},
                      # matched by its exact signature only (no "else all" fallback since switch-hw-session-2)
                      known_issues={'a': {'title': 'P1 swap', 'reason': 'stand-in judge: FAIL', 'rows': ['k']}},
                      evidence_index={'P1-SWAP-EVIDENCE.md': {'title': 'P1', 'rows': ['p', 'k']}},
                      session_recording={'marks': ['row-start', 'row-end', 'shot'], 'per_row_files': True})
        r.run()
        status = {rid: receipt(folder, rid)['status'] for rid in 'pfuen'}
        self.assertEqual(status, {'p': 'PASS', 'f': 'FAILED', 'u': 'RAN-UNVERIFIED', 'e': 'RAN-UNVERIFIED',
                                  'n': 'FAILED'})
        p = receipt(folder, 'p')
        self.assertEqual((p['gates_status'], p['judge']['state'], p['judge']['model']), ('PASS', 'done', 'stand-in'))
        self.assertEqual(p['judge']['saw_expected'], 'say PASS')
        self.assertTrue(all(Path(s).is_file() for s in p['judge']['shots_cited']))  # full-size labelled stills
        self.assertIn('disagreement', receipt(folder, 'e')['status_reason'])
        self.assertEqual(receipt(folder, 'n')['clip_check']['reason'], 'no-visible-effect')
        job = json.loads((folder / 'out' / 'judge' / 'jobs' / 'p.json').read_text())
        self.assertEqual([s['phase'] for s in job['shots']], ['before', 'after'])
        self.assertEqual(job['shots'][0]['label'], 'before:before')  # judge.py: shots [{label, path}]
        self.assertEqual(job['frames'], [])  # clip frames go in as shots labelled "clip start/mid/end"
        self.assertIn('frame_error', job)    # (the fake clip has no video: none here)
        real = Path('/home/intpa/smash-training-lobby/artifacts/switch-hw-runbook/judge/judge.py')
        if real.is_file():  # the calibrated judge accepts the job as written
            import importlib.util
            spec = importlib.util.spec_from_file_location('real_judge', real)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertEqual(mod.load_job(folder / 'out' / 'judge' / 'jobs' / 'p.json')['row'], 'p')
        self.assertTrue(job['clip'].endswith('clip.mp4'))  # queued once the row's own clip file was closed
        rerun = json.loads((folder / 'out' / 'rerun.json').read_text())
        self.assertEqual(rerun['rows'], ['f', 'u', 'e', 'n'])  # k: a known product issue, not rerun
        k = receipt(folder, 'k')
        self.assertEqual((k['status'], k['known_issue']['ids']), ('KNOWN-PRODUCT-ISSUE', ['a']))
        self.assertIn('| k | a |', (folder / 'out' / 'FAILING.md').read_text())
        evidence = (folder / 'out' / 'P1-SWAP-EVIDENCE.md').read_text()
        self.assertEqual([l.split(' | ')[0] for l in evidence.splitlines() if l.startswith('| p') or l.startswith('| k')],
                         ['| p', '| k'])
        self.assertIn('--rows f,u,e,n', rerun['command'])

    def test_async_judge_pass_clears_only_visual_blockers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)

        def row(name, word, hook=None, visual=False):
            steps = [{'shot': f'{name}-before'}]
            if hook:
                steps.append({'run': f'hooks/result.py:{hook}', 'effect': True})
            steps.append({'shot': f'{name}-after'})
            result = {'id': name, 'expected': f'say {word}', 'steps': steps,
                      'shots': [{'name': f'{name}-before', 'phase': 'before'},
                                {'name': f'{name}-after', 'phase': 'after'}]}
            if visual:
                result['outcome'] = 'visual'
            return result

        rows = [row('missing', 'PASS', 'missing'), row('bad', 'PASS', 'bad'),
                row('visual', 'PASS', visual=True), row('plain', 'PASS'),
                row('mixed', 'PASS', 'missing', visual=True), row('fail', 'FAIL', 'missing', visual=True),
                row('unsure', 'UNSURE', 'missing', visual=True), row('known', 'FAIL', visual=True)]
        engine = make_plan(folder, rows, target={}, require_effect=True, judge={'cmd': 'judge.py', 'wait_s': 60},
                           known_issues={'issue': {'title': 'known', 'reason': 'stand-in judge: FAIL',
                                                   'rows': ['known']}})
        (folder / 'hooks/result.py').write_text(
            'def missing(ctx): return {"unverified": "P1 delivery count not observed"}\n'
            'def bad(ctx): return {"verdict": "FAIL"}\n')
        self.assertEqual(engine.run(), 'DONE')

        missing = receipt(folder, 'missing')
        self.assertEqual(missing['status'], 'RAN-UNVERIFIED')
        self.assertEqual((missing['status'], missing['review'], missing['review_kind']),
                         ('RAN-UNVERIFIED', True, 'unverified'))
        self.assertEqual(missing['effects_passed'], [])
        self.assertEqual(missing['judge']['verdict'], 'PASS')
        self.assertEqual([item['kind'] for item in missing['blockers']], ['unverified'])
        self.assertEqual(missing['status_reason'], 'hook hooks/result.py:missing: P1 delivery count not observed')

        bad = receipt(folder, 'bad')
        self.assertEqual((bad['status'], bad['review_kind'], bad['effects_passed']),
                         ('RAN-UNVERIFIED', 'unverified', []))
        self.assertEqual(bad['status_reason'], 'effect hook hooks/result.py:bad returned verdict FAIL')
        self.assertEqual([item['kind'] for item in bad['blockers']], ['effect'])
        self.assertEqual(bad['judge']['verdict'], 'PASS')

        visual = receipt(folder, 'visual')
        self.assertEqual((visual['status'], visual['blockers'], visual['effects_passed']), ('PASS', [], []))
        self.assertEqual(visual['cleared'][0]['kind'], 'visual')
        self.assertEqual(visual['cleared'][0]['by'], 'judge')
        self.assertEqual(visual['cleared'][0]['landed'], visual['judge']['landed'])
        self.assertEqual(receipt(folder, 'plain')['status'], 'PASS')
        self.assertEqual(receipt(folder, 'plain')['blockers'], [])

        mixed = receipt(folder, 'mixed')
        self.assertEqual((mixed['status'], mixed['review_kind']), ('RAN-UNVERIFIED', 'unverified'))
        self.assertEqual([item['kind'] for item in mixed['blockers']], ['unverified'])
        self.assertEqual((mixed['cleared'][0]['kind'], mixed['cleared'][0]['by']), ('visual', 'judge'))
        self.assertEqual(mixed['cleared'][0]['landed'], mixed['judge']['landed'])
        for name, status in (('fail', 'FAILED'), ('unsure', 'RAN-UNVERIFIED'), ('known', 'KNOWN-PRODUCT-ISSUE')):
            result = receipt(folder, name)
            self.assertEqual(result['status'], status)
            self.assertIn('visual', [item['kind'] for item in result['blockers']])
            self.assertNotIn('cleared', result)
        rerun = json.loads((folder / 'out/rerun.json').read_text())
        self.assertEqual(rerun['rows'], ['missing', 'bad', 'mixed', 'fail', 'unsure'])

    def test_legacy_default_role_with_skipped_judge_and_navigation_gate_passes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        r = make_plan(folder, [{'id': 'a', 'judge': 'skip', 'steps': [{'gate': 'nav'}]}],
                      require_effect=True, judge={'cmd': 'judge.py'})
        self.assertEqual(r.run(), 'DONE')
        result = receipt(folder, 'a')
        self.assertEqual((result['role'], result['role_source']), ('test', 'default'))
        self.assertEqual((result['status'], result['judge']['state']), ('PASS', 'skipped'))
        self.assertEqual(result['status_reason'], 'setup row: judge skipped')

    def test_setup_row_with_judge_skip_is_not_judged(self):
        folder = Path(tempfile.mkdtemp())
        r = make_plan(folder, [{'id': 's', 'judge': 'skip', 'steps': [{'gate': 'nav'}]}],
                      require_effect=True, judge={'cmd': 'judge.py', 'wait_s': 60})
        r.run()
        s = receipt(folder, 's')
        self.assertEqual((s['status'], s['judge']['state']), ('PASS', 'skipped'))
        self.assertFalse((folder / 'out' / 'judge' / 'jobs' / 's.json').exists())

    def test_judge_fails_landing_late_stop_the_run_a_row_or_two_late(self):
        folder = Path(tempfile.mkdtemp())

        def row(rid, word):
            return {'id': rid, 'expected': f'say {word}', 'steps': [{'shot': f'{rid}-before'}, {'wait_ms': 2000},
                                                                     {'shot': f'{rid}-after'}],
                    'shots': [{'name': f'{rid}-before', 'phase': 'before'}, {'name': f'{rid}-after', 'phase': 'after'}]}
        r = make_plan(folder, [row('j1', 'FAIL'), row('j2', 'FAIL'), row('j3', 'FAIL'), row('k1', 'PASS'),
                               row('k2', 'PASS'), row('k3', 'PASS'), row('k4', 'PASS')],
                      require_effect=True, judge={'cmd': 'judge.py', 'wait_s': 60}, stop_after_consecutive_failures=3,
                      session_recording={'marks': ['row-start', 'row-end', 'shot'], 'per_row_files': True})
        with self.assertRaises(harness.EarlyStop) as stop:
            r.run()
        self.assertIn('j1, j2, j3', str(stop.exception))
        self.assertEqual([receipt(folder, i)['status'] for i in ('j1', 'j2', 'j3')], ['FAILED'] * 3)
        ran = [i for i in ('k1', 'k2', 'k3', 'k4') if (folder / 'out' / f'{i}.json').exists()]
        self.assertTrue(1 <= len(ran) <= 2, ran)  # the deterministic gates passed; the verdicts stopped it late
        self.assertFalse((folder / 'out' / 'k4.json').exists())

    def test_three_consecutive_failures_stop_the_run_not_fewer(self):
        folder = Path(tempfile.mkdtemp())
        fail, good = [{'gate': 'never'}], [{'gate': 'fx'}]
        rows = [('f1', fail), ('f2', fail), ('ok', good), ('f3', fail), ('f4', fail), ('f5', fail), ('later', good)]
        r = make_plan(folder, [{'id': i, 'steps': s} for i, s in rows], require_effect=True,
                      stop_after_consecutive_failures=3, early_stop={'after': 2})
        with self.assertRaises(harness.EarlyStop):
            r.run()
        self.assertEqual(receipt(folder, 'ok')['status'], 'PASS')  # two failures (same cause) did not stop it
        self.assertEqual(receipt(folder, 'f5')['early_stop']['rule'], 'stop_after_consecutive_failures')
        self.assertFalse((folder / 'out' / 'later.json').exists())


if __name__ == '__main__':
    unittest.main()
