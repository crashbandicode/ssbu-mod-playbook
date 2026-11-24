"""Checked effects, the async judge, the clip check and the consecutive-failure stop rule (judge_queue.py + runner)."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


class StatusRules(unittest.TestCase):
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

    def test_clip_check_no_visible_effect_and_missing_shot(self):
        from PIL import Image
        d = Path(tempfile.mkdtemp())
        for name, colour in (('a', (90, 90, 90)), ('b', (91, 90, 90)), ('c', (200, 30, 30))):
            Image.new('RGB', (64, 36), colour).save(d / f'{name}.png')
        same = {'shots': [{'name': 'x-a', 'phase': 'before', 'path': str(d / 'a.png')},
                          {'name': 'x-b', 'phase': 'after', 'path': str(d / 'b.png')}]}
        self.assertEqual(jq.clip_check(same, {})['reason'], 'no-visible-effect')
        changed = {'shots': [same['shots'][0], {'name': 'x-c', 'phase': 'after', 'path': str(d / 'c.png')}]}
        self.assertTrue(jq.clip_check(changed, {})['ok'])
        missing = {'shots': [same['shots'][0], {'name': 'x-d', 'phase': 'after', 'path': None}]}
        self.assertIn('expected-result shot missing', jq.clip_check(missing, {})['reason'])
        # the clip frames count too: start vs end
        self.assertEqual(jq.clip_check({'shots': []}, {'start': str(d / 'a.png'), 'end': str(d / 'b.png')})['reason'],
                         'no-visible-effect')


class RunnerEffects(unittest.TestCase):
    def hook_run(self, value, *, target=False, effect=False, role='test', outcome=None):
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
        (folder / 'hooks/result.py').write_text(f'def check(ctx):\n    return {value!r}\n')
        self.assertEqual(r.run(), 'DONE')
        return folder, r

    def record_read(self, folder, still, verdict='PASS', text='the expected text is visible'):
        return subprocess.run([sys.executable, '-I', '-B', str(HERE / 'runner.py'), 'record-read',
                               str(folder / 'rows.json'), '--out', str(folder / 'out'), '--rows', 'a',
                               '--reader', 'reviewer', '--verdict', verdict, '--text', text, '--still', str(still)],
                              capture_output=True, text=True)

    def test_unverified_hook_markers_never_become_pass_without_a_judge(self):
        for value, marker in (({'file': None, 'grade': 'NEEDS VISUAL READ'}, 'NEEDS VISUAL READ'),
                              ({'unverified': 'look at the bar'}, 'look at the bar'),
                              ({'visual_verdict': 'pending'}, 'pending')):
            with self.subTest(value=value):
                folder, _ = self.hook_run(value)
                r = receipt(folder, 'a')
                self.assertEqual(r['status'], 'RAN-UNVERIFIED')
                self.assertEqual((r['gates_status'], r['review'], r['review_kind']), ('PASS', True, 'visual'))
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
        folder, _ = self.hook_run(True, outcome='visual')
        still = folder / 'out/a/still.png'
        still.write_bytes(b'fixture image')
        self.assertEqual(receipt(folder, 'a')['status'], 'RAN-UNVERIFIED')
        result = self.record_read(folder, still, 'FAIL', 'shows 30..127, expected 30..97')
        self.assertEqual(result.returncode, 0, result.stderr)
        failed = receipt(folder, 'a')
        self.assertEqual(failed['status'], 'FAILED')
        self.assertEqual(failed['visual_read']['text'], 'shows 30..127, expected 30..97')
        self.assertNotEqual(self.record_read(folder, still).returncode, 0)
        self.assertEqual(receipt(folder, 'a'), failed)

    def test_record_read_accepts_only_a_pending_visual_receipt_and_owned_still(self):
        folder, _ = self.hook_run(True, outcome='visual')
        still = folder / 'out/a/still.png'
        still.write_bytes(b'fixture image')
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
        changed = {'visual_read', 'visual_reads', 'status', 'status_reason', 'review'}
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
