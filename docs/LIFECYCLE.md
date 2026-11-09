# Eden lifecycle: claim through release

Use [TEST-PLAN](TEST-PLAN.md) and [TEST-RUN](MACROS.md) for product rows;
`tools/eden-test/eden_test.py` is the run entry. The lifecycle owns isolation,
claims, bounded launch and cleanup. Up/Down/status do not send project input,
play macros or implement rows; an explicitly declared watchdog park is the
limited [WATCHDOG](WATCHDOG.md) exception and grants no new input authority.

```text
python3 -B -I tools/eden-lifecycle/lifecycle.py preflight config.json
python3 -B -I tools/eden-lifecycle/lifecycle.py up config.json --execute
python3 -B -I tools/eden-lifecycle/lifecycle.py status config.json
python3 -B -I tools/eden-lifecycle/lifecycle.py down config.json --execute
```

Use the host's required PowerShell 7 and local memory/time guard. Put Down
in the caller's own cleanup/finally even when the runner fails. Up returns
after load, before rows; the guest's hard runtime cap remains active.
Controller death leaves a durable journal and claim for a fresh Down.
Never assume expiry, silently restart or reuse a journal/work root.

All config/generated instance paths are **executor-host paths**. Set exact
kernel `hostname`, not an SSH alias; wrong-host Up/Down/status refuse before
profile/claim operations. For Yoga append `--ssh memento-claw-yoga-ubuntu`:
the same source/config travels on stdin through its PowerShell 7 login shell,
without script staging. Run the shared runner there too; no implicit file or
socket forwarding exists. Surface/XPS can use `ledger_ssh` for Yoga's
authoritative ledger. Windows transport is specified in [WINDOWS](WINDOWS.md).

Configure owner/label/host/hostname, disjoint fresh work/state roots, ledger,
runner and rows pins, runtime/caps, and instance keys with distinct source,
display, game, app and approved plugin/file pins. Use actual pin objects,
not illustrative strings; canonical path/input rules are in [PROFILES](PROFILES.md).
The shared runner's actual `dry_run()` must return nonempty READY or
READY_TO_RECORD rows; retain `dry-run.json`. Missing media in READY_TO_RECORD
is acquired/recorded by that shared runner; it is not a lifecycle acceptance.
An explicitly assigned same-host pair sets `pair_authorized:true` and uses
distinct inputs/profiles/displays; [HOSTS](HOSTS.md) checks repeat per launch.
`defer_load` leaves the frontend ready for boot tracing; the runner owns load.

Preflight is read-only: pins/source/game, optional update-version and
playbook-commit declarations, declared symbol ELF existence, resources,
compilers, ledger and a probeable declared display. Up repeats pin/game/
update/commit/display checks before state, then admission and per-launch
resource checks. Expected version declarations do not inspect all game bytes.
A stale socket or trace log is reported untouched by preflight; Up removes
the socket/moves the trace log aside with a `stale-runtime` receipt. Readiness
means a successful bounded `status` reply, never socket-file presence.
Preflight writes no claim/staging/ledger; human lines use stderr, one JSON
receipt stdout, and `ok:false` exits 1.

The authoritative ledger uses locked append+fsync, preserving raw bytes,
with no normalization, automatic release or default expiry. Unknown records
block. Exact-token readback reconciles lost claim/release ACKs; an outage
still attempts owned stop/restore, but retains the claim until history is
checked. Do not edit SSH credentials/config or reconcile foreign claims.

Optional `stale_claim_hours` has **no default**; omission keeps strict blocking.
Only a finite positive number is valid (not zero, negative, null, boolean or
nonnumeric). Skip only an open **foreign-owner** claim with explicit matching
host scope, parseable timezone-aware timestamp, age **greater than** the
threshold, and executor evidence of no foreign Eden/compiler. Unknown or
conflicting scopes, missing owner, malformed/naive time, future/young claims,
this owner's own claims or absent process evidence still block. Resource
gates remain. Carry predicate/results over ledger SSH; ledger-host process
state does not substitute for the executor's. Each skip retains original
`line`, `age_hours`, UTC `checked_at` in `actions.jsonl`
(`stale_claim_skipped`), Up receipt and journal. No foreign release is written;
the original claim remains in strict history/readback.

Snapshot the full private clone before staging. Persist zero audio volume,
then `settings.set(core.volume,0)`, save and read back **before** `game.load`.
Apply/read back XPS settings at that point too. Linux guests use unique bounded
8 GiB/swap0/Tasks256 systemd units, full console/control logs and no inherited
LAN/runtime/seed environment. Socket/load/stop/job waits are bounded and
journalled; accepted load/stop jobs require successful **terminal** responses,
not ACKs. Runtime defaults to 5400 seconds and permits up to 10800.

Down attempts exact owned forced cleanup even after capped `game.stop`
failure. Check unit/cgroup and PID birth identity; never `pkill -f` or adopt
foreign processes. [LINUX-DOWN](LINUX-DOWN.md) requires main before FUSE;
[WINDOWS](WINDOWS.md) requires exact session Job/task/PID cleanup. Only after
all owned PIDs are absent: preserve post-run evidence, restore full snapshot,
perform **separate** complete verify, then release only the exact owned claim.
Leftovers, restore/verify failure or unreleased claim mean cleanup failure and
retain ownership for recovery. A verified retry can finish without restoring
twice. After clean release, Down exports changed files and prunes the copies;
the exact export/prune/retention and retry contract is in [PROFILES](PROFILES.md).
Inspect shutdown health separately: successful forced cleanup does
not waive SIGBUS/panic/fatal console tails or prove graceful `game.stop`.

Keep `lifecycle.json`, `actions.jsonl`, dry-run/snapshot/restore/verify receipts,
run console/control logs and `instances.json` mappings/source aliases. Keep
`run.json`/`memory.peak` and [evidence](WORKFLOW.md) under the run label.
Current mechanics: [lifecycle.py](../tools/eden-lifecycle/lifecycle.py);
dated coverage/limits: [qualification history](../tools/eden-lifecycle/README.md).
