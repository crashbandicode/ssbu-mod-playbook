# Completion checklist

Read [START](START.md), this index, and every concern your task touches before work.
Every **M** applies; every **C** needs a pass or an explicit N/A reason. History is on demand.
Each row is a summary; its linked page retains the full contract, exceptions and evidence.
Keep `Requirement | Tag | Result | Evidence` beside the handoff/evidence. A claim without evidence is not a pass.
Run `python3 -B -I tools/check/compliance.py <repo> --base <branch-start> --trace <design> --nro <shipped-file>` once per phase/candidate.
Complete every `agent` row as `pass`, `fail`, `N/A: <reason>` or `not authorized`; machine exit0 alone is insufficient.
Final reporting lists failed, skipped and unverified rows. Per-run `RESULTS.md` stays at most20 lines.

| ID | Tag | One-line rule summary and canonical home |
| --- | --- | --- |
| M01 | M | Build a [cdylib only](BUILD.md); no rlib; record shipped resident size. |
| M02 | M | Keep [exact release/toolchain/Skyline pins](BUILD.md): opt s, LTO,1 codegen unit, abort, Skyline=0.6.0, Rust1.98.1. |
| M03 | M | Keep [pure host-tested model logic and Switch-only runtime](BUILD.md). |
| M04 | M | Pass all five [separate Cargo gates](BUILD.md): test, warning-free clippy, no-default, Skyline release and diag. |
| M05 | M | [Hooks allocate no heap](BUILD.md); use counting allocator and install-line `heap_allocations=0`. |
| M06 | M | Use [fixed buffers, static writer stack and stack-built file paths](BUILD.md). |
| M07 | M | Fix [root causes](DESIGN.md); approve/date any workaround before shipping and assess one shared abstraction. |
| M08 | M | Every added [user-facing refusal](DESIGN.md) cites a nearby restriction ID with genuine recorded operator approval. |
| M09 | M | Before code, [trace every verbatim operator clause](DESIGN.md) to design and existing proving tests; prove parity in shared cases. |
| M10 | M | [Install nothing unless game13.0.5](HOOKS.md); log unsupported-version outcome. |
| M11 | M | Apply [64-byte guards to every hook/called/relied site](HOOKS.md); log and leave mismatches alone with sharing exceptions. |
| M12 | M | [Scan a dated card copy before site selection](HOOKS.md), including virtual slots; scan absence is not runtime proof. |
| M13 | M | [Call the game function or hook its most specific post-prologue instruction](HOOKS.md). |
| M14 | M | Keep [diag code, diag.txt and stand-in markers out of shipped NRO](BUILD.md). |
| M15 | M | Ship [install-line-only logging](BUILD.md), with the C13 logging-product exception and no per-frame diagnostics. |
| M16 | M | [Claim before Up; Down after failure too](LIFECYCLE.md); retain the claim until owned cleanup/restore/separate verify succeed. |
| M17 | M | Follow [host slots/10–9GiB admission](HOSTS.md); never source profilea, display61 or foreign runs. |
| M18 | M | Keep [XPS half-resolution precedence, thermald on and timed rows on Surface/Yoga](HOSTS.md). |
| M19 | M | Use [private profiles with immutable hard links and real mutable copies](PROFILES.md); change only your copy and verify storage. |
| M20 | M | Keep [per-copy targets/fresh source times/locked seeds/shared cargo home/disk checks/deletion manifest](BUILD.md). |
| M21 | M | [Gate every wait](MACROS.md); rounded-up fixed sleeps are only for states with neither log nor screenshot checks. |
| M22 | M | Prepare [shared runner/dry-run before Up](TEST-PLAN.md), meet15/30min deadlines, chain rows and preserve project media/hooks plus tool revision. |
| M23 | M | Declare [fixture/segment/hook/bounded-record setup](TEST-PLAN.md); act on40-input/15min-no-progress/runtime-margin pace limits. |
| M24 | M | [Abort a missing precondition and retry a new label](TEST-RUN.md), bounded by the batch try cap. |
| M25 | M | [Trace game facts on an existing run](TRACING.md); explain any diag rebuild/extra trace boot and retain actual trace/build evidence. |
| M26 | M | Use the [shortest ruleset](TEST-PLAN.md):1 stock,1:00, items/hazards/FS off; justify longer tests. |
| M27 | M | [Restore the run-owned profile and release only after separate verification](LIFECYCLE.md). |
| M28 | M | [Review panic/abort/crash/Unmapped and native pointer types](TRACING.md); retain console, run identity and memory evidence. |
| M29 | M | Bound [every local command by memory/time](START.md); put `${var}` use in a script file. |
| M30 | M | Keep [each run's outputs under its own label](EVIDENCE.md). |
| M31 | M | Write the [short README with whole-screen1280-wide <10s GIF](EVIDENCE.md), dated build caption, install/build/license/AI lines. |
| M32 | M | Keep [dated build/run/behavior/log evidence](EVIDENCE.md); row receipts and <=20-line RESULTS feed one phase compliance report. |
| M33 | M | [Group every reachable open row into as few boots as possible](TEST-PLAN.md); split only for stated build/host/profile/boot needs. |
| M34 | M | [Continue reachable rows after failures](TEST-RUN.md); recover/check start state, defer with cause, stop safely on unrecoverable state/crash. |
| M35 | M | Make [no unsupported claims](EVIDENCE.md); keep operator quotes out of shipped docs, with the working-design trace exception. |
| M36 | M | Keep [handoff branch/commit/evidence/live-state/next commands current](EVIDENCE.md), with no secrets. |
| M37 | M | [Only the user connects/disconnects or controls DBI/UMS](INSTALL.md); agents never mount/unmount/eject/remount/power off media. |
| M38 | M | Obtain [explicit action-specific orders](RELEASE.md) before push/tag/release/card writes; report lack of authorization. |
| M39 | M | Use [Forgejo and temporary UTF-8 commit-message files](START.md); no GitHub publication or unordered push. |
| M40 | M | Follow the external [model/effort/watcher/routing authority](START.md); do not duplicate its policy. |
| M41 | M | [Every agent/worker reads START+CHECKLIST+task pages](START.md), records playbook commit, makes its checklist and runs compliance before READY. |
| M42 | M | [Hooks never block on plugin-held locks](BUILD.md); prove lock reach with a seeded-failure test or a reason none applies; trace any MainThread-only exception. |
| M43 | M | Declare an [empty diag feature and keep working tree clean](BUILD.md); retain these code-only checker obligations. |
| M44 | M | Retain [exact build/shipped-NRO identity](EVIDENCE.md); explicit artifacts outrank newest-file inference and a tag is required only for release. |
| M45 | M | [Pin resolved build paths and matching trace symbols](PROFILES.md); disclose mixed builds and separate client identity from app pins. |
| M46 | M | [Snapshot before staging; mute/save/read back before load](LIFECYCLE.md); bounded terminal jobs, durable journal and ACK-loss-safe owned claims. |
| M47 | M | [Stop Linux main before FUSE/backing processes](LINUX-DOWN.md), using owned birth identity/pidfd and fail-closed claim retention. |
| M48 | M | Change [shared tools only in a separate worktree](RUNNER.md); generic extensions get tests; commit/reuse successful route inputs. |
| M49 | M | Keep [one driver per instance and same-frame macro chords](MACROS.md); review settled state before playback. |
| M50 | M | Respect [input lease/recording/hash/edit invariants](MACROS.md); incomplete recordings cannot become passing media. |
| M51 | M | Budget [rows+boot/navigation+route work under runtime minus margin](TEST-PLAN.md); order wedge/crash/teardown checks last. |
| M52 | M | [Review actual behavior and settled HUD state](RUNNER.md); fixture applied, bareTrue and clip saved do not prove behavior. |
| M53 | M | [Ban extracted game/mod assets and dumps from git](EVIDENCE.md); reviewed evidence screenshots and required reference crops are allowed. |
| M54 | M | Treat [cleanup success separately from graceful/native health](LINUX-DOWN.md); inspect unit/console fatal tails and qualification limits. |
| M55 | M | Stop only [owned displays/processes/paths](LIFECYCLE.md); ambiguous identity or failed restore/verify retains claim for recovery. |
| M56 | M | [Export files/manifests then prune owned run copies after clean Down](PROFILES.md); keep receipts/logs, use --keep-state only when asked, and require60GB before Up. |
| C01 | C | If sites may overlap, use [sharing/chaining](HOOK-SHARING.md), election before guard, both stand-ins and load orders; cite dated scan for N/A. |
| C02 | C | Online code obeys [read-only checked-RNG/native-order/simulated-state constraints](ONLINE.md), hook by hook. |
| C03 | C | Online code passes [same replay A/B](ONLINE.md), identical detector values and positions. |
| C04 | C | Online-only paths need [both-detector two-console proof](ONLINE.md) through later sessions, desync0 at matching frame. |
| C05 | C | Mods need [pinned ARCropolis and native-screen mod proof in that boot](PROFILES.md). |
| C06 | C | Log-derived numbers come from [committed host-tested scripts](EVIDENCE.md), with the regeneration command beside them. |
| C07 | C | Asset claims need [decoded expected/frame/stock comparison](EVIDENCE.md); rule out stock without committing extracted sources. |
| C08 | C | A changed recorded decision needs [document/item/date citation, disclosed reversal and updated original record](DESIGN.md). |
| C09 | C | Authorized release needs [minor bump, annotated tag, required assets and anonymous hash verification](RELEASE.md). |
| C10 | C | Release must have a [byte-identical rebuild of the tagged commit](RELEASE.md). |
| C11 | C | Authorized card work uses [fresh preflight, archives/rollback/readbacks and inspected non-repeated receipts](INSTALL.md); single writer, loader last, disconnect after final verify. |
| C12 | C | Multi-worker work needs [persistent sessions/routes, no context actions, done-checklists,<=60s polls, deadlines/stall alerts and direct acceptance](WORKERS.md). |
| C13 | C | A logging product may ship [its data files as well as install line](BUILD.md), without per-frame diagnostics. |
| C14 | C | Shared owners implement [ABI/election/early-late registration/hook-only guest service and per-site logs](HOOK-SHARING.md). |
| C15 | C | Foreign hooks obey [far/branch/stub/trampoline/called-relied/restore rules](HOOK-CHAIN.md); both plugin and stand-in counters keep running. |
| C16 | C | Windows work uses [fresh private/source roots, exact MSI/session/SID/helper pins and unchanged source verification](WINDOWS.md). |
| C17 | C | Windows guests need [launch-once token tasks and bounded8GiB-commit/256-process/kill-on-close Jobs](WINDOWS.md). |
| C18 | C | Windows firewall changes are [explicitly authorized token Block-only](WINDOWS.md); no Allow edits/prompt dismissal/implicit hosting authority. |
| C19 | C | Windows cleanup uses [named pipes and exact session Job/task/PID identities](WINDOWS.md);1312 retry once only before admission. |
| C20 | C | Windows qualification needs [new grant/queue/current identities/fresh-process evidence](WINDOWS.md); no-row/HOLD/offline proof grants no gameplay acceptance. |
| C21 | C | Optional [stale-claim admission](LIFECYCLE.md) preserves every fail-closed predicate/receipt; never releases a foreign claim. |
| C22 | C | Declared [watchdog state/probe/detector/park/sidecar](WATCHDOG.md) keeps40-line-window and idle-vs-demo distinctions; safe label alone does nothing. |
| C23 | C | Macro editing obeys [frame/sample/game/player/press-release/replay/hash limits](MACROS.md), without savestate assumptions. |
| C24 | C | GIF capture follows [distinct README/evidence envelopes, reasons/status/timing/frame review](GIFS.md); removals need comparable before/after clips. |
| C25 | C | Image/hook gates preserve [static/tolerance/scaling/ROI/evidence/REVIEW semantics](RUNNER.md); unchecked/bareTrue alone prove neither start state nor behavior. |
| C26 | C | Reused/new segments retain [sha256/start/end gates, receipts and reviewed harvest provenance](RUNNER.md). |
| C27 | C | Before shipping, use the [full card plugin set for one match through results/back](TEST-RUN.md); inspect every install line and console. |

Machine checks are useful approximations, not semantic proof. See each concern's evidence and tooling limits.
An approval JSON, old receipt, example pass count, or zero wrapper exit is not a new authorization or complete acceptance.
