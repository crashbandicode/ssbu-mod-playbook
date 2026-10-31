# From idea to the Switch card

2026-10-31. The path gsp-visualizer, ssbu-quiet-elite-switch, elite-rematch
and ssbu-tile-cursor took (all at `git.syntaxbyexample.com/intpa/<name>`).
Most of the speed comes from reusing their layout and tooling instead of
starting over.

## 1. Start from a working plugin

| Piece | Copy from | Notes |
| --- | --- | --- |
| `Cargo.toml` | gsp-visualizer | See below. |
| `rust-toolchain.toml` | gsp-visualizer | `1.98.1`, with rustfmt and clippy. |
| `src/lib.rs` | gsp-visualizer | Declares the modules; `runtime` is `#[cfg(target_os = "switch")]`. |
| `src/model.rs` | — | Pure code, host-tested: the version string, the site table with each site's 64 bytes from 13.0.5, parsing and formatting. |
| `src/runtime.rs` | — | Switch only: `#[skyline::main]`, version check, guards, hooks, logging. |
| `src/share.rs`, `src/chain.rs` | gsp-visualizer | Hook sharing and chaining. The same files are in quiet-elite-switch; see [HOOK-SHARING](HOOK-SHARING.md). |

`Cargo.toml`:

- `crate-type = ["cdylib"]` only.
- `skyline = "=0.6.0"` under `[target.'cfg(target_os = "switch")'.dependencies]`.
- `panic = "abort"`.
- Release profile: `opt-level = "s"`, `lto = true`, `codegen-units = 1`.
- An empty `diag` feature.

Whatever doesn't touch the game belongs in `model.rs`. `cargo test` checks it
in seconds, and Eden is then needed only for what needs the game.

**cdylib only.** If the crate types include `rlib`, Cargo skips LTO for that
crate without saying so, and the code LTO would have dropped stays in the
resident image, which the game's heap pays for on every boot. Eleven repos
were changed on 2026-10-31. Shaderdb-probe went from 1,081,344 to 618,496
bytes resident, with the same exports. Add `rlib` only when something must
link the crate, which no plugin here does. A test or example that needs a
module includes it by path:

```rust
#[path = "../src/model.rs"]
#[allow(dead_code)]
mod model;
```

## 2. Guard, then hook

Finding a new site is reverse-engineering work (the `reverse-engineer-anything`
skill covers it). This guide starts once you have the offsets. Each plugin
here installs only what it can show is safe:

- **Game version:** must read `13.0.5` (`model::SUPPORTED_VERSION`).
  Otherwise the plugin logs `result=unsupported_version` and returns.
- **Site bytes:** each hooked site, each called function and each
  instruction the hook relies on is compared, 64 bytes at a time, with the
  13.0.5 bytes. Other plugins' hooks are allowed as
  [HOOK-SHARING](HOOK-SHARING.md) describes. A site that fails is left
  alone, and the log says which site.
- **Hook placement:** hook after a function's prologue instead of at its
  entry, which leaves the entry free for plugins that replace it
  (ssbu-tile-cursor `docs/HOOKS.md`).
- **Call before hook:** prefer calling a game function to hooking it.

**Before you choose a site, check the card.** The plugins on the card are
copied in `/home/intpa/gsp-visualizer-work/card-20261031/`, as of
2026-10-31 and before the shaderdb-probe removal. For a newer set, use the
local copies an MTP preflight keeps. Then run:

```text
python3 -I tools/build/scan-offsets.py <folder of NROs> 1a1c118 147fb64
```

It lists every NRO that refers to an offset, or to one within 0x40 of it. If
another plugin does, follow [HOOK-SHARING](HOOK-SHARING.md): register with
that plugin's hook, or share the site, so both plugins keep working.

The scan is a first look, not the guard. It finds an offset only as a
32- or 64-bit constant or a MOVZ/MOVK pair (low half first, within 8
instructions), and only in the copy you give it. Offsets built another way
and plugins that reach the card after the copy don't show
(ssbu-quiet-elite-switch reached it after elite-rematch's copy). What
protects a site is reading its words at install time, in every load order,
and logging the result per site, as [HOOK-SHARING](HOOK-SHARING.md)
describes. Test that path in Eden with the diag stand-ins (`foreign=`).

## 3. Log what happened

- **Install line:** one line at boot, to `sd:/ultimate/<plugin>/events.log`
  and the Skyline log:
  ```text
  <tick> plugin=gsp-visualizer/0.3.0 … result=installed heap_allocations=0
  ```
  It covers each site's outcome, and `heap_allocations` comes from a
  counting global allocator. Hooks shouldn't allocate.
- **Events:** one line per event, in the diag build. The hook puts the line
  in a static buffer, and a writer thread appends it to the file.
  gsp-visualizer and elite-rematch keep the buffer and the thread out of the
  shipped NRO, which writes only its install line; ssbu-tile-cursor, an Eden
  tool, writes every 100 ms.
- **Eden-only switches:** these go behind `--features diag`, which reads
  `sd:/ultimate/<plugin>/diag.txt`. They are never in the shipped NRO.

Eden checks then read lines instead of interpreting screenshots
(ssbu-tile-cursor `docs/EDEN-TEST.md` is the model).

## 4. Gates

Run one cargo subcommand per call, with `CARGO_TERM_COLOR=never` and
`CARGO_TERM_HYPERLINKS=false`:

```text
cargo test
cargo clippy --all-targets -- -D warnings
cargo build --no-default-features
cargo skyline build --release              # target/aarch64-skyline-switch/release/lib<name>.nro
cargo skyline build --release --features diag   # the Eden diagnostic build, kept in its own dist/ folder
```

`python3 -I tools/build/nro_size.py <nro>…` prints the resident size
(`.text`+`.rodata`+`.data`+`.bss`). That is what the plugin costs the game's
memory.

### Build caches and disk

A cargo `target/` directory reaches 5–20 GB for a large plugin. On
2026-10-31 the Surface filled to 100% (2.8 GB free) from build caches that
finished work had left behind, one per experiment, worker and build copy,
plus unpacked source copies kept next to identical archives. Deleting them
freed about 106 GB.

- **One target per source copy:** give each source copy (worktree, stage,
  worker copy) its own `CARGO_TARGET_DIR` and reuse it for every build of
  that copy. Share only the cargo home (registry and git checkouts) per host
  and toolchain. Never point two copies of the same workspace at one target:
  cargo names a workspace's own crates by their path relative to the
  workspace root, so every copy's crate gets the same artifact name, and it
  judges freshness by file times. A build then links another copy's crate,
  and tests pass or fail on code the copy does not contain (2026-10-31: E0432
  on a module the copy had, 70 s after the same tests had passed in it).
- **Refresh sources with new file times:** `tar -x`, `cp -p` and `rsync -a`
  keep old times, so cargo can judge stale output fresh. Unpack with
  `tar -m`, or run `cargo clean -p <crate>` for the workspace's own crates
  before the next build.
- **One copy of the source per task.** Keep either an unpacked stage or its
  archive, not both.
- **Check before you build:** run `df -h /` before a build or an Eden
  launch, and stop below 40 GB free.
- **When the work is done:** delete its build products and keep the NROs,
  receipts, logs and reports. Write a manifest of what you deleted.
- **Copies:** link what is identical and copy what is not ([EDEN](EDEN.md),
  "Link what is identical, copy what is not").

## 5. Eden

Follow [EDEN](EDEN.md):

1. Set up a profile with only the NROs under test, plus the tile cursor.
2. Script the route ([MACROS](MACROS.md)) and pick fighters or stages
   exactly ([TILE-CURSOR](TILE-CURSOR.md)).
3. Read `events.log`, take screenshots, record the GIF ([GIFS](GIFS.md)).

For a sanity check before shipping, use the card's full plugin set, play one
match to the results screen and back, then check each plugin's install line
and Eden's `console.log`.

### Plugins that run during online play

Follow "Rules for plugins" in `ssbu-desync-detector`'s
`docs/WHAT-IS-CHECKED.md`: draw no checked random number, don't skip or
reorder native code that draws one, and change no simulated state. Then test
both ways:

- **In the match:** the detector's replay test (the same replay with and
  without the plugin, `cargo run --example compare`).
- **Online-only paths** (results, pop-ups, rematch, matchmaking), which a
  replay can't reach: a same-PC pair (`tools/eden-pair/local_pair.ps1`, TLS
  on, the rating runtime). One console runs the plugin and the other
  doesn't, and both run the detector. Play the changed path to its end, so
  the session after it is checked too. For elite-rematch that meant playing
  the rematch, then answering No on both consoles.
- **Server state:** where the path depends on it, seed it
  (`-SeedS`/`-SeedY slot:rating`) so the game takes the path by itself,
  instead of forcing it in a diag build.
- **Pass:** every session on both consoles ends with
  `sync_end … desync_seen=0`, at the same `frame=` on both.

## 6. README and evidence

- **README:** one paragraph on what the plugin does, a GIF, the install path,
  the build commands, a license line, and the disclaimer about AI assistance
  that the other READMEs carry.
- **Evidence:** `docs/EDEN-RESULTS.md`, `VALIDATION.md` or `docs/SAFETY.md`,
  dated, with the run label, the build (commit or NRO sha256), what was done
  and what the log showed. For an online plugin, include the detector's
  result on both consoles.
- **Numbers from the logs:** every number the evidence cites should come out
  of a script in `tools/` that reads the logged files, so a reviewer re-runs
  it and a later run set updates the doc without anyone's transcript. Test
  the scripts on the host with synthetic fair data, and name the command
  next to the numbers. ssbu-gnw-judge-log
  (https://git.syntaxbyexample.com/intpa/ssbu-gnw-judge-log) is the model:
  `judge_table.py`, `window_table.py` and `match_report.py` turn the
  plugin's TSV logs into the tables its README cites, and
  `test_judge_table.py` checks them, including that a planted bias is
  flagged and a fair draw is not.
- **Wording:** short, no promises beyond the tests, no operator quotes.

## 7. Release and install (operator order only)

- **Version:**
  - Bump the minor version for each build that changes what is on the card.
  - Use an annotated tag.
  - Create a Forgejo release containing the NRO, `manifest.json`,
    `SHA256SUMS.txt` and, for some repos, an SD-card zip. Examples are in
    `tools/release/examples/`.
  - The Forgejo credential comes from `git credential fill` and stays in
    memory.
  - Check each asset by downloading it anonymously.
- **Provenance:** to find out what is on the card, rebuild the suspected
  commit. The build is reproducible, so a byte-identical NRO identifies it.
- **Switch:** the Switch is plugged into the Surface, with DBI's MTP
  responder running. `tools/switch/` holds the scripts:
  - `upgrade2-mtp.py` replaces an NRO.
  - `remove-mtp.py` removes one, archiving it first.
  - `deploy-mtp.py` adds a new one. Its name, data folder and expected
    sha256 are set in the script (gsp-visualizer's).
  Each runs `preflight <dir>` and then `install <dir>` (`remove` for
  removal) from a folder holding the NRO and an `operator-approval.json`
  (`operator_order`, `operator_install_authorized`, `name`, `data`, `old`,
  `new`):
  - **Preflight:** lists the card and keeps local copies of every plugin and
    of the protected config files. It checks that the installed NRO's sha256
    is `old`.
  - **Install:** keeps a rollback copy on the card at
    `<data>/backups/pre-<new16>.nro` and writes the NRO. It then reads back
    the new NRO, every other plugin and the protected files.
  - **The receipts** are JSON files in that folder. If a receipt already
    exists, the phase refuses to run again: inspect it first.
  - **Steps:** `tools/switch/examples/` copies the folder to the Surface and
    runs each phase over ssh. One writer at a time.
