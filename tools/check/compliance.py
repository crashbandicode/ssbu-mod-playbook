"""Fill in the machine-checkable rows of docs/CHECKLIST.md for a plugin repo.

Usage:
  compliance.py <plugin repo> [--nro <file>] [--out report.md]

Prints one Markdown table, `Requirement | Tag | Result | Evidence`. Rows a
machine can verify get `pass` or `fail` with what was read; the others are
printed as `agent` for the agent to complete with evidence or `N/A: reason`.
Exit status 1 when a mandatory machine row fails. Read-only."""
import argparse
import hashlib
import re
import struct
import subprocess
import sys
from pathlib import Path

RELEASE_PROFILE = {'opt-level': '"s"', 'lto': 'true', 'codegen-units': '1', 'panic': '"abort"'}
DIAG_MARKERS = (b'diag.txt', b'standin')
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
    ('Eden: one instance per host, MemAvailable >= 10 GiB, not profile a or :61', 'M'),
    ('Eden: profile copy with hard-linked game content; only the copy changed', 'M'),
    ('Copies: byte-identical files linked, differing or written-in-place files copied', 'M'),
    ('Disk: one target dir per source copy (never shared between copies), cargo home shared per host and toolchain, df checked (>= 40 GB), finished build products deleted with a manifest', 'M'),
    ('Eden: waits gated on a log line or screenshot check', 'M'),
    ('Eden: failed precondition aborted and retried under the next label', 'M'),
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


def git(repo, *args):
    try:
        done = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


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
    markers = [m.decode() for m in DIAG_MARKERS if m in data]
    return hashlib.sha256(data).hexdigest(), text + ro + rw + bss, markers


def rows_for(repo, nro_path):
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
    sources = {p: read(p) for p in sorted((repo / 'src').glob('*.rs'))}
    all_src = '\n'.join(sources.values())
    row('supported version 13.0.5 in model', 'M', '13.0.5' in read(repo / 'src' / 'model.rs'),
        'src/model.rs')
    guards = [g for g in re.findall(r'guard:\s*\[([^\]]*)\]', all_src, re.S)
              if 'u8' not in g]  # skip the field's type, `[u8; 64]`
    sizes = [len(re.findall(r'0x[0-9a-fA-F]{2}', g)) for g in guards]
    row('every site guard is 64 bytes', 'M', bool(sizes) and all(s == 64 for s in sizes),
        f'{len(sizes)} guard(s) in src/, sizes {sorted(set(sizes)) or "none"}')
    allocator = 'GlobalAlloc' in all_src and 'heap_allocations' in all_src
    row('counting allocator reports heap_allocations', 'M', allocator, 'src/*.rs')
    tests = len(re.findall(r'#\[test\]', all_src))
    row('host tests in model (cargo test)', 'M', tests > 0, f'{tests} #[test] in src/')

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
    for name, tag in AGENT_ROWS:
        rows.append((name, tag, 'agent', ''))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('repo', help='plugin repository')
    parser.add_argument('--nro', help='shipped NRO (default: dist/ or target/ release build)')
    parser.add_argument('--out', help='also write the report here')
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    rows = rows_for(repo, args.nro)
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
