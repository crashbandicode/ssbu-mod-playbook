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
SHARED_DIR = Path(__file__).resolve().parent


def shared_hook_names():
    from runner import SHARED_HOOKS
    return SHARED_HOOKS


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


_TEMPLATE_LIMIT = 512
_TEMPLATE_KEY = r'[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*'
_TEMPLATE_NAME = r'[A-Za-z_][A-Za-z0-9_]*'
_TEMPLATE_INT = r'[iu](?:8|16|32|64|128|size)'
_TEMPLATE_RAW = re.compile(r'b?r(#*)"')
_TEMPLATE_CHAR = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|.)|[^'\\\n])'")
_TEMPLATE_MACRO = re.compile(r'\b(write|writeln|format|format_args)!\s*$')
_TEMPLATE_CHAIN = {'iter': 1, 'into_iter': 1, 'iter_mut': 1, 'copied': 2, 'cloned': 2, 'enumerate': 3, 'skip': 4}
_TEMPLATE_SHAPE = 'its loop is not over a range, a written-out list or a named array'


def _template_lex(text):
    """Rust source with comments and the insides of string and char literals blanked, offsets kept, and the plain
    string literals: the offset of the opening quote gives (the offset after the closing quote, the content)."""
    code = list(text)
    strings = {}
    size = len(text)
    pos = 0

    def blank(start, end):
        for index in range(start, min(end, size)):
            if code[index] != '\n':
                code[index] = ' '

    while pos < size:
        char = text[pos]
        word = pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] == '_')
        raw = _TEMPLATE_RAW.match(text, pos) if char in 'br' and not word else None
        if text.startswith('//', pos):
            end = text.find('\n', pos)
            end = size if end < 0 else end
            blank(pos, end)
            pos = end
        elif text.startswith('/*', pos):
            depth, end = 1, pos + 2
            while end < size and depth:
                if text.startswith('/*', end):
                    depth, end = depth + 1, end + 2
                elif text.startswith('*/', end):
                    depth, end = depth - 1, end + 2
                else:
                    end += 1
            blank(pos, end)
            pos = end
        elif raw:
            end = text.find('"' + raw.group(1), raw.end())
            end = size if end < 0 else end
            blank(raw.end(), end)
            pos = end + 1 + len(raw.group(1))
        elif char == '"' or (char == 'b' and not word and text.startswith('b"', pos)):
            quote = pos + (char == 'b')
            end = quote + 1
            while end < size and text[end] != '"':
                end += 2 if text[end] == '\\' else 1
            if end >= size:
                blank(quote + 1, size)
                break
            if char == '"':
                strings[quote] = (end + 1, text[quote + 1:end])
            blank(quote + 1, end)
            pos = end + 1
        elif char == "'":
            literal = _TEMPLATE_CHAR.match(text, pos)
            if literal:
                blank(pos + 1, literal.end() - 1)
                pos = literal.end()
            else:
                pos += 1
        else:
            pos += 1
    return ''.join(code), strings


def _template_pairs(code):
    """The offset of the closing bracket of every opening bracket."""
    close, stack = {}, []
    for pos, char in enumerate(code):
        if char in '([{':
            stack.append(pos)
        elif char in ')]}' and stack:
            close[stack.pop()] = pos
    return close


def _template_parts(code, start, end):
    """The spans of the comma-separated parts of code[start:end], outside any bracket."""
    parts, depth, last = [], 0, start
    for pos in range(start, end):
        char = code[pos]
        if char in '([{':
            depth += 1
        elif char in ')]}':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append((last, pos))
            last = pos + 1
    if code[last:end].strip():
        parts.append((last, end))
    return parts


def _template_number(token, code):
    """An integer written out, or the one integer constant of that name in this file; None otherwise."""
    token = re.sub(r'\s+as\s+' + _TEMPLATE_INT + r'\s*$', '', token.strip())
    if re.fullmatch(r'[0-9][0-9_]*(?:' + _TEMPLATE_INT + r')?', token):
        return int(re.match(r'[0-9_]+', token).group(0).replace('_', ''))
    if not re.fullmatch(_TEMPLATE_NAME, token):
        return None
    if len(re.findall(r'\b(?:const|static|let)\s+(?:mut\s+)?' + token + r'\b', code)) != 1:
        return None
    constant = re.search(r'\bconst\s+' + token + r'\s*:\s*' + _TEMPLATE_INT
                         + r'\s*=\s*([0-9][0-9_]*)(?:' + _TEMPLATE_INT + r')?\s*;', code)
    return int(constant.group(1).replace('_', '')) if constant else None


def _template_loops(code, close):
    """Every `for <pattern> in <expression> { body }` of the blanked code."""
    loops = []
    for match in re.finditer(r'\bfor\b', code):
        pos = match.end()
        word = None
        while pos < len(code) and code[pos] not in '{;':
            if code[pos] in '([' and pos in close:
                pos = close[pos]
            elif re.match(r'in\b', code[pos:pos + 3]) and not (code[pos - 1].isalnum() or code[pos - 1] == '_'):
                word = pos
                break
            pos += 1
        if word is None:
            continue
        pos = word + 2
        while pos < len(code) and code[pos] not in '{;':
            if code[pos] in '([' and pos in close:
                pos = close[pos]
            pos += 1
        if pos < len(code) and code[pos] == '{' and pos in close:
            loops.append({'start': match.start(), 'pattern': code[match.end():word], 'expr': (word + 2, pos),
                          'open': pos, 'close': close[pos]})
    return loops


def _template_path(pattern, name):
    """Where a loop pattern binds `name`: [] for the name itself, else (index, tuple size) per tuple level; None if
    the pattern is not that name or a tuple of names holding it."""
    pattern = pattern.strip()
    plain = re.fullmatch(r'(?:&\s*)?(?:mut\s+)?(' + _TEMPLATE_NAME + r')', pattern)
    if plain:
        return [] if plain.group(1) == name else None
    if pattern.startswith('(') and _template_pairs(pattern).get(0) == len(pattern) - 1:
        parts = _template_parts(pattern, 1, len(pattern) - 1)
        for index, (start, end) in enumerate(parts):
            path = _template_path(pattern[start:end], name)
            if path is not None:
                return [(index, len(parts))] + path
    return None


def _template_string(code, close, strings, start, end, path):
    """The string written out at `path` of the item code[start:end]; None when that is not a written-out string or
    the item is not a tuple of the pattern's size."""
    while start < end and code[start].isspace():
        start += 1
    while end > start and code[end - 1].isspace():
        end -= 1
    if not path:
        return strings[start][1] if start in strings and strings[start][0] == end else None
    if start >= end or code[start] != '(' or close.get(start) != end - 1:
        return None
    parts = _template_parts(code, start + 1, end - 1)
    index, size = path[0]
    if len(parts) != size:
        return None
    return _template_string(code, close, strings, *parts[index], path[1:])


def _template_length(base, code, before):
    """The length of the array `base` as the enclosing function declares it (`base: [T; LEN]`); None otherwise."""
    start = max((match.start() for match in re.finditer(r'\bfn\b', code[:before])), default=None)
    if start is None:
        return None
    span = code[start:before]
    if len(re.findall(r'\b' + base + r'\s*:', span)) != 1 or re.search(r'\blet\s+(?:mut\s+)?' + base + r'\b', span):
        return None
    declared = re.search(r'\b' + base + r'\s*:\s*(?:&\s*(?:mut\s+)?)?\[[^\[\];]*;([^\[\];]*)\]', span)
    return _template_number(declared.group(1), code) if declared else None


def _template_loop_values(loop, path, name, code, close, strings):
    """What the loop gives `name`: (values, how to say them) or (None, why they cannot be told)."""
    start, end = loop['expr']
    if not path:
        span = re.fullmatch(r'\s*\(?\s*([A-Za-z0-9_ ]+?)\s*\.\.(=?)\s*([A-Za-z0-9_ ]+?)\s*\)?\s*', code[start:end])
        if span:
            low, high = _template_number(span.group(1), code), _template_number(span.group(3), code)
            if low is None or high is None:
                return None, 'a bound of its range is not a number or an integer constant of this file'
            high += 1 if span.group(2) else 0
            if high - low > _TEMPLATE_LIMIT:
                return None, f'its range has more than {_TEMPLATE_LIMIT} values'
            return [str(value) for value in range(low, high)], f'{name} = {low} to {high - 1}'
    pos = start
    while pos < end and code[pos].isspace():
        pos += 1
    items = base = None
    if pos < end and code[pos] == '[' and close.get(pos, end) < end:
        items = _template_parts(code, pos + 1, close[pos])
        pos = close[pos] + 1
    else:
        word = re.compile(_TEMPLATE_NAME).match(code, pos, end)
        if not word:
            return None, _TEMPLATE_SHAPE
        base, pos = word.group(0), word.end()
    skip, enumerated, stage = 0, False, 0
    while True:
        call = re.compile(r'\s*\.\s*([a-z_]+)\s*\(').match(code, pos, end)
        if not call:
            break
        opening = call.end() - 1
        if close.get(opening, end) >= end:
            return None, _TEMPLATE_SHAPE
        method, argument = call.group(1), code[opening + 1:close[opening]]
        pos = close[opening] + 1
        order = _TEMPLATE_CHAIN.get(method)
        if order is None or order <= stage or (method != 'skip' and argument.strip()):
            return None, f'its loop goes through .{method}() where the gate does not follow it'
        stage = order
        if method == 'enumerate':
            enumerated = True
        elif method == 'skip':
            skip = _template_number(argument, code)
            if skip is None:
                return None, 'its skip is not a number or an integer constant of this file'
    if code[pos:end].strip():
        return None, _TEMPLATE_SHAPE
    if enumerated:
        if not path or path[0][1] != 2:
            return None, f'{name} is not the index or the item of its enumerate'
        if path[0][0] == 0:
            if len(path) != 1:
                return None, f'{name} is not the index or the item of its enumerate'
            count = len(items) if items is not None else _template_length(base, code, loop['start'])
            if count is None:
                return None, f'the length of {base} is not written as an array length in this function'
            if count - skip > _TEMPLATE_LIMIT:
                return None, f'its index has more than {_TEMPLATE_LIMIT} values'
            return [str(value) for value in range(skip, count)], f'{name} = {skip} to {count - 1}'
        path = path[1:]
    if items is None:
        return None, f'the items of {base} are not written out in the loop'
    values = [_template_string(code, close, strings, *item, path) for item in items[skip:]]
    if any(value is None for value in values):
        return None, 'an item of its list is not a written-out string'
    return values, f'{name} = one of ' + ', '.join(values[:6]) + (', …' if len(values) > 6 else '')


def _template_values(name, at, arguments, code, close, strings, loops):
    """What `name` can hold in a format string at `at`, one value per pass of the loop that binds it: (values, how
    to say them, that loop) or (None, why they cannot be told, None). Anything that could make the written values
    differ from the loop's own is refused: the gate does not follow arithmetic, conditions or early exits."""
    if re.search(r'\b' + name + r'\s*=(?!=)', arguments):
        return None, f'{name} is a named argument of the macro', None
    for loop in sorted((loop for loop in loops if loop['open'] < at < loop['close']), key=lambda loop: -loop['open']):
        if not re.search(r'\b' + name + r'\b', loop['pattern']):
            continue
        if re.search(r'\bmut\s+' + name + r'\b', loop['pattern']):
            return None, f'{name} is a mutable binding', None
        path = _template_path(loop['pattern'], name)
        if path is None:
            return None, f'the loop pattern that binds {name} is not a name or a tuple of names', None
        if re.search(r'\b(?:break|continue|return)\b', code[loop['open']:loop['close']]):
            return None, 'its loop can stop or skip a pass (break, continue or return)', None
        if re.search(r'\b' + name + r'\b', code[loop['open']:at]):
            return None, f'{name} is used inside its loop before the key is written', None
        values, said = _template_loop_values(loop, path, name, code, close, strings)
        return values, said, loop if values is not None else None
    return None, f'no loop around the key binds {name}', None


def _template_keys(text):
    """Keys a source writes through a format string, such as `cpu.{entry}.ai_thinks`: the keys of every template
    whose placeholders are bound by a loop the gate can read, and a note per template (the template, its line, what
    its placeholders hold, or why that cannot be told). A placeholder the gate cannot follow writes no key."""
    code, strings = _template_lex(text)
    close = _template_pairs(code)
    loops = _template_loops(code, close)
    keys, notes = set(), []
    for start, (end, content) in sorted(strings.items()):
        if '{' not in content or '=' not in content:
            continue
        opening = max((pos for pos, closing in close.items() if pos < start < closing), default=None)
        if opening is None or code[opening] != '(':
            continue
        macro = _TEMPLATE_MACRO.search(code, max(0, opening - 40), opening)
        if not macro:
            continue
        # The literal must be the format string: the first argument, or the second of write! and writeln!.
        before = _template_parts(code, opening + 1, start)
        gap = code[before[-1][1] + 1:start] if before else code[opening + 1:start]
        if len(before) != (1 if macro.group(1) in ('write', 'writeln') else 0) or gap.strip():
            continue
        arguments = code[end:close[opening]]
        event = re.match(r'^(?:\[[A-Za-z]+\] )?(' + _TEMPLATE_KEY + r') ', content)
        for match in re.finditer(r'(?:^|\\n| )((?:[a-z0-9_.]|\{' + _TEMPLATE_NAME + r'\})+)=', content):
            template = match.group(1)
            names = list(dict.fromkeys(re.findall(r'\{(' + _TEMPLATE_NAME + r')\}', template)))
            if not names:
                continue
            bound, reason = {}, None
            for name in names:
                values, detail, loop = _template_values(name, start, arguments, code, close, strings, loops)
                if values is None:
                    reason = detail
                    break
                bound.setdefault(loop['open'], []).append((name, values, detail))
            # Placeholders of one loop belong together, pass by pass; only different loops combine.
            combos, said = [{}], []
            for group in bound.values() if reason is None else ():
                if len({len(values) for _, values, _ in group}) != 1:
                    reason = 'placeholders of one loop give lists of different lengths'
                    break
                passes = [{name: values[index] for name, values, _ in group} for index in range(len(group[0][1]))]
                if len(combos) * len(passes) > _TEMPLATE_LIMIT:
                    reason = f'it would write more than {_TEMPLATE_LIMIT} keys'
                    break
                combos = [{**combo, **one} for combo in combos for one in passes]
                details = ' and '.join(detail for _, _, detail in group)
                said.append(details + (', pass by pass' if len(group) > 1 else ''))
            notes.append((template, text.count('\n', 0, start) + 1, ', '.join(said), reason))
            if reason is not None:
                continue
            for combo in combos:
                key = re.sub(r'\{(' + _TEMPLATE_NAME + r')\}', lambda found: combo[found.group(1)], template)
                if re.fullmatch(_TEMPLATE_KEY, key):
                    keys.add(key)
                    if event:
                        keys.add(f'{event.group(1)} {key}')
    return keys, notes


def _template_hint(tree, commit, files, key):
    """For a refused read: the format-string key it has the shape of, and what that template writes; '' if none."""
    for path in files:
        try:
            text = _without_test_items(_git(tree, 'show', f'{commit}:{path}').stdout.decode('utf-8'))
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
            continue
        for template, line, said, reason in _template_keys(text)[1]:
            pieces = re.split(r'(\{[^{}]*\})', template)
            if not any(re.search(r'[a-z0-9_]', piece) for piece in pieces if not piece.startswith('{')):
                continue
            shape = ''.join(r'[^.=\s]+' if piece.startswith('{') else re.escape(piece) for piece in pieces)
            if not re.fullmatch(shape, key):
                continue
            if reason is not None:
                return f'; {path}:{line} writes {template}, but the gate cannot tell its values: {reason}'
            return f'; {path}:{line} writes {template} with {said}'
    return ''


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
        try:
            text = _without_test_items(text)
        except ValueError as error:
            raise ValueError(f'{path} at {commit[:8]}: {error}') from error
        for literal in re.findall(r'"((?:\\.|[^"\\])*)"', text):
            found = re.findall(r'(?:^|\\n| )([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)=', literal)
            keys.update(found)
            event = re.match(r'^(?:\[[A-Za-z]+\] )?([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*) ', literal)
            if event:
                keys.update(f'{event.group(1)} {key}' for key in found)
            if whole_literals and re.fullmatch(r'[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+', literal):
                keys.add(literal)
        keys.update(re.findall(r'\bpush_str\s*\(\s*out\s*,\s*"((?:\\.|[^"\\])*)"', text))
        keys.update(_template_keys(text)[0])
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


_CFG = re.compile(r'#\s*(!?)\s*\[\s*cfg\s*\(')
_TEST_ITEM = re.compile(r'(?:pub(?:\s*\([^()]*\))?\s+)?(?:(?:default|const|async|unsafe|extern)\s+)*'
                        r'(fn|mod|struct|enum|union|impl|trait|extern|macro_rules|use|const|static|type)\b')


def _test_item_end(code, pos, to_semicolon):
    """The offset after the item that starts at `pos` in lexed code: after the `;` that ends it, or after the
    closing brace of its body. `use`, `const`, `static` and `type` items end at their `;` only."""
    size = len(code)
    paren = bracket = angle = depth = 0
    while pos < size:
        char = code[pos]
        if to_semicolon:
            if char in '([{':
                depth += 1
            elif char in ')]}':
                depth -= 1
                if depth < 0:
                    break
            elif char == ';' and not depth:
                return pos + 1
        elif char == '(':
            paren += 1
        elif char == ')':
            paren -= 1
        elif char == '[':
            bracket += 1
        elif char == ']':
            bracket -= 1
        elif char == '<':
            angle += 1
        elif char == '>':
            if angle and code[pos - 1] != '-':
                angle -= 1
        elif not (paren or bracket or angle):
            if char == ';':
                return pos + 1
            if char == '}':
                break
            if char == '{':
                depth = 1
                pos += 1
                while pos < size and depth:
                    depth += (code[pos] == '{') - (code[pos] == '}')
                    pos += 1
                if depth:
                    break
                return pos
        if paren < 0 or bracket < 0:
            break
        pos += 1
    raise ValueError('could not be scanned')


def _cfg_outside_tests(predicate):
    """Whether a `cfg` predicate (lexed, so `feature = "x"` reads `feature =`) can hold in a build that is not a
    test build: False when it cannot, so the item is test-only; True when it always holds; None when that depends
    on a feature, the target or anything else the gate does not follow."""
    tokens = re.findall(r'[A-Za-z_][A-Za-z0-9_]*|[(),=]', predicate)
    pos = 0

    def parse():
        nonlocal pos
        name = tokens[pos]
        if name in '(),=':
            raise ValueError
        pos += 1
        if pos < len(tokens) and tokens[pos] == '(':
            pos += 1
            parts = []
            while tokens[pos] != ')':
                parts.append(parse())
                if tokens[pos] == ',':
                    pos += 1
                elif tokens[pos] != ')':
                    raise ValueError
            pos += 1
            if name == 'not' and len(parts) == 1:
                return None if parts[0] is None else not parts[0]
            if name == 'any':
                return True if True in parts else None if None in parts else False
            if name == 'all':
                return False if False in parts else None if None in parts else True
            return None
        if pos < len(tokens) and tokens[pos] == '=':
            pos += 1
            return None
        return False if name == 'test' else None

    try:
        value = parse()
        if pos != len(tokens):
            raise ValueError
    except (IndexError, ValueError):
        raise ValueError('could not be scanned') from None
    return value


def _without_test_items(text):
    """Rust source with every test-only item blanked, line breaks kept: the `#[cfg(...)]` attribute, any attributes
    after it, and the item itself up to the `;` that ends it or the closing brace of its body. An item is test-only
    when its condition cannot hold outside a test build: `cfg(test)`, `cfg(all(test, ...))`. An item under
    `cfg(any(test, target_os = "switch"))` or `cfg(not(test))` is built for the console and stays.

    What the gate cannot follow fails loudly as `could not be scanned`: a condition it cannot read, a test-only
    attribute on something that is not an item (a field, a statement, a match arm), a test-only item that never
    closes, and a file or module that is test-only as a whole (`#![cfg(test)]`)."""
    if not (_CFG.search(text) and 'test' in text):
        return text
    code, _ = _rust_lex(text)
    kept = list(text)
    size = len(code)
    done = 0
    for attribute in _CFG.finditer(code):
        if attribute.start() < done:
            continue
        depth, pos = 1, attribute.end()
        while pos < size and depth:
            depth += (code[pos] == '(') - (code[pos] == ')')
            pos += 1
        closing = re.compile(r'\s*\]').match(code, pos)
        if depth or not closing:
            raise ValueError('could not be scanned')
        if _cfg_outside_tests(code[attribute.end():pos - 1]) is not False:
            continue
        if attribute.group(1):
            raise ValueError('could not be scanned')
        pos = closing.end()
        while True:
            while pos < size and code[pos].isspace():
                pos += 1
            more = re.compile(r'#\s*\[').match(code, pos)
            if not more:
                break
            depth, pos = 1, more.end()
            while pos < size and depth:
                depth += (code[pos] == '[') - (code[pos] == ']')
                pos += 1
            if depth:
                raise ValueError('could not be scanned')
        item = _TEST_ITEM.match(code, pos)
        if not item:
            raise ValueError('could not be scanned')
        done = _test_item_end(code, pos, item.group(1) in ('use', 'const', 'static', 'type'))
        for index in range(attribute.start(), done):
            if kept[index] != '\n':
                kept[index] = ' '
    return ''.join(kept)


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
        try:
            texts.append(_without_test_items(text))
        except ValueError as error:
            raise ValueError(f'{error} at {commit[:8]}') from error
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
        text = _without_test_items(text)
        list(_rust_function_bodies(text))
        keys.update(_block_keys(text))
    return keys


def _target(plan, base, errors):
    target = plan['target']
    if not isinstance(target, dict):
        errors.append('G2 target: target must be an object')
        return {}, None
    required = []
    if base.name == 'rows':
        config_path = base.parent / 'config.json'
        try:
            try:
                config_path.lstat()
            except FileNotFoundError:
                config = {}
            else:
                config = json.loads(config_path.read_text(encoding='utf-8'))
                if not isinstance(config, dict):
                    raise ValueError('expected a JSON object')
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f'G2 target: run config {config_path} cannot be read: {error}')
            return {}, None
        if 'staged_targets' in config:
            required = config['staged_targets']
            if (not isinstance(required, list) or any(not isinstance(commit, str)
                    or not re.fullmatch(r'[0-9a-f]{40}', commit) for commit in required)):
                errors.append('G2 target: run config staged_targets is invalid')
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
        if not isinstance(tree, str) or not Path(tree).is_absolute():
            errors.append(f'G2 target: {label}.tree must be an existing absolute directory')
            continue
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            errors.append(f'G2 target: {label}.commit must be 40 lower-case hex characters')
            continue
        staged = base.parent.parent / '_eden-test' / 'stage' / base.parent.name / 'targets' / commit
        if required and commit not in required:
            errors.append(f'G2 target: {label}.commit {commit} was not staged for this run')
            continue
        if commit in required and not staged.is_dir():
            errors.append(f'G2 target: {label} staged tree {staged} is missing; this run gates staged targets only '
                          f'(stage-target --commit {commit} --label {base.parent.name})')
            continue
        if base.name == 'rows' and (staged.exists() or staged.is_symlink()):
            try:
                paths = [base.parent.parent / '_eden-test',
                         base.parent.parent / '_eden-test' / 'stage',
                         base.parent.parent / '_eden-test' / 'stage' / base.parent.name,
                         staged.parent, staged]
                if any(path.is_symlink() for path in paths):
                    raise ValueError('symlink in staged path')
                if (staged.is_symlink() or not staged.is_dir() or not (staged / '.git').is_dir()
                        or (staged / '.git').is_symlink()):
                    raise ValueError('not a directory')
                head = _git(staged, 'rev-parse', 'HEAD').stdout.decode('utf-8').strip()
                if head != commit:
                    errors.append(f'G2 target: {label} staged tree {staged} is at {head}, not {commit}')
                    continue
                status = _git(staged, '--no-optional-locks', 'status', '--porcelain',
                              '--untracked-files=all', '--ignored').stdout.decode('utf-8', errors='replace')
                if status.strip():
                    errors.append(f'G2 target: {label} staged tree {staged} changed after staging: '
                                  f'{status.splitlines()[0]}')
                    continue
            except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as error:
                errors.append(f'G2 target: {label} staged tree {staged} cannot be read: {error}')
                continue
            tree = str(staged)
        elif not Path(tree).is_dir():
            if Path(tree).exists():
                errors.append(f'G2 target: {label}.tree must be an existing absolute directory')
            else:
                errors.append(f'G2 target: {label}.tree {tree} does not exist on this host and '
                              f'the run has no staged tree for {commit}')
            continue
        try:
            _git(tree, 'cat-file', '-e', f'{commit}^{{commit}}')
        except (OSError, subprocess.SubprocessError):
            errors.append(f'G2 target: {label}.commit {commit} is not a commit in {tree}')
            continue
        repos[name] = dict(spec, tree=tree)
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


def _providers(plan, errors, warnings):
    declared = plan['target'].get('providers', {})
    if not isinstance(declared, dict):
        errors.append('G2 target: providers must be an object')
        declared = {}
    providers = dict(PROVIDERS)
    for name, source in declared.items():
        if name not in PROVIDERS:
            providers[name] = source
            continue
        builtin = PROVIDERS[name]
        differences = []
        if not isinstance(source, dict) or source.get('repo') != builtin['repo']:
            differences.append('repo')
        files = source.get('files') if isinstance(source, dict) else None
        if (not isinstance(files, list) or not all(isinstance(path, str) for path in files)
                or sorted(files) != sorted(builtin['files'])):
            differences.append('files')
        for field in ('block', 'helpers', 'whole_literals'):
            if isinstance(source, dict) and field in source:
                expected = builtin.get(field)
                if type(source[field]) is not type(expected) or source[field] != expected:
                    differences.append(field)
        if differences:
            errors.append(f'G2 target: providers.{name} redefines a built-in provider '
                          f'({", ".join(differences)}); remove the entry or give the provider another name')
        else:
            warnings.append(f'G2 target: providers.{name} repeats a built-in provider; '
                            'the built-in extraction is used (remove the entry)')
    return providers


def _reads(plan, repos, errors, providers):
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
                hint = '' if source.get('block') else _template_hint(repo['tree'], repo['commit'], source['files'], key)
                errors.append(f'G2 row {row_id}: {read} is not written by {source["repo"]} '
                              f'{repo["commit"][:8]} ({files}){hint}')
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


def _strings(value, location=()):
    if isinstance(value, str):
        yield location, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(child, (*location, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _strings(child, (*location, index))


def _paths(plan, base, raw, modules, repos, errors):
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

    for location, value in _strings(raw):
        name = None
        if len(location) == 3 and location[0] == 'target' and location[1] in ('plugin', 'bridge') \
                and location[2] == 'tree':
            name = location[1]
        elif len(location) == 4 and location[:2] == ('target', 'repos') and location[3] == 'tree':
            name = location[2]
        if name in repos and base.name == 'rows':
            staged = base.parent.parent / '_eden-test' / 'stage' / base.parent.name / 'targets' / repos[name]['commit']
            if repos[name]['tree'] == str(staged):
                continue
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


def _play_set(value):
    """The names of a tuple, list or set of strings written out; None for anything else, and for an empty one."""
    if not isinstance(value, (ast.Tuple, ast.List, ast.Set)) or not value.elts:
        return None
    if not all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts):
        return None
    return {item.value for item in value.elts}


_PLAY_DEPTH = 6
_PLAY_MUTATORS = {'update', 'pop', 'popitem', 'setdefault', 'clear', 'append', 'extend', 'insert', 'remove', 'add',
                  'discard', 'sort', 'reverse'}
_PLAY_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _bound_names(tree):
    """How often each name is bound anywhere in a module: an assignment, a parameter, a loop target, an import or a
    definition."""
    bound = {}

    def count(name):
        if isinstance(name, str):
            bound[name] = bound.get(name, 0) + 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            count(node.id)
        elif isinstance(node, ast.arg):
            count(node.arg)
        elif isinstance(node, ast.alias):
            count((node.asname or node.name).split('.')[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            count(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                count(name)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            count(node.name)
        elif isinstance(node, ast.MatchMapping):
            count(node.rest)
    return bound


def _inside(node, chain=()):
    """(the functions around it, innermost first; the node) for every node inside `node`."""
    stack = [(chain, child) for child in ast.iter_child_nodes(node)]
    while stack:
        chain, child = stack.pop()
        yield chain, child
        inner = (child,) + chain if isinstance(child, _PLAY_FUNCTIONS) else chain
        stack.extend((inner, below) for below in ast.iter_child_nodes(child))


def _parameters(function):
    """The names a function takes by position, and every name it takes."""
    args = function.args
    positional = [arg.arg for arg in args.posonlyargs + args.args]
    every = set(positional) | {arg.arg for arg in args.kwonlyargs}
    every.update(arg.arg for arg in (args.vararg, args.kwarg) if arg is not None)
    return positional, every


def _stores(function, name):
    """The nodes with which a function binds `name` itself, nested functions aside; None when it declares the
    name global or nonlocal. The function's bindings are read once and kept on its node."""
    bindings = getattr(function, '_play_bindings', None)
    if bindings is None:
        bindings, outer = {}, set()
        stack = list(ast.iter_child_nodes(function))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bindings.setdefault(node.name, []).append(node)
                continue
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    alias._play_import = node
            bound = None
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                outer.update(node.names)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound = node.id
            elif isinstance(node, ast.alias):
                bound = (node.asname or node.name).split('.')[0]
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
                bound = node.name
            elif isinstance(node, ast.MatchMapping):
                bound = node.rest
            if isinstance(bound, str):
                bindings.setdefault(bound, []).append(node)
            stack.extend(ast.iter_child_nodes(node))
        bindings.update(dict.fromkeys(outer))
        function._play_bindings = bindings
    return bindings.get(name, [])


def _module_level(node):
    """Every node of a module that is not inside a function or a class."""
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield child
            yield from _module_level(child)


class _PlayModule:
    """One hook module, read with ast only: its functions, classes, the names it binds once to a value, and what
    it imports from the rows folder."""

    def __init__(self, path, label, find):
        self.path, self.label = path, label
        self.tree = ast.parse(path.read_text())
        self.bound = _bound_names(self.tree)
        self.functions, self.classes, self.constants = {}, {}, {}
        self.local, self.imported, self.stars = {}, {}, []
        self.routes, self.route_play, self.mutated = {'routes'}, set(), set()
        # The module's own imports. An import inside a function belongs to that function (`_PlayReader.binding`).
        for node in _module_level(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if '.' in alias.name:
                        continue
                    if alias.name == 'routes':
                        self.routes.add(alias.asname or alias.name)
                    found = find(path, alias.name)
                    if found is not None:
                        self.local[alias.asname or alias.name] = found
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                found = find(path, node.module)
                for alias in node.names:
                    if node.module == 'routes' and alias.name == 'play':
                        self.route_play.add(alias.asname or alias.name)
                    elif found is not None and alias.name == '*':
                        self.stars.append(found)
                    elif found is not None:
                        self.imported[alias.asname or alias.name] = (found, alias.name)
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del))
                    and isinstance(node.value, ast.Name)):
                self.mutated.add(node.value.id)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in _PLAY_MUTATORS and isinstance(node.func.value, ast.Name)):
                self.mutated.add(node.func.value.id)
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = [item for item in node.body
                                           if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
            elif (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                  and self.bound.get(node.targets[0].id) == 1):
                self.constants[node.targets[0].id] = node.value


class _PlayReader:
    """The play calls a plan can reach, and the names they can take (G8).

    It starts at the functions the manifest names (steps, recovery, failure hooks, hook gates) and follows calls
    and references into the same module and into other modules of the rows folder. A module a step names is read
    as a whole; a module that is only imported is read in the functions the plan reaches.

    A name is resolved when it is written at the call, a choice between resolved names, a module constant, an entry
    of a written-out module table, a local set once, or a parameter whose every argument resolves: at each call the
    plan reaches, and in the step's `args`. Anything else is not resolved, and the gate says so. The runner refuses
    a `ctx.play` of a name that is not in the resolved or declared set, so a miss here stops a run; it does not
    let a name through."""

    def __init__(self, plan, base):
        from runner import SHARED_HOOKS

        self.plan, self.base = plan, Path(base).resolve()
        self.modules, self.steps, self.entries = {}, {}, []
        self.reached, self.sites, self.escaped, self.contexts = {}, {}, set(), set()
        for name, function, args in _play_hooks(plan):
            if name in SHARED_HOOKS:
                continue
            path = Path(name)
            path = (path if path.is_absolute() else self.base / path).resolve()
            if self.base not in path.parents or not path.is_file():
                continue
            self.steps.setdefault(path, name)
            if self.module(path, name) is not None:
                self.entries.append((path, function, args))
        self.reach()
        self.flow()

    def find(self, path, name):
        for folder in (path.parent, self.base):
            candidate = (folder / f'{name}.py').resolve()
            if candidate.is_file() and self.base in candidate.parents:
                return candidate
        return None

    def module(self, path, label=None):
        if path not in self.modules:
            try:
                self.modules[path] = _PlayModule(path, label or path.relative_to(self.base).as_posix(), self.find)
            except (OSError, SyntaxError, UnicodeError, ValueError):
                self.modules[path] = None
        return self.modules[path]

    def targets(self, mod, name, seen=()):
        """The functions a module-level name stands for, each with its module and whether it is a plain function
        defined once (its parameters can be followed)."""
        if mod is None or (mod.path, name) in seen:
            return []
        if name in mod.functions:
            return [(mod, mod.functions[name], mod.bound.get(name) == 1)]
        if name in mod.classes:
            return [(mod, method, False) for method in mod.classes[name]]
        seen += ((mod.path, name),)
        if name in mod.imported:
            path, other = mod.imported[name]
            return self.targets(self.module(path), other, seen)
        # Another module-level name for a function: `ok = helper.press`.
        value = mod.constants.get(name)
        if isinstance(value, ast.Name):
            return self.targets(mod, value.id, seen)
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id in mod.local:
            return self.targets(self.module(mod.local[value.value.id]), value.attr, seen)
        if not name.startswith('_'):
            for path in mod.stars:
                found = self.targets(self.module(path), name, seen)
                if found:
                    return found
        return []

    def binding(self, mod, name, chain):
        """How the functions around a use bind a name. None: none of them does, so the module's own binding holds.
        ('module', path), ('from', path, name), ('routes',) or ('route play',): one import inside a function.
        ('hidden',): anything else (a parameter, a local, a name the gate cannot place)."""
        for function in chain:
            if name in _parameters(function)[1]:
                return ('hidden',)
            stores = _stores(function, name)
            if stores == []:
                continue
            if stores is None or len(stores) != 1 or not isinstance(stores[0], ast.alias):
                return ('hidden',)
            alias, source = stores[0], stores[0]._play_import
            if isinstance(source, ast.Import):
                if '.' in alias.name:
                    return ('hidden',)
                if alias.name == 'routes':
                    return ('routes',)
                found = self.find(mod.path, alias.name)
                return ('module', found) if found is not None else ('hidden',)
            if source.level or not source.module:
                return ('hidden',)
            if source.module == 'routes':
                return ('route play',) if alias.name == 'play' else ('hidden',)
            found = self.find(mod.path, source.module)
            return ('from', found, alias.name) if found is not None else ('hidden',)
        return None

    def named(self, mod, name, seen=()):
        """(the value, its module, its name there) of the module constant a module-level name stands for."""
        while mod is not None and (mod.path, name) not in seen:
            seen += ((mod.path, name),)
            if name in mod.constants:
                return mod.constants[name], mod, name
            if name in mod.imported and mod.bound.get(name) == 1:
                path, name = mod.imported[name]
                mod = self.module(path)
                continue
            if name not in mod.bound and not name.startswith('_'):
                for path in mod.stars:
                    found = self.named(self.module(path), name, seen)
                    if found[0] is not None:
                        return found
            break
        return None, None, None

    def constant(self, expr, mod, chain=()):
        """(the value, its module, its name there) of a module constant written as a name or as `module.NAME`."""
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            bound = self.binding(mod, expr.value.id, chain)
            if bound is None and expr.value.id in mod.local:
                return self.named(self.module(mod.local[expr.value.id]), expr.attr)
            if bound is not None and bound[0] == 'module':
                return self.named(self.module(bound[1]), expr.attr)
        elif isinstance(expr, ast.Name):
            bound = self.binding(mod, expr.id, chain)
            if bound is None:
                return self.named(mod, expr.id)
            if bound[0] == 'from':
                return self.named(self.module(bound[1]), bound[2])
        return None, None, None

    def refs(self, mod, node, chain):
        """The functions an expression names, as a name or as `module.name`, and whether the gate found them only
        inside a module constant (a dispatch table), which is a way it does not follow."""
        found = []
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            bound = self.binding(mod, node.id, chain)
            if bound is None:
                found = self.targets(mod, node.id)
            elif bound[0] == 'from':
                found = self.targets(self.module(bound[1]), bound[2])
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            bound = self.binding(mod, node.value.id, chain)
            if bound is None and node.value.id in mod.local:
                found = self.targets(self.module(mod.local[node.value.id]), node.attr)
            elif bound is not None and bound[0] == 'module':
                found = self.targets(self.module(bound[1]), node.attr)
        else:
            return [], False
        if found:
            return found, False
        value, home, _ = self.constant(node, mod, chain)
        if value is not None:
            for inner in ast.walk(value):
                if not isinstance(inner, ast.Constant):
                    found.extend(self.refs(home, inner, ())[0])
        return found, True

    def reach(self):
        todo = []
        for path, function, _ in self.entries:
            todo.extend(self.targets(self.module(path), function))
        while todo:
            mod, node, plain = todo.pop()
            if not plain or node.decorator_list:
                self.escaped.add(id(node))
            if id(node) in self.reached:
                continue
            self.reached[id(node)] = (mod, node)
            called = {id(child.func) for _, child in _inside(node) if isinstance(child, ast.Call)}
            for chain, child in _inside(node, (node,)):
                if id(child) in called:
                    continue
                call = isinstance(child, ast.Call)
                found, indirect = self.refs(mod, child.func if call else child, chain)
                for target in found:
                    todo.append(target)
                    if call and not indirect and target[2]:
                        self.sites.setdefault(id(target[1]), []).append((child, mod, chain))
                    else:
                        self.escaped.add(id(target[1]))

    def flow(self):
        """The parameters that hold the step context: the first one of every function the manifest names, and
        every parameter that is given the context at a call the plan reaches."""
        for path, function, _ in self.entries:
            for _, node, plain in self.targets(self.module(path), function):
                positional = _parameters(node)[0]
                if plain and positional:
                    self.contexts.add((id(node), positional[0]))
        changed = True
        while changed:
            changed = False
            for target, sites in self.sites.items():
                positional, every = _parameters(self.reached[target][1])
                for call, _, chain in sites:
                    given = [(keyword.arg, keyword.value) for keyword in call.keywords if keyword.arg in every]
                    if not any(isinstance(arg, ast.Starred) for arg in call.args):
                        given.extend(zip(positional, call.args))
                    for name, value in given:
                        if (target, name) not in self.contexts and self.context(value, chain):
                            self.contexts.add((target, name))
                            changed = True

    def context(self, expr, chain, depth=_PLAY_DEPTH):
        """Whether an expression is the step context, as far as the gate can tell: the name `ctx`, an attribute
        `.ctx`, a parameter the context flows into, or a local set once to one of these."""
        if isinstance(expr, ast.Attribute):
            return expr.attr == 'ctx'
        if not isinstance(expr, ast.Name) or depth <= 0:
            return False
        if expr.id == 'ctx':
            return True
        for index, function in enumerate(chain):
            if expr.id in _parameters(function)[1]:
                return (id(function), expr.id) in self.contexts
            stores = _stores(function, expr.id)
            if stores == []:
                continue
            if stores is None or len(stores) != 1:
                return False
            for _, node in _inside(function):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and node.targets[0] is stores[0]:
                    return self.context(node.value, chain[index:], depth - 1)
            return False
        return False

    def table(self, expr, mod, chain):
        """The entries of a module table written out with constant keys and names as values; None otherwise."""
        value, home, name = self.constant(expr, mod, chain)
        if not isinstance(value, ast.Dict) or name in home.mutated or not all(
                isinstance(key, ast.Constant) and isinstance(item, ast.Constant) and isinstance(item.value, str)
                for key, item in zip(value.keys, value.values)):
            return None
        return {key.value: item.value for key, item in zip(value.keys, value.values)}

    def declared(self, expr, mod, chain):
        """The closed set a play declares with `among=`: written at the call, or a module constant; None if the
        gate cannot read it."""
        if isinstance(expr, (ast.Name, ast.Attribute)):
            expr, home, name = self.constant(expr, mod, chain)
            if expr is None or name in home.mutated:
                return None
        return _play_set(expr)

    def resolve(self, expr, mod, chain, depth=_PLAY_DEPTH, active=frozenset()):
        """The names an expression can be, or None when the gate cannot tell."""
        if depth <= 0:
            return None
        if isinstance(expr, ast.Constant):
            return {expr.value} if isinstance(expr.value, str) else None
        if isinstance(expr, ast.IfExp):
            first = self.resolve(expr.body, mod, chain, depth, active)
            second = self.resolve(expr.orelse, mod, chain, depth, active)
            return first | second if first is not None and second is not None else None
        if isinstance(expr, ast.Subscript):
            table = self.table(expr.value, mod, chain)
            if table is None:
                return None
            if isinstance(expr.slice, ast.Constant):
                return {table[expr.slice.value]} if expr.slice.value in table else None
            return set(table.values())
        if isinstance(expr, ast.Name):
            for index, function in enumerate(chain):
                if expr.id in _parameters(function)[1]:
                    if index != len(chain) - 1 or isinstance(function, ast.Lambda):
                        return None
                    return self.parameter(function, expr.id, depth, active)
                stores = _stores(function, expr.id)
                if stores is None:
                    return None
                if len(stores) == 1 and isinstance(stores[0], ast.alias):
                    break
                if stores:
                    value = None
                    for _, node in _inside(function):
                        if isinstance(node, ast.Assign) and len(node.targets) == 1 and node.targets[0] is stores[0]:
                            value = node.value
                    if len(stores) != 1 or value is None:
                        return None
                    return self.resolve(value, mod, chain[index:], depth - 1, active)
        if isinstance(expr, (ast.Name, ast.Attribute)):
            value, home, _ = self.constant(expr, mod, chain)
            return self.resolve(value, home, (), depth - 1, active) if value is not None else None
        return None

    def parameter(self, function, name, depth, active):
        """The names a parameter of a reached function can be: from the step's `args` and its default where the
        manifest names the function, and from the argument at every call the plan reaches."""
        key = (id(function), name)
        if key in active:
            return set()
        args = function.args
        if (id(function) not in self.reached or id(function) in self.escaped or _stores(function, name) != []
                or name in [arg.arg for arg in (args.vararg, args.kwarg) if arg is not None]):
            return None
        mod = self.reached[id(function)][0]
        active = active | {key}
        positional = _parameters(function)[0]
        defaults = dict(zip(reversed(positional), reversed(args.defaults)))
        defaults.update({arg.arg: value for arg, value in zip(args.kwonlyargs, args.kw_defaults)
                         if value is not None})
        names, used = set(), False
        for path, entry, given in self.entries:
            if not any(target[1] is function for target in self.targets(self.module(path), entry)):
                continue
            used = True
            if given is None:
                return None
            if name in given:
                found = {given[name]} if isinstance(given[name], str) else None
            else:
                found = self.resolve(defaults[name], mod, (), depth - 1, active) if name in defaults else None
            if found is None:
                return None
            names |= found
        for call, caller, chain in self.sites.get(id(function), []):
            used = True
            if any(isinstance(arg, ast.Starred) for arg in call.args) or any(k.arg is None for k in call.keywords):
                return None
            given, home, scope = None, caller, chain
            if name in positional and positional.index(name) < len(call.args):
                given = call.args[positional.index(name)]
            for keyword in call.keywords:
                if keyword.arg == name:
                    given = keyword.value
            if given is None:
                if name not in defaults:
                    return None
                given, home, scope = defaults[name], mod, ()
            found = self.resolve(given, home, scope, depth - 1, active)
            if found is None:
                return None
            names |= found
        return names if used else None

    def kind(self, mod, call, chain, reached):
        """('segment' | 'macro', where the name stands) for a play the gate knows; (None, 0) for a `.play(` on
        something it does not know; (False, 0) for a call that is no play.

        In a function the plan reaches, every `.play(` the gate cannot place is reported. In the rest of a step
        module, which is read as before, only a `.play(ctx, ...)` on something unknown is."""
        func = call.func
        if isinstance(func, ast.Name):
            bound = self.binding(mod, func.id, chain)
            routed = bound == ('route play',) or (
                bound is None and func.id in mod.route_play and mod.bound.get(func.id) == 1)
            return ('macro', 1) if routed else (False, 0)
        if not isinstance(func, ast.Attribute) or func.attr != 'play':
            return False, 0
        owner = func.value
        if self.context(owner, chain):
            return 'segment', 0
        if isinstance(owner, ast.Name):
            bound = self.binding(mod, owner.id, chain)
            if bound == ('routes',) or (bound is None and owner.id in mod.routes):
                return 'macro', 1
            if (bound is None and owner.id in mod.local) or (bound is not None and bound[0] == 'module'):
                return False, 0
        if reached or (call.args and self.context(call.args[0], chain)):
            return None, 0
        return False, 0

    def calls(self):
        """One record per play call: (module label, 'segment' | 'macro' | None, the names it can take or None,
        the set it declares or None, the name written at the call or None)."""
        scopes = [(self.module(path), self.module(path).tree, ()) for path in self.steps
                  if self.module(path) is not None]
        scopes.extend((mod, node, (node,)) for mod, node in self.reached.values())
        seen, records = set(), []
        for mod, node, chain in scopes:
            for inner, call in _inside(node, chain):
                if not isinstance(call, ast.Call) or id(call) in seen:
                    continue
                reached = any(id(function) in self.reached for function in inner)
                kind, index = self.kind(mod, call, inner, reached)
                if kind is False:
                    continue
                seen.add(id(call))
                argument = call.args[index] if kind and len(call.args) > index else None
                among = next((keyword.value for keyword in call.keywords if keyword.arg == 'among'), None)
                declared = self.declared(among, mod, inner) if kind and among is not None else None
                written = isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                names = self.resolve(argument, mod, inner) if argument is not None else None
                records.append((mod.label, kind, names, declared, argument.value if written else None))
        return records


def _play_hooks(plan):
    """Every hook a manifest names, as (module, function, the step's args); the args are None for a gate hook,
    which the runner calls with the gate."""
    def one(hook):
        spec = hook.get('run') if isinstance(hook, dict) else hook
        args = hook.get('args', {}) if isinstance(hook, dict) else {}
        if isinstance(spec, str) and ':' in spec:
            module, _, function = spec.partition(':')
            yield module, function, args if isinstance(args, dict) else {}

    yield from one(plan.get('on_failure_hook'))
    for row in plan.get('rows') if isinstance(plan.get('rows'), list) else []:
        if isinstance(row, dict):
            yield from one(row.get('on_failure_hook'))
    for _, step in _plan_steps(plan):
        if 'run' in step:
            yield from one(step)
    gates = plan.get('gates') if isinstance(plan.get('gates'), dict) else {}
    for gate in gates.values():
        if (isinstance(gate, dict) and gate.get('kind') == 'hook' and isinstance(gate.get('module'), str)
                and isinstance(gate.get('function'), str)):
            yield gate['module'], gate['function'], None


def _plays(plan, base):
    """G8 for a plan's hooks: per module label its errors and its warning, and the segment names `ctx.play` was
    checked for."""
    import routes

    segments = plan.get('segments') if isinstance(plan.get('segments'), dict) else {}
    known = {'segment': set(segments), 'macro': set(routes.routes_index()['macros'])}
    known['macro'].update(json.loads((routes.LIBRARY / 'index.json').read_text())['segments'])
    callee = {'segment': 'ctx.play', 'macro': 'routes.play'}
    found, checked = {}, set()
    for label, kind, names, declared, written in _PlayReader(plan, base).calls():
        entry = found.setdefault(label, {'segment': set(), 'macro': set(), 'among segment': set(),
                                         'among macro': set(), 'outside': set(), 'unchecked': 0})
        if kind is None or (names is None and declared is None):
            entry['unchecked'] += 1
            continue
        if declared is not None:
            entry[f'among {kind}'].update(declared)
            entry['outside'].update((callee[kind], name) for name in (names or set()) - declared)
        if names is not None and (declared is None or written is not None):
            entry[kind].update(names)
        if kind == 'segment':
            checked.update(declared if declared is not None else names)
    result = {}
    for label, entry in found.items():
        errors = [f'G8 {label}: ctx.play("{name}") names no segment of this plan'
                  for name in sorted(entry['segment'] - known['segment'])]
        errors.extend(f'G8 {label}: routes.play("{name}") names no routes library macro'
                      for name in sorted(entry['macro'] - known['macro']))
        errors.extend(f'G8 {label}: ctx.play among "{name}" names no segment of this plan'
                      for name in sorted(entry['among segment'] - known['segment']))
        errors.extend(f'G8 {label}: routes.play among "{name}" names no routes library macro'
                      for name in sorted(entry['among macro'] - known['macro']))
        errors.extend(f'G8 {label}: {call}("{name}") is not among the names it declares'
                      for call, name in sorted(entry['outside']))
        warning = (f'G8 {label}: {entry["unchecked"]} play call(s) with a computed name or unknown callee '
                   'are not checked') if entry['unchecked'] else None
        result[label] = (errors, warning)
    return result, checked & known['segment']


def play_names(plan, base):
    """The segment names the gate checked for `ctx.play` in this plan's hooks. The runner plays no other name
    through `ctx.play`."""
    return sorted(_plays(plan, base)[1])


def _hook_checks(plan, base, modules, repos, errors, warnings, providers):
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
        return []
    plays, checked = _plays(plan, base)
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
        reads = set()
        for node in ast.walk(tree):
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
        found, warning = plays.pop(module, ([], None))
        errors.extend(found)
        if warning:
            warnings.append(warning)
        for key in sorted(reads):
            errors.append(f'G2 {module}: reads "{key}", which the target does not write')
    # Modules the plan reaches only through an import: G8 names them too.
    for module in sorted(plays):
        found, warning = plays[module]
        errors.extend(found)
        if warning:
            warnings.append(warning)
    return sorted(checked)


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
import importlib, importlib.util, json, pathlib, sys
judge_path = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(judge_path.parent))
sys.path.insert(0, sys.argv[1])
if len(sys.argv) > 5:
    sys.path.insert(0, str(judge_path.parent))
    module = importlib.import_module(sys.argv[5])
else:
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


def _shared_judge(module, hook, row_id, errors):
    path = SHARED_DIR / f'{module}.py'
    try:
        tree = ast.parse(path.read_text())
        assignments = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == 'EFFECT_JUDGES' for target in node.targets):
                assignments.append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == 'EFFECT_JUDGES':
                assignments.append(node.value)
        if not assignments:
            return None
        if len(assignments) != 1 or not isinstance(assignments[0], ast.Dict):
            raise ValueError('must be one dict literal')
        table = ast.literal_eval(assignments[0])
        if not isinstance(table, dict):
            raise ValueError('must be a dict')
        for name, entry in table.items():
            if (not isinstance(name, str) or not isinstance(entry, dict)
                    or set(entry) != {'judge', 'cases'} or not isinstance(entry['judge'], str)
                    or not isinstance(entry['cases'], str)):
                raise ValueError('entries need a hook name, judge and cases strings')
    except (OSError, SyntaxError, UnicodeError, ValueError, TypeError) as error:
        errors.append(f'G9 row {row_id}: {module}.EFFECT_JUDGES is malformed: {error}')
        return False
    if hook not in table:
        return None
    entry = table[hook]
    judge, folder = entry['judge'], entry['cases']
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    valid = True
    if judge not in functions:
        errors.append(f'G9 row {row_id}: {module}:{judge} is not a function of {module}')
        valid = False
    hook_function = functions.get(hook)
    if not hook_function or not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                                    and node.func.id == judge for node in ast.walk(hook_function)):
        errors.append(f'G9 row {row_id}: {module}:{hook} does not call its judge {judge}')
        valid = False
    try:
        root = (SHARED_DIR / 'library' / 'cases').resolve()
        component = bool(folder) and folder not in ('.', '..') and not any(ch in folder for ch in '/\\\0')
        directory = (root / folder).resolve() if component else None
        exists = directory is not None and directory.is_relative_to(root) and directory.is_dir()
    except (OSError, ValueError, RuntimeError):
        exists = False
    if not exists:
        errors.append(f'G9 row {row_id}: cases folder library/cases/{folder} is missing')
        valid = False
    return (path, judge, directory, root) if valid else False


def _cases(plan, base, errors, warnings):
    from runner import row_role

    root = base.resolve()
    result, cache = {}, {}
    for row in plan.get('rows', []):
        row_id = row.get('id', '?')
        is_test = (('role' in row or ('start' in row and 'end' in row)) and row_role(row) == 'test')
        for step in _steps(row.get('steps')):
            spec = step.get('run')
            if not isinstance(spec, str) or not step.get('effect'):
                continue
            module, separator, hook = spec.partition(':')
            if module == 'fixtures' and separator and not is_test:
                continue
            shared_module = module if is_test and separator and module in shared_hook_names() else None
            case_root = root
            shared = None
            if shared_module:
                if 'cases' in step:
                    errors.append(f'G9 row {row_id}: {spec} is a shared hook; its judge cases belong to '
                                  'the runner (remove "cases")')
                    continue
                shared = _shared_judge(shared_module, hook, row_id, errors)
                if shared is False:
                    continue
            if not shared and 'cases' not in step:
                if is_test:
                    message = f'G9 row {row_id}: effect hook {spec} has no judge cases; its evidence is trusted'
                    if plan.get('require_cases'):
                        errors.append(message + ' (require_cases)')
                    else:
                        warnings.append(message)
                continue
            if shared:
                path, function, directory, case_root = shared
                judge_file, judge_spec = f'{module}.py', f'{module}:{function}'
            else:
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
                    if not case_path.resolve().is_relative_to(case_root):
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
                        if not source.is_relative_to(case_root) or not source.is_file():
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
            if not shared and hook_file != path:
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
                    command = [sys.executable, '-I', '-B', '-c', CASE_LOADER, str(runner_dir),
                               str(path), function, str(directory)]
                    if shared:
                        command.append(shared_module)
                    proc = subprocess.run(command,
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
              'target': plan.get('target'), 'emitted': {}, 'rows': {}, 'cases': {}, 'plays': []}
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
        providers = _providers(plan, errors, report['warnings'])
        report['emitted'] = _reads(plan, repos, errors, providers)
        _bindings(plan, base, settings, errors, report['warnings'])
        report['plays'] = _hook_checks(plan, base, modules, repos, errors, report['warnings'], providers)
    _paths(plan, base, raw, modules, repos, errors)
    report['rows'] = _needs(plan, errors)
    _effects(plan, errors)
    report['cases'] = _cases(plan, base, errors, report['warnings'])
    report['result'] = 'BROKEN' if errors else 'OK'
    return report
