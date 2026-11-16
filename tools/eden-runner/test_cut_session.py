"""cut_session.py: mark parsing/pairing and span maths (pure), then real cuts from a small synthetic video.

The synthetic recording is made with GStreamer itself (x264enc where the host has it, else libav's MPEG-4 part 2
in MP4: this host has no H.264 encoder, only the avdec_h264 decoder). cut_session decodes through decodebin, so
the codec does not change the path under test. Frame grey level is 40 * floor(t), so a decoded frame's brightness
says which second of the file a clip or still came from. No OBS is needed."""
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # beside cut_session.py in tools/eden-runner
import cut_session as cs  # noqa: E402


def mark(event, row=None, label=None, rec_ms=None, **extra):
    return {'event': event, 'row': row, 'label': label, 'wall': 1.0, 'rec_ms': rec_ms,
            'frame': None if rec_ms is None else round(rec_ms * 60 / 1000), 'active': True, **extra}


def gif_label(name, preset='evidence', out=None, width=640, fps=15, max_duration_s=None, max_bytes=None):
    return json.dumps({'gif': name, 'preset': preset, 'out': out, 'width': width, 'fps': fps,
                       'max_duration_s': max_duration_s, 'max_bytes': max_bytes}, separators=(',', ':'))


class WithoutPyGObject(unittest.TestCase):
    """The module imports, parses, pairs and reports exit 2 on a machine with no PyGObject (gi blocked)."""

    def test_import_parse_and_exit_2_without_gi(self):
        import importlib.util
        blocked = {name: None for name in ('gi', 'gi.repository')}
        saved = {name: sys.modules.get(name) for name in blocked}
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        try:
            sys.modules.update(blocked)
            with self.assertRaises(ImportError):
                import gi  # noqa: F401  (proves the block is in force)
            spec = importlib.util.spec_from_file_location('cut_session_nogi', Path(cs.__file__))
            fresh = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fresh)
            (d / 'recording.jsonl').write_text(
                json.dumps(mark('start')) + '\n' + json.dumps(mark('row-start', 'R1', None, 1000)) + '\n' +
                json.dumps(mark('row-end', 'R1', None, 2000)) + '\n{"event":"sh', encoding='utf-8')
            marks, bad = fresh.parse_marks(d / 'recording.jsonl')
            self.assertEqual(len(marks), 3)
            self.assertEqual(len(bad), 1)
            self.assertEqual(len(fresh.pair_marks(marks)['rows']), 1)
            self.assertEqual(fresh.clip_plan('rows', {}, 0, 1000)['preset'], 'evidence')
            (d / 'recording.jsonl').write_text(json.dumps(mark('row-start', 'R1', None, 1)) + '\n', encoding='utf-8')
            summary, code = fresh.run(d)                      # exit 2 needs no decoder
            self.assertEqual(code, fresh.EXIT_NO_START)
            with self.assertRaises(ImportError):              # only the decode path wants gi
                fresh._gst()
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


class ParseMarks(unittest.TestCase):
    def write(self, text):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / 'recording.jsonl').write_text(text, encoding='utf-8')
        return d / 'recording.jsonl'

    def test_truncated_tail_and_garbage_are_skipped(self):
        good = json.dumps(mark('row-start', 'R1', 'R1:row-start', 1000))
        path = self.write(good + '\n\nnot json\n{"event":"bogus"}\n' + json.dumps(mark('row-end', 'R1', 'x', 2000))[:30])
        marks, bad = cs.parse_marks(path)
        self.assertEqual([m['event'] for m in marks], ['row-start'])
        self.assertEqual(bad, [3, 4, 5])

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            cs.parse_marks('/nonexistent/recording.jsonl')


class Pairing(unittest.TestCase):
    def test_rows_gifs_and_shots(self):
        la = gif_label('a', out='gifs/a.gif')
        paired = cs.pair_marks([
            mark('start'), mark('row-start', 'R1', 'R1:row-start', 1000), mark('shot', 'R1', 'menu', 1500),
            mark('gif-start', 'R1', la, 2000), mark('gif-end', 'R1', la, 4000), mark('row-end', 'R1', 'x', 5000),
            mark('stop')])
        self.assertEqual([(s['row'], s['start_ms'], s['end_ms']) for s in paired['rows']], [('R1', 1000, 5000)])
        gif = paired['gifs'][0]
        self.assertEqual((gif['name'], gif['start_ms'], gif['end_ms'], gif['spec']['out']), ('a', 2000, 4000, 'gifs/a.gif'))
        self.assertEqual(paired['shots'], [{'row': 'R1', 'label': 'menu', 'ms': 1500.0}])
        self.assertEqual(paired['incomplete'] + paired['unusable'], [])

    def test_retry_replaces_aborted_attempt(self):
        paired = cs.pair_marks([
            mark('row-start', 'R1', None, 1000), mark('row-start', 'R1', None, 3000), mark('row-end', 'R1', None, 4000)])
        self.assertEqual([(s['start_ms'], s['end_ms'], s['attempts']) for s in paired['rows']], [(3000, 4000, 2)])
        self.assertEqual(len(paired['incomplete']), 1)

    def test_open_span_and_orphan_end(self):
        paired = cs.pair_marks([mark('row-start', 'R1', None, 1000), mark('row-end', 'R2', None, 2000),
                                mark('gif-start', 'R1', gif_label('g'), 1500)])
        self.assertEqual(paired['rows'] + paired['gifs'], [])
        self.assertEqual(sorted(i['reason'] for i in paired['incomplete']),
                         ['end mark without a start', 'no end mark yet', 'no end mark yet'])

    def test_marks_without_rec_ms_are_unusable(self):
        paired = cs.pair_marks([mark('row-start', 'R1', None, None), mark('row-end', 'R1', None, 2000),
                                mark('shot', 'R1', 'x', None)])
        self.assertEqual(paired['rows'], [])
        self.assertEqual(len(paired['unusable']), 2)

    def test_same_gif_name_in_two_rows_is_two_spans(self):
        la = gif_label('a')
        paired = cs.pair_marks([mark('gif-start', 'R1', la, 0), mark('gif-end', 'R1', la, 1000),
                                mark('gif-start', 'R2', la, 2000), mark('gif-end', 'R2', la, 3000)])
        self.assertEqual([g['row'] for g in paired['gifs']], ['R1', 'R2'])


class SpanMaths(unittest.TestCase):
    def test_span_ns_and_offset(self):
        self.assertEqual(cs.span_ns(1500, 2500), (1_500_000_000, 2_500_000_000))
        self.assertEqual(cs.span_ns(1500, 2500, 250), (1_750_000_000, 2_750_000_000))
        self.assertEqual(cs.span_ns(10, 20, -500), (0, 0))

    def test_evidence_plan(self):
        plan = cs.clip_plan('rows', {}, 0, 20000)
        self.assertEqual((plan['preset'], plan['width'], plan['fps'], plan['max_bytes']), ('evidence', 640, 15.0, 10 * 1024 * 1024))
        self.assertEqual(plan['duration_s'], 20.0)
        self.assertFalse(plan['truncated'])

    def test_label_cannot_exceed_preset_limits(self):
        plan = cs.clip_plan('gifs', {'preset': 'evidence', 'fps': 60, 'width': 800, 'max_bytes': 999_999_999}, 0, 1000)
        self.assertEqual((plan['fps'], plan['width'], plan['max_bytes']), (15.0, 800, 10 * 1024 * 1024))
        plan = cs.clip_plan('gifs', {'preset': 'evidence', 'fps': 5, 'max_bytes': 1000}, 0, 1000)
        self.assertEqual((plan['fps'], plan['max_bytes']), (5.0, 1000))

    def test_docs_plan_caps_at_ten_seconds(self):
        plan = cs.clip_plan('gifs', {'preset': 'docs', 'width': 1280, 'fps': 15, 'max_duration_s': 30}, 0, 25000)
        self.assertEqual((plan['preset'], plan['width'], plan['duration_s'], plan['truncated']), ('docs', 1280, 10.0, True))
        plan = cs.clip_plan('gifs', {'preset': 'docs', 'max_duration_s': 4}, 0, 25000)
        self.assertEqual(plan['duration_s'], 4.0)

    def test_long_row_lowers_fps_to_fit_the_frame_budget(self):
        plan = cs.clip_plan('rows', {}, 0, 300_000)
        self.assertLessEqual(plan['duration_s'] * plan['fps'], cs.FRAME_CAP + 1e-6)
        self.assertGreaterEqual(plan['fps'], 1.0)
        self.assertFalse(plan['truncated'])
        plan = cs.clip_plan('rows', {}, 0, 3_000_000)
        self.assertTrue(plan['truncated'])

    def test_still_and_out_names(self):
        taken = set()
        self.assertEqual(cs.still_name({'row': 'R1', 'label': 'R1:menu open'}, taken), 'R1-menu_open.png')
        self.assertEqual(cs.still_name({'row': 'R1', 'label': 'R1:menu open'}, taken), 'R1-menu_open-2.png')
        out = Path('/o')
        self.assertEqual(cs.gif_out_path(out, {'row': 'R1', 'name': 'a b', 'spec': {}}), out / 'R1' / 'a_b.gif')
        self.assertEqual(cs.gif_out_path(out, {'row': 'R1', 'name': 'a', 'spec': {'out': 'g/a.gif'}}), out / 'g' / 'a.gif')
        self.assertEqual(cs.gif_out_path(out, {'row': 'R1', 'name': 'a', 'spec': {'out': '/x/a.gif'}}), Path('/x/a.gif'))


class Resampling(unittest.TestCase):
    def pick(self, fps, pts_list, max_frames=None):
        picker = cs.SlotPicker(fps, max_frames)
        for i, pts in enumerate(pts_list):
            picker.add(pts, lambda i=i: i)
        return picker.finish()

    def test_60_to_15_takes_every_fourth_frame(self):
        pts = [i * 16_666_667 for i in range(600)]
        picked = self.pick(15, pts)
        self.assertEqual(picked[:4], [0, 4, 8, 12])
        self.assertEqual(len(picked), 150)

    def test_gap_repeats_last_good_frame_to_keep_real_time(self):
        pts = [0, 33_333_333, 66_666_667, 500_000_000, 533_333_333]     # a half-second stall
        picked = self.pick(10, pts)
        # slots at 0, .1, .2, .3, .4, .5 s: the newest frame at or before each (frame 2 holds through the stall)
        self.assertEqual(picked, [0, 2, 2, 2, 2, 3])

    def test_max_frames_and_duplicate_pts(self):
        pts = [i * 16_666_667 for i in range(600)] + [599 * 16_666_667]
        self.assertEqual(len(self.pick(15, pts, max_frames=20)), 20)
        self.assertEqual(len(self.pick(15, pts)), 150)

    def test_gif_durations_track_real_time(self):
        d = cs.gif_durations(15, 15)
        self.assertTrue(all(x % 10 == 0 and x >= 20 for x in d))
        self.assertEqual(sum(d), 1000)
        self.assertEqual(sum(cs.gif_durations(30, 1.0)), 30000)


class Encoding(unittest.TestCase):
    def noise(self, n, size=(160, 90)):
        import os
        from PIL import Image
        return [Image.frombytes('RGB', size, os.urandom(size[0] * size[1] * 3)) for _ in range(n)]

    def test_fits_by_dropping_colours_then_frames(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest('Pillow is not installed')
        images = self.noise(12)
        full, info = cs.encode_fitting(images, 12.0, None)
        self.assertEqual((info['frames'], info['colours']), (12, 128))
        small, info = cs.encode_fitting(images, 12.0, int(len(full) * 0.45))
        self.assertLessEqual(len(small), int(len(full) * 0.45))
        self.assertTrue(info['colours'] < 128 or info['frames'] < 12)

    def test_impossible_limit_raises(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest('Pillow is not installed')
        with self.assertRaises(ValueError):
            cs.encode_fitting(self.noise(4), 4.0, 100)


class RunErrors(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def write(self, marks):
        (self.d / 'recording.jsonl').write_text(''.join(json.dumps(m) + '\n' for m in marks), encoding='utf-8')

    def test_no_log_or_no_start_exits_2(self):
        summary, code = cs.run(self.d)
        self.assertEqual(code, cs.EXIT_NO_START)
        self.write([mark('row-start', 'R1', None, 1000), mark('row-end', 'R1', None, 2000)])
        summary, code = cs.run(self.d)
        self.assertEqual(code, cs.EXIT_NO_START)
        self.assertIn('no start event', summary['error'])

    def test_cli_exit_code_and_message(self):
        self.write([mark('row-start', 'R1', None, 1000)])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cs.main([str(self.d)])
        self.assertEqual(code, 2)
        self.assertIn('no start event', err.getvalue())
        self.assertFalse(json.loads(out.getvalue())['ok'])

    def test_runner_reason_is_reported_for_exit_2(self):
        (self.d / 'actions.jsonl').write_text(
            json.dumps({'call': 'x'}) + '\nnot json\n' +
            json.dumps({'session_recording': 'unavailable', 'error': "OBS unavailable: ObsError('auth')", 'time': 1}) + '\n',
            encoding='utf-8')
        summary, code = cs.run(self.d)                       # no recording.jsonl at all
        self.assertEqual(code, cs.EXIT_NO_START)
        self.assertEqual(summary['session_unavailable'], "OBS unavailable: ObsError('auth')")
        self.assertIn("runner reason: OBS unavailable", summary['error'])
        self.write([mark('row-start', 'R1', None, 1000)])    # a log with marks but no start
        summary, code = cs.run(self.d)
        self.assertEqual(code, cs.EXIT_NO_START)
        self.assertIn('no start event', summary['error'])
        self.assertIn('runner reason', summary['error'])
        (self.d / 'actions.jsonl').write_text(json.dumps({'session_recording': 'unavailable', 'reason': 'no card'}) + '\n')
        self.assertEqual(cs.run(self.d)[0]['session_unavailable'], 'no card')

    def test_no_runner_record_leaves_the_message_plain(self):
        (self.d / 'actions.jsonl').write_text(json.dumps({'call': 'x'}) + '\n')
        summary, _ = cs.run(self.d)
        self.assertNotIn('session_unavailable', summary)
        self.assertNotIn('runner reason', summary['error'])

    def test_missing_recording_exits_3(self):
        self.write([mark('start'), mark('stop', path='/nonexistent/rec.mp4')])
        _, code = cs.run(self.d, recording='/nonexistent/other.mp4')
        self.assertEqual(code, cs.EXIT_NO_RECORDING)


def _encoder():
    """(encoder element, muxer element) usable on this host for the synthetic video, or None."""
    try:
        Gst = cs._gst()
    except Exception:  # noqa: BLE001
        return None
    for enc, mux in (('x264enc', 'mp4mux'), ('avenc_mpeg4', 'mp4mux'), ('vp8enc', 'matroskamux')):
        if all(Gst.ElementFactory.find(n) for n in (enc, mux, 'appsrc', 'decodebin', 'videoconvert')):
            return enc, mux
    return None


def make_video(path, enc, mux, seconds=6, fps=30, size=(320, 180)):
    """A video whose grey level is 40 * floor(t); returns when the file is finalised."""
    Gst = cs._gst()
    props = {'x264enc': 'key-int-max=30 tune=zerolatency', 'avenc_mpeg4': 'gop-size=30 bitrate=3000000',
             'vp8enc': 'keyframe-max-dist=30 target-bitrate=3000000'}[enc]
    pipe = Gst.parse_launch(
        f'appsrc name=src format=time caps=video/x-raw,format=RGB,width={size[0]},height={size[1]},framerate={fps}/1 '
        f'! videoconvert ! {enc} {props} ! {mux} ! filesink location="{path}"')
    src = pipe.get_by_name('src')
    pipe.set_state(Gst.State.PLAYING)
    for n in range(seconds * fps):
        grey = 40 * (n // fps)
        buf = Gst.Buffer.new_wrapped(bytes([grey]) * (size[0] * size[1] * 3))
        buf.pts = n * Gst.SECOND // fps
        buf.duration = Gst.SECOND // fps
        src.emit('push-buffer', buf)
    src.emit('end-of-stream')
    msg = pipe.get_bus().timed_pop_filtered(30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipe.set_state(Gst.State.NULL)
    if msg is None or msg.type == Gst.MessageType.ERROR:
        raise RuntimeError(f'could not make the test video: {msg and msg.parse_error()}')


ENCODER = _encoder()


@unittest.skipIf(ENCODER is None, 'no usable GStreamer encoder/muxer (x264enc, avenc_mpeg4 or vp8enc) or PyGObject')
class RealCuts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp())
        cls.video = cls.base / ('rec.mkv' if ENCODER[1] == 'matroskamux' else 'rec.mp4')
        make_video(cls.video, *ENCODER)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, True)

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out, True)

    def write(self, marks):
        (self.out / 'recording.jsonl').write_text(''.join(json.dumps(m) + '\n' for m in marks), encoding='utf-8')

    def grey(self, image):
        from PIL import ImageStat
        return ImageStat.Stat(image.convert('L')).mean[0]

    def session(self, extra, stop=True):
        marks = [mark('start')] + extra
        if stop:
            marks.append(mark('stop', path=str(self.video), active=False))
        self.write(marks)

    def test_still_is_full_size_and_from_the_marked_second(self):
        from PIL import Image
        self.session([mark('shot', 'R1', 'R1:menu', 2500), mark('shot', 'R1', 'late', 4200)])
        summary, code = cs.run(self.out, only=['stills'])
        self.assertEqual(code, 0, summary)
        names = sorted(Path(s['path']).name for s in summary['stills'])
        self.assertEqual(names, ['R1-late.png', 'R1-menu.png'])
        menu = Image.open(self.out / 'stills' / 'R1-menu.png')
        late = Image.open(self.out / 'stills' / 'R1-late.png')
        self.assertEqual(menu.size, (320, 180))                       # full size, not scaled to a preset
        self.assertAlmostEqual(self.grey(menu), 80, delta=10)         # t = 2.5 s -> second 2
        self.assertAlmostEqual(self.grey(late), 160, delta=10)        # t = 4.2 s -> second 4

    def test_gif_pair_cut_with_receipt(self):
        from PIL import Image
        label = gif_label('demo', 'evidence', 'gifs/demo.gif')
        self.session([mark('row-start', 'R1', None, 500), mark('gif-start', 'R1', label, 1000),
                      mark('gif-end', 'R1', label, 3500), mark('row-end', 'R1', None, 5500)])
        summary, code = cs.run(self.out, only=['gifs'])
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary['counts']['gifs'], 1)
        self.assertEqual(summary['counts']['rows'], 0)
        path = self.out / 'gifs' / 'demo.gif'
        receipt = json.loads(Path(str(path) + '.json').read_text())
        data = path.read_bytes()
        self.assertEqual(receipt['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(receipt['bytes'], len(data))
        self.assertEqual(receipt['path'], str(path))
        self.assertEqual(receipt['source']['rec_ms'], [1000, 3500])
        self.assertEqual(receipt['source']['recording'], str(self.video))
        gif = Image.open(io.BytesIO(data))
        self.assertEqual(gif.size[0], 640)
        self.assertLessEqual(receipt['fps'], 15)
        self.assertEqual(gif.n_frames, receipt['frames'])        # the receipt counts what the file holds
        self.assertAlmostEqual(receipt['frames_sampled'], 2.5 * 15, delta=2)   # frames fed (Pillow merges still runs)
        self.assertAlmostEqual(receipt['duration_s'], 2.5, delta=0.2)
        gif.seek(0)
        self.assertAlmostEqual(self.grey(gif.convert('RGB')), 40, delta=10)           # starts in second 1
        gif.seek(gif.n_frames - 1)
        self.assertAlmostEqual(self.grey(gif.convert('RGB')), 120, delta=10)          # ends in second 3 (t=3.4)

    def test_docs_gif_is_cut_at_ten_seconds_and_default_path(self):
        label = gif_label('long', 'docs', None, 1280, 15, 2)
        self.session([mark('gif-start', 'R7', label, 0), mark('gif-end', 'R7', label, 5000)])
        summary, code = cs.run(self.out, only=['gifs'])
        self.assertEqual(code, 0, summary)
        receipt = summary['gifs'][0]
        self.assertEqual(receipt['path'], str(self.out / 'R7' / 'long.gif'))
        self.assertEqual(receipt['width'], 1280)
        self.assertAlmostEqual(receipt['duration_s'], 2.0, delta=0.2)
        self.assertEqual(receipt['preset'], 'docs')

    def test_row_clip_stills_and_row_filter(self):
        self.session([mark('row-start', 'R1', None, 1000), mark('shot', 'R1', 'a', 1500), mark('row-end', 'R1', None, 2000),
                      mark('row-start', 'R2', None, 3000), mark('shot', 'R2', 'b', 3500), mark('row-end', 'R2', None, 4000)])
        summary, code = cs.run(self.out, rows=['R2'])
        self.assertEqual(code, 0, summary)
        self.assertEqual([Path(r['path']).name for r in summary['rows']], ['R2.gif'])
        self.assertEqual([Path(s['path']).name for s in summary['stills']], ['R2-b.png'])
        self.assertTrue((self.out / 'clips' / 'R2.gif.json').is_file())
        self.assertFalse((self.out / 'clips' / 'R1.gif').exists())
        summary, code = cs.run(self.out, only=['rows'])
        self.assertEqual((summary['counts']['rows'], summary['counts']['stills']), (2, 0))

    def test_partial_skips_open_and_truncated_spans_without_failing(self):
        la = gif_label('ok', out='g/ok.gif')
        lb = gif_label('cut', out='g/cut.gif')
        self.session([mark('gif-start', 'R1', la, 1000), mark('gif-end', 'R1', la, 2000),
                      mark('gif-start', 'R1', lb, 5000), mark('gif-end', 'R1', lb, 60_000),   # beyond the 6 s file
                      mark('row-start', 'R2', None, 3000),                                       # no end mark yet
                      mark('shot', 'R2', 'beyond', 90_000)], stop=False)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cs.main([str(self.out), '--partial', '--recording', str(self.video)])
        self.assertEqual(code, 0, out.getvalue())
        summary = json.loads(out.getvalue())
        self.assertEqual([Path(g['path']).name for g in summary['gifs']], ['ok.gif'])
        self.assertFalse((self.out / 'g' / 'cut.gif').exists())
        reasons = {(s['kind'], s['name']): s['reason'] for s in summary['skipped']}
        self.assertIn('truncated tail', reasons[('gifs', 'cut')])
        self.assertEqual(reasons[('rows', 'R2')], 'no end mark yet')
        self.assertIn('does not reach', reasons[('still', 'beyond')])
        self.assertTrue(summary['ok'])

    def test_unfinished_video_file_is_a_failure_not_a_crash(self):
        broken = self.out / 'broken.mp4'
        broken.write_bytes(b'\x00' * 2048)
        self.session([mark('row-start', 'R1', None, 0), mark('row-end', 'R1', None, 1000)], stop=False)
        summary, code = cs.run(self.out, recording=str(broken))
        self.assertEqual(code, cs.EXIT_FAILED)
        self.assertEqual(summary['failed'][0]['row'], 'R1')


if __name__ == '__main__':
    unittest.main()
