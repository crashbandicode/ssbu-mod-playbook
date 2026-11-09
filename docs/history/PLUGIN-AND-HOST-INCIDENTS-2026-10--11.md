# Historical plugin and host incidents, October–November 2026

Historical observations at source `87b3de0`; dates, paths, hashes and
qualifications describe those runs only. They do not identify present host
state, qualify a changed implementation or grant permission to operate.
Current rules live in [BUILD](../BUILD.md), [DESIGN](../DESIGN.md),
[HOOKS](../HOOKS.md), [HOOK-SHARING](../HOOK-SHARING.md),
[HOOK-CHAIN](../HOOK-CHAIN.md), [HOSTS](../HOSTS.md),
[TRACING](../TRACING.md) and [WORKERS](../WORKERS.md).
References below name original source paths/lines at `87b3de0`.

## Build and design incidents

`docs/WORKFLOW.md:3–5,30–42` records the reused layouts of
gsp-visualizer, ssbu-quiet-elite-switch, elite-rematch and ssbu-tile-cursor
at `git.syntaxbyexample.com/intpa/<name>`. On **2026-10-31**, eleven
repos dropped `rlib`: shaderdb-probe's resident size went from
**1,081,344 to 618,496 bytes**, with identical exports. The old paragraph
allowed `rlib` for a linked crate, but none of these plugins needed it;
the mandatory cdylib-only contract supersedes that unqualified exception.
The source's test/example alternative included `../src/model.rs` by path
with `#[allow(dead_code)]`; it was not permission to suppress a real defect.

WORKFLOW:208–211 records **2026-10-31**'s copy-registry hang: restricting
Kirby to a window's first slot avoided a state the game never builds,
Kirby as an entry's second body. The root fix constructed valid game states
and retained free player rotation order. WORKFLOW:215–224's decision-reversal
example named **SPEC-core-07 Q16, 2026-11-01**, runtime-read portraits
with nothing baked; that was an example citation, not a current decision
for every plugin. `docs/CHECKLIST.md:51–57` recorded a worker shipping
a Player-Swapping-on refusal on **2026-11-05** without approval, motivating
the restriction registry rather than granting that restriction.

WORKFLOW:228–232,239–242 records targets of **5–20 GB** and the Surface
at **100%, 2.8 GB free** on **2026-10-31** from abandoned experiment /
worker targets plus unpacked source beside identical archives. Cleanup
freed about **106 GB**. A shared-target/freshness failure raised **E0432**
for a module that existed in that source copy, **70 s** after the same
tests passed there. `docs/EDEN.md:74–75` records a separate dedup pass
hard-linking about **105 GiB** of identical profile files without loss.

## Providers, collisions and load-order evidence

`docs/HOOK-SHARING.md:8–16` records deluxe-rewrite's **2026-10-16**
boot crash: Skyline decoded a far jump's embedded address as instructions.
A far patch over an earlier one-word hook could also cover its return word;
merely refusing the second plugin did not satisfy coexistence.

SHARING:30–41 listed dedicated providers and their consumers:

| Provider | Export/API | Historical consumer |
| --- | --- | --- |
| `libnro_hook.nro` | `add_nro_load_hook`, Skyline `nro::add_hook` | Training Modpack fork `src/lib.rs` |
| `libparam_hook.nro` | param-load `add_hook(params_main)` | Training Modpack fork `src/training/mod.rs` |
| ARCropolis | `arcrop_register_callback`, `arcrop_load_file` | ssbu-fx-control `src/runtime/arc.rs` |
| Online Deluxe | `ssbu_online_deluxe_online_context_v1`, exported state | ssbu-fx-control `src/runtime/online.rs` |

The Modpack fork panicked without its providers. ssbu-fx-control instead
installed two hooks when Deluxe was absent. These were dedicated-provider
examples, distinct from peer inline-sharing v1.

SHARING:49–87 records shared `src/share.rs` in gsp-visualizer,
quiet-elite-switch and elite-rematch. The election-before-guard pitfall was
found in Eden. State examples were gsp `APPLY_STATE`, quiet `POPUP_STATE`,
elite `STATE` (1 hooked, 2 not), with `HOOKED` in share probes. Up to
**2026-10-31**, gsp's share probe re-read an already patched site instead
of using stored state. In **Eden e17**, that probe loaded first as owner
and took gsp-visualizer as its guest after the fix. gsp **0.3.0**'s
disabled-label case retained the
shared hook as owner (`result=hook_only`) but registered nothing as guest.

SHARING:97–152 records the function-entry example in deluxe-rewrite
`src/hook_chain.rs::plan_far_chain`, elite-rematch `hook_lookup`, and
shared inline chaining in gsp **0.3.0** / quiet **0.2.0** `src/chain.rs`.
Eden **2026-10-30** emitted one-word inline branches and a far replacement
hook into a plugin. The called text setter was `.37a2288`; the relied-on
hover call was `bl .1a1c070`. elite's `foreign=1` stood in on three inline
sites and the lookup replacement.

SHARING:154–169 records elite-rematch's **2026-10-30** `docs/SAFETY.md`
result: `branch/near` on three inline sites, `chain/far` on lookup.
The **2026-10-30/31** gsp/quiet diagnostic runs included elite **0.3.1**,
gsp's share probe and seeded Elite Sephiroth:

- **e15**, `foreign=inline`: `apply=branch/near`, `label=on`,
  `other_hook=set_text`, `result=installed`, `heap_allocations=0`;
  `popup=branch/near result=installed`. Label and probe callback ran.
  Stand-ins counted `apply_inline=10`, `set_text_inline=2218`,
  `popup_inline=1`; a non-Elite fighter suppressed the pop-up,
  `suppressed total=1`.
- **e16**, `foreign=far`: `apply=chain/far`, `popup=chain/far`, same
  behavior; `apply_far=10`, `set_text_inline=2206`, `popup_far=1`,
  `suppressed total=1`, probe hits **10**.

SHARING:188–202's source pointers were gsp `src/share.rs`, `chain.rs`,
`model.rs` (`Kind`, `Site::usable`), `runtime.rs` (`main`,
`register_apply`, `hook_apply`, `apply`), `docs/CSS-LABEL.md` and
`test-plugins/share-probe`; quiet `runtime.rs` (`main`, `register_popup`,
`popup_usable`, `hook_popup`) and `docs/HOOK.md`; elite `model::entry`,
`runtime::{hook_inline,hook_lookup}`, `.147fb64`, `.147f4d0`, `.168a5b8`
and the `.147fb64` share probe. Repositories were
`https://git.syntaxbyexample.com/intpa/gsp-visualizer` and
`https://git.syntaxbyexample.com/intpa/elite-rematch`.

WORKFLOW:62–82's scan example used
`/home/intpa/gsp-visualizer-work/card-20261031/`, dated **2026-10-31**
before shaderdb-probe removal, querying `1a1c118 147fb64`.
quiet-elite-switch arrived on the card after elite-rematch's snapshot,
illustrating why a scanner result applies only to its input copy.

## Host releases and thermal measurements

`docs/EDEN.md:7–54` records
`/home/intpa/Applications/Eden-GIF/current/Eden.AppImage` as the old
entry alias. At **2026-11-03 21:05Z**, Surface selected
`20261103-063f1b80bb2c-persistent-tracing-6a0c80d2b858`, replacing
`20261031-b3e672c43f03-nv01-timer`; Yoga still had the earlier build
pending eden-tracing's install. The release included `tools/edenctl.py`,
`eden-run.py`, `docs/CLIControl.md`, `InputMacros.md`,
`ELITE_RATING_TESTS.md` and `GUEST_TRACEPOINT.md`.
The old profile example cloned Yoga source
`/home/intpa/ssbu-desync-1029/profiles/b` to
`/home/intpa/gsp-visualizer-work/profiles/y`; `s` cloned `y` for a pair.
These paths are historical, not available-run assertions.

XPS **ubuntu-xps**, Ubuntu **26.04**, Iris Xe, was added **2026-11-06**
with release `20261104-1961a200485e-unmapped-stop-0c7f89f711d6`.
Its host config used `xps`, hostname `ubuntu-xps`, authoritative ledger SSH
`memento-claw-yoga-ubuntu`; profile/game/display helper paths matched Yoga.
**xps-dual-02**, November 6 at 1x, completed pair match/rematch cleanly:
`desync_seen=0`, rematch connected, available memory at least **12.8 GiB**,
about **53 simulated frames/s**. After the half-resolution default,
thermald `--adaptive` reduced loaded MMIO PL1 to **15 W**, versus **29.5 W**
idle. **xps-dual-03**, 0.5x with thermald on, had median **59.3 frames/s**;
**xps-dual-04**, thermald off, about **60 frames/s at 100 °C**.
The **13.0.5 update NSP** was added November 6 under
`~/Games/Roms/Switch Rom Updates and DLC`; previously XPS booted **13.0.3**
and all 13.0.5-guarded plugins refused. These measurements are not current
thermal state or permission to disable thermald.

## Native-pointer and console-log blind spots

EDEN:292–321 records a **2026-11-01** group swap that passed **878 Eden
lanes** while logging `HW.Memory … Unmapped Read32 @ 0x00000000000008`,
then crashed Switch at that read. Repeated `Read64 @ 0x0` / `Read8 @ 0x38`
pairs were an example of other stock/emulator noise requiring attribution.
On **2026-11-04**, **lr-14t**, build `6a0c80d2` (same logging code in
`d483c082`), TrainingRooms RC8 `ldr x8,[x0,#0x20]` ran with `x0=0x1`,
read `0x21` as zero, and produced no HW.Memory/Unmapped console line;
Switch would fault. A `cset w0` returning bool was a concrete warning
against treating a native return as a pointer.

Before `20261104-f916cf4b8c68`, clean logs did not prove null safety;
review plus hardware remained necessary. The unmapped-stop build added
exact-PC/module records (first 16, then power-of-two totals),
`total`/`emitted`/`suppressed` counters and `fault` capture. Counter zero
covered only exercised paths; fault mode included stock reads. Old claims
about sampled PCs/fault not existing apply only to older builds.

## Worker churn observations

WORKFLOW:276–300 records **14 worker sessions in eight hours** on the
TrainingRooms Phase 2/3 day, **2026-11-04**. Restarts, re-reading, early
stops and rebuilding routes dominated lost time. Two workers were replaced
on a broker meter while at **265K** and **428K** tokens; the source noted
the meter read 100% around 256K, a quarter of GLM 5.3 Flash's 1M window.
Those are archived incident figures, not a measurement or trigger for
current context management. The November 5 policy was to keep sessions
through automatic compaction. WORKFLOW:308–320 also recorded an Ally brief
omitting the tile cursor and a tile hook sending L3/D-pad in different
frames; three workers independently rediscovered chords/fixtures/routes.

## Plugin-thread lock hang, 2026-11-09

WORKFLOW:84–136 adds the TrainingRooms **rc.6** Quickplay waiting-area
freeze, **rc6-qpf3**, read through Eden's GDB stub. MainThread spun on
core 0 in a task join; TaskWorker1 waited in the UI-tick hook on menu lock
`EDITED`; plugin `tr-menu`, holding it, was runnable on core 0 but never
scheduled. It struck after **37, 39 and 70 menu loops** in three runs.
A Training-entry freeze immediately after a `mii-catalog` hand-off into
locks used by per-frame Mii hooks was considered very likely the same
class, but had **no stack**, so that attribution remained provisional.

Eden stayed alive while presented frames and all plugin logs, including
its watchdog, stopped. MainThread spins rather than sleeps in some task
joins, so a worker hook blocking on a plugin lock can starve its holder.
Horizon's strict-priority scheduling made a console hang expected, but it
had **not been observed on a console**. MainThread-only hook waits free
that core; no untraced hook was therefore assumed MainThread-only.

The diagnostic procedure traced hook `thread_id`/`core_id`, or booted with
`core.use_gdbstub` before `game.load` and dumped every frozen thread.
Stub and tracing could not be enabled together. The signature was a
spinning core, hook lock wait in Arbitration, and runnable-but-starved
plugin holder. Dump helper:
`/home/intpa/smash-training-lobby/artifacts/phase23/rc6-eden/rows-hang-qp/gdbdump.py`
(without gdb). Reference lock/callgraph tests were
TrainingRoomsForUltimate `tests/hook_locks.rs`, `tests/lock_order.rs`,
`tests/common/callgraph.rs`, branch `p23/rc6-qp-respawn-freeze`.
Current try-lock/publication, plugin-side no-wait, trace exceptions,
seeded failure and manual dynamic-edge obligations live in [BUILD](../BUILD.md).
