"""Offline checks for a runner packet."""
import ast
import hashlib
import inspect
import json
from pathlib import Path
import re
import subprocess
import sys

import recipe_macro

PROVIDERS = {
    'tr.menu': {'repo': 'plugin', 'files': ['src/model/bridge_state.rs', 'src/model/record_place.rs']},
    'tr.practice': {'repo': 'plugin', 'files': ['src/model/practice_state.rs']},
    'tr.event': {'repo': 'plugin', 'files': ['src/model/bridge_events.rs']},
}
BUTTONS = (
    ('A', 1), ('B', 2), ('X', 4), ('Y', 8), ('L', 64), ('R', 128), ('ZL', 256), ('ZR', 512),
    ('Up', 8192), ('Down', 32768), ('Left', 4096), ('Right', 16384), ('Plus', 1024), ('Minus', 2048),
    ('L-stick', 16), ('R-stick', 32),
)
RUNS = Path('/home/intpa/eden-runs')


def packet_digest(base):
    base = Path(base)
    files = []
    for path in base.rglob('*'):
        rel = path.relative_to(base)
        if (not path.is_file() or path.is_symlink() or 'out' in rel.parts or '__pycache__' in rel.parts
                or path.suffix == '.pyc' or path.name == 'packet-gate.json'):
            continue
        files.append((rel.as_posix(), path))
    digest = hashlib.sha256()
    for name, path in sorted(files):
        digest.update(f'{name}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n'.encode())
    return digest.hexdigest()


def _git(tree, *args):
    return subprocess.run(['git', '-C', str(tree), *args], capture_output=True, timeout=20, check=True)


def emitted_keys(tree, commit, files):
    keys = set()
    for path in files:
        text = _git(tree, 'show', f'{commit}:{path}').stdout.decode('utf-8')
        text = re.split(r'(?m)^\s*#\[cfg\(test\)\]\s*$', text, maxsplit=1)[0]
        for literal in re.findall(r'"((?:\\.|[^"\\])*)"', text):
            found = re.findall(r'(?:^|\\n| )([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)=', literal)
            keys.update(found)
            event = re.match(r'^([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*) ', literal)
            if event:
                keys.update(f'{event.group(1)} {key}' for key in found)
            if re.fullmatch(r'[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+', literal):
                keys.add(literal)
        keys.update(re.findall(r'\bpush_str\s*\(\s*out\s*,\s*"((?:\\.|[^"\\])*)"', text))
    keys.discard('schema')
    return keys


def _target(plan, base, errors):
    target = plan['target']
    if not isinstance(target, dict):
        errors.append('G2 target: target must be an object')
        return {}, None
    repos = {}
    for name in ('plugin', 'bridge'):
        if name == 'bridge' and name not in target:
            continue
        spec = target.get(name)
        if not isinstance(spec, dict):
            errors.append(f'G2 target: {name} must be an object')
            continue
        tree, commit = spec.get('tree'), spec.get('commit')
        if not isinstance(tree, str) or not Path(tree).is_absolute() or not Path(tree).is_dir():
            errors.append(f'G2 target: {name}.tree must be an existing absolute directory')
            continue
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            errors.append(f'G2 target: {name}.commit must be 40 lower-case hex characters')
            continue
        try:
            _git(tree, 'cat-file', '-e', f'{commit}^{{commit}}')
        except (OSError, subprocess.SubprocessError):
            errors.append(f'G2 target: {name}.commit {commit} is not a commit in {tree}')
            continue
        repos[name] = spec
        if name == 'plugin' and not re.fullmatch(r'[0-9a-fA-F]{64}', str(spec.get('nro_sha256', ''))):
            errors.append('G2 target: plugin.nro_sha256 must be 64 hex characters')
    settings = None
    if 'settings' in target:
        try:
            value = target['settings']
            path = (base / value).resolve()
            if not isinstance(value, str) or Path(value).is_absolute() or base.resolve() not in path.parents:
                raise ValueError('must be a path under the rows folder')
            settings = json.loads(path.read_text())
        except (OSError, ValueError, TypeError) as error:
            errors.append(f'G3 target: settings {target["settings"]!r}: {error}')
    return repos, settings


def _steps(steps):
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict):
            yield step
            gif = step.get('gif')
            if isinstance(gif, dict):
                yield from _steps(gif.get('steps'))


def _hooks(plan, base, errors):
    from runner import load_hook, load_step

    gates_checked = set()
    modules = set()

    def step_hook(row_id, hook):
        spec = hook.get('run') if isinstance(hook, dict) else hook
        args = hook.get('args', {}) if isinstance(hook, dict) else {}
        if not isinstance(spec, str):
            return
        path = spec.partition(':')[0]
        if path not in ('routes', 'routes_online', 'fixtures'):
            modules.add(path)
        try:
            fn = load_step(base, spec)
        except Exception:
            return
        try:
            inspect.signature(fn).bind(object(), **args)
        except (TypeError, ValueError) as error:
            errors.append(f'G1 row {row_id}: {spec}: {error}')

    def gate_hook(row_id, name):
        gate = (plan.get('gates') or {}).get(name)
        if not isinstance(gate, dict) or gate.get('kind') != 'hook' or name in gates_checked:
            return
        gates_checked.add(name)
        modules.add(gate.get('module', ''))
        try:
            fn = load_hook(base, gate)
        except Exception:
            return
        try:
            inspect.signature(fn).bind(object(), gate)
        except (TypeError, ValueError) as error:
            errors.append(f'G1 row {row_id}: gate {name}: {error}')

    rows = plan.get('rows', [])
    plan_row = rows[0].get('id', '?') if rows else '<plan>'
    for step in _steps(plan.get('recover')):
        if 'run' in step:
            step_hook(plan_row, step)
        if 'gate' in step:
            gate_hook(plan_row, step['gate'])
    step_hook(plan_row, plan.get('on_failure_hook'))
    for row in rows:
        row_id = row.get('id', '?')
        steps = list(_steps(row.get('steps')))
        steps.extend(_steps(row.get('recover')))
        for step in steps:
            if 'run' in step:
                step_hook(row_id, step)
            if 'gate' in step:
                gate_hook(row_id, step['gate'])
            if 'play' in step:
                gate_hook(row_id, (plan.get('segments', {}).get(step['play']) or {}).get('start_gate'))
        gate_hook(row_id, row.get('start_gate'))
        step_hook(row_id, row.get('on_failure_hook'))
    for name in plan.get('gates', {}):
        gate_hook('<plan>', name)
    runner_dir = Path(__file__).resolve().parent
    loader = ('import importlib.util, pathlib, sys; '
              'sys.path.insert(0, sys.argv[1]); p = pathlib.Path(sys.argv[2]); '
              's = importlib.util.spec_from_file_location("step_hook_" + p.stem, p); '
              'm = importlib.util.module_from_spec(s); s.loader.exec_module(m)')
    seen_paths = set()
    for module in sorted(m for m in modules if isinstance(m, str) and m):
        path = Path(module)
        path = path if path.is_absolute() else base / path
        resolved = path.resolve()
        if resolved.parent == runner_dir or resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        try:
            proc = subprocess.run([sys.executable, '-I', '-B', '-c', loader, str(runner_dir), str(path)],
                                  timeout=60, capture_output=True, text=True)
            if proc.returncode == 0:
                continue
            lines = [line for line in proc.stderr.splitlines() if line.strip()]
            detail = lines[-1][:200] if lines else f'exit {proc.returncode}'
        except subprocess.TimeoutExpired as error:
            stderr = error.stderr or ''
            if isinstance(stderr, bytes):
                stderr = stderr.decode('utf-8', errors='replace')
            lines = [line for line in stderr.splitlines() if line.strip()]
            detail = lines[-1][:200] if lines else 'timed out after 60 seconds'
        except OSError as error:
            detail = str(error)[:200]
        errors.append(f'G1 {module}: does not import on its own: {detail}')
    return modules


def _reads(plan, repos, errors):
    declared = plan['target'].get('providers', {})
    if not isinstance(declared, dict):
        errors.append('G2 target: providers must be an object')
        declared = {}
    providers = {**PROVIDERS, **declared}
    emitted = {}
    failed = set()
    for row in plan.get('rows', []):
        row_id = row.get('id', '?')
        for read in row.get('reads', []):
            if not isinstance(read, str) or ':' not in read or not all(read.split(':', 1)):
                errors.append(f'G2 row {row_id}: invalid read {read!r}')
                continue
            provider, key = read.split(':', 1)
            source = providers.get(provider)
            if not isinstance(source, dict) or source.get('repo') not in ('plugin', 'bridge'):
                errors.append(f'G2 row {row_id}: no extractor for provider {provider} (add target.providers)')
                continue
            repo = repos.get(source['repo'])
            if repo is None:
                errors.append(f'G2 row {row_id}: provider {provider} needs target.{source["repo"]}')
                continue
            if provider not in emitted and provider not in failed:
                try:
                    emitted[provider] = emitted_keys(repo['tree'], repo['commit'], source['files'])
                except (KeyError, TypeError, OSError, UnicodeError, subprocess.SubprocessError) as error:
                    errors.append(f'G2 row {row_id}: extractor {provider}: {error}')
                    failed.add(provider)
            if provider in emitted and key not in emitted[provider]:
                files = ', '.join(source['files'])
                errors.append(f'G2 row {row_id}: {read} is not written by {source["repo"]} '
                              f'{repo["commit"][:8]} ({files})')
    return {provider: sorted(keys) for provider, keys in emitted.items()}


def _names(bits):
    return '+'.join(name for name, bit in BUTTONS if bits & bit) or 'none'


def _bindings(plan, base, settings, errors, warnings):
    menu = settings.get('menu') if isinstance(settings, dict) else None
    chords = {}
    if isinstance(menu, dict):
        for action, flags in menu.items():
            if (isinstance(flags, list) and len(flags) == len(BUTTONS)
                    and all(flag in (0, 1) for flag in flags) and sum(flags) >= 2):
                mask = sum(bit for flag, (_, bit) in zip(flags, BUTTONS) if flag)
                chords.setdefault(mask, []).append(action)
    for name, seg in (plan.get('segments') or {}).items():
        action = seg.get('binding')
        try:
            recipe = {recipe_macro.mask(item['buttons']) for item in seg.get('recipe', []) if item.get('buttons')}
        except (TypeError, ValueError) as error:
            errors.append(f'G3 segment {name}: {error}')
            continue
        recipe.discard(0)
        held = set()
        path = base / seg['file'] if seg.get('file') else None
        if path and path.is_file():
            try:
                macro = json.loads(path.read_text())
                held = {event['buttons'] for event in macro.get('events', []) if event.get('buttons')}
                if any(not isinstance(mask, int) or isinstance(mask, bool) or mask < 0 for mask in held):
                    raise ValueError('buttons must be non-negative integer masks')
            except (OSError, ValueError, TypeError, KeyError) as error:
                errors.append(f'G3 segment {name}: compiled macro: {error}')
                continue
        if 'settings' not in plan['target']:
            for mask in sorted(recipe | held):
                if mask.bit_count() >= 2:
                    errors.append(f'G3 segment {name}: presses the chord {_names(mask)} but the target names '
                                  'no settings, so its binding cannot be checked')
        if action:
            flags = menu.get(action) if isinstance(menu, dict) else None
            if not isinstance(flags, list) or len(flags) != len(BUTTONS) or any(flag not in (0, 1) for flag in flags):
                errors.append(f'G3 segment {name}: no persisted binding menu.{action} in '
                              f'{plan["target"].get("settings")}')
                continue
            persisted = sum(bit for flag, (_, bit) in zip(flags, BUTTONS) if flag)
            if recipe != {persisted}:
                names = ', '.join(_names(mask) for mask in sorted(recipe)) or 'none'
                errors.append(f'G3 segment {name}: recipe presses {names} but {action} is bound to {_names(persisted)}')
            if path and path.is_file() and held != {persisted}:
                names = ', '.join(_names(mask) for mask in sorted(held)) or 'none'
                errors.append(f'G3 segment {name}: the compiled macro holds {names} '
                              f'but {action} is bound to {_names(persisted)}')
            continue
        if recipe and path and path.is_file() and recipe != held:
            recipe_names = ', '.join(_names(mask) for mask in sorted(recipe))
            held_names = ', '.join(_names(mask) for mask in sorted(held)) or 'none'
            message = f'G3 segment {name}: recipe presses {recipe_names} but the compiled macro holds {held_names}'
            (errors if isinstance(seg.get('compiled'), dict) else warnings).append(message)
        for mask in sorted(recipe | held):
            if mask in chords:
                actions = ', '.join(sorted(chords[mask]))
                errors.append(f'G3 segment {name}: presses {_names(mask)}, the persisted chord of {actions}, '
                              'without a "binding"')


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _paths(plan, base, raw, modules, errors):
    allowed = {item['path'] for item in plan.get('allow_paths', [])
               if isinstance(item, dict) and isinstance(item.get('path'), str)
               and isinstance(item.get('reason'), str) and item['reason'].strip()}
    own = base.parent.name if base.name == 'rows' and base.parent.parent == RUNS else None

    def check_path(value, where, missing):
        if value in allowed:
            return
        match = re.match(r'^/home/intpa/eden-runs/([^/]+)/', value)
        if match and not match.group(1).startswith('_') and match.group(1) != own:
            errors.append(f'G4 {where}: path into another run: {value}')
        elif (missing and value.startswith(('/home/', '/tmp/', '/dev/shm/', '/mnt/'))
              and len(Path(value).parts) >= 3
              and not re.search(r'\s|<', value) and not Path(value).exists()):
            errors.append(f'G4 rows.json: missing file {value}')

    for value in _strings(raw):
        check_path(value, 'rows.json', True)
    for module in sorted(modules):
        path = (base / module).resolve()
        if base.resolve() not in path.parents or not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                check_path(node.value, module, False)


def _hook_checks(plan, base, modules, repos, errors, warnings):
    import routes

    segment_names = set(plan.get('segments', {}))
    route_names = set(routes.routes_index()['macros'])
    route_names.update(json.loads((routes.LIBRARY / 'index.json').read_text())['segments'])
    sources = []
    for module in sorted(modules):
        path = (base / module).resolve()
        if base.resolve() not in path.parents or not path.is_file():
            continue
        try:
            sources.append((module, ast.parse(path.read_text())))
        except (OSError, SyntaxError, UnicodeError):
            continue
    if not sources:
        return
    declared = plan['target'].get('providers', {})
    providers = {**PROVIDERS, **(declared if isinstance(declared, dict) else {})}
    written = set()
    for source in providers.values():
        if not isinstance(source, dict) or source.get('repo') not in repos:
            continue
        repo = repos[source['repo']]
        files = source.get('files')
        if not isinstance(files, list):
            continue
        for file in files:
            try:
                written.update(emitted_keys(repo['tree'], repo['commit'], [file]))
            except (KeyError, TypeError, OSError, UnicodeError, subprocess.SubprocessError):
                continue
    prefixes = {key.split('.', 1)[0] for key in written if ' ' not in key}
    ignored = {item['key'] for item in plan.get('reads_ignore', [])
               if isinstance(item, dict) and isinstance(item.get('key'), str)
               and isinstance(item.get('reason'), str) and item['reason'].strip()}
    for module, tree in sources:
        route_aliases = {'routes'}
        for imported in ast.walk(tree):
            if isinstance(imported, ast.Import):
                route_aliases.update(alias.asname or alias.name for alias in imported.names if alias.name == 'routes')
        segments, macros, reads, unchecked = set(), set(), set(), 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'play':
                owner = node.func.value
                if isinstance(owner, ast.Name) and owner.id == 'ctx':
                    index = 0
                    names = segments
                elif node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == 'ctx':
                    if not isinstance(owner, ast.Name) or owner.id not in route_aliases:
                        unchecked += 1
                        continue
                    index = 1
                    names = macros
                else:
                    continue
                argument = node.args[index] if len(node.args) > index else None
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    names.add(argument.value)
                else:
                    unchecked += 1
            key = None
            if isinstance(node, ast.Subscript):
                key = node.slice
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == 'get' and node.args):
                key = node.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                value = key.value
                if (re.fullmatch(r'[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+', value)
                        and value.split('.', 1)[0] in prefixes and value not in written and value not in ignored):
                    reads.add(value)
        for name in sorted(segments - segment_names):
            errors.append(f'G8 {module}: ctx.play("{name}") names no segment of this plan')
        for name in sorted(macros - route_names):
            errors.append(f'G8 {module}: routes.play("{name}") names no routes library macro')
        if unchecked:
            warnings.append(f'G8 {module}: {unchecked} play call(s) with a computed name or unknown callee are not checked')
        for key in sorted(reads):
            errors.append(f'G2 {module}: reads "{key}", which the target does not write')


def _needs(plan, errors):
    state, rows = {}, {}

    def give(tokens, source):
        for token in tokens or []:
            if isinstance(token, str):
                name, separator, value = token.partition('=')
                state[name] = (value if separator else None, source)

    for item in plan.get('setup', []):
        if isinstance(item, dict):
            give(item.get('gives'), 'setup')
    for row in plan.get('rows', []):
        row_id = row['id']
        needs = row.get('needs', [])
        provided = {}
        for token in needs:
            name, separator, wanted = token.partition('=')
            if name not in state:
                errors.append(f'G6 row {row_id}: needs "{token}": no setup entry and no earlier row gives it')
            else:
                value, source = state[name]
                if separator and value != wanted:
                    errors.append(f'G6 row {row_id}: needs "{token}" but {name} is "{value}" (set by {source})')
                else:
                    provided[token] = source
        rows[row_id] = {'needs': list(needs), 'provided_by': provided}
        give(row.get('gives'), row_id)
        for step in _steps(row.get('steps')):
            spec = step.get('run', '')
            if isinstance(spec, str) and spec.startswith('fixtures:'):
                import fixtures
                function = fixtures.GIVES.get(spec.partition(':')[2])
                if function:
                    try:
                        give(function(step.get('args', {})), row_id)
                    except (KeyError, TypeError, ValueError):
                        pass
    return rows


def _effects(plan, errors):
    from runner import row_role

    gates = plan.get('gates') or {}
    judge = plan.get('judge')
    has_judge = isinstance(judge, dict) and bool(judge.get('cmd'))
    for row in plan.get('rows', []):
        if 'role' not in row and ('start' not in row or 'end' not in row):
            continue
        if row_role(row) != 'test':
            continue
        steps = list(_steps(row.get('steps')))
        gate_effect = any(step.get('gate') in gates and
                          (step.get('effect') or gates[step['gate']].get('effect')) for step in steps)
        hook_effect = any('run' in step and step.get('effect') for step in steps)
        judged = has_judge and bool(row.get('expected')) and row.get('judge') != 'skip'
        if gate_effect or hook_effect or judged or row.get('outcome') == 'visual':
            continue
        message = (f'G5 row {row.get("id", "?")}: a test row with no effect gate, no effect hook, '
                   'no judged "expected" and no "outcome": "visual"; its PASS would only mean that no step raised')
        if row.get('expected') and not has_judge:
            message += ' ("expected" is set, but the plan has no judge)'
        errors.append(message)


def check(plan, base, raw):
    base = Path(base)
    report = {'errors': [], 'warnings': [], 'result': 'NO TARGET', 'packet_sha256': packet_digest(base),
              'gate_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'target': plan.get('target'), 'emitted': {}, 'rows': {}}
    if 'target' not in plan:
        return report
    errors = report['errors']
    repos, settings = _target(plan, base, errors)
    modules = _hooks(plan, base, errors)
    if isinstance(plan['target'], dict):
        report['emitted'] = _reads(plan, repos, errors)
        _bindings(plan, base, settings, errors, report['warnings'])
        _hook_checks(plan, base, modules, repos, errors, report['warnings'])
    _paths(plan, base, raw, modules, errors)
    report['rows'] = _needs(plan, errors)
    _effects(plan, errors)
    report['result'] = 'BROKEN' if errors else 'OK'
    return report
