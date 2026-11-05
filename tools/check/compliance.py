"""Fill in the machine-checkable rows of docs/CHECKLIST.md for a plugin repo.

Usage:
  compliance.py <plugin repo> [--nro <file>] [--base <ref>] [--trace <design.md>]... [--out report.md]

Prints one Markdown table, `Requirement | Tag | Result | Evidence`. Rows a
machine can verify get `pass` or `fail` with what was read; the others are
printed as `agent` for the agent to complete with evidence or `N/A: reason`.
Exit status 1 when a mandatory machine row fails. Read-only.

--base <ref>: the commit the branch started from. Every user-facing refusal
the branch adds (a string literal that refuses or restricts, e.g. "Turn X on
to ...", "unavailable", "only when") must carry `// restriction: <ID>` on its
line or the 3 lines above, and <ID> must be approved in docs/RESTRICTIONS.md.
--trace <file>: a design report whose requirement-trace table maps each of the
operator's clauses to a test; every row needs a test and every test must exist."""
import argparse
import hashlib
import re
import struct
import subprocess
import sys
from pathlib import Path

RELEASE_PROFILE = {'opt-level': '"s"', 'lto': 'true', 'codegen-units': '1', 'panic': '"abort"'}
DIAG_MARKERS = (b'diag.txt', b'standin')
SCRIPT_SUFFIXES = {'.py', '.ps1', '.psm1', '.sh', '.bash', '.cmd', '.bat'}
# A command that mounts, unmounts, ejects or powers off a disk, written as a
# shell line or as an argv list ('/usr/bin/udisksctl', 'unmount'). The user
# connects and disconnects the Switch; a script never does.
CARD_COMMANDS = re.compile(
    r"udisksctl['\"]?\s*,?\s*['\"]?(?:mount|unmount|power-off)\b"
    r"|\bgio['\"]?\s*,?\s*['\"]?mount\b"
    r"|\bumount\b"
    r"|\beject['\"]?\s*,?\s*['\"]?(?:-|/dev/)"
    r"|(?:^|[;&|(]\s*|\bsudo\s+)mount\s+(?:-|/dev/)")
# A string literal that refuses or restricts what the user asked for. Restrictions
# need the operator's approval (WORKFLOW: workarounds and restrictions), so a branch
# that adds one must cite an approved entry of docs/RESTRICTIONS.md.
REFUSAL = re.compile(
    r'"[^"\n]*(?:\bturn\b[^"\n]{0,40}\bon\b|\bunavailable\b|\bnot available\b|\brefus\w*'
    r"|\bcan't\b|\bcannot\b|\bcan not\b|\bonly (?:when|while|in|with|after|before)\b"
    r'|\brequires?\b|\bnot allowed\b|\bblocked\b|\bdisabled\b'
    # Scope limits phrased positively ("Combos work in Training", 2026-11-05) and waits imposed on the user.
    r'|\b(?:works?|available|allowed)\s+(?:only\s+)?(?:in|on|with|while|after|during)\b|\bwait\s+for\b'
    r')[^"\n]*"', re.I)
RESTRICTION_TAG = re.compile(r'restriction:\s*([A-Za-z0-9_.\-]+)')
APPROVED_EMPTY = {'', '-', 'pending', 'no', 'not yet', 'tbd'}
AGENT_ROWS = [
    ('Gates run: test, clippy -D warnings, no-default-features, skyline release, diag build', 'M'),
    ('Fixes: root cause fixed, or each workaround approved by the operator before it shipped and listed with its date', 'M'),
    ('Card scan for the site (and vtable slot) over a dated card copy', 'M'),
    ('Hook sharing or chaining; both stand-in modes and load orders in Eden', 'C'),
    ('Hook after the prologue or the function called instead', 'M'),
    ('Shipped NRO writes only its install line (plus a logging plugin\'s data files)', 'M'),
    ('Online: read-only rules reviewed hook by hook', 'C'),
    ('Online: replay A/B with the desync detector identical', 'C'),
    ('Online: two consoles past the changed path, desync_seen=0 at the same frame', 'C'),
    ('Eden: claim written and released, also after a failed run', 'M'),
    ('Eden: at most two instances per Linux host, one on the Ally; MemAvailable >= 10 GiB; not profile a or :61', 'M'),
    ('Eden: profile copy with hard-linked game content; only the copy changed', 'M'),
    ('Copies: byte-identical files linked, differing or written-in-place files copied', 'M'),
    ('Disk: one target dir per source copy (never shared between copies), cargo home shared per host and toolchain, df checked (>= 40 GB), finished build products deleted with a manifest', 'M'),
    ('Eden: waits gated on a log line or screenshot check', 'M'),
    ('Eden: test run plays a runner dry-run before Up (recorded segments + sha256, automated gates, per-row receipts, rows chained on end state); manual input only to build a missing segment', 'M'),
    ('Eden: rows grouped into as few boots as hosts/builds allow; a failed row is fixed or deferred on the same boot; per-run RESULTS.md <= 20 lines, compliance once per phase', 'M'),
    ('Eden: failed precondition aborted and retried under the next label', 'M'),
    ('Eden: game-side facts from guest tracing (persistent trace.* points or the one-shot tracepoint) on a run already happening; diag rebuild or extra trace boot only with a stated reason', 'M'),
    ('Eden: profile restored, temp files removed, host released', 'M'),
    ('Eden: console.log searched for panic/abort/crash; run.json and memory.peak kept', 'M'),
    ('Every local command under the memory and time guard', 'M'),
    ('Evidence doc states run labels, build, what was done, what the log showed', 'M'),
    ('GIF caption gives date and build; clip shows start, action and result', 'M'),
    ('No operator quotes, no claims beyond the tests', 'M'),
    ('Handoff file current (branch, commit, evidence, live state, next commands)', 'M'),
    ('Release: version bump, annotated tag, assets verified by anonymous download', 'C'),
    ('Release: rebuild of the tag byte-identical to the released NRO', 'C'),
    ('Card: MTP preflight, rollback copy, readbacks, receipts not re-run', 'C'),
    ('Card: the user connected and disconnected it; the agent never mounted, unmounted, ejected or remounted it, nor started or stopped DBI', 'M'),
    ('Nothing pushed, tagged or written to the card without the operator\'s order', 'M'),
    ('Model/effort gate and watcher per the routing authority', 'M'),
]


def read(path):
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def section(text, name):
    """The body of a TOML table `[name]` (crude, enough for Cargo.toml)."""
    match = re.search(rf'^\[{re.escape(name)}\]\s*$(.*?)(?=^\[|\Z)', text, re.M | re.S)
    return match.group(1) if match else ''


def toml_value(body, key):
    match = re.search(rf'^\s*{re.escape(key)}\s*=\s*(.+?)\s*(#.*)?$', body, re.M)
    return match.group(1).strip() if match else None


def card_commands(paths, base):
    """`file:line` of every script line (comment lines skipped) that would
    mount, unmount, eject or power off a disk."""
    hits = []
    for path in paths:
        if path.suffix.lower() not in SCRIPT_SUFFIXES or not path.is_file():
            continue
        for number, line in enumerate(read(path).splitlines(), 1):
            code = line.strip()
            if code.startswith(('#', '//', '::')) or code.upper().startswith('REM '):
                continue
            if CARD_COMMANDS.search(code):
                hits.append(f'{path.relative_to(base)}:{number}')
    return hits


def git(repo, *args):
    try:
        done = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def added_lines(repo, base, suffix='.rs'):
    """(path, line number, text) of every line `base...HEAD` adds to a file ending in
    `suffix`; None when git can't produce the diff (unknown base)."""
    diff = git(repo, 'diff', '--unified=0', '--no-color', '--no-ext-diff', f'{base}...HEAD', '--',
               f'*{suffix}')
    if diff is None:
        return None
    added, path, number = [], None, 0
    for line in diff.splitlines():
        if line.startswith('+++ '):
            path = line[6:] if line.startswith('+++ b/') else None
        elif line.startswith('@@'):
            match = re.search(r'\+(\d+)', line)
            number = int(match.group(1)) if match else 0
        elif line.startswith('+') and path:
            added.append((path, number, line[1:]))
            number += 1
    return added


def is_test_path(path):
    parts = Path(path).parts
    name = Path(path).name
    return 'tests' in parts or name in ('tests.rs', 'test.rs') or name.endswith(('_tests.rs', '_test.rs'))


def approved_restrictions(repo):
    """IDs of docs/RESTRICTIONS.md table rows whose last column records an approval."""
    approved = set()
    for line in read(repo / 'docs' / 'RESTRICTIONS.md').splitlines():
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if not line.lstrip().startswith('|') or len(cells) < 2 or set(cells[0]) <= set('-: '):
            continue
        ident = cells[0].strip('`* ')
        if ident.lower() in ('id', '') or cells[-1].strip('`* ').lower() in APPROVED_EMPTY:
            continue
        approved.add(ident)
    return approved


def test_lines(lines):
    """1-based line numbers inside `#[cfg(test)]` items (an in-file `mod tests { … }` or a test fn), by brace depth."""
    inside, i = set(), 0
    while i < len(lines):
        if '#[cfg(test)]' not in lines[i]:
            i += 1
            continue
        start, depth, opened = i, 0, False
        while i < len(lines):
            depth += lines[i].count('{') - lines[i].count('}')
            opened = opened or '{' in lines[i]
            i += 1
            if opened and depth <= 0:
                break
        inside.update(range(start + 1, i + 1))
    return inside


def new_refusals(repo, base):
    """Each refusal string the branch adds, with the restriction ID it cites (or None).
    Comment lines, assertions, test files and `#[cfg(test)]` items are skipped. None when the diff can't be read."""
    added = added_lines(repo, base)
    if added is None:
        return None
    hits, files, tests = [], {}, {}
    for path, number, text in added:
        code = text.strip()
        if code.startswith(('//', '/*', '*')) or is_test_path(path) or not REFUSAL.search(code) \
                or re.match(r'(?:debug_)?assert(?:_eq|_ne)?!', code):
            continue
        if path not in files:
            files[path] = read(repo / path).splitlines()
            tests[path] = test_lines(files[path])
        if number in tests[path]:
            continue
        lines = files[path]
        window = lines[max(0, number - 4):number] if lines else [text]
        tags = [m.group(1) for line in window for m in [RESTRICTION_TAG.search(line)] if m]
        hits.append((f'{path}:{number}', code[:90], tags[-1] if tags else None))
    return hits


def trace_tables(text):
    """Rows of every Markdown table whose header names an operator clause and a test:
    [(clause, tests)], tests being the backticked names in the test column."""
    rows, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        header = lines[i].strip()
        cells = [c.strip().lower() for c in header.strip('|').split('|')] if header.startswith('|') else []
        test_col = next((k for k, c in enumerate(cells) if 'test' in c), None)
        clause_col = next((k for k, c in enumerate(cells) if 'clause' in c or 'operator' in c), None)
        if test_col is None or clause_col is None or i + 1 >= len(lines) \
                or not re.fullmatch(r'\|?[\s:\-|]+\|?', lines[i + 1].strip()):
            i += 1
            continue
        i += 2
        while i < len(lines) and lines[i].strip().startswith('|'):
            row = [c.strip() for c in lines[i].strip().strip('|').split('|')]
            if len(row) > max(test_col, clause_col):
                names = re.findall(r'`([^`]+)`', row[test_col])
                rows.append((row[clause_col], [n.split('::')[-1].split('(')[0].strip() for n in names]))
            i += 1
    return rows


def defined_tests(repo):
    """Every `fn name` in the repo's Rust sources and `def name` in its Python tools."""
    names = set()
    for path in list(repo.glob('src/**/*.rs')) + list(repo.glob('tests/**/*.rs')) \
            + list(repo.glob('crates/**/*.rs')) + list(repo.glob('tools/**/*.py')):
        names.update(re.findall(r'\b(?:fn|def)\s+([A-Za-z_]\w*)', read(path)))
    return names


def gif_info(path):
    try:
        from PIL import Image
    except ImportError:
        return None
    with Image.open(path) as image:
        width = image.width
        duration = 0
        for frame in range(getattr(image, 'n_frames', 1)):
            image.seek(frame)
            duration += image.info.get('duration', 0)
    return width, duration / 1000


def find_nro(repo, given):
    """The shipped NRO: --nro, else the newest release NRO under dist/, else
    the last build in target/ (which may be a diag build; the row says so)."""
    if given:
        return Path(given)
    for pattern in ('dist/**/release/*.nro', 'dist/*.nro',
                    'target/aarch64-skyline-switch/release/*.nro'):
        found = [f for f in repo.glob(pattern) if 'diag' not in str(f.relative_to(repo))]
        if found:
            return max(found, key=lambda f: f.stat().st_mtime)
    return None


def nro_facts(path):
    data = path.read_bytes()
    if data[0x10:0x14] != b'NRO0':
        return None
    text, ro, rw = (struct.unpack_from('<II', data, 0x20 + 8 * i)[1] for i in range(3))
    bss = struct.unpack_from('<I', data, 0x38)[0]
    # Whole-word matches only: `standin` inside `outstanding` (a common log word) is not a marker.
    markers = [m.decode() for m in DIAG_MARKERS if re.search(rb'(?<![A-Za-z])' + re.escape(m) + rb'(?![A-Za-z])', data)]
    return hashlib.sha256(data).hexdigest(), text + ro + rw + bss, markers


def guard_size(initializer, all_src):
    """Count explicit bytes or a Rust repeat initializer; unresolved lengths fail closed."""
    repeated = re.fullmatch(r"[^;]+;\s*([A-Za-z_]\w*|[0-9][0-9_]*)\s*", initializer.strip())
    if repeated:
        length = repeated.group(1)
        if re.fullmatch(r"[0-9][0-9_]*", length):
            return int(length.replace("_", ""))
        definitions = re.findall(
            rf"\bconst\s+{re.escape(length)}\s*:\s*usize\s*=\s*([^;]+);", all_src)
        if not definitions or any(not re.fullmatch(r"[0-9][0-9_]*", d.strip())
                                  for d in definitions):
            return 0
        values = {int(d.strip().replace("_", "")) for d in definitions}
        return values.pop() if len(values) == 1 else 0
    return len(re.findall(r"0x[0-9a-fA-F]{2}", initializer))


def rows_for(repo, nro_path, base=None, traces=()):
    rows = []

    def row(name, tag, ok, evidence):
        rows.append((name, tag, 'pass' if ok else 'fail', evidence))

    cargo = read(repo / 'Cargo.toml')
    crate_types = re.search(r'crate-type\s*=\s*\[([^\]]*)\]', cargo)
    types = [t.strip().strip('"') for t in crate_types.group(1).split(',') if t.strip()] \
        if crate_types else []
    row('crate-type is ["cdylib"] only (no rlib)', 'M', types == ['cdylib'],
        f'Cargo.toml crate-type = {types or "missing"}')
    release = section(cargo, 'profile.release')
    wrong = {k: toml_value(release, k) for k, v in RELEASE_PROFILE.items()
             if toml_value(release, k) != v}
    row('release profile opt-level "s", lto, codegen-units 1, panic abort', 'M', not wrong,
        'Cargo.toml [profile.release]' + (f' differs: {wrong}' if wrong else ' as required'))
    row('skyline pinned to =0.6.0', 'M', 'skyline = "=0.6.0"' in cargo,
        'Cargo.toml' if 'skyline = "=0.6.0"' in cargo else 'Cargo.toml: pin not found')
    row('diag cargo feature declared', 'M', bool(re.search(r'^\s*diag\s*=\s*\[', cargo, re.M)),
        'Cargo.toml [features]')
    toolchain = read(repo / 'rust-toolchain.toml')
    row('toolchain 1.98.1', 'M', '1.98.1' in toolchain,
        'rust-toolchain.toml' + ('' if '1.98.1' in toolchain else ': channel not 1.98.1'))

    lib = read(repo / 'src' / 'lib.rs')
    row('runtime only under cfg(target_os = "switch")', 'M', 'target_os = "switch"' in lib,
        'src/lib.rs')
    # Recursive: plugins that split model/runtime into folders (src/model/, src/runtime/) keep these items below src/.
    sources = {p: read(p) for p in sorted((repo / 'src').rglob('*.rs'))}
    all_src = '\n'.join(sources.values())
    model_dir = repo / 'src' / 'model'
    model_files = [p for p in [repo / 'src' / 'model.rs'] if p.is_file()] + \
        (sorted(model_dir.rglob('*.rs')) if model_dir.is_dir() else [])
    version_files = [p for p in model_files if '13.0.5' in read(p)]
    row('supported version 13.0.5 in model', 'M', bool(version_files),
        ', '.join(str(p.relative_to(repo)) for p in version_files[:3])
        or ('13.0.5 not found in ' + (', '.join(str(p.relative_to(repo)) for p in model_files[:3]) or 'src/model.rs / src/model/')))
    guards = [g for g in re.findall(r'guard:\s*\[([^\]]*)\]', all_src, re.S)
              if 'u8' not in g]  # skip the field's type, `[u8; 64]`
    sizes = [guard_size(g, all_src) for g in guards]
    row('every site guard is 64 bytes', 'M', bool(sizes) and all(s == 64 for s in sizes),
        f'{len(sizes)} guard(s) in src/, sizes {sorted(set(sizes)) or "none"}')
    allocator = 'GlobalAlloc' in all_src and 'heap_allocations' in all_src
    row('counting allocator reports heap_allocations', 'M', allocator, 'src/**/*.rs')
    tests = len(re.findall(r'#\[test\]', all_src))
    row('host tests in model (cargo test)', 'M', tests > 0, f'{tests} #[test] in src/**')

    readme = read(repo / 'README.md')
    row('README carries the AI-assistance disclaimer', 'M',
        'assistance of ai' in readme.lower(), 'README.md')
    row('README has a GIF, build commands and a license line', 'M',
        '.gif' in readme and 'cargo skyline build' in readme and 'license' in readme.lower(),
        'README.md')
    gifs = sorted(repo.glob('docs/media/*.gif'))
    for gif in gifs:
        info = gif_info(gif)
        if info is None:
            rows.append((f'GIF {gif.name}: 1280 wide, under 10 s', 'M', 'agent',
                         'Pillow missing; check by hand'))
            continue
        width, seconds = info
        row(f'GIF {gif.name}: 1280 wide, under 10 s', 'M', width == 1280 and seconds < 10,
            f'{width} px, {seconds:.1f} s')
    if not gifs:
        row('a GIF under docs/media', 'M', False, 'docs/media/*.gif: none')
    candidates = [p for p in list(repo.glob('*.md')) + list(repo.glob('docs/*.md'))
                  if p.name not in ('README.md', 'HANDOFF.md')]
    dated = sorted(str(p.relative_to(repo)) for p in candidates
                   if re.search(r'20\d\d-\d\d-\d\d', read(p)))
    row('dated evidence doc present', 'M', bool(dated),
        ', '.join(dated) or 'no dated Markdown besides README/HANDOFF')
    scripts = sorted(p.name for p in (repo / 'tools').glob('*.py')) if (repo / 'tools').is_dir() else []
    tests_py = [s for s in scripts if s.startswith('test_')]
    rows.append(('numbers in the evidence come from tested scripts over the logs', 'C',
                 'pass' if tests_py else 'agent',
                 f'tools/: {", ".join(scripts) or "none"}'
                 + ('' if tests_py else '; no test_*.py: N/A only if no numbers are cited')))
    row('handoff file present', 'M', (repo / 'HANDOFF.md').exists(), 'HANDOFF.md')

    remote = git(repo, 'remote', 'get-url', 'origin') or ''
    row('remote is Forgejo, not GitHub', 'M',
        'git.syntaxbyexample.com' in remote and 'github' not in remote.lower(),
        remote or 'no origin')
    status = git(repo, 'status', '--porcelain')
    row('working tree clean', 'M', status == '', 'git status' + (': dirty' if status else ''))
    tracked = [repo / name for name in (git(repo, 'ls-files') or '').splitlines()]
    tools = Path(__file__).resolve().parents[1]
    hits = card_commands(tracked, repo) + [
        f'playbook tools/{hit}' for hit in card_commands(sorted((tools / 'switch').rglob('*')), tools)]
    row('no script mounts, unmounts or ejects the card (the user connects and disconnects it)', 'M', not hits,
        ', '.join(hits[:8]) + (f' (+{len(hits) - 8} more)' if len(hits) > 8 else '')
        or 'none in the tracked scripts or the playbook\'s tools/switch')
    head = git(repo, 'rev-parse', '--short', 'HEAD') or '?'
    tag = git(repo, 'describe', '--tags', '--exact-match') or 'no tag on HEAD'
    rows.append(('build identity', 'M', 'info', f'HEAD {head}; {tag}'))

    nro = find_nro(repo, nro_path)
    if nro and nro.exists():
        facts = nro_facts(nro)
        if facts:
            sha, resident, markers = facts
            shown = str(nro.relative_to(repo)) if nro.is_relative_to(repo) else str(nro)
            if 'target/' in shown:
                shown += ' (last build; pass --nro for the shipped file)'
            rows.append(('shipped NRO identity', 'M', 'info',
                         f'{shown} sha256 {sha[:16]} resident {resident} B ({resident / 1024:.0f} KiB)'))
            row('no diag strings in the shipped NRO', 'M', not markers,
                f'{nro.name}: ' + (', '.join(markers) + ' found' if markers else 'no diag markers'))
        else:
            row('shipped NRO readable', 'M', False, f'{nro}: not an NRO0 file')
    else:
        rows.append(('shipped NRO identity and diag strings', 'M', 'agent',
                     'no NRO found (pass --nro)'))
    refusal_row = 'New user-facing refusals cite an approved restriction (// restriction: ID, docs/RESTRICTIONS.md)'
    if base:
        hits = new_refusals(repo, base)
        if hits is None:
            row(refusal_row, 'M', False, f'git diff {base}...HEAD failed (unknown base?)')
        else:
            approved = approved_restrictions(repo)
            bad = [f'{where} {text}' + (f' (cites {tag}: not approved)' if tag else ' (no restriction: tag)')
                   for where, text, tag in hits if tag not in approved]
            row(refusal_row, 'M', not bad,
                '; '.join(bad[:6]) + (f' (+{len(bad) - 6} more)' if len(bad) > 6 else '')
                if bad else f'{len(hits)} added refusal(s) since {base}, all cite approved IDs')
    else:
        rows.append((refusal_row, 'M', 'agent', 'pass --base <the commit the branch started from>'))
    trace_row = 'Design report traces every operator clause to a test that exists'
    if traces:
        defined = defined_tests(repo)
        problems, total = [], 0
        for trace in traces:
            table = trace_tables(read(Path(trace)))
            if not table:
                problems.append(f'{Path(trace).name}: no table with an operator-clause and a test column')
            for clause, names in table:
                total += 1
                if not names:
                    problems.append(f'"{clause[:50]}": no test named')
                problems += [f'"{clause[:40]}": `{n}` not found' for n in names if n not in defined]
        row(trace_row, 'M', not problems,
            '; '.join(problems[:6]) + (f' (+{len(problems) - 6} more)' if len(problems) > 6 else '')
            if problems else f'{total} clause(s), every test found')
    else:
        rows.append((trace_row, 'M', 'agent', 'pass --trace <design report with the requirement-trace table>'))
    for name, tag in AGENT_ROWS:
        rows.append((name, tag, 'agent', ''))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('repo', help='plugin repository')
    parser.add_argument('--nro', help='shipped NRO (default: dist/ or target/ release build)')
    parser.add_argument('--base', help='the commit the branch started from (checks added refusals)')
    parser.add_argument('--trace', action='append', default=[],
                        help='design report with the requirement-trace table (repeatable)')
    parser.add_argument('--out', help='also write the report here')
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    rows = rows_for(repo, args.nro, args.base, args.trace)
    lines = [f'# Compliance report: {repo.name}', '',
             '| Requirement | Tag | Result | Evidence |', '| --- | --- | --- | --- |']
    lines += [f'| {name} | {tag} | {result} | {evidence} |' for name, tag, result, evidence in rows]
    failed = [name for name, tag, result, _ in rows if tag == 'M' and result == 'fail']
    agent = sum(result == 'agent' for _, _, result, _ in rows)
    lines += ['', f'{len(failed)} mandatory machine row(s) failed; {agent} row(s) for the agent.']
    text = '\n'.join(lines) + '\n'
    sys.stdout.write(text)
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
