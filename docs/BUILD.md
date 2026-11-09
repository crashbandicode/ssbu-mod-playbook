# Build and runtime discipline

Use a working plugin's small layout: pure, host-tested logic in `model.rs`
(or `model/`), Switch hooks and install code in `runtime.rs` under
`#[cfg(target_os = "switch")]`. `lib.rs` declares the modules. Reuse reviewed
`share.rs`/`chain.rs` where appropriate; [HOOKS](HOOKS.md) covers site safety.

Required build configuration:

- `crate-type = ["cdylib"]` only; no `rlib` exception applies here.
- `skyline = "=0.6.0"` under the Switch target dependencies.
- Toolchain `1.98.1`, with rustfmt and clippy.
- Release `opt-level = "s"`, `lto = true`, `codegen-units = 1`,
  `panic = "abort"`, and an empty `diag` feature.

Run each gate in its own cargo call, with `CARGO_TERM_COLOR=never` and
`CARGO_TERM_HYPERLINKS=false`, and retain its output:

```text
cargo test
cargo clippy --all-targets -- -D warnings
cargo build --no-default-features
cargo skyline build --release
cargo skyline build --release --features diag
```

Keep the diagnostic output in its own `dist/` folder so it cannot replace
the shipped release. Run `tools/build/nro_size.py <shipped-nro>` and keep
the resident size (`.text` + `.rodata` + `.data` + `.bss`), which costs the
game memory. The historical LTO measurements are in
[incident history](WORKFLOW.md).

Hooks allocate no heap. Use a counting global allocator and show
`heap_allocations=0` in the install line. Use fixed buffers, a writer thread
on a static stack, and stack-built file paths. Presence of allocator or guard
text is not proof of runtime behavior.

A game hook (`#[skyline::hook]` or inline) never blocks on a lock a plugin
thread can hold. Use `try_lock` and skip its work when busy, or read a value
published by the plugin side: an atomic, a published copy such as
TrainingRooms' `hos_lock::Published`, or a lock-free mailbox. The plugin
side never waits for the game either. A worker hook can deadlock the game
when MainThread spins waiting for that worker and the lock holder cannot
run on MainThread's core; [incident history](WORKFLOW.md)
records the observed scheduler failure and hang-diagnosis procedure.

Treat every hook as exposed to game-worker execution. Only a trace showing
that a named hook runs exclusively on MainThread permits its named test
exception; retain the run label and hit counts. A hook not reached by that
trace remains exposed. Trace `thread_id` and `core_id` under
[TRACING](EDEN.md), rather than assuming a hook's calling thread.

Keep a lock-reach test that walks the call graph from every hook and every
plugin-thread entry, following helpers that acquire locks for their caller.
It fails if a hook's reachable code takes a blocking lock also reachable
from a plugin thread. The branch passes and a seeded violation fails.
List edges through `dyn`, trait objects and stored closures by hand; source
scanning cannot discover them. TrainingRoomsForUltimate's
`tests/hook_locks.rs`, `tests/lock_order.rs` and `tests/common/callgraph.rs`
on `p23/rc6-qp-respawn-freeze` are references. Record the reason when no
such test applies (no plugin threads or no locks), with the evidence.

Write one boot install line to `sd:/ultimate/<plugin>/events.log` and the
Skyline log: tick, plugin/version, each site's outcome, result and allocation
count. The shipped NRO writes only this line and nothing per frame. A
logging plugin whose product is the log may also write its data files, but
still writes no per-frame diagnostics.

Diagnostic events go through a static buffer to the writer. Design screen
and state change lines with a plugin's menus, so runner gates can read a
stable `screen=` or `mode=` value. Eden-only switches and `diag.txt` reads
stay behind `--features diag`; no diag code, `diag.txt` or `standin` strings
belong in the shipped NRO. Run compliance with the explicit shipped file;
its string scan is only a partial check, so also verify the feature/build.

Give each source copy or worktree its own `CARGO_TARGET_DIR` and reuse it
for that copy. Never share a target between copies of the same workspace:
Cargo's relative artifact names and freshness checks can link another
copy's crate. Share only the cargo home per host and toolchain.

Refresh source file times. Archives extracted with preserved timestamps,
`cp -p` and `rsync -a` can leave stale output apparently fresh; use `tar -m`
or clean the workspace's own crates with `cargo clean -p <crate>` before
building. Seed a target only while holding the compiler lock, then clean
the workspace's own crates immediately. Retain either an unpacked source
stage or its identical archive, not both.

Check `df -h /` before every build and Eden launch. Stop a build below
40 GB free; the pre-Up Eden floor is 60 GB. Use [HOSTS](EDEN.md) and
[LIFECYCLE](EDEN.md) for admission, copies and clean-Down pruning.
Link byte-identical immutable files; make real copies for different or
writable files as [PROFILES](EDEN.md) requires. When work finishes,
delete its build products with a deletion manifest, while keeping shipped
NROs, receipts, logs and reports. Preserve pinned outputs of live runs;
new heads build into new folders under [WORKERS](WORKERS.md).

Before completion, keep a clean working tree and record the shipped NRO identity.
