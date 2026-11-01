# Completion checklist

2026-10-31. What an agent proves before it says a plugin task is done. Each
item is tagged **M** (mandatory for every plugin), **C** (conditional: the
condition is stated, and when it does not apply the report says N/A and
why) or **E** (an example of how it was done, not a rule).

```text
python3 -I tools/check/compliance.py <plugin repo> [--nro <file>] [--out report.md]
```

fills the rows a machine can verify and prints the report. The agent
completes the rest with evidence: a file, a log line, a command's output. A
row without evidence is not a pass. Keep the finished report with the
evidence doc.

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
- **M** One Eden per host, MemAvailable at least 10 GiB, never profile `a`,
  display `:61` or another agent's runs.
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
- **M** A run whose precondition line never comes is aborted and retried
  under the next label ([EDEN](EDEN.md), "Queue runs").
- **M** The ruleset is the shortest that covers the path under test: 1
  stock, a 1:00 limit, items, hazards and FS meter off ([EDEN](EDEN.md),
  "Keep matches short"). A longer match only when the test needs it, with
  the reason in the handoff. Evidence: the ruleset screenshot or the match
  length in the log.
- **M** Profile restored after the run: set-aside NROs back, saves and
  replays back, temp files removed, the host released.
- **M** `console.log` searched for `panic`, `abort`, `crash` and `Unmapped`;
  an `Unmapped Read`/`Unmapped Write` below `0x1000` or inside the plugin's
  activity window fails the lane, because Eden tolerates the null read the
  console faults on ([EDEN](EDEN.md), "Read the results"). `run.json` and
  `memory.peak` kept with the run.
- **M** Every local command under the memory and time guard; anything with
  `${var}` in a script file.
- **M** Each run's outputs kept under its label.

## Evidence and docs

- **M** README: one paragraph, a full-screen GIF (1280 wide, under 10 s,
  caption with date and build), the install path, the build commands, a
  license line and the AI-assistance disclaimer.
- **M** A dated evidence doc: run labels, the build (commit or NRO sha256),
  what was done, what the log showed.
- **C** (the evidence cites numbers computed from logs) every such number
  comes from a committed script over the logged files, with host tests, and
  the command is named next to the numbers.
- **M** No operator quotes, no claims beyond the tests.
- **M** The handoff file current at each checkpoint: branch, commit,
  evidence, live state, next commands; no secrets.

## Release and card (C: the operator ordered it)

- **C** Minor version bumped, annotated tag, Forgejo release with the NRO,
  `manifest.json` and `SHA256SUMS.txt`; every asset verified by anonymous
  download.
- **C** A rebuild of the tagged commit is byte-identical to the released
  NRO.
- **C** Install over DBI MTP (or the mounted card with `upgrade2-ums.py`): preflight (card listing, local copies, `old`
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

## The report

One table, `Requirement | Tag | Result | Evidence`, with Result one of
`pass`, `fail`, `N/A: <reason>` or `not authorized`. `compliance.py` prints
it with the machine rows filled and the others marked `agent`; replace
every `agent` before declaring completion, and list the failed, skipped and
unverified rows in the final message.
