# Eden hosts and admission

Read [START](START.md) and [CHECKLIST](CHECKLIST.md), then this page for host,
display, admission or scheduling work. [LIFECYCLE](EDEN.md) owns claims
and recovery; [WINDOWS](../tools/eden-lifecycle/README.md) adds the Ally requirements.

| Executor | Maximum Eden instances | Additional policy |
| --- | ---: | --- |
| Surface | 2 | Timed rows allowed. |
| Yoga | 2 | Timed rows allowed; authoritative ledger host. |
| XPS (`ubuntu-xps`) | 2 | Half resolution by default; thermald stays on. |
| Ally | 1 | Native Windows isolation and live grant required. |

MemAvailable is at least **10 GiB for the first instance, 9 GiB for a second**.
Count owned and foreign instances together. Check `df -h /` before every
build or launch: builds stop below 40 GB; pre-Up default is **60 GB**
(`disk_floor_bytes`, raised from 40 GB on 2026-11-09). Both
`memory_floor_bytes` and `disk_floor_bytes` accept positive integer overrides
(not boolean, zero, negative, fractional or string values), including lower
floors. Tool acceptance of an override does not authorize weakening the
required resource policy. Inspect config and admission receipts together.

The ledger admits a second claim only alongside a lifecycle-admitted claim
(`host=<h> lifecycle=<token> Eden lifecycle admitted`), each with its own
private profile. Manual or foreign claims block; unknown host/owner records
also block. Optional stale admission has the narrow predicate and receipts in
[LIFECYCLE](EDEN.md); it never releases another owner's claim.
No foreign Eden/compiler is adopted or killed. An authorized same-run pair
uses `pair_authorized:true`, distinct instance inputs, profiles and displays;
resource and process checks repeat before **each** launch.

The lifecycle refuses Up while a compiler runs. Builders wait while an Eden
worker holds its lock around Up and Down. Builds for a new head use a new
output folder so a running worker's pinned NRO path and hash stay valid.
Cargo/source cache isolation and finished-build cleanup are in [BUILD](BUILD.md).

Never use source profile `a`, display `:61`, or another agent's runs. An
optional pinned `display_helper` is the reviewed `display-rootful.ps1`; it
starts unique journalled weston/Xwayland units and Down stops only those
units/PIDs. Without it the declared display already exists, is not owned,
and is not stopped. Admission probes the display where the host supports it.

On XPS, before `game.load`, set/save/read back `core.resolution_setup=1`
(`Res1_2X`, 0.5x), alongside the mute. A run or instance `eden_settings`
object can override that key with a non-negative integer: instance wins over
run, then the XPS default. Surface, Yoga and Ally keep their settings unless
an explicit setting applies. Keep thermald on; timed rows use Surface/Yoga.
Thermal measurements and former build locations are dated
[host history](WORKFLOW.md), not current identity.

Queue unattended with bounded checks, a deadline, a memory/time guard, an
outer timeout longer than the deadline, and output to a file. Recheck after
an atomic claim race and wait again when another owner wins. Use current
admission rules rather than the old one-instance queue example. Every actual
attempt has a unique label and always attempts Down; precondition retries
and try caps are in [TEST-RUN](MACROS.md).

Evidence: resource/limit receipts, authoritative claim/release lines, host
and display identity, compiler-lock coordination, and any setting readbacks.
Implementation: [admission and host backends](../tools/eden-lifecycle/lifecycle.py).
