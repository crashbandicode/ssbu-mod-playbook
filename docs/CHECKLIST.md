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
| M24 | M | [Abort a missing precondition and retry a new label](MACROS.md), bounded by the batch try cap. |
| M25 | M | [Trace game facts on an existing run](EDEN.md); explain any diag rebuild/extra trace boot and retain actual trace/build evidence. |
| M26 | M | Use the [shortest ruleset](TEST-PLAN.md):1 stock,1:00, items/hazards/FS off; justify longer tests. |
| M27 | M | [Restore the run-owned profile and release only after separate verification](LIFECYCLE.md). |
| M28 | M | [Review panic/abort/crash/Unmapped and native pointer types](EDEN.md); retain console, run identity and memory evidence. |
| M29 | M | Bound [every local command by memory/time](START.md); put `${var}` use in a script file. |
| M30 | M | Keep [each run's outputs under its own label](WORKFLOW.md). |
| M31 | M | Write the [short README with whole-screen1280-wide <10s GIF](WORKFLOW.md), dated build caption, install/build/license/AI lines. |
| M32 | M | Keep [dated build/run/behavior/log evidence](WORKFLOW.md); row receipts and <=20-line RESULTS feed one phase compliance report. |
| M33 | M | [Group every reachable open row into as few boots as possible](TEST-PLAN.md); split only for stated build/host/profile/boot needs. |
| M34 | M | [Continue reachable rows after failures](MACROS.md); recover/check start state, defer with cause, stop safely on unrecoverable state/crash. |
| M35 | M | Make [no unsupported claims](WORKFLOW.md); keep operator quotes out of shipped docs, with the working-design trace exception. |
| M36 | M | Keep [handoff branch/commit/evidence/live-state/next commands current](WORKFLOW.md), with no secrets. |
| M37 | M | [Only the user connects/disconnects or controls DBI/UMS](WORKFLOW.md); agents never mount/unmount/eject/remount/power off media. |
| M38 | M | Obtain [explicit action-specific orders](WORKFLOW.md) before push/tag/release/card writes; report lack of authorization. |
| M39 | M | Use [Forgejo and temporary UTF-8 commit-message files](START.md); no GitHub publication or unordered push. |
| M40 | M | Follow the external [model/effort/watcher/routing authority](START.md); do not duplicate its policy. |
| M41 | M | [Every agent/worker reads START+CHECKLIST+task pages](START.md), records playbook commit, makes its checklist and runs compliance before READY. |
| M42 | M | [Hooks never wait on a plugin thread](BUILD.md); preserve the new base's exact nonblocking contract. |
| M43 | M | Declare an [empty diag feature and keep working tree clean](BUILD.md); retain these code-only checker obligations. |
| M44 | M | Retain [exact build/shipped-NRO identity](WORKFLOW.md); explicit artifacts outrank newest-file inference and a tag is required only for release. |
| M45 | M | [Pin resolved build paths and matching trace symbols](PROFILES.md); disclose mixed builds and separate client identity from app pins. |
| M46 | M | [Snapshot before staging; mute/save/read back before load](LIFECYCLE.md); bounded terminal jobs, durable journal and ACK-loss-safe owned claims. |
| M47 | M | [Stop Linux main before FUSE/backing processes](LINUX-DOWN.md), using owned birth identity/pidfd and fail-closed claim retention. |
| M48 | M | Change [shared tools only in a separate worktree](MACROS.md); generic extensions get tests; commit/reuse successful route inputs. |
| M49 | M | Keep [one driver per instance and same-frame macro chords](MACROS.md); review settled state before playback. |
| M50 | M | Respect [input lease/recording/hash/edit invariants](MACROS.md); incomplete recordings cannot become passing media. |
| M51 | M | Budget [rows+boot/navigation+route work under runtime minus margin](TEST-PLAN.md); order wedge/crash/teardown checks last. |
| M52 | M | [Review actual behavior and settled HUD state](MACROS.md); fixture applied, bareTrue and clip saved do not prove behavior. |
| M53 | M | [Ban extracted game/mod assets and dumps from git](WORKFLOW.md); reviewed evidence screenshots and required reference crops are allowed. |
| M54 | M | Treat [cleanup success separately from graceful/native health](LINUX-DOWN.md); inspect unit/console fatal tails and qualification limits. |
| M55 | M | Stop only [owned displays/processes/paths](LIFECYCLE.md); ambiguous identity or failed restore/verify retains claim for recovery. |
| M56 | M | Follow [60GB pre-Up admission and verified-clean-Down pruning](HOSTS.md); preserve receipts/logs/reports and failed-cleanup recovery state, never foreign copies. |
| C01 | C | If sites may overlap, use [sharing/chaining](HOOK-SHARING.md), election before guard, both stand-ins and load orders; cite dated scan for N/A. |
| C02 | C | Online code obeys [read-only checked-RNG/native-order/simulated-state constraints](WORKFLOW.md), hook by hook. |
| C03 | C | Online code passes [same replay A/B](WORKFLOW.md), identical detector values and positions. |
| C04 | C | Online-only paths need [both-detector two-console proof](WORKFLOW.md) through later sessions, desync0 at matching frame. |
| C05 | C | Mods need [pinned ARCropolis and native-screen mod proof in that boot](PROFILES.md). |
| C06 | C | Log-derived numbers come from [committed host-tested scripts](WORKFLOW.md), with the regeneration command beside them. |
| C07 | C | Asset claims need [decoded expected/frame/stock comparison](WORKFLOW.md); rule out stock without committing extracted sources. |
| C08 | C | A changed recorded decision needs [document/item/date citation, disclosed reversal and updated original record](DESIGN.md). |
| C09 | C | Authorized release needs [minor bump, annotated tag, required assets and anonymous hash verification](WORKFLOW.md). |
| C10 | C | Release must have a [byte-identical rebuild of the tagged commit](WORKFLOW.md). |
| C11 | C | Authorized card work uses [fresh preflight, archives/rollback/readbacks and inspected non-repeated receipts](WORKFLOW.md); single writer, loader last, disconnect after final verify. |
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
| C25 | C | Image/hook gates preserve [static/tolerance/scaling/ROI/evidence/REVIEW semantics](MACROS.md); unchecked/bareTrue alone prove neither start state nor behavior. |
| C26 | C | Reused/new segments retain [sha256/start/end gates, receipts and reviewed harvest provenance](MACROS.md). |
| C27 | C | Before shipping, use the [full card plugin set for one match through results/back](MACROS.md); inspect every install line and console. |

Machine checks are useful approximations, not semantic proof. See each concern's evidence and tooling limits.
An approval JSON, old receipt, example pass count, or zero wrapper exit is not a new authorization or complete acceptance.

## Baseline contracts during migration

The following87b3de0 contracts remain available during migration. TASK2 approved the reading/screenshot exceptions above; original wording is retained here for clause review, not to reverse those decisions.

# Completion checklist

2026-10-31. What an agent proves before it says a plugin task is done. Each
item is tagged **M** (mandatory for every plugin), **C** (conditional: the
condition is stated, and when it does not apply the report says N/A and
why) or **E** (an example of how it was done, not a rule).

```text
python3 -I tools/check/compliance.py <plugin repo> [--nro <file>] [--base <ref>] [--trace <design.md>]... [--out report.md]
```

Pass `--base` (the commit the branch started from) and `--trace` (the design report) for every branch: without them
the two rows below stay `agent` rows, and a brief can require them.

fills the rows a machine can verify and prints the report. The agent
completes the rest with evidence: a file, a log line, a command's output. A
row without evidence is not a pass. Keep the finished report with the
evidence doc.

Run it once per phase or release candidate, over all of that phase's Eden
runs, not after every run. A single run only writes its short `RESULTS.md`
([MACROS](MACROS.md), "Test runs", rule 9).

## Implementation

- **M** `crate-type = ["cdylib"]` only. An `rlib` alongside makes Cargo skip
  LTO for the crate without saying so, and its code stays in the resident
  image the game's heap pays for. Shaderdb-probe went from 1,081,344 to
  618,496 bytes resident with the same exports. Evidence: `Cargo.toml` and
  `tools/build/nro_size.py` on the shipped NRO.
- **M** Release profile `opt-level = "s"`, `lto = true`,
  `codegen-units = 1`, `panic = "abort"`; `skyline = "=0.6.0"`; toolchain
  `1.98.1`.
- **M** Pure logic in `model.rs` with host tests; `runtime.rs` only under
  `#[cfg(target_os = "switch")]`.
- **M** Gates pass, one cargo call each: `cargo test`,
  `cargo clippy --all-targets -- -D warnings`,
  `cargo build --no-default-features`, `cargo skyline build --release`, and
  the diag build on its own. Evidence: the outputs.
- **M** No heap in hooks: a counting allocator and `heap_allocations=0` in
  the install line.
- **M** Fixed buffers, a writer thread on a static stack, files opened from
  stack-built paths. **E** gsp-visualizer, ssbu-gnw-judge-log.
- **M** Every fix solves the root cause. Otherwise the workaround (guard,
  skip, restriction, tolerance, lint `allow`, feature gate, setting off) went
  to the operator with its root cause, cost and a recommendation, and was
  approved before it shipped. The report lists each approved workaround with
  its date ([WORKFLOW](WORKFLOW.md), "Root cause, or the operator's
  approval"). Defects that share one cause were checked for a single
  abstraction, and the report says which approach was chosen and why.
- **M** Every user-facing refusal the branch adds cites an approved restriction (operator 2026-11-05, after a worker
  shipped "Turn Player Swapping on to switch Player 1" without asking). A refusal is a string literal that refuses or
  restricts what the user asked for ("Turn X on to …", "unavailable", "not available", "only when", "requires",
  "cannot", …). The line, or one of the 3 above it, carries `// restriction: <ID>`, and `docs/RESTRICTIONS.md` has a
  table row `| <ID> | message | why | approved |` whose last cell records the operator's approval (a date or decision
  ID; `pending` does not count). An ordinary refusal ("busy", "a save state is loading") is listed once like any other.
  Evidence: `compliance.py --base <branch start>`.
- **M** The design report, written before the code, has a requirement-trace table: one row per clause of the
  operator's request, quoted verbatim, the design element that implements it, and the test(s) that prove it, as
  backticked function names (`| Operator clause | Design | Test |`). A requirement "the same as X" is proven by a
  parity test that runs X and the new path through one table of cases. Every row names a test and every named test
  exists. The design report is a working file beside the handoff; the README and shipped docs still carry no operator
  quotes. Evidence: `compliance.py --trace <design report>`.

## Hook safety

- **M** Version guard: the game reads `13.0.5` or nothing is installed and
  the log says `result=unsupported_version`.
- **M** 64-byte guard on every hooked site, every called function and every
  relied-on instruction; a mismatch leaves that site alone and the log names
  it.
- **M** Card scan before the site is chosen: `tools/build/scan-offsets.py`
  over the card copy, for the site and, for a virtual function, its vtable
  slot. Evidence: the scan's output and the copy's date.
- **C** (another plugin hooks or may hook the site) Sharing v1 or the chain
  rules ([HOOK-SHARING](HOOK-SHARING.md)); the owner election runs before
  the byte guard; both stand-in modes (`foreign=inline`, `foreign=far`) and
  both load orders tested in Eden. N/A only when the scan showed nobody at
  the site, with the scan cited.
- **M** Hook after the prologue, or call the game function instead of
  hooking it; hook the most specific instruction.
- **M** No game hook blocks on a lock a plugin thread can hold: the hook
  `try_lock`s or reads a published value ([WORKFLOW](WORKFLOW.md), "Hooks
  never wait on a plugin thread"). Evidence: a lock-reach test, like
  TrainingRoomsForUltimate's `tests/hook_locks.rs`/`tests/lock_order.rs`,
  passing on the branch and failing on a seeded violation; or the
  recorded reason none applies (no plugin threads, or no locks). Every
  hook counts as exposed; only one a trace shows on MainThread alone may
  be excepted, by name, with the run label and hit counts (never reached:
  still exposed).
- **M** No diag code or strings in the shipped NRO (`diag` feature off; no
  `diag.txt` or `standin` strings). Evidence: `compliance.py`.
- **M** The shipped NRO writes its install line and nothing per frame.
  **C** (a logging plugin whose product is the log) its data files too, but
  no per-frame diagnostics.

## Online (C: the plugin runs during online play)

- **C** Read-only rules reviewed in the code, hook by hook: no checked random
  number drawn, no native code that draws one skipped or reordered, no
  simulated state written (ssbu-desync-detector `docs/WHAT-IS-CHECKED.md`).
  Evidence: the review in the evidence doc.
- **C** Replay A/B with the desync detector: the same replay with and
  without the plugin, identical check values and positions.
- **C** Online-only paths: two consoles, the detector on both, the changed
  path played to its end so the session after it is checked;
  `desync_seen=0` at the same `frame=` on both.
- **E** ssbu-gnw-judge-log: four replay A/Bs, including a replay without
  the plugin's fighter that produced only the install line.

## Eden

- **M** Claim written before `Up` and released after `Down`, also after a
  failed run. Evidence: the coordination file's lines.
- **M** At most two Eden instances on the Surface, Yoga or XPS, and one on the Ally (operator 2026-11-04, XPS 2026-11-06;
  the lifecycle enforces it), MemAvailable at least 10 GiB for the first instance and 9 GiB for a second one (operator 2026-11-04), never profile `a`,
  display `:61` or another agent's runs.
- **M** The lifecycle boots every XPS Eden at 0.5x (`core.resolution_setup` = 1 before `game.load`; operator 2026-11-06:
  "yes, make half res the XPS default"). Thermald stays on. Timed rows stay on the Surface and Yoga. At load, thermald
  `--adaptive` cuts PL1 to 15 W; 29.5 W is the idle value. `xps-dual-03` ran a median of 59.3 simulated frames/s with
  thermald on; `xps-dual-04` ran about 60 with thermald off, at 100 °C. An `eden_settings` value on the run or the
  instance overrides the default, and the instance wins ([EDEN](EDEN.md)).
- **M** The profile is a copy with the game content hard-linked (`gv.py
  prep`, or `cp -al`); a full copy per profile fills the filesystem. Only
  the copy is changed. Byte-identical files are links, and files that differ
  or that the run writes in place (saves, rewritten INI/config, shader
  caches, logs) are real copies ([EDEN](EDEN.md), "Link what is identical,
  copy what is not"). Evidence: `du -sh` against `du -sh --apparent-size`
  on the copy.
- **M** One `CARGO_TARGET_DIR` per source copy, never shared between copies
  of the same workspace, and the cargo home shared per host and toolchain;
  sources refreshed with new file times; `df -h /` checked before every build or Eden
  launch, stopping below 40 GB free; finished work's build products deleted,
  with a manifest, while NROs, receipts and logs are kept ([WORKFLOW](WORKFLOW.md),
  "Build caches and disk").
- **M** Every wait gated on a log line or a screenshot check, between macro
  segments too ([MACROS](MACROS.md), "Hybrid"); a fixed sleep only where
  neither exists, and then rounded up.
- **M** A test run plays `tools/eden-runner/runner.py` with the run's own
  `rows.json`, extended by project step hooks (`eden/steps.py`) where the
  plugin needs them; no copied or forked runner. It was written and had an offline
  dry run before `Up`: recorded segments with sha256, automated gates, a receipt per
  row, rows chained on their end state, and the full restore once at `Down`
  ([MACROS](MACROS.md), "Test runs: the runner is ready before Up"). Manual
  input only to build a missing segment, which is then recorded into the
  runner. If no row is done 15 minutes after the first settled screen, stop
  and fix the runner. Up within 15 minutes of the order (30 when segments
  must be recorded), with the rows that pass; no extra offline suites before
  boot. At `Down`, segments, runner and row list committed to `eden/`.
  Evidence: the runner's path and dry-run log, the order and `Up` times, and
  the `macro.play` calls in `control.jsonl`.
- **M** Starting states come from a fixture, a segment, a hook or one bounded
  recording, named in `rows.json` `"setup"` before `Up` (`eden_test.py up`
  refuses without it); `eden_test.py pace` stayed under budget (40
  hand-driven inputs, 15 minutes without a newly passed row) or the boot was
  stopped and the rows fixed ([MACROS](MACROS.md), rule 13). Evidence: the
  `setup` list and the `pace:` lines in the run's output.
- **C** (a row depends on a mod in `ultimate/mods`: stage, fighter, UI or
  sound replacement) the card's `libarcropolis.nro` is in the run's pinned
  plugins. The lifecycle moves every unpinned NRO aside, so without it no mod
  loads and the row shows stock assets ([EDEN](EDEN.md), "Mods need
  ARCropolis"). Evidence: the pin in `config.json`, and the game's own screen
  showing the mod in the same boot (for a stage, the native stage select).
- **M** A run whose precondition line never comes is aborted and retried
  under the next label ([EDEN](EDEN.md), "Queue runs").
- **M** Game-side facts are captured with guest tracing on a run already
  happening: persistent `trace.*` points (from boot via the lifecycle's
  `trace`, or armed during the run) on builds that have them, the one-shot
  tracepoint otherwise ([EDEN](EDEN.md), "Instrument the running game
  without rebuilding"). A diag rebuild or an extra trace boot only for what
  tracing can't see (allocation accounting, watchpoints), with the reason in
  the handoff. Evidence: the trace JSONL or captured result, and the build
  it ran on.
- **M** The ruleset is the shortest that covers the path under test: 1
  stock, a 1:00 limit, items, hazards and FS meter off ([EDEN](EDEN.md),
  "Keep matches short"). A longer match only when the test needs it, with
  the reason in the handoff. Evidence: the ruleset screenshot or the match
  length in the log.
- **M** Profile restored after the run: set-aside NROs back, saves and
  replays back, temp files removed, the host released.
- **M** The run's copies pruned at `Down`: the files the run wrote copied to `out/post-run-<key>/` first, then
  `work/`, `state/snapshot-*` and `state/post-run-*` deleted (`--keep-state` only when asked), and `Up` refused below
  60 GB free (`disk_floor_bytes`) ([EDEN](EDEN.md), "Down"). Evidence: RESULTS.md's `Prune:` line.
- **M** `console.log` searched for `panic`, `abort`, `crash` and `Unmapped`;
  an `Unmapped Read`/`Unmapped Write` below `0x1000` or inside the plugin's
  activity window fails the lane, because Eden tolerates the null read the
  console faults on ([EDEN](EDEN.md), "Read the results"). A clean scan
  doesn't prove there was no null read (Eden misses some; lr-14t): every
  native return or game-memory value used as a pointer has its type checked
  against the game's bytes. `run.json` and `memory.peak` kept with the run.
- **M** Every local command under the memory and time guard; anything with
  `${var}` in a script file.
- **M** Each run's outputs kept under its label.

## Evidence and docs

- **M** README: one paragraph, a full-screen GIF (1280 wide, under 10 s,
  caption with date and build), the install path, the build commands, a
  license line and the AI-assistance disclaimer.
- **M** A dated evidence doc: run labels, the build (commit or NRO sha256),
  what was done, what the log showed. For Eden test runs, each run's short
  `RESULTS.md` plus the runner receipts are that doc; the phase's
  compliance report collects them.
- **M** Eden rows are grouped into as few boots as the hosts and builds allow:
  one `rows.json` per boot covering every reachable open row. A failed row
  is fixed or deferred and the same boot carries on ([MACROS](MACROS.md),
  "Test runs", rule 8). Evidence: the run labels against the row list.
- **M** Test runs continue past a failing row and the batch is fixed in one
  go. The operator, 2026-11-06: "it should also not stop at the first bug if
  possible but test everything it can and fix it all in one go to avoid one
  bug fix, reboot, another, reboot etc". Evidence: `out/summary.json`.
- **C** (the evidence cites numbers computed from logs) every such number
  comes from a committed script over the logged files, with host tests, and
  the command is named next to the numbers.
- **M** No operator quotes, no claims beyond the tests.
- **C** (the evidence says a particular picture, icon or mod asset is shown)
  the frame is put next to the expected image decoded from its source file
  (the mod's file, or the stock file from `data.arc`), and the stock case is
  ruled out the same way ([GIFS](GIFS.md), "Check before you use it").
  Evidence: the side-by-side image and the decoded files' paths.
- **C** (the change alters something a recorded decision covers) the commit
  and report cite the decision (document, item, date) and, when a newer
  order reverses it, say so plainly; the new decision is recorded where the
  old one lives ([WORKFLOW](WORKFLOW.md), "Design changes and recorded
  decisions").
- **M** The handoff file current at each checkpoint: branch, commit,
  evidence, live state, next commands; no secrets.

## Release and card (C: the operator ordered it)

- **C** Minor version bumped, annotated tag, Forgejo release with the NRO,
  `manifest.json` and `SHA256SUMS.txt`; every asset verified by anonymous
  download.
- **C** A rebuild of the tagged commit is byte-identical to the released
  NRO.
- **C** Install over DBI MTP (or the mounted card with `upgrade2-ums.py`; a swap of one plugin for another with `swap-ums.py`; Skyline's `exefs/subsdk9` with `replace-exefs.py`): preflight (card listing, local copies, `old`
  sha256 matches), a rollback copy on the card, readback of the new NRO,
  every other plugin and the protected files; receipts kept; a phase with an
  existing receipt is not re-run.
- **M** The user connects and disconnects the Switch. The agent never
  mounted, unmounted, ejected or remounted the card and never started or
  stopped DBI or hekate UMS; it read and wrote the SD only through what the
  user exposed (MTP, or the mount point), and its report told the user when
  the card could be disconnected. `compliance.py` fails scripts that carry a
  mount, unmount, eject or power-off command.
- **M** Without the order: nothing pushed, tagged or written to the card,
  and the report says so.

## Git and routing

- **M** Forgejo, never GitHub; push only on the operator's order; commit
  messages from a UTF-8 file.
- **M** The model and effort gate and the per-thread watcher follow the
  routing authority (`/home/intpa/.codex/DELEGATION_ROUTING.md`); they are
  not restated here.
- **M** Every spawned worker read this playbook itself (`README.md`, all of
  `docs/`, `tools/PROVENANCE.md`) before changing code, built its own
  checklist and ran `tools/check/compliance.py` before READY; the brief that
  spawned it said so and recorded the playbook commit it followed.
- **C** (several workers or Eden hosts) one persistent worker per host
  carries follow-ups in its own session. Every brief ends in a done-checklist:
  PASS, FAIL or BLOCKED with a reason. Working routes, chords and fixtures go
  into the shared route library when they first work. The coordinator enforces
  the Up deadline and watches for stalls ([WORKFLOW](WORKFLOW.md), "Running
  agent workers without churn"). Evidence: the session list against the
  hosts, and the library's commits.

## The report

One table, `Requirement | Tag | Result | Evidence`, with Result one of
`pass`, `fail`, `N/A: <reason>` or `not authorized`. `compliance.py` prints
it with the machine rows filled and the others marked `agent`; replace
every `agent` before declaring completion, and list the failed, skipped and
unverified rows in the final message.
