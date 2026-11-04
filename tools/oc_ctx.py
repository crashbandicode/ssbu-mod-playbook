"""Read-only: per OpenCode session, the token counts of its last few assistant requests (the real context in use).

Usage: oc_ctx.py <ses_id>...
Prints input / cache-read / output tokens of the newest assistant messages, so the actual prompt size per request
(input + cache read) can be compared with the model's context window.
"""
import json
import sqlite3
import sys

con = sqlite3.connect('file:/home/intpa/.local/share/opencode/opencode.db?mode=ro', uri=True)
for sid in sys.argv[1:]:
    rows = con.execute('select time_created, data from session_message where session_id=? order by time_created desc limit 40',
                       (sid,)).fetchall()
    shown = 0
    for t, d in rows:
        j = json.loads(d)
        tok = j.get('tokens') or (j.get('usage') or {})
        if not tok:
            continue
        cache = tok.get('cache') or {}
        prompt = (tok.get('input') or 0) + (cache.get('read') or 0) + (cache.get('write') or 0)
        print(sid[:14], t, 'prompt≈', prompt, 'input', tok.get('input'), 'cache', cache, 'output', tok.get('output'),
              'summary' if j.get('summary') else '')
        shown += 1
        if shown >= 3:
            break
    if not shown:
        print(sid[:14], 'no token data; keys of newest:', list(json.loads(rows[0][1]).keys()) if rows else [])
