# Guest facts and memory safety

For register/field values, branch execution or lifetime questions, trace
**a run already happening**. Prefer persistent `trace.*` on capable builds;
use one-shot otherwise. A diag rebuild or extra trace boot is only for facts
tracing cannot see (allocation accounting, data watchpoints or unreachable
plugin state); state the reason in the handoff. One driver owns the instance.
Retain trace/captured result plus exact application/plugin build identity.

For a hook-lock hang, [BUILD](BUILD.md) requires hook/thread reach review and
traced thread/core evidence for any MainThread-only exception. Hang diagnosis
can enable `core.use_gdbstub` before load and dump all threads; **GDB stub
and guest tracing cannot run together**. An unreached hook remains exposed.
Suspect a hook-lock hang when Eden stays alive but frames and all plugin logs
stop: look for a spinning core, a hook in Arbitration waiting on its lock, and
the plugin lock holder runnable but never scheduled. Dump every thread with
`/home/intpa/smash-training-lobby/artifacts/phase23/rc6-eden/rows-hang-qp/gdbdump.py`
(after verifying that historical helper's current path and identity).

| Build capability | Evidence/limit |
| --- | --- |
| Since `20261031-…` | One-shot `tracepoint.*`; on older builds this is the available form. |
| Since `20261103-063f1b80bb2c-persistent-tracing-…` | Persistent points and JSONL; old unmapped logs can be sampled, without exact fault PC. |
| Since `20261104-f916cf4b8c68` unmapped-stop | `unmapped_access:"log"` counts every guest unmapped data access, exact PC/module; first 16 then powers of two emit. Save total/emitted/suppressed receipt. |
| Direct fault API | Stops first access anywhere, including stock; use only to inspect registers for an access already found. Lifecycle config accepts **log/off only**. |

Check the **pinned build's** `docs/GUEST_TRACEPOINT.md`, `CLIControl.md` and
adjacent client; historical host `current` locations are not capability proof.
Persistent tracing keeps up to **256 points** armed. Lifecycle instance
`trace:{points:[...],symbols:[...]}` writes boot `trace.json` and exposes
`trace_jsonl` in instances; unresolved modules resolve when loaded. During a
run use `trace.arm/read/counters/list/disarm` via client, row `call` or
`ctx.call`. Read incrementally using the prior returned `next` as `after`.

Targets can be `main+0x15d9a70` or `libtraining_rooms.nro!hook_entry`.
Read the actual module base first. For symbols, load the **unstripped ELF**
with `symbols.load` or boot symbols; its build ID **must match the loaded
NRO**. `modules.list` reports loaded identities. RO-loaded unnamed NROs
remain `nro:<build-id>` until a matching ELF names them.

Points capture registers; up to four nested pointer reads with types
(`u32`, `c-string(32)`, bytes); conditions on registers/memory/thread;
paired `on_return` with x0; backtrace; or counters-only mode. Records include
frame/vsync, thread/core IDs and module/symbol locations. Use a capped log
gate over `trace_jsonl` when a traced event is the desired checkpoint.
Keep loss records/counters: only **1,024 in-memory records**, default hot-point
rate limit **1,000 records/s**, with loss disclosed. FPS overhead is not
measured; keep heavy tracing out of timing/performance rows. Data watchpoints,
allocation accounting and hitch sampling are not persistent-trace substitutes.

One-shot: arm an address with selected x0–x30/sp/pc/pstate registers and up
to **4096 bytes** via memory address or register+offset, then poll
`tracepoint.status` (idle/armed/captured/expired/cancelled) or cancel. Captures
occur just before that instruction, without patching guest code; execution
continues automatically. At most one armed request/one hit; deadline at
most **10 minutes wall time**, cancelled on unload. Requires AArch64 guest,
x86-64 host, GDB stub off, mapped **4-byte-aligned** address. The memory
buffer is what that CPU sees, not an atomic snapshot. Windows uses the same
JSON methods through its named-pipe client.

Before the run list address, registers/memory, in-game moment and what each
answer decides. The existing single driver arms requests in order. CLI shape:

```text
edenctl --socket <run>/control.sock tracepoint arm 0x<addr> --register x0 --register pc --memory-register x1 --memory-offset 16 --memory-size 64 --timeout-ms 600000
edenctl --socket <run>/control.sock tracepoint status
edenctl --socket <run>/control.sock tracepoint cancel
```

Always search full `console.log` for **panic, abort, crash, Unmapped Read and
Unmapped Write**. An unmapped address below **0x1000**, or any unmapped access
in the plugin's activity window, fails the lane: Eden can return zero and
continue where the Switch faults. Attribute other accesses by timestamps
against plugin logs before dismissing stock/emulator noise. A clean old log
means only nothing was logged; it does **not** prove absence of null reads.

Check the game's bytes/type for **every native return or game-memory value
used as a pointer** (`cset w0` returns bool, not pointer). On an existing run,
trace the load's actual address. Before unmapped-stop, null safety needs
that review plus hardware, not logs. On a capable build with log enabled,
`trace.unmapped_access` total **0** proves only the exercised paths; save the
receipt. Nonzero access policy above still applies; a fault mode is no
module-filtered safety shortcut.

Keep console, traces, `run.json` and `memory.peak` under the run label.
Compare timestamps as UTC using `[DateTimeOffset]::Parse(...)`; PowerShell
`[DateTime]'...Z'` converts to local time. Shutdown health and fatal tails
remain separate from successful restoration. Exact null-read incidents,
build chronology and historical host locations are in
[host history](../history/PLUGIN-AND-HOST-INCIDENTS-2026-10--11.md).
