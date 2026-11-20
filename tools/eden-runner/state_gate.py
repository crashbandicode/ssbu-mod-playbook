"""Plugin state blocks from the hardware bridge (ssbu-hw-bridge docs/PLUGIN-STATE.md v2.1), for the runner's `state` gate.

A block is framed:
    [Bridge] state begin name=<provider> schema=<name>/<version> rev=<n> boot=<hex> lines=<N> filtered=<0|1>
    [Bridge] state.<i> <key>=<value>                 (or, cut under the line bound: state.<i>.part=<k>/<n> <chunk>)
    [Bridge] state end name=<provider> rev=<n> crc=<crc32 hex of the payload lines joined by \\n>
each line with the bridge's ` frame= wall=` suffix (and ` ev=` with `events on`). Only a complete block counts: a missing
begin or end, a wrong line count, a missing part, a CRC mismatch or a rev/boot that changes inside the block (a reply
torn across a reconnect) is not a block.

Gate (`{"kind": "state", "provider": "tr.menu", "where": {...}, "cap": 5}`), after a `{"state_baseline": "tr.menu"}` step
taken before the input: passes on the first complete block newer than the baseline (same boot, provider and schema)
in which every `where` key is present and equal (values compare as the bridge sends them, strings unquoted).
`"transition": "focus"` also needs `focus` to differ from the baseline's; `"ack": 1` needs `ack` exactly one past the
baseline's with `ack.result` "handled". A bare revision change passes neither.
"""
import re
import zlib

SUFFIX = re.compile(r'(?: ev=\d+)? frame=\d+ wall=\d+$')
BEGIN = re.compile(r'\[Bridge\] state begin name=(\S+) schema=(\S+) rev=(\d+) boot=(\S+) lines=(\d+) filtered=([01])')
LINE = re.compile(r'\[Bridge\] state\.(\d+)(?:\.part=(\d+)/(\d+))? (.*)$')
END = re.compile(r'\[Bridge\] state end name=(\S+) rev=(\d+) crc=([0-9a-f]{8})')
# A connection boundary: a block open across it is torn, whatever follows.
BOUNDARY = re.compile(r'\[Bridge\] hello |\[Adapter\] (bridge connected|reconnect|connection idle)')


def unquote(value):
    """A quoted string value (`"…"` with \\" \\\\ \\n escapes), or the value as sent."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        out, i, s = [], 0, value[1:-1]
        while i < len(s):
            if s[i] == '\\' and i + 1 < len(s):
                out.append({'n': '\n', '"': '"', '\\': '\\'}.get(s[i + 1], s[i + 1]))
                i += 2
            else:
                out.append(s[i])
                i += 1
        return ''.join(out)
    return value


def blocks(text, provider):
    """Every complete, valid block of `provider` in `text` after its last connection boundary, oldest first: dicts with
    rev, boot, schema and values. A hello or reconnect drops everything before it, complete blocks included, so no
    pre-reconnect result can satisfy a gate (review-bridge-astra 23:32Z)."""
    out, cur = [], None
    for raw in text.splitlines():
        line = SUFFIX.sub('', raw)
        if BOUNDARY.search(line):
            out, cur = [], None
            continue
        m = BEGIN.search(line)
        if m:
            cur = None
            if m[1] == provider:
                cur = {'name': m[1], 'schema': m[2], 'rev': int(m[3]), 'boot': m[4], 'count': int(m[5]),
                       'filtered': m[6] == '1', 'lines': {}, 'parts': {}}
            continue
        if cur is None:
            continue
        m = LINE.search(line)
        if m:
            n = int(m[1])
            if m[2]:
                cur['parts'].setdefault(n, {})[int(m[2])] = (int(m[3]), m[4])
            else:
                cur['lines'][n] = m[4]
            continue
        m = END.search(line)
        if m:
            block, cur = cur, None
            if m[1] != block['name'] or int(m[2]) != block['rev']:
                continue  # torn: the end belongs to another block
            for n, parts in block['parts'].items():
                total = next(iter(parts.values()))[0]
                if sorted(parts) != list(range(1, total + 1)) or any(t != total for t, _ in parts.values()):
                    break
                block['lines'][n] = ''.join(parts[k][1] for k in range(1, total + 1))
            else:
                payload = [block['lines'].get(n) for n in range(block['count'])]
                if None in payload or len(block['lines']) != block['count']:
                    continue
                if f'{zlib.crc32(chr(10).join(payload).encode()):08x}' != m[3]:
                    continue
                values = {}
                for item in payload:
                    key, _, value = item.partition('=')
                    values[key] = unquote(value)
                out.append({'name': block['name'], 'schema': block['schema'], 'rev': block['rev'], 'boot': block['boot'],
                            'filtered': block['filtered'], 'values': values})
    return out


def latest(text, provider):
    found = blocks(text, provider)
    return found[-1] if found else None


def satisfies(block, where, baseline=None, transition=None, ack=None, input_gate=None):
    """(passes, why) for `block`.

    A plain assertion (no transition, no ack) checks `where` on a complete block. An input gate (`transition`
    "focus" or `ack`, or `input_gate=True`) needs this step's pre-input `baseline` and a newer block from the same boot
    and schema, and either the expected focus transition (focus present and different from the baseline's) or exactly
    one handled ack; a where-only match or a bare revision change (a notice) never passes it."""
    input_gate = bool(input_gate or transition or ack is not None)
    if input_gate:
        if baseline is None:
            return False, 'an input gate needs this step\'s pre-input baseline'
        if transition is None and ack is None:
            return False, 'an input gate needs "transition": "focus" or "ack": 1'
    if baseline is not None:
        if block['boot'] != baseline['boot']:
            return False, f'boot {block["boot"]} != baseline {baseline["boot"]}'
        if block['schema'] != baseline['schema']:
            return False, f'schema {block["schema"]} != baseline {baseline["schema"]}'
        if block['rev'] <= baseline['rev']:
            return False, f'rev {block["rev"]} not newer than baseline {baseline["rev"]}'
    values = block['values']
    for key, want in where.items():
        if key not in values:
            return False, f'{key} missing'
        if values[key] != str(want):
            return False, f'{key}={values[key]!r} wants {want!r}'
    if transition == 'focus':
        now, was = values.get('focus'), baseline['values'].get('focus')
        if now is None:
            return False, 'focus missing from the new block'
        if now == was:
            return False, 'focus did not change'
    if ack is not None:
        try:
            advanced = int(values.get('ack', '')) - int(baseline['values'].get('ack', ''))
        except ValueError:
            return False, 'ack missing'
        if advanced != int(ack) or values.get('ack.result') != 'handled':
            return False, f'ack advanced {advanced} result {values.get("ack.result")!r}'
    return True, 'ok'
