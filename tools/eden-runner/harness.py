#!/usr/bin/env python3
"""Plugin-agnostic harness controls for the shared Eden runner.

Failure kinds, offline gate fixtures and the self-test boot read only what a
row plan declares. Screen classes, assertion roles and recovery routes come
from that plan. This module does not name a plugin.
"""
import importlib.util
import io
import json
from pathlib import Path


ASSERTS = ('screen', 'effect', 'card', 'trace')
SCREEN_KINDS = ('image', 'ui', 'status', 'change')


class EarlyStop(RuntimeError):
    """The same cause has failed often enough that continuing would repeat it."""


def assertion_role(gate):
    """What a gate is claiming. A plan may set asserts; otherwise the kind decides."""
    declared = gate.get('asserts') if isinstance(gate, dict) else None
    if declared in ASSERTS:
        return declared
    kind = (gate or {}).get('kind')
    if kind in SCREEN_KINDS:
        return 'screen'
    if kind == 'json':
        return 'card'
    source = str((gate or {}).get('source', ''))
    if kind == 'log' and 'trace' in source:
        return 'trace'
    return 'effect'


def early_stop_limit(plan):
    """Positive count, or None when early stop is off. Invalid plans are the dry-run's job."""
    try:
        return _early_stop_limit(plan)
    except ValueError:
        return 3


def _early_stop_limit(plan):
    raw = plan.get('early_stop', 3) if isinstance(plan, dict) else 3
    if raw is False or raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get('after', 3)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 1:
        raise ValueError('early_stop must be a positive number, false, or {"after": n}')
    return int(raw)


def validate_declarations(plan, errors):
    """Declared harness policy only. Plans that omit the keys stay valid."""
    if not isinstance(plan, dict):
        return
    if 'early_stop' in plan:
        try:
            _early_stop_limit(plan)
        except ValueError as error:
            errors.append(str(error))
    classes = plan.get('screen_classes')
    if classes is not None:
        if not isinstance(classes, list):
            errors.append('screen_classes must be a list')
        else:
            for spec in classes:
                if (not isinstance(spec, dict) or not spec.get('class')
                        or not isinstance(spec.get('reach'), list) or not spec.get('gate')):
                    errors.append(f'screen class {spec!r} needs class, reach steps, and gate')
                    continue
                if spec['gate'] not in plan.get('gates', {}):
                    errors.append(f'screen class {spec["class"]}: unknown gate {spec["gate"]}')
    for name, gate in (plan.get('gates') or {}).items():
        if isinstance(gate, dict) and 'asserts' in gate and gate['asserts'] not in ASSERTS:
            errors.append(f'gate {name}: asserts {gate["asserts"]!r} must be screen, effect, card, or trace')


def frames_unchanged(baseline, latest, tolerance):
    """True when two captured frames are the same screen within tolerance. None if they cannot be read."""
    if not baseline or not latest:
        return None
    if baseline == latest:
        return True
    try:
        from PIL import Image
        with Image.open(io.BytesIO(baseline)) as left, Image.open(io.BytesIO(latest)) as right:
            frame = left.convert('RGB')
            other = right.convert('RGB')
            if frame.size != other.size:
                return False
            first, second = frame.tobytes(), other.tobytes()
            if len(first) != len(second) or not first:
                return False
            return sum(abs(a - b) for a, b in zip(first, second)) / len(first) <= tolerance
    except Exception:  # noqa: BLE001 - a comparison failure is "unknown", not a changed screen
        return None


def classify_failure(error, facts):
    """Return (kind, cause) from evidence the runner already recorded.

    kind is harness-suspect, product-suspect, or unknown.
    """
    facts = facts or {}
    if facts.get('start_expected') is not None:
        return ('harness-suspect', f'start-state:{facts["start_expected"]}!={facts.get("start_actual")}')
    if facts.get('recovery'):
        return ('harness-suspect', 'recovery-failed:' + str(facts.get('recovery_target') or 'row'))
    if facts.get('missing_evidence'):
        return ('harness-suspect', 'missing-evidence:' + str(facts.get('gate') or 'row'))
    role = facts.get('role')
    if facts.get('screen_reached') and role in ('effect', 'card', 'trace'):
        return ('product-suspect', f'{role}:{facts.get("gate")}')
    if facts.get('timed_out') and facts.get('screen_unchanged') is True:
        return ('harness-suspect', 'gate-timeout-unchanged:' + str(facts.get('gate')))
    if facts.get('timed_out'):
        return ('unknown', 'gate-timeout:' + str(facts.get('gate')))
    return ('unknown', 'unclassified:' + type(error).__name__)


def gate_inputs_missing(runner, on, gate):
    """True when a gate's reference, source file, or hook module is not on disk."""
    kind = gate.get('kind')
    try:
        if kind in ('log', 'json'):
            return not runner.source(on, gate['source']).is_file()
        if kind == 'image' and not gate.get('acquire'):
            return not (runner.base / gate['reference']).is_file()
        if kind == 'hook':
            return not (runner.base / gate['module']).is_file()
    except Exception:  # noqa: BLE001 - a source that cannot be named is missing evidence
        return True
    return False


def timeout_facts(runner, on, name, gate, missing, tolerance):
    unchanged = frames_unchanged(getattr(runner, '_gate_baseline', None), getattr(runner, '_gate_latest', None), tolerance)
    return {'gate': name, 'on': on, 'role': assertion_role(gate), 'timed_out': True,
            'screen_unchanged': unchanged, 'missing_evidence': bool(missing),
            'screen_reached': bool(getattr(runner, 'screen_reached', False))}


def start_gate_name(plan, row):
    if row.get('start_gate'):
        return row['start_gate']
    if row.get('start') in (plan.get('gates') or {}):
        return row['start']
    return None


def recover_steps(plan, row):
    if isinstance(row, dict) and 'recover' in row:
        return row['recover']
    if isinstance(plan.get('recover'), list):
        return plan['recover']
    return None


def bind_step(step, on):
    """A shared recover list names no instance. Bind it to the row's instance."""
    if isinstance(step, dict):
        bound = dict(step)
        bound.setdefault('on', on)
        return bound
    return step


def self_test_boot(runner):
    """Check each row's declared start gate, then each declared screen class's recovery back to it.

    Screen classes (menu, pop-up, prompt, crop, overlay, or any other name) and the
    route that reaches them are plan data. Nothing here invents a route.
    """
    plan = runner.plan
    classes = plan.get('screen_classes')
    if not isinstance(classes, list) or not classes:
        return {'ok': False, 'recoveries': [], 'error': 'no screen classes declared',
                'lines': ['FAIL self-test: no screen classes declared']}
    results = []
    for row in plan['rows']:
        on = row.get('on', runner.default)
        runner.current_row = row
        runner.current_on = on
        runner.row_dir = runner.out / 'self-test' / str(row['id'])
        runner.row_dir.mkdir(parents=True, exist_ok=True)
        gate_name = start_gate_name(plan, row)
        if not gate_name:
            results.append({'row': row['id'], 'check': 'start', 'ok': False, 'detail': 'no start gate declared'})
        else:
            try:
                runner.gate(on, gate_name)
                results.append({'row': row['id'], 'check': 'start', 'class': row.get('start'), 'ok': True,
                                'detail': f'start gate {gate_name} passed'})
            except Exception as error:  # noqa: BLE001 - one probe's failure is a report row
                results.append({'row': row['id'], 'check': 'start', 'ok': False, 'detail': str(error)})
        steps = recover_steps(plan, row)
        if steps is None:
            results.append({'row': row['id'], 'check': 'recover', 'ok': False, 'detail': 'no recover list declared'})
            continue
        if not gate_name:
            continue
        for screen in classes:
            class_name = str(screen.get('class', ''))
            try:
                for step in screen.get('reach') or []:
                    runner.execute_step(bind_step(step, on))
                if screen.get('gate'):
                    runner.gate(on, screen['gate'])
            except Exception as error:  # noqa: BLE001
                results.append({'row': row['id'], 'check': 'recovery', 'class': class_name, 'ok': False,
                                'detail': f'could not reach {class_name}: {error}'})
                continue
            try:
                for step in steps:
                    runner.execute_step(bind_step(step, on))
                runner.gate(on, gate_name)
                results.append({'row': row['id'], 'check': 'recovery', 'class': class_name, 'ok': True,
                                'detail': f'recovered from {class_name} to {gate_name}'})
            except Exception as error:  # noqa: BLE001
                results.append({'row': row['id'], 'check': 'recovery', 'class': class_name, 'ok': False,
                                'detail': f'recovery from {class_name} failed: {error}'})
    lines = [f"{'OK' if item['ok'] else 'FAIL'} self-test {item['row']} {item.get('class') or item['check']}: "
             f"{item.get('detail', '')}" for item in results]
    return {'ok': bool(results) and all(item['ok'] for item in results), 'recoveries': results, 'lines': lines}


def capture_passing_row(runner, row, dest):
    """Save the evidence a passing row just produced, so its gates can be replayed without Eden."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    on = row.get('on', runner.default)
    sources = {}
    for step in row.get('steps') or []:
        if not isinstance(step, dict) or 'gate' not in step:
            continue
        gate = runner.plan.get('gates', {}).get(step['gate']) or {}
        if gate.get('kind') not in ('log', 'json') or not gate.get('source'):
            continue
        side = step.get('on', on)
        path = runner.source(side, gate['source'])
        if not path.is_file():
            continue
        name = f'{side}-{gate["source"]}{path.suffix or ".txt"}'
        (dest / name).write_bytes(path.read_bytes())
        sources.setdefault(side, {})[gate['source']] = name
    shot = runner.shot(on, 'fixture-screen')
    (dest / 'screen.png').write_bytes(Path(shot).read_bytes())
    instances = {}
    for key, inst in runner.instances.items():
        spec_sources = dict(sources.get(key, {}))
        for name, value in inst.items():
            if name in ('socket', 'screenshot_target') or name in spec_sources:
                continue
            if isinstance(value, str) and Path(value).is_file():
                filename = f'{key}-{name}{Path(value).suffix or ".txt"}'
                (dest / filename).write_bytes(Path(value).read_bytes())
                spec_sources[name] = filename
        instances[key] = {'shot': 'screen.png', 'sources': spec_sources}
    fixture = {'schema': 1, 'row': row['id'], 'instances': instances}
    path = dest / 'fixture.json'
    path.write_text(json.dumps(fixture, indent=2) + '\n')
    return path


class FixtureClient:
    """Serves one captured screen and recorded replies. No socket and no emulator."""

    def __init__(self, root, spec):
        self.root = Path(root)
        self.spec = spec or {}

    def request(self, method, params):
        params = params or {}
        if method == 'ui.screenshot':
            source = self.root / self.spec.get('shot', 'screen.png')
            Path(params['path']).write_bytes(source.read_bytes())
            return {'ok': True, 'result': {}}
        if method == 'ui.tree':
            return {'ok': True, 'result': self.spec.get('ui') or [
                {'id': 'screen', 'class': 'GRenderWindow', 'visible': True}]}
        if method == 'status':
            return {'ok': True, 'result': self.spec.get('status') or {
                'running': True, 'loading_complete': True, 'last_frame_age_ms': 0}}
        if method in ('trace.counters', 'trace.read'):
            return {'ok': True, 'result': (self.spec.get('trace_calls') or {}).get(method, {})}
        if method == 'input.reset':
            return {'ok': True, 'result': {}}
        return {'ok': True, 'result': {}}


def replay_fixture(manifest, fixture_dir, out, runner_cls):
    """Run the row's gate steps and steps.py hooks against a captured fixture."""
    fixture_dir = Path(fixture_dir)
    fixture = json.loads((fixture_dir / 'fixture.json').read_text())
    instances = {}

    def factory(socket):
        key = str(socket).split(':', 1)[1]
        return FixtureClient(fixture_dir, fixture['instances'][key])

    for key, spec in fixture.get('instances', {}).items():
        inst = {'socket': f'fixture:{key}', 'screenshot_target': 'screen'}
        for name, rel in (spec.get('sources') or {}).items():
            inst[name] = str(fixture_dir / rel)
        instances[key] = inst
    if not instances:
        return {'ok': False, 'checks': [], 'error': 'fixture has no instances',
                'lines': ['FAIL replay: fixture has no instances']}
    tool = runner_cls(Path(manifest), instances, Path(out), 15, client_factory=factory)
    report = tool.replay_checks(fixture['row'])
    report['fixture'] = str(fixture_dir / 'fixture.json')
    report['lines'] = [f"{'OK' if item['ok'] else 'FAIL'} replay {item.get('name')}: {item.get('error') or 'passed'}"
                       for item in report.get('checks', [])]
    if not report.get('checks'):
        report['lines'] = ['FAIL replay: row has no gate or step-hook checks']
    return report


def load_callable(path, function):
    module_path = Path(path)
    spec = importlib.util.spec_from_file_location('harness_hook_' + module_path.stem, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, function)
