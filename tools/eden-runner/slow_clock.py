"""Which tests depend on real wall time? Run one test module twice in this process: once as it is, once with a
slow clock, and report the tests that pass the first time and not the second.

    /usr/bin/python3 -I -B slow_clock.py <test_module.py> [--delay 0.3] [--json out.json]

The slow clock adds `delay` seconds to every read of `time.monotonic`, `time.time` and `time.perf_counter` made
through the `time` module (the way the runner's code reads them): the n-th read returns the real value plus
n * delay. That is a loaded host as the code under test sees it: time passes between two reads. Nothing sleeps, so
the run is not slower. A clock a test injects itself is not touched, so a converted test is unaffected.
A test counts when anything of it fails slowed: the method, one of its subtests, or a fixture of its class or
module.
A clock bound as a default argument (`clock=time.monotonic`) in a module of the tools folders is slowed too.
Not covered: `from time import monotonic` (none in the runner at 1672ff52), a clock stored in a module global or
an instance attribute before the slow run starts, and real sleeps.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import time
import unittest

sys.dont_write_bytecode = True
NAMES = ('monotonic', 'time', 'perf_counter')


def load(path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


RANK = {'ok': 0, 'skip': 1, 'fail': 2, 'error': 3}


def run(module):
    """One run of the module's tests, per test method.

    Returns {test id: 'ok' | 'fail' | 'error' | 'skip'}, the last line of each test's first failure, the failed
    subcases per test, and the class and module fixtures that failed. unittest reports a subtest's failure and a
    fixture's failure under ids of their own; here a subtest counts for its test method, and a fixture for every
    test of its class or module. An id that fits no test is kept as an entry of its own."""
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    result = unittest.TestResult()
    outcome, why, cases, fixtures = {}, {}, {}, []

    def ids(node):
        for item in node:
            if isinstance(item, unittest.TestSuite):
                yield from ids(item)
            else:
                yield item.id()

    def mark(name, kind, line):
        if RANK[kind] > RANK[outcome.get(name, 'ok')]:
            outcome[name] = kind
        outcome.setdefault(name, kind)
        why.setdefault(name, line)

    for name in ids(suite):
        outcome[name] = 'ok'
    known = set(outcome)
    suite.run(result)
    for kind, entries in (('fail', result.failures), ('error', result.errors), ('skip', result.skipped)):
        for test, text in entries:
            lines = [line for line in str(text).strip().splitlines() if line.strip()]
            line = lines[-1][:240] if lines else ''
            parent = getattr(test, 'test_case', None)
            if parent is not None:
                if kind == 'skip':
                    continue
                mark(parent.id(), kind, line)
                describe = getattr(test, '_subDescription', None)
                cases.setdefault(parent.id(), []).append(f'{describe() if describe else test}: {line}')
            elif test.id() in known:
                mark(test.id(), kind, line)
            else:
                # A class or module fixture: 'setUpClass (module.Class)', 'setUpModule (module)'.
                description = test.id()
                owner = description[description.find('(') + 1:description.rfind(')')] if '(' in description else ''
                owned = [name for name in known if owner and name.startswith(owner + '.')]
                fixtures.append(description)
                for name in owned or [description]:
                    mark(name, kind, f'{description}: {line}')
    return outcome, why, cases, fixtures


def bind_defaults(real, slowed_clock, folder):
    """A clock bound as a default argument (`def run(..., clock=time.monotonic)`) was read from `time` when the
    function was defined, so replacing `time.monotonic` does not reach it. Swap those defaults for the slow clock
    in every function of every module loaded from the tools folders. Returns the undo calls and the names."""
    swap = {id(real[name]): slowed_clock[name] for name in NAMES}
    tools = folder.parent
    undo, bound = [], []

    def functions(namespace, seen):
        for value in list(vars(namespace).values()):
            target = getattr(value, '__func__', value)
            if isinstance(target, type(bind_defaults)):
                yield target
            elif isinstance(value, type) and id(value) not in seen:
                seen.add(id(value))
                yield from functions(value, seen)

    for module in list(sys.modules.values()):
        path = getattr(module, '__file__', None)
        if not path or tools not in Path(path).resolve().parents:
            continue
        for function in functions(module, set()):
            defaults = function.__defaults__
            if defaults and any(id(value) in swap for value in defaults):
                function.__defaults__ = tuple(swap.get(id(value), value) for value in defaults)
                undo.append(lambda function=function, defaults=defaults: setattr(function, '__defaults__', defaults))
                bound.append(f'{module.__name__}.{function.__qualname__}')
            keywords = function.__kwdefaults__
            if keywords and any(id(value) in swap for value in keywords.values()):
                function.__kwdefaults__ = {key: swap.get(id(value), value) for key, value in keywords.items()}
                undo.append(lambda function=function, keywords=keywords: setattr(function, '__kwdefaults__', keywords))
                bound.append(f'{module.__name__}.{function.__qualname__}')
    return undo, sorted(set(bound))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('module', type=Path)
    parser.add_argument('--delay', type=float, default=0.3)
    parser.add_argument('--json', type=Path)
    args = parser.parse_args()
    module = load(args.module.resolve())
    plain, plain_why, plain_cases, plain_fixtures = run(module)
    real = {name: getattr(time, name) for name in NAMES}
    reads = {name: 0 for name in NAMES}

    def slow(name):
        def read():
            reads[name] += 1
            return real[name]() + sum(reads.values()) * args.delay
        return read

    slowed_clock = {name: slow(name) for name in NAMES}
    for name in NAMES:
        setattr(time, name, slowed_clock[name])
    undo, bound = bind_defaults(real, slowed_clock, args.module.resolve().parent)
    try:
        slowed, slow_why, slow_cases, slow_fixtures = run(module)
    finally:
        for name in NAMES:
            setattr(time, name, real[name])
        for restore in undo:
            restore()
    depends = sorted(name for name in {*plain, *slowed}
                     if plain.get(name, 'ok') == 'ok' and slowed.get(name, 'ok') != 'ok')
    broken = sorted(name for name in plain if plain[name] in ('fail', 'error'))
    report = {'module': str(args.module), 'delay': args.delay, 'tests': len(plain),
              'plain': {kind: sum(1 for value in plain.values() if value == kind) for kind in ('ok', 'fail', 'error', 'skip')},
              'clock_reads_in_slow_run': reads, 'default_bound_clocks_slowed': bound,
              'depends_on_wall_time': [{'test': name, 'slow': slowed.get(name), 'why': slow_why.get(name, ''),
                                        'cases': slow_cases.get(name, [])} for name in depends],
              'failing_without_delay': [{'test': name, 'why': plain_why.get(name, ''),
                                         'cases': plain_cases.get(name, [])} for name in broken],
              'fixture_failures_slowed': slow_fixtures, 'fixture_failures_plain': plain_fixtures}
    if args.json:
        args.json.write_text(json.dumps(report, indent=1) + '\n')
    print(f'{args.module.name}: {len(plain)} tests, plain {report["plain"]}, slow-clock reads {sum(reads.values())}, '
          f'depend on wall time: {len(depends)}')
    for entry in report['depends_on_wall_time']:
        subcases = f' [{len(entry["cases"])} subcases]' if entry['cases'] else ''
        print(f'  WALL  {entry["test"].split(".", 1)[-1]}{subcases}: {entry["why"]}')
    for entry in report['failing_without_delay']:
        print(f'  PLAIN-FAIL  {entry["test"].split(".", 1)[-1]}: {entry["why"]}')
    return int(bool(depends or broken))


if __name__ == '__main__':
    sys.exit(main())
