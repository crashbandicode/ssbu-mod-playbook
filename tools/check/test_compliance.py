import subprocess
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
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

    def test_in_file_test_modules_and_assertions_are_not_reported(self):
        """2026-11-05 rc.5: `assert!(… " - unavailable")` in a `#[cfg(test)] mod tests` was reported."""
        repo, base = repo_with({'src/a.rs': 'fn a() {}\n'}, {'src/a.rs': (
            'fn a() {}\n'
            '#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {\n'
            '        let s = "x - unavailable";\n        assert!(s.ends_with(" - unavailable"));\n    }\n}\n'
            'fn after() { notify("Swap unavailable"); }\n')})
        self.assertEqual([h[0] for h in new_refusals(repo, base)], ['src/a.rs:10'])

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


if __name__ == "__main__":
    unittest.main()
