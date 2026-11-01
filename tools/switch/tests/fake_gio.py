#!/usr/bin/python3
"""Test double for gio. It runs the real /usr/bin/gio (so the script under test sees
gio's real output and exit codes on a `file://` tree), logs every call, and applies the
faults listed in the JSON plan named by $FAKE_GIO_PLAN:

  {"log": "<file>", "rules": [{"dest_endswith": "exefs/subsdk9", "nth": 1, "every": false,
                                "fault": "flip_byte" | "truncate" | "fail_partial" | "side_modify",
                                "file": "<absolute path, side_modify only>"}]}

A rule matches a `gio copy` whose destination (card path or local path) ends with
`dest_endswith`; `nth` counts the matching calls (1 = the first) and `every` keeps
applying it to the later ones. The fault happens after the copy, so the script sees a
write that reported success and left other bytes: flip_byte changes one byte of the
destination, truncate halves it, fail_partial writes half and exits 1 (a transfer that
dropped), side_modify changes one byte of another file (another writer)."""
import json
import os
import subprocess
import sys
from urllib.parse import unquote

REAL = '/usr/bin/gio'


def local(text):
    return unquote(text[len('file://'):]) if text.startswith('file://') else text


def flip(path):
    data = bytearray(open(path, 'rb').read())
    data[len(data) // 2] ^= 0xFF
    open(path, 'wb').write(bytes(data))


def main():
    args = sys.argv[1:]
    plan_path = os.environ.get('FAKE_GIO_PLAN')
    plan = json.load(open(plan_path)) if plan_path else {'rules': []}
    if plan.get('log'):
        with open(plan['log'], 'a') as log:
            log.write(json.dumps(args) + '\n')
    counters_path = (plan_path or '') + '.counters'
    counters = json.load(open(counters_path)) if plan_path and os.path.exists(counters_path) else {}
    rule = None
    if args and args[0] == 'copy':
        destination = local(args[-1])
        for index, candidate in enumerate(plan.get('rules', [])):
            if destination.endswith(candidate['dest_endswith']):
                counters[str(index)] = counters.get(str(index), 0) + 1
                nth = candidate.get('nth', 1)
                if counters[str(index)] == nth or (candidate.get('every') and counters[str(index)] > nth):
                    rule = candidate
                    break
        if plan_path:
            json.dump(counters, open(counters_path, 'w'))
    if rule and rule['fault'] == 'fail_partial':
        data = open(local(args[-2]), 'rb').read()
        open(local(args[-1]), 'wb').write(data[:len(data) // 2])
        sys.stderr.write('gio: transfer dropped (fake)\n')
        return 1
    done = subprocess.run([REAL, *args], stdin=subprocess.DEVNULL)
    if done.returncode == 0 and rule:
        destination = local(args[-1])
        if rule['fault'] == 'flip_byte':
            flip(destination)
        elif rule['fault'] == 'truncate':
            data = open(destination, 'rb').read()
            open(destination, 'wb').write(data[:len(data) // 2])
        elif rule['fault'] == 'side_modify':
            flip(rule['file'])
    return done.returncode


sys.exit(main())
