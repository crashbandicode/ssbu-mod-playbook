import contextlib
import io
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import compliance
from compliance import guard_size, new_refusals, approved_restrictions, trace_tables, defined_tests, rows_for


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)


def repo_with(base_files, branch_files):
    """A temp git repo: one commit with base_files, a second with branch_files. Returns (path, base sha)."""
    root = Path(tempfile.mkdtemp())
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 't@example.invalid')
    git(root, 'config', 'user.name', 't')
    for files in (base_files, branch_files):
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding='utf-8')
        git(root, 'add', '-A')
        git(root, 'commit', '-q', '-m', 'c', '--allow-empty')
        if files is base_files:
            base = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], check=True,
                                  capture_output=True, text=True).stdout.strip()
    return root, base


class Refusals(unittest.TestCase):
    """The 2026-11-05 combo-switch case: a Switch refused with "Turn Player Swapping on" without approval."""

    def test_an_unannotated_refusal_the_branch_adds_is_reported(self):
        repo, base = repo_with({'src/a.rs': 'fn a() {}\n'}, {'src/a.rs': (
            'fn a() {}\nfn b() {\n    notify("Turn Player Swapping on to switch Player 1");\n}\n')})
        hits = new_refusals(repo, base)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], 'src/a.rs:3')
        self.assertIsNone(hits[0][2])

    def test_an_annotated_refusal_cites_its_id_and_the_registry_approves_it(self):
        repo, base = repo_with({'src/a.rs': 'fn a() {}\n', 'docs/RESTRICTIONS.md': (
            '| ID | Message | Why | Approved |\n| --- | --- | --- | --- |\n'
            '| `swap-off` | Turn Player Swapping on | P1 cycle policy | operator 2026-11-05 |\n'
            '| `later` | x | y | pending |\n')}, {'src/a.rs': (
            'fn a() {}\n// restriction: swap-off\nfn b() { notify("Turn Player Swapping on to switch"); }\n')})
        hits = new_refusals(repo, base)
        self.assertEqual([h[2] for h in hits], ['swap-off'])
        self.assertEqual(approved_restrictions(repo), {'swap-off'})

    def test_existing_refusals_comments_tests_and_plain_strings_are_not_reported(self):
        repo, base = repo_with({'src/a.rs': 'fn a() { notify("Character swap unavailable"); }\n'}, {
            'src/a.rs': 'fn a() { notify("Character swap unavailable"); }\n// "this is unavailable" in a comment\n'
                        'fn c() { notify("Stage ready"); }\n',
            'tests/t.rs': 'fn t() { assert_eq!(x, "unavailable"); }\n',
            'src/b_tests.rs': 'fn t() { let _ = "cannot"; }\n'})
        self.assertEqual(new_refusals(repo, base), [])

    def test_positively_phrased_scope_limits_and_waits_are_reported(self):
        """2026-11-05: "Combos work in Training" limited combos to Training and slipped past the first pattern."""
        repo, base = repo_with({'src/a.rs': ''}, {'src/a.rs': (
            'fn a() { return Err("Combos work in Training".into()); }\n'
            'fn b() { return Err("Wait for the practice fighters to finish loading".into()); }\n'
            'fn c() { notify("Recording works only in native Training"); }\n'
            'fn d() { notify("Stage ready"); }\n')})
        self.assertEqual([h[0] for h in new_refusals(repo, base)], ['src/a.rs:1', 'src/a.rs:2', 'src/a.rs:3'])

    def test_in_file_test_modules_and_assertions_are_not_reported(self):
        """2026-11-05 rc.5: `assert!(… " - unavailable")` in a `#[cfg(test)] mod tests` was reported."""
        repo, base = repo_with({'src/a.rs': 'fn a() {}\n'}, {'src/a.rs': (
            'fn a() {}\n'
            '#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {\n'
            '        let s = "x - unavailable";\n        assert!(s.ends_with(" - unavailable"));\n    }\n}\n'
            'fn after() { notify("Swap unavailable"); }\n')})
        self.assertEqual([h[0] for h in new_refusals(repo, base)], ['src/a.rs:10'])

    def test_a_touched_call_whose_refusal_text_was_already_there_is_not_reported(self):
        """2026-11-11 TrainingRooms rc.8: `bank_refused(b, …)` became `bank_refused(&b, …)`, the same message."""
        call = 'bank_refused({}, "CPU bank member switch refused before the native request", false);'
        repo, base = repo_with({'src/a.rs': 'fn a(b: u8) {\n    ' + call.format('b') + '\n}\n'},
                               {'src/a.rs': 'fn a(b: u8) {\n    ' + call.format('&b') + '\n}\n'})
        self.assertEqual(new_refusals(repo, base), [])

    def test_a_refusal_moved_to_another_file_is_not_reported(self):
        line = 'fn a() { notify("Character swap unavailable"); }\n'
        repo, base = repo_with({'src/a.rs': line, 'src/b.rs': ''}, {'src/a.rs': '', 'src/b.rs': line})
        self.assertEqual(new_refusals(repo, base), [])

    def test_a_further_copy_of_an_existing_refusal_is_reported(self):
        repo, base = repo_with({'src/a.rs': 'fn a() { notify("Character swap unavailable"); }\n'}, {
            'src/a.rs': 'fn a() { notify("Character swap unavailable"); }\n'
                        'fn b() { notify("Character swap unavailable"); }\n'})
        self.assertEqual([h[0] for h in new_refusals(repo, base)], ['src/a.rs:2'])

    def test_a_removed_comment_or_assertion_gives_no_budget_to_a_new_refusal(self):
        """The docs lane's review of R12 (2026-11-11): text the row never counts can't offset a real new refusal."""
        for skipped in ('// notify("Character swap unavailable");', 'assert_eq!(m, "Character swap unavailable");'):
            repo, base = repo_with({'src/a.rs': f'fn a(m: &str) {{\n    {skipped}\n}}\n'},
                                   {'src/a.rs': 'fn a(m: &str) {\n    notify("Character swap unavailable");\n}\n'})
            self.assertEqual([h[0] for h in new_refusals(repo, base)], ['src/a.rs:2'], skipped)

    def test_rows_fail_without_approval_and_ask_for_base_when_none_is_given(self):
        repo, base = repo_with({'src/a.rs': ''}, {'src/a.rs': 'fn b() { notify("Only when Player Swapping is On"); }\n'})
        row = {name: (result, ev) for name, _, result, ev in rows_for(repo, None, base)}
        refusal = next(v for k, v in row.items() if k.startswith('New user-facing refusals'))
        self.assertEqual(refusal[0], 'fail')
        self.assertIn('src/a.rs:1', refusal[1])
        unset = {name: result for name, _, result, _ in rows_for(repo, None)}
        self.assertEqual(next(v for k, v in unset.items() if k.startswith('New user-facing refusals')), 'agent')


class Trace(unittest.TestCase):
    REPORT = ('# Design\n\n| Operator clause | Design | Test |\n| --- | --- | --- |\n'
              '| "the same logic that the replay macro uses" | shared gate | `parity_with_named_macros` |\n'
              '| "they can say yes or no" | Switch/Keep | `tests::switch_and_keep`, `missing_fn` |\n'
              '| "no prompt when they match" | gate | |\n\nOther text.\n')

    def test_the_trace_table_is_read_with_its_test_names(self):
        rows = trace_tables(self.REPORT)
        self.assertEqual([names for _, names in rows],
                         [['parity_with_named_macros'], ['switch_and_keep', 'missing_fn'], []])

    def test_the_row_fails_on_a_missing_test_and_a_clause_without_one(self):
        repo, base = repo_with({'src/a.rs': 'fn parity_with_named_macros() {}\n'},
                               {'tests/t.rs': 'fn switch_and_keep() {}\n'})
        report = repo / 'design.md'
        report.write_text(self.REPORT, encoding='utf-8')
        self.assertIn('switch_and_keep', defined_tests(repo))
        result = {name: (res, ev) for name, _, res, ev in rows_for(repo, None, None, [str(report)])}
        trace = next(v for k, v in result.items() if k.startswith('Design report traces'))
        self.assertEqual(trace[0], 'fail')
        self.assertIn('missing_fn', trace[1])
        self.assertIn('no test named', trace[1])


class ReleaseProfile(unittest.TestCase):
    def row(self, opt):
        cargo = ('[package]\nname = "p"\n[lib]\ncrate-type = ["cdylib"]\n[profile.release]\n'
                 f'opt-level = {opt}\nlto = true\ncodegen-units = 1\npanic = "abort"\n')
        repo, _ = repo_with({'Cargo.toml': cargo}, {})
        rows = {name: result for name, _, result, _ in rows_for(repo, None)}
        return next(v for k, v in rows.items() if k.startswith('release profile'))

    def measured(self, opt):
        cargo = ('[package]\nname = "p"\n[lib]\ncrate-type = ["cdylib"]\n[profile.release]\n'
                 f'opt-level = {opt}\nlto = true\ncodegen-units = 1\npanic = "abort"\n')
        repo, _ = repo_with({'Cargo.toml': cargo}, {})
        return [result for name, _, result, _ in rows_for(repo, None) if name.startswith('opt-level "z" measured')]

    def test_s_and_measured_z_pass_and_other_levels_fail(self):
        # TrainingRooms rc.8 R6 (2026-11-11): "z" when measured (CHECKLIST); "3" and "2" stay refused.
        self.assertEqual(self.row('"s"'), 'pass')
        self.assertEqual(self.row('"z"'), 'pass')
        # "z" adds the agent row for its measurements; "s" doesn't.
        self.assertIn('agent', self.measured('"z"'))
        self.assertEqual(self.measured('"s"'), [])
        self.assertEqual(self.row('3'), 'fail')
        self.assertEqual(self.row('"2"'), 'fail')


class GuardSize(unittest.TestCase):
    def test_project_repeat_initializer_has_sixty_four_bytes(self):
        source = "pub const GUARD_BYTES: usize = 64;"
        self.assertEqual(guard_size("0; GUARD_BYTES", source), 64)
        self.assertEqual(guard_size("0; 64", ""), 64)
        self.assertEqual(guard_size("0; 6_4", ""), 64)

    def test_wrong_and_unresolved_repeats_do_not_pass(self):
        self.assertEqual(guard_size("0; 63", ""), 63)
        for source in ("", "const GUARD_BYTES: usize = other;",
                       "const GUARD_BYTES: usize = 64; const GUARD_BYTES: usize = 32;",
                       "const GUARD_BYTES: usize = 64; const GUARD_BYTES: usize = other;"):
            self.assertEqual(guard_size("0; GUARD_BYTES", source), 0)

    def test_existing_literal_guard_counts_are_preserved(self):
        for count in (0, 1, 63, 64, 65):
            self.assertEqual(guard_size(", ".join(["0x12"] * count), ""), count)


class PublicationMedia(unittest.TestCase):
    VALID_VIDEO = ('<video src="docs/media/clip.mp4" autoplay muted loop playsinline controls></video>')
    VIDEO_INFO = {'codec': 'h264', 'pix_fmt': 'yuv420p', 'width': 1280, 'height': 720,
                  'fps': 60.0, 'seconds': 10.0, 'audio': False, 'faststart': True}

    def repo(self, readme):
        repo = repo_with({'README.md': 'Demo\n\ncargo skyline build\n\nMIT License\n\n' + readme}, {})[0]
        git(repo, 'remote', 'add', 'origin', 'https://git.example.invalid/owner/repo.git')
        return repo

    def asset(self, repo, name, content=b'media fixture'):
        path = repo / 'docs' / 'media' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def committed_asset(self, repo, name, content=b'media fixture'):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        git(repo, 'add', '--', name)
        git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-q', '-m', 'fixture asset')
        return subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD:' + name], check=True,
                              capture_output=True, text=True).stdout.strip()

    def blob_url(self, blob):
        return 'https://git.example.invalid/owner/repo/media/blob/' + blob

    def video(self, source):
        return self.VALID_VIDEO.replace('docs/media/clip.mp4', source)

    def rows(self, repo):
        return {name: (result, evidence) for name, _, result, evidence in rows_for(repo, None)}

    def tagged_rows(self, repo):
        return {name: (tag, result, evidence) for name, tag, result, evidence in rows_for(repo, None)}

    def test_forge_blob_url_of_a_committed_clip_is_an_embed(self):
        repo = self.repo('')
        blob = self.committed_asset(repo, 'docs/media/clip.mp4')
        (repo / 'README.md').write_text('cargo skyline build\nMIT License\n' + self.video(self.blob_url(blob)))
        (repo / 'docs' / 'guide.md').write_text(self.video(self.blob_url(blob.upper())))
        with patch('compliance.mp4_info', return_value=self.VIDEO_INFO):
            rows = self.rows(repo)
        self.assertEqual(rows['README has a demo, build commands and a license line'][0], 'pass')
        self.assertEqual(rows['MP4 docs/media/clip.mp4: publication encoding'][0], 'pass')
        self.assertFalse(any(name.endswith('MP4 embed') for name in rows))

    def test_forge_blob_url_must_name_an_mp4_committed_under_docs_media(self):
        repo = self.repo('')
        blob = self.committed_asset(repo, 'docs/media/clip.mp4', b'committed clip')
        outside = self.committed_asset(repo, 'assets/clip.mp4', b'outside clip')
        gif = self.committed_asset(repo, 'docs/media/clip.gif', b'gif file')
        draft = self.asset(repo, 'draft.mp4', b'untracked clip')
        staged = self.asset(repo, 'staged.mp4', b'staged clip')
        git(repo, 'add', '--', 'docs/media/staged.mp4')

        def hash_file(path):
            return subprocess.run(['git', '-C', str(repo), 'hash-object', str(path)], check=True,
                                  capture_output=True, text=True).stdout.strip()

        url = self.blob_url(blob)
        cases = {
            'unknown': self.blob_url('0' * 40),
            'untracked': self.blob_url(hash_file(draft)),
            'staged': self.blob_url(hash_file(staged)),
            'outside': self.blob_url(outside),
            'gif': self.blob_url(gif),
            'http': url.replace('https://', 'http://'),
            'wrong path': url.replace('/media/blob/', '/raw/blob/'),
            'query': url + '?x=1',
            'fragment': url + '#clip',
        }
        for case, source in cases.items():
            with self.subTest(case=case):
                (repo / 'README.md').write_text('cargo skyline build\nMIT License\n' + self.video(source))
                with patch('compliance.mp4_info', return_value=self.VIDEO_INFO):
                    rows = self.rows(repo)
                self.assertEqual(rows['README.md video 1: MP4 embed'][0], 'fail')
                self.assertEqual(rows['README has a demo, build commands and a license line'][0], 'fail')

    def test_a_relative_mp4_src_stays_valid_beside_a_blob_url(self):
        repo = self.repo('')
        self.committed_asset(repo, 'docs/media/relative.mp4', b'relative clip')
        blob = self.committed_asset(repo, 'docs/media/clip.mp4', b'blob clip')
        relative = self.VALID_VIDEO.replace('clip.mp4', 'relative.mp4')
        (repo / 'README.md').write_text('cargo skyline build\nMIT License\n' +
                                       relative + '\n' + self.video(self.blob_url(blob)))
        with patch('compliance.mp4_info', return_value=self.VIDEO_INFO):
            rows = self.rows(repo)
        self.assertEqual(rows['README has a demo, build commands and a license line'][0], 'pass')
        self.assertEqual(rows['MP4 docs/media/relative.mp4: publication encoding'][0], 'pass')
        self.assertIn('MP4 docs/media/clip.mp4: publication encoding', rows)
        self.assertEqual(rows['MP4 docs/media/clip.mp4: publication encoding'][0], 'pass')
        self.assertFalse(any(name.endswith('MP4 embed') for name in rows))

    def test_blob_source_points_to_the_checked_repository(self):
        repo = self.repo('')
        blob = self.committed_asset(repo, 'docs/media/clip.mp4')
        for segment in ('git.example.invalid', 'owner', 'repo/'):
            with self.subTest(segment=segment):
                source = self.blob_url(blob).replace(segment, 'other-' + segment)
                (repo / 'README.md').write_text(self.video(source))
                with patch('compliance.mp4_info', return_value=self.VIDEO_INFO) as probe:
                    rows = self.rows(repo)
                self.assertEqual(rows.get('README.md video 1: MP4 embed', ('pass',))[0], 'fail')
                probe.assert_not_called()

    def test_blob_source_does_not_grade_a_local_replacement(self):
        repo = self.repo('')
        original = b'committed encoding'
        blob = self.committed_asset(repo, 'docs/media/clip.mp4', original)
        (repo / 'README.md').write_text(self.video(self.blob_url(blob)))
        self.asset(repo, 'clip.mp4', b'another encoding')

        def inspect(path):
            self.assertEqual(path.read_bytes(), original)
            return self.VIDEO_INFO

        with patch('compliance.mp4_info', side_effect=inspect) as probe:
            rows = self.rows(repo)
        self.assertEqual(rows['MP4 docs/media/clip.mp4: publication encoding'][0], 'pass')
        probe.assert_called_once()

    def test_readme_embedded_mp4_needs_no_gif(self):
        repo = self.repo(self.VALID_VIDEO)
        self.committed_asset(repo, 'docs/media/clip.mp4')
        with patch('compliance.mp4_info', create=True, return_value=self.VIDEO_INFO):
            rows = self.rows(repo)
        self.assertEqual(rows['README has a demo, build commands and a license line'][0], 'pass')
        self.assertEqual(rows['MP4 docs/media/clip.mp4: publication encoding'][0], 'pass')
        self.assertNotIn('a GIF under docs/media', rows)

    def test_docs_video_is_checked_and_code_example_is_ignored(self):
        repo = self.repo('See docs.')
        self.committed_asset(repo, 'docs/media/clip.mp4')
        (repo / 'docs' / 'guide.md').write_text(
            self.VALID_VIDEO.replace('docs/media/', 'media/') + '\n\n```html\n'
            + self.VALID_VIDEO.replace('clip.mp4', 'example.mp4') + '\n```\n')
        with patch('compliance.mp4_info', create=True, return_value=self.VIDEO_INFO):
            rows = self.rows(repo)
        self.assertEqual(rows['MP4 docs/media/clip.mp4: publication encoding'][0], 'pass')
        self.assertFalse(any('example.mp4' in name for name in rows))

    def test_incomplete_video_embed_and_wrong_encoding_are_notes(self):
        repo = self.repo(self.VALID_VIDEO.replace(' playsinline', ''))
        self.committed_asset(repo, 'docs/media/clip.mp4')
        bad = self.VIDEO_INFO | {'height': 1080, 'audio': True, 'seconds': 10.1, 'faststart': False}
        with patch('compliance.mp4_info', create=True, return_value=bad):
            rows = self.tagged_rows(repo)
        self.assertEqual(rows['README has a demo, build commands and a license line'][1], 'pass')
        self.assertNotIn('README.md video 1: MP4 embed', rows)
        self.assertEqual(rows.get('README.md video 1: embed attributes', ('missing', 'missing'))[:2], ('E', 'note'))
        self.assertIn('playsinline', rows['README.md video 1: embed attributes'][2])
        self.assertEqual(rows.get('Docs videos resolve to a committed MP4', ('missing', 'missing'))[:2], ('M', 'pass'))
        self.assertEqual(rows['MP4 docs/media/clip.mp4: publication encoding'][:2], ('E', 'note'))

    def test_a_missing_attribute_is_a_note_when_the_src_resolves(self):
        repo = self.repo(self.VALID_VIDEO.replace(' playsinline', ''))
        self.committed_asset(repo, 'docs/media/clip.mp4')
        with patch('compliance.mp4_info', return_value=self.VIDEO_INFO):
            rows = self.tagged_rows(repo)
        self.assertFalse(any(name.endswith('MP4 embed') for name in rows))
        self.assertEqual(rows['README has a demo, build commands and a license line'][1], 'pass')
        self.assertEqual(rows.get('README.md video 1: embed attributes', ('missing', 'missing'))[:2], ('E', 'note'))
        self.assertIn('playsinline', rows['README.md video 1: embed attributes'][2])
        self.assertEqual(rows.get('Docs videos resolve to a committed MP4', ('missing', 'missing'))[:2], ('M', 'pass'))

    def test_a_video_without_src_fails(self):
        repo = self.repo('<video autoplay muted loop playsinline controls></video>')
        with patch('compliance.mp4_info', return_value=self.VIDEO_INFO):
            rows = self.tagged_rows(repo)
        self.assertEqual(rows['README.md video 1: MP4 embed'][:2], ('M', 'fail'))
        self.assertEqual(rows['README has a demo, build commands and a license line'][1], 'fail')
        self.assertNotIn('README.md video 1: embed attributes', rows)
        self.assertNotIn('Docs videos resolve to a committed MP4', rows)

    def test_a_readme_preview_must_be_committed(self):
        repo = self.repo('![Preview](docs/media/menu.gif)')
        self.asset(repo, 'menu.gif', b'GIF fixture')
        demo = 'README has a demo, build commands and a license line'
        self.assertEqual(self.rows(repo)[demo][0], 'fail')
        git(repo, 'add', '--', 'docs/media/menu.gif')
        self.assertEqual(self.rows(repo)[demo][0], 'fail')
        git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-q', '-m', 'fixture preview')
        self.assertEqual(self.rows(repo)[demo][0], 'pass')

    def test_a_local_alias_does_not_publish_an_mp4_or_preview(self):
        for suffix in ('mp4', 'gif'):
            with self.subTest(suffix=suffix):
                url = f'docs/media/alias.{suffix}'
                repo = self.repo(self.video(url) if suffix == 'mp4' else f'![Preview]({url})')
                self.committed_asset(repo, f'docs/media/published.{suffix}')
                (repo / url).symlink_to(f'published.{suffix}')
                with patch('compliance.mp4_info', return_value=self.VIDEO_INFO):
                    rows = self.rows(repo)
                self.assertEqual(rows['README has a demo, build commands and a license line'][0], 'fail')
                if suffix == 'mp4':
                    self.assertEqual(rows['README.md video 1: MP4 embed'][0], 'fail')

    def test_committed_clip_resolves_after_worktree_deletion_in_both_forms(self):
        repo = self.repo('')
        original = b'committed clip'
        blob = self.committed_asset(repo, 'docs/media/clip.mp4', original)
        (repo / 'docs' / 'media' / 'clip.mp4').unlink()
        for source in ('docs/media/clip.mp4', self.blob_url(blob)):
            with self.subTest(source=source):
                (repo / 'README.md').write_text('cargo skyline build\nMIT License\n' + self.video(source))

                def inspect(path):
                    self.assertEqual(path.read_bytes(), original)
                    return self.VIDEO_INFO

                with patch('compliance.mp4_info', side_effect=inspect) as probe:
                    rows = self.rows(repo)
                self.assertEqual(rows['README has a demo, build commands and a license line'][0], 'pass')
                self.assertFalse(any(name.endswith('MP4 embed') for name in rows))
                self.assertEqual(rows['MP4 docs/media/clip.mp4: publication encoding'][0], 'pass')
                probe.assert_called_once()

    def test_a_relative_src_must_be_committed(self):
        repo = self.repo(self.VALID_VIDEO)
        self.asset(repo, 'clip.mp4')
        with patch('compliance.mp4_info', return_value=self.VIDEO_INFO) as probe:
            rows = self.rows(repo)
        self.assertEqual(rows.get('README.md video 1: MP4 embed', ('missing',))[0], 'fail')
        self.assertEqual(rows['README has a demo, build commands and a license line'][0], 'fail')
        probe.assert_not_called()

    def test_encoding_departures_are_notes_and_never_fail(self):
        repo = self.repo(self.VALID_VIDEO)
        self.committed_asset(repo, 'docs/media/clip.mp4')
        bad = self.VIDEO_INFO | {'height': 1080, 'audio': True, 'seconds': 10.1, 'faststart': False}
        with patch('compliance.mp4_info', return_value=bad):
            rows = self.tagged_rows(repo)
        encoding = rows['MP4 docs/media/clip.mp4: publication encoding']
        self.assertEqual(encoding[:2], ('E', 'note'))
        for departure in ('size', 'audio track', 'length', 'no fast start'):
            self.assertIn(departure, encoding[2])
        self.assertFalse(any(tag == 'M' and result == 'fail' and
                             (name.startswith('MP4 ') or name.endswith('MP4 embed'))
                             for name, (tag, result, _) in rows.items()))
        self.assertEqual(rows.get('Docs videos resolve to a committed MP4', ('missing', 'missing'))[:2], ('M', 'pass'))
        self.assertEqual(rows['README has a demo, build commands and a license line'][1], 'pass')

    def test_a_zoom_close_up_is_a_note_at_any_size(self):
        repo = self.repo(self.video('docs/media/clip-zoom.mp4') + '\n' + self.VALID_VIDEO)
        self.committed_asset(repo, 'docs/media/clip-zoom.mp4', b'zoom clip')
        self.committed_asset(repo, 'docs/media/clip.mp4', b'main clip')

        def inspect(path):
            return self.VIDEO_INFO | ({'width': 1080, 'height': 330} if path.read_bytes() == b'zoom clip' else {})

        with patch('compliance.mp4_info', side_effect=inspect):
            rows = self.tagged_rows(repo)
        zoom = rows['MP4 docs/media/clip-zoom.mp4: publication encoding']
        self.assertEqual(zoom[:2], ('E', 'note'))
        self.assertTrue(zoom[2].startswith('close-up'))
        self.assertNotIn('differs', zoom[2])
        self.assertEqual(rows['MP4 docs/media/clip.mp4: publication encoding'][:2], ('E', 'pass'))

    def test_the_resolve_row_counts_embeds_and_is_absent_when_one_fails(self):
        repo = self.repo(self.VALID_VIDEO + '\n' + self.VALID_VIDEO)
        self.committed_asset(repo, 'docs/media/clip.mp4')
        page = repo / 'docs' / 'guide.md'
        page.write_text(self.video('media/clip.mp4'))
        with patch('compliance.mp4_info', return_value=self.VIDEO_INFO):
            rows = self.tagged_rows(repo)
        self.assertEqual(rows.get('Docs videos resolve to a committed MP4'),
                         ('M', 'pass', '3 video embed(s) in 2 page(s)'))
        page.write_text(self.video('media/clip.mp4') + '\n' + self.video('media/missing.mp4'))
        with patch('compliance.mp4_info', return_value=self.VIDEO_INFO):
            rows = self.tagged_rows(repo)
        self.assertNotIn('Docs videos resolve to a committed MP4', rows)
        self.assertEqual(rows['docs/guide.md video 2: MP4 embed'][1], 'fail')

    def test_the_summary_counts_advisory_notes(self):
        repo = self.repo(self.video('docs/media/clip-zoom.mp4'))
        self.committed_asset(repo, 'docs/media/clip-zoom.mp4')
        output = io.StringIO()
        with patch('compliance.mp4_info', return_value=self.VIDEO_INFO), contextlib.redirect_stdout(output):
            compliance.main([str(repo)])
        self.assertIn('1 advisory note(s)', output.getvalue().splitlines()[-1])

    def test_probe_errors_and_missing_ffprobe_are_advisory(self):
        repo = self.repo(self.VALID_VIDEO)
        self.committed_asset(repo, 'docs/media/clip.mp4')
        name = 'MP4 docs/media/clip.mp4: publication encoding'
        with patch('compliance.mp4_info', side_effect=ValueError):
            rows = self.tagged_rows(repo)
        self.assertEqual(rows[name], ('E', 'note', 'cannot inspect MP4 (ValueError)'))
        with patch('compliance.mp4_info', return_value=None):
            rows = self.tagged_rows(repo)
        self.assertEqual(rows[name], ('E', 'agent', 'ffprobe missing; check by hand'))

    def test_mp4_path_must_exist_and_a_hyperlink_is_not_an_embed(self):
        repo = self.repo('[video](docs/media/clip.mp4)')
        self.asset(repo, 'clip.mp4')
        self.assertEqual(self.rows(repo)['README has a demo, build commands and a license line'][0], 'fail')
        (repo / 'README.md').write_text('cargo skyline build\nLicense\n' +
                                       self.VALID_VIDEO.replace('clip.mp4', 'missing.mp4'))
        self.assertEqual(self.rows(repo)['README has a demo, build commands and a license line'][0], 'fail')

    def test_legacy_gif_readme_preview_still_passes(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('Pillow required for GIF fixture')
        repo = self.repo('![Menu preview][menu]\n\n[menu]: docs/media/menu.gif\n')
        path = self.asset(repo, 'menu.gif')
        frames = [Image.new('RGB', (1280, 8), color) for color in ('red', 'blue')]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
        self.committed_asset(repo, 'docs/media/menu.gif', path.read_bytes())
        rows = self.rows(repo)
        self.assertEqual(rows['README has a demo, build commands and a license line'][0], 'pass')
        self.assertEqual(rows['GIF menu.gif: animated, 1280 wide, under 10 s'][0], 'pass')

    def test_invalid_gif_reports_failure_without_crashing(self):
        try:
            import PIL
        except ImportError:
            self.skipTest('Pillow required for GIF validation')
        repo = self.repo('![Menu preview](docs/media/invalid.gif)')
        self.asset(repo, 'invalid.gif')
        rows = self.rows(repo)
        self.assertEqual(rows['GIF invalid.gif: animated, 1280 wide, under 10 s'][0], 'fail')


if __name__ == "__main__":
    unittest.main()
