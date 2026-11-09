"""Isolated skill-pointer tests; never writes the real home or installs globally."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).with_name('install.py')
SPEC = importlib.util.spec_from_file_location('playbook_skill_install', SOURCE)
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='playbook-skill-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'fake-home'
        self.repo = self.root / 'fake-repo'
        self.target = self.repo / 'skills' / INSTALL.NAME
        self.target.mkdir(parents=True)
        (self.target / 'SKILL.md').write_text('# Isolated fixture\n', encoding='utf-8')

    def run_main(self, *args):
        output = io.StringIO()
        with patch.object(INSTALL.Path, 'home', return_value=self.home), contextlib.redirect_stdout(output):
            result = INSTALL.main(['--repo', str(self.repo), *args])
        return result, output.getvalue()

    def inventory(self):
        if not self.home.exists():
            return {}
        return {
            str(path.relative_to(self.home)): ('link', str(path.resolve())) if path.is_symlink()
            else ('file', path.read_bytes())
            for path in self.home.rglob('*') if path.is_file() or path.is_symlink()
        }

    def test_policy_names_mandatory_pages_and_on_demand_history(self):
        text = INSTALL.POINTER
        self.assertIn('docs/START.md', text)
        self.assertIn('docs/CHECKLIST.md', text)
        self.assertIn('task', text.lower())
        self.assertIn('history', text.lower())
        self.assertIn('on demand', text.lower())
        self.assertNotIn('every file in docs/', text)
        self.assertNotIn('read completely', text.lower())
        self.assertIn('tools/check/compliance.py', text)

    def test_install_links_all_tools_and_updates_all_pointer_surfaces(self):
        self.home.mkdir()
        claude = self.home / '.claude' / 'CLAUDE.md'
        claude.parent.mkdir()
        claude.write_text('Preserved unrelated instructions.\n', encoding='utf-8')
        result, _ = self.run_main()
        self.assertEqual(result, 0)
        for relative in INSTALL.SKILL_DIRS.values():
            self.assertEqual((self.home / relative / INSTALL.NAME).resolve(), self.target)
        pointers = [self.home / p for p in INSTALL.POINTER_FILES]
        pointers.append(self.home / '.cursor' / 'rules' / f'{INSTALL.NAME}.mdc')
        for pointer in pointers:
            text = pointer.read_text(encoding='utf-8')
            self.assertIn('docs/START.md', text)
            self.assertIn('docs/CHECKLIST.md', text)
            self.assertIn('on demand', text)
            self.assertEqual(text.count(INSTALL.START), 1)
            self.assertEqual(text.count(INSTALL.END), 1)
        self.assertTrue(claude.read_text(encoding='utf-8').startswith('Preserved unrelated instructions.'))

    def test_check_reports_without_creating_or_rewriting_files(self):
        before = self.inventory()
        result, output = self.run_main('--check')
        self.assertEqual(result, 0)  # Existing report-only exit behavior is preserved.
        self.assertIn('missing', output)
        self.assertEqual(self.inventory(), before)

    def test_second_install_is_idempotent(self):
        self.run_main()
        before = self.inventory()
        result, output = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(self.inventory(), before)
        self.assertNotIn('written', output)

    def test_stale_reading_block_replaced_without_changing_surroundings(self):
        path = self.root / 'pointer.md'
        path.write_text('Before\n' + INSTALL.START + '\nRead every file in docs/.\n' + INSTALL.END + '\nAfter\n', encoding='utf-8')
        replacement = INSTALL.POINTER.replace('{repo}', str(self.repo))
        self.assertEqual(INSTALL.place_pointer(path, replacement, '', False), 'written')
        actual = path.read_text(encoding='utf-8')
        self.assertTrue(actual.startswith('Before\n'))
        self.assertTrue(actual.endswith('\nAfter\n'))
        self.assertNotIn('Read every file in docs/', actual)
        self.assertIn('docs/START.md', actual)


if __name__ == '__main__':
    unittest.main()
