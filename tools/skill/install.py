"""Install the ssbu-mod-playbook skill for every agent tool on this machine.

Usage:
  install.py [--repo /home/intpa/ssbu-mod-playbook] [--check]

For Claude Code (~/.claude/skills), Codex (~/.agents/skills), Cursor
(~/.cursor/skills) and OpenCode (~/.config/opencode/skills) it links
`<skills dir>/ssbu-mod-playbook` to `<repo>/skills/ssbu-mod-playbook`, the way
record-ui-demo is shared here. It then puts one pointer block, between
markers so it can be re-run, into ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md
and ~/.config/opencode/AGENTS.md, and writes the Cursor rule
~/.cursor/rules/ssbu-mod-playbook.mdc. A real directory already at a link's
place is reported and left alone. --check reports without changing anything."""
import argparse
import os
import sys
from pathlib import Path

NAME = 'ssbu-mod-playbook'
SKILL_DIRS = {
    'Claude Code': '.claude/skills',
    'Codex': '.agents/skills',
    'Cursor': '.cursor/skills',
    'OpenCode': '.config/opencode/skills',
}
POINTER_FILES = ('.claude/CLAUDE.md', '.codex/AGENTS.md', '.config/opencode/AGENTS.md')
START, END = f'<!-- {NAME}:start -->', f'<!-- {NAME}:end -->'
POINTER = f"""{START}
## SSBU Skyline plugins

For any work on a Super Smash Bros. Ultimate Skyline plugin (creating,
changing, testing, validating, releasing or installing one, or driving Eden
to test one), follow the `{NAME}` skill: pull and read
`{{repo}}` completely (README.md, every file in docs/,
tools/PROVENANCE.md) before changing code, work through docs/CHECKLIST.md,
and run tools/check/compliance.py before declaring the work done.
{END}
"""
CURSOR_RULE = """---
description: SSBU Skyline plugin playbook and completion checklist
alwaysApply: true
---

"""


def points_to(path):
    try:
        return os.path.realpath(path)
    except OSError:
        return ''


def link(skills_dir, target, check):
    path = skills_dir / NAME
    if path.is_symlink() or (os.name == 'nt' and path.exists() and os.path.realpath(path) != str(path)):
        if points_to(path) == os.path.realpath(target):
            return 'ok'
        if check:
            return f'points elsewhere ({points_to(path)})'
        path.unlink() if path.is_symlink() else os.rmdir(path)
    elif path.exists():
        return 'a real directory is in the way; left alone'
    if check:
        return 'missing'
    skills_dir.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(target, target_is_directory=True)
        return 'linked'
    except OSError:
        if os.name != 'nt':
            raise
        import _winapi  # a junction needs no privilege on Windows
        _winapi.CreateJunction(str(target), str(path))
        return 'junction'


def place_pointer(path, text, prefix, check):
    current = path.read_text(encoding='utf-8') if path.exists() else ''
    if START in current and END in current:
        head, rest = current.split(START, 1)
        _, tail = rest.split(END, 1)
        updated = head + text.rstrip('\n') + tail
    elif current:
        updated = current.rstrip('\n') + '\n\n' + text
    else:
        updated = prefix + text
    if updated == current:
        return 'ok'
    if check:
        return 'missing or stale'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding='utf-8')
    return 'written'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--repo', default=str(Path.home() / 'ssbu-mod-playbook'),
                        help='the playbook clone (default: ~/ssbu-mod-playbook)')
    parser.add_argument('--check', action='store_true', help='report only')
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    target = repo / 'skills' / NAME
    if not (target / 'SKILL.md').is_file():
        print(f'no skill at {target}', file=sys.stderr)
        return 1
    home = Path.home()
    text = POINTER.replace('{repo}', str(repo))
    for tool, rel in SKILL_DIRS.items():
        print(f'{tool:<12} {home / rel / NAME}: {link(home / rel, target, args.check)}')
    for rel in POINTER_FILES:
        print(f'{"pointer":<12} {home / rel}: {place_pointer(home / rel, text, "", args.check)}')
    rule = home / '.cursor' / 'rules' / f'{NAME}.mdc'
    print(f'{"Cursor rule":<12} {rule}: {place_pointer(rule, text, CURSOR_RULE, args.check)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
