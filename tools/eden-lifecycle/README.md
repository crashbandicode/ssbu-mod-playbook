# Eden lifecycle

This tool owns fresh private profiles, atomic claims, bounded guest processes,
restoration, separate verification and exact release. Product rows use
[`eden_test.py`](../eden-test/eden_test.py) and the shared runner.
Clean Down exports changed files before pruning copies; [PROFILES](../../docs/PROFILES.md)
defines exclusions/receipts/retries. `down --evidence-dir DIR` selects exports;
`--keep-state` retains copies only when explicitly asked.

```text
python3 -B -I lifecycle.py preflight config.json
python3 -B -I lifecycle.py up config.json --execute
python3 -B -I lifecycle.py status config.json
python3 -B -I lifecycle.py watchdog config.json --execute
python3 -B -I lifecycle.py down config.json --execute
/usr/bin/systemd-run --user --scope -p MemoryMax=256M -p MemorySwapMax=0 /usr/bin/timeout 60 /usr/bin/python3 -B -I test_lifecycle.py
```

Run through the required host PowerShell 7. The offline suite uses the normal
**256 MiB / 60 s** guard above; passing it authorizes no live run.

Read [LIFECYCLE](../../docs/LIFECYCLE.md), [HOSTS](../../docs/HOSTS.md),
[PROFILES](../../docs/PROFILES.md), and the applicable [Linux Down](../../docs/LINUX-DOWN.md)
or [Windows](../../docs/WINDOWS.md) policy. [WATCHDOG](../../docs/WATCHDOG.md)
describes its declared park exception. [Qualification history](../../docs/history/LIFECYCLE-QUALIFICATION-2026-11.md)
preserves dated bases, tests, receipts, incidents and unqualified paths;
offline test success is neither a live grant nor native acceptance.
