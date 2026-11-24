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
    'ssbu.ui': {'repo': 'bridge', 'files': ['src/native_ui/mod.rs'], 'block': 'ssbu.ui',
                'helpers': ['src/native_ui/pause_focus.rs']},
    'bridge': {'repo': 'bridge', 'files': ['src/proto2.rs', 'src/hooks.rs', 'src/model.rs', 'src/runtime.rs'],
               'whole_literals': False},
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


def _plan_steps(plan):
    rows = plan.get('rows') if isinstance(plan.get('rows'), list) else []
    first = next((row.get('id', '?') for row in rows if isinstance(row, dict)), '<plan>')
    for step in _steps(plan.get('recover')):
        yield first, step
    for row in rows:
        if isinstance(row, dict):
            for name in ('steps', 'recover'):
                for step in _steps(row.get(name)):
                    yield row.get('id', '?'), step
    classes = plan.get('screen_classes') if isinstance(plan.get('screen_classes'), list) else []
    for screen in classes:
        if isinstance(screen, dict):
            for step in _steps(screen.get('reach')):
                yield first, step


def _packet_inputs(base, plan):
    from runner import SHARED_HOOKS, _HERE

    paths = {(base / 'rows.json').resolve()}

    def add(value):
        if isinstance(value, str) and value:
            paths.add((base / value).resolve())

    def hook(spec):
        if isinstance(spec, dict):
            spec = spec.get('run')
        if isinstance(spec, str):
            name, separator, _ = spec.partition(':')
            if separator and name:
                add(str(_HERE / f'{name}.py') if name in SHARED_HOOKS else name)

    segments = plan.get('segments') if isinstance(plan.get('segments'), dict) else {}
    for segment in segments.values():
        if isinstance(segment, dict):
            add(segment.get('file'))
    gates = plan.get('gates') if isinstance(plan.get('gates'), dict) else {}
    for gate in gates.values():
        if isinstance(gate, dict):
            add(gate.get('reference'))
            if gate.get('kind') == 'hook':
                add(gate.get('module'))
    hook(plan.get('on_failure_hook'))
    rows = plan.get('rows') if isinstance(plan.get('rows'), list) else []
    for row in rows:
        if isinstance(row, dict):
            hook(row.get('on_failure_hook'))
    for _, step in _plan_steps(plan):
        hook(step.get('run'))
    return paths


def _gif_destinations(base, plan):
    from runner import gif_dest_path, project_root

    try:
        root = Path(plan['root']) if plan.get('root') else project_root(base)
    except (OSError, TypeError, ValueError):
        return
    for row_id, step in _plan_steps(plan):
        gif = step.get('gif')
        if not isinstance(gif, dict) or not gif.get('out'):
            continue
        try:
            yield row_id, gif['out'], gif_dest_path(root, base, gif)
        except (OSError, ValueError):
            pass


def _gif_names_input(path, inputs):
    return path in inputs or path.suffix in ('.py', '.json')


def inputs_identity(base, plan):
    base = Path(base)
    excluded = set()
    segments = plan.get('segments') if isinstance(plan.get('segments'), dict) else {}
    for segment in segments.values():
        if not isinstance(segment, dict):
            continue
        name = segment.get('file')
        if isinstance(name, str):
            path = base / name
            if segment.get('recorded') or not path.exists():
                excluded.add(path.resolve())
    gates = plan.get('gates') if isinstance(plan.get('gates'), dict) else {}
    for gate in gates.values():
        if not isinstance(gate, dict):
            continue
        name = gate.get('reference')
        if isinstance(name, str) and (gate.get('acquire') or gate.get('acquired')):
            excluded.add((base / name).resolve())
    inputs = _packet_inputs(base, plan)
    destinations = [path for _, _, path in _gif_destinations(base, plan)]
    gif_outputs = {path for path in destinations if not _gif_names_input(path, inputs)}
    protected = {path for path in destinations if _gif_names_input(path, inputs)}
    excluded.update(gif_outputs)
    files = []
    for path in base.rglob('*'):
        rel = path.relative_to(base)
        if not path.is_file() or path.is_symlink():
            continue
        resolved = path.resolve()
        if resolved in excluded:
            continue
        if (resolved not in protected and (rel == Path('rows.json') or 'out' in rel.parts
                or '__pycache__' in rel.parts or path.suffix == '.pyc' or path.name == 'packet-gate.json')):
            continue
        files.append((rel.as_posix(), path))
    for path in Path(__file__).resolve().parent.glob('*.py'):
        if path.is_file() and not path.is_symlink() and not path.name.startswith('test_'):
            files.append((f'runner:{path.name}', path))
    result = hashlib.sha256()
    for name, path in sorted(files):
        result.update(f'{name}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n'.encode())
    return result.hexdigest()


def _git(tree, *args):
    return subprocess.run(['git', '-C', str(tree), *args], capture_output=True, timeout=20, check=True)


def emitted_keys(tree, commit, files, whole_literals=True):
    keys = set()
    for path in files:
        try:
            text = _git(tree, 'show', f'{commit}:{path}').stdout.decode('utf-8')
        except subprocess.CalledProcessError as error:
            detail = error.stderr.decode('utf-8', errors='replace').strip() if error.stderr else ''
            raise ValueError(f'{path} at {commit}: {detail or f"git show exit {error.returncode}"}') from error
        text = re.split(r'(?m)^\s*#\[cfg\(test\)\]\s*$', text, maxsplit=1)[0]
        for literal in re.findall(r'"((?:\\.|[^"\\])*)"', text):
            found = re.findall(r'(?:^|\\n| )([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)=', literal)
            keys.update(found)
            event = re.match(r'^(?:\[[A-Za-z]+\] )?([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*) ', literal)
            if event:
                keys.update(f'{event.group(1)} {key}' for key in found)
            if whole_literals and re.fullmatch(r'[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+', literal):
                keys.add(literal)
        keys.update(re.findall(r'\bpush_str\s*\(\s*out\s*,\s*"((?:\\.|[^"\\])*)"', text))
    keys.discard('schema')
    return keys


def _rust_lex(text):
    code = list(text)
    literals = []
    i = 0
    while i < len(text):
        start = i
        if text.startswith('//', i):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
        elif text.startswith('/*', i):
            i += 2
            depth = 1
            while i < len(text) and depth:
                if text.startswith('/*', i):
                    depth += 1
                    i += 2
                elif text.startswith('*/', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise ValueError('could not be scanned')
        else:
            raw = re.match(r'(?:b)?r(#+)?"', text[i:])
            if raw:
                hashes = raw.group(1) or ''
                content = i + raw.end()
                end = text.find('"' + hashes, content)
                if end < 0:
                    raise ValueError('could not be scanned')
                i = end + 1 + len(hashes)
                literals.append((start, i, text[content:end], True))
            elif text[i] == '"' or text.startswith('b"', i):
                quote = i + (text[i] == 'b')
                i = quote + 1
                while i < len(text) and text[i] != '"':
                    i += 2 if text[i] == '\\' else 1
                if i >= len(text):
                    raise ValueError('could not be scanned')
                literals.append((start, i + 1, text[quote + 1:i], False))
                i += 1
            elif text[i] == "'":
                char = re.match(r"'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|.)|[^'\\\n])'", text[i:])
                if char:
                    i += char.end()
                else:
                    i += 1
                    continue
            else:
                i += 1
                continue
        code[start:i] = ['\n' if ch == '\n' else ' ' for ch in text[start:i]]
    return ''.join(code), literals


def _rust_function_bodies(text):
    code, _ = _rust_lex(text)
    depth = 0
    for char in code:
        depth += (char == '{') - (char == '}')
        if depth < 0:
            raise ValueError('could not be scanned')
    if depth:
        raise ValueError('could not be scanned')
    for match in re.finditer(r'\bfn\s+[A-Za-z_][A-Za-z0-9_]*', code):
        paren = bracket = angle = 0
        pos = match.end()
        while pos < len(code):
            char = code[pos]
            if char == '(':
                paren += 1
            elif char == ')':
                paren -= 1
            elif char == '[':
                bracket += 1
            elif char == ']':
                bracket -= 1
            elif char == '<':
                angle += 1
            elif char == '>' and angle:
                angle -= 1
            elif char == ';' and not (paren or bracket or angle):
                break
            elif char == '{' and not (paren or bracket or angle):
                start = pos
                depth = 1
                pos += 1
                while pos < len(code) and depth:
                    depth += (code[pos] == '{') - (code[pos] == '}')
                    pos += 1
                if depth:
                    raise ValueError('could not be scanned')
                yield start, pos
                break
            pos += 1


def _block_keys(text):
    code, literals = _rust_lex(text)
    keys = set()
    key_pattern = r'[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*'
    for _, _, literal, raw in literals:
        if re.match(r'^\[[^\]\r\n]+\] ', literal):
            continue
        keys.update(re.findall((r'^(' if raw else r'(?:^|\\n)(') + key_pattern + r')=', literal))
    calls = re.finditer(r'\b[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*'
                        r'(?:::\s*<[^(){};]*>)?\s*\(\s*(?:out|&\s*mut\s+out)\s*,', code)
    for call in calls:
        arg_start = call.end()
        stack = []
        for pos in range(arg_start, len(code)):
            char = code[pos]
            if char in '([{':
                stack.append(char)
                continue
            if char in ')]}':
                if stack:
                    stack.pop()
                    continue
                if char != ')':
                    break
            elif char != ',' or stack:
                continue
            argument = next((literal for start, end, literal, _ in literals
                             if arg_start <= start and end <= pos
                             and not code[arg_start:start].strip() and not code[end:pos].strip()), None)
            if argument is not None:
                if re.fullmatch(key_pattern, argument):
                    keys.add(argument)
                break
            if char == ')':
                break
            arg_start = pos + 1
    return keys


def _provider_keys(tree, commit, source, partial=False):
    files = source['files']
    block = source.get('block')
    if not block:
        if not partial:
            return emitted_keys(tree, commit, files, source.get('whole_literals') is not False)
        keys = set()
        for path in files:
            try:
                keys.update(emitted_keys(tree, commit, [path], source.get('whole_literals') is not False))
            except (KeyError, TypeError, ValueError, OSError, UnicodeError, subprocess.SubprocessError):
                continue
        return keys
    texts = []
    for path in files:
        try:
            text = _git(tree, 'show', f'{commit}:{path}').stdout.decode('utf-8')
        except subprocess.CalledProcessError as error:
            if partial:
                continue
            detail = error.stderr.decode('utf-8', errors='replace').strip() if error.stderr else ''
            raise ValueError(f'{path} at {commit}: {detail or f"git show exit {error.returncode}"}') from error
        texts.append(re.split(r'(?m)^\s*#\[cfg\(test\)\]\s*$', text, maxsplit=1)[0])
    keys = set()
    found = False
    for text in texts:
        try:
            bodies = list(_rust_function_bodies(text))
            _, literals = _rust_lex(text)
        except ValueError as error:
            raise ValueError(f'{error} at {commit[:8]}') from error
        for start, end in bodies:
            if any(start < pos < end and f'schema={block}/' in value for pos, _, value, _ in literals):
                found = True
                keys.update(_block_keys(text[start:end]))
    if not found:
        raise ValueError(f'no function writes schema={block}/ in {", ".join(files)} at {commit[:8]}')
    declarations = '\n'.join(_rust_lex(text)[0] for text in texts)
    for path in source.get('helpers', []):
        stem = Path(path).stem
        if not re.search(r'\bmod\s+' + re.escape(stem) + r'\s*;', declarations):
            continue
        try:
            text = _git(tree, 'show', f'{commit}:{path}').stdout.decode('utf-8')
        except subprocess.CalledProcessError:
            continue
        list(_rust_function_bodies(text))
        keys.update(_block_keys(text))
    return keys


def _target(plan, base, errors):
    target = plan['target']
    if not isinstance(target, dict):
        errors.append('G2 target: target must be an object')
        return {}, None
    repos = {}
    extra = target.get('repos', {})
    if not isinstance(extra, dict):
        errors.append('G2 target: repos must be an object')
        extra = {}
    specs = [('plugin', target.get('plugin'), 'plugin')]
    if 'bridge' in target:
        specs.append(('bridge', target['bridge'], 'bridge'))
    for name, spec in extra.items():
        if name in ('plugin', 'bridge'):
            errors.append(f'G2 target: repos.{name} is reserved')
        elif not isinstance(name, str) or not name:
            errors.append(f'G2 target: invalid repository name {name!r}')
        else:
            specs.append((name, spec, f'repos.{name}'))
    for name, spec, label in specs:
        if not isinstance(spec, dict):
            errors.append(f'G2 target: {label} must be an object')
            continue
        tree, commit = spec.get('tree'), spec.get('commit')
        if not isinstance(tree, str) or not Path(tree).is_absolute() or not Path(tree).is_dir():
            errors.append(f'G2 target: {label}.tree must be an existing absolute directory')
            continue
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            errors.append(f'G2 target: {label}.commit must be 40 lower-case hex characters')
            continue
        try:
            _git(tree, 'cat-file', '-e', f'{commit}^{{commit}}')
        except (OSError, subprocess.SubprocessError):
            errors.append(f'G2 target: {label}.commit {commit} is not a commit in {tree}')
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
    extra = plan['target'].get('repos', {})
    repo_names = {'plugin', 'bridge'} | ({name for name in extra if isinstance(name, str)}
                                         if isinstance(extra, dict) else set())
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
            if (not isinstance(source, dict) or not isinstance(source.get('repo'), str)
                    or source['repo'] not in repo_names):
                errors.append(f'G2 row {row_id}: no extractor for provider {provider} (add target.providers)')
                continue
            repo = repos.get(source['repo'])
            if repo is None:
                name = source['repo']
                label = name if name in ('plugin', 'bridge') else f'repos.{name}'
                errors.append(f'G2 row {row_id}: provider {provider} needs target.{label}')
                continue
            if provider not in emitted and provider not in failed:
                try:
                    emitted[provider] = _provider_keys(repo['tree'], repo['commit'], source)
                except (KeyError, TypeError, ValueError, OSError, UnicodeError, subprocess.SubprocessError) as error:
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
        if not isinstance(source, dict) or not isinstance(source.get('repo'), str) or source['repo'] not in repos:
            continue
        repo = repos[source['repo']]
        files = source.get('files')
        if not isinstance(files, list):
            continue
        try:
            written.update(_provider_keys(repo['tree'], repo['commit'], source, partial=True))
        except (KeyError, TypeError, ValueError, OSError, UnicodeError, subprocess.SubprocessError):
            continue
    prefixes = {key.split('.', 1)[0] for key in written if ' ' not in key and '.' in key}
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


CASE_LOADER = r'''
import importlib.util, json, pathlib, sys
judge_path = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(judge_path.parent))
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location(judge_path.stem, judge_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
judge = getattr(module, sys.argv[3])
from judge_queue import effect_evidence
result = {}
directory = pathlib.Path(sys.argv[4])
for path in sorted(directory.rglob("*.json"), key=lambda p: p.relative_to(directory).as_posix()):
    case = json.loads(path.read_text())
    args = dict(case.get("args", {}))
    for key, name in case.get("files", {}).items():
        args[key] = (path.parent / name).read_text().splitlines()
    try:
        value = judge(**args)
        outcome = "accept" if effect_evidence(value) else "reject"
        text = repr(value)
    except Exception as error:
        outcome, text = "reject", str(error)
    result[path.relative_to(directory).as_posix()] = {"outcome": outcome, "text": text[:300]}
print(json.dumps(result))
'''


def _cases(plan, base, errors, warnings):
    from runner import row_role

    root = base.resolve()
    result, cache = {}, {}
    for row in plan.get('rows', []):
        row_id = row.get('id', '?')
        is_test = (('role' in row or ('start' in row and 'end' in row)) and row_role(row) == 'test')
        for step in _steps(row.get('steps')):
            spec = step.get('run')
            if not isinstance(spec, str) or not step.get('effect') or spec.startswith('fixtures:'):
                continue
            if 'cases' not in step:
                if is_test:
                    message = f'G9 row {row_id}: effect hook {spec} has no judge cases; its evidence is trusted'
                    if plan.get('require_cases'):
                        errors.append(message + ' (require_cases)')
                    else:
                        warnings.append(message)
                continue
            cases = step['cases']
            if not isinstance(cases, dict) or not isinstance(cases.get('judge'), str) or not isinstance(cases.get('dir'), str):
                errors.append(f'G9 row {row_id}: cases: needs judge and dir')
                continue
            judge_spec, directory = cases['judge'], (base / cases['dir']).resolve()
            judge_file, separator, function = judge_spec.partition(':')
            path = (base / judge_file).resolve()
            if (not separator or not judge_file or not function or not path.is_relative_to(root)):
                errors.append(f'G9 row {row_id}: cases: invalid judge {judge_spec!r}')
                continue
            if not directory.is_relative_to(root) or not directory.is_dir():
                errors.append(f'G9 row {row_id}: cases: directory {cases["dir"]!r} is not a folder inside the rows folder')
                continue
            files = sorted(directory.rglob('*.json'), key=lambda p: p.relative_to(directory).as_posix())
            parsed, malformed = {}, False
            for case_path in files:
                case_name = case_path.relative_to(directory).as_posix()
                try:
                    if not case_path.resolve().is_relative_to(root):
                        raise ValueError(f'{case_name}: case leaves the rows folder')
                    case = json.loads(case_path.read_text())
                    if not isinstance(case, dict) or case.get('expect') not in ('accept', 'reject'):
                        raise ValueError(f'{case_name}: expect must be accept or reject')
                    args, inputs = case.get('args', {}), case.get('files', {})
                    if not isinstance(args, dict) or not isinstance(inputs, dict):
                        raise ValueError(f'{case_name}: args and files must be objects')
                    if 'contains' in case and not isinstance(case['contains'], str):
                        raise ValueError(f'{case_name}: contains must be a string')
                    for key, name in inputs.items():
                        if not isinstance(key, str) or not isinstance(name, str) or key in args:
                            raise ValueError(f'{case_name}: invalid file argument {key!r}')
                        source = (case_path.parent / name).resolve()
                        if not source.is_relative_to(root) or not source.is_file():
                            raise ValueError(f'{case_name}: file {name!r} leaves the rows folder or is missing')
                    parsed[case_name] = case
                except (OSError, ValueError, TypeError) as error:
                    errors.append(f'G9 row {row_id}: cases: {error}')
                    malformed = True
            if malformed:
                continue
            accepts = sum(case['expect'] == 'accept' for case in parsed.values())
            rejects = sum(case['expect'] == 'reject' for case in parsed.values())
            if not accepts or not rejects:
                errors.append(f'G9 row {row_id}: cases need at least one accept and one reject '
                              f'(found {accepts} accept, {rejects} reject)')
            hook_file = (base / spec.partition(':')[0]).resolve()
            if hook_file != path:
                try:
                    tree = ast.parse(hook_file.read_text())
                    imported = any(
                        (isinstance(node, ast.Import) and any(alias.name == path.stem for alias in node.names))
                        or (isinstance(node, ast.ImportFrom) and node.module == path.stem)
                        for node in ast.walk(tree))
                except (OSError, SyntaxError, UnicodeError):
                    imported = False
                if not imported:
                    errors.append(f'G9 row {row_id}: {spec.partition(":")[0]} does not import {judge_file}')
            key = (str(path), function, str(directory))
            if key not in cache:
                runner_dir = Path(__file__).resolve().parent
                try:
                    proc = subprocess.run([sys.executable, '-I', '-B', '-c', CASE_LOADER, str(runner_dir),
                                           str(path), function, str(directory)],
                                          timeout=120, capture_output=True, text=True, errors='replace')
                    lines = [line for line in proc.stderr.splitlines() if line.strip()]
                    if proc.returncode:
                        raise RuntimeError(lines[-1][:200] if lines else f'exit {proc.returncode}')
                    try:
                        output = json.loads(proc.stdout)
                        if not isinstance(output, dict) or set(output) != set(parsed) or any(
                                not isinstance(value, dict) or value.get('outcome') not in ('accept', 'reject')
                                or not isinstance(value.get('text'), str) for value in output.values()):
                            raise ValueError('invalid case output')
                    except (ValueError, TypeError):
                        raise RuntimeError(lines[-1][:200] if lines else 'non-JSON or invalid case output') from None
                    cache[key] = output, None
                except subprocess.TimeoutExpired as error:
                    stderr = error.stderr or ''
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode(errors='replace')
                    lines = [line for line in stderr.splitlines() if line.strip()]
                    cache[key] = None, lines[-1][:200] if lines else 'timed out after 120 seconds'
                except (OSError, RuntimeError) as error:
                    cache[key] = None, str(error)[:200]
            output, failure = cache[key]
            if failure is not None:
                errors.append(f'G9 row {row_id}: {judge_spec} could not run its cases: {failure}')
                continue
            agreed = {'accept': 0, 'reject': 0}
            for name, case in parsed.items():
                actual = output[name]
                text = actual['text'][:300]
                if case['expect'] == 'reject' and actual['outcome'] == 'accept':
                    errors.append(f'G9 row {row_id}: case {name}: expected reject, the judge accepted')
                elif case['expect'] == 'accept' and actual['outcome'] == 'reject':
                    errors.append(f'G9 row {row_id}: case {name}: expected accept, the judge refused: {text}')
                elif case['expect'] == 'reject' and case.get('contains') not in (None, '') and case['contains'] not in text:
                    errors.append(f'G9 row {row_id}: case {name}: refused, but not with "{case["contains"]}": {text}')
                else:
                    agreed[case['expect']] += 1
            result.setdefault(row_id, {})[judge_spec] = agreed
    return result


def check(plan, base, raw):
    base = Path(base)
    report = {'errors': [], 'warnings': [], 'result': 'NO TARGET', 'packet_sha256': packet_digest(base),
              'gate_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'inputs_identity': inputs_identity(base, plan),
              'target': plan.get('target'), 'emitted': {}, 'rows': {}, 'cases': {}}
    inputs = _packet_inputs(base, plan)
    for row_id, name, path in _gif_destinations(base, plan):
        if _gif_names_input(path, inputs):
            report['errors'].append(f'G4 row {row_id}: gif out {name} names a packet input')
    if 'target' not in plan:
        report['cases'] = _cases(plan, base, report['errors'], report['warnings'])
        if report['errors']:
            report['result'] = 'BROKEN'
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
    report['cases'] = _cases(plan, base, errors, report['warnings'])
    report['result'] = 'BROKEN' if errors else 'OK'
    return report
