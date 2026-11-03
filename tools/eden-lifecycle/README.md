# Eden lifecycle draft — 2026-11-03

**Offline-tested draft; no live launch, claim, profile mutation or SSH invocation
was performed during development.** Companion to `../eden-runner/runner.py`;
the lifecycle never sends input, plays macros or implements project rows.

Playbook read: `c95d1f40255df6eca041eced2596d647c74dff5e` (README, all docs,
PROVENANCE); source references read: pkg4 runner3 lifecycle, lead lr03/runtime
lifecycle, playbook gv/prep/launch/stop and the shared runner. Original files and
runner.py remain untouched. Phase2 remains paused at product commit4814381,
with no-default/review/native acceptance pending and no owned host claim/job.

## CLI and integration

```text
python3 -B -I lifecycle.py status config.json
python3 -B -I lifecycle.py up config.json --execute
python3 -B -I ../eden-runner/runner.py run rows.json --instances <state_dir>/instances.json
python3 -B -I lifecycle.py down config.json --execute
```

Wrap local commands in the usual memory/time guard and PowerShell7. The caller
must place `down` in its own cleanup/finally, even if runner execution fails.
`up` returns after loading, not after rows; systemd enforces the guest hard cap.
Controller death leaves a durable journal/claim for a fresh `down`, never an
expired-claim assumption or silent restart. `up` refuses a reused journal/root.

Append `--ssh memento-claw-yoga-ubuntu` to run the SAME lifecycle code on Yoga
through its PowerShell7 login shell, with source/config on stdin, no script
staging. **All config paths and generated instances paths are executor-host
paths.** Run the shared runner on that host too; no implicit socket forwarding.
For local Surface with the authoritative ledger on Yoga, set `ledger_ssh` to
that SSH host. Claims/releases run under a remote file lock, preserving raw
ledger bytes; unknown host/owner records block rather than disappear. No SSH
credential/config edits or foreign claim reconciliation are performed.

Config (`PIN` means `{"path":"/absolute/executor/file","sha256":"64hex"}`):

```json
{
  "owner": "OpenCode SESSION/stream", "label": "e42", "host": "surface", "hostname": "ubuntu-surface",
  "work_root": "/home/intpa/eden-life-e42",
  "state_dir": "/home/intpa/eden-life-state/e42",
  "ledger": "/tmp/deluxe-surface-1007/OWNER-COORDINATION.md",
  "ledger_ssh": "memento-claw-yoga-ubuntu",
  "runner": "PIN for the existing shared tools/eden-runner/runner.py",
  "rows": "PIN for the run's rows.json",
  "runtime": 5400, "socket_cap": 90, "load_cap": 180,
  "stop_cap": 30, "force_cap": 30,
  "instances": [{
    "key": "a", "display": ":101", "source": "/home/intpa/source/profiles/g",
    "app": "PIN for Eden.AppImage", "game": "/absolute/game.xci",
    "plugins": ["PIN for each approved NRO; [] for vanilla"],
    "files": [{"path":"/absolute/settings.json", "sha256":"64hex",
               "to":"user/sdmc/ultimate/TrainingRooms/settings.json"}],
    "sources": {"settings":"user/sdmc/ultimate/TrainingRooms/settings.json"}
  }]
}
```

The illustrative strings above must be replaced with actual pin objects.
Set `hostname` to the executor's actual kernel hostname, not its SSH alias.
Up, Down and status refuse a wrong host before any profile/claim operation.
Pins are verified before claim; staged copies are checked again. Use canonical
paths without symlink ancestors: resolve a release's `current` alias before
building the config and keep that resolved path for Down. The shared runner's
actual `dry_run()` must report nonempty READY or READY_TO_RECORD rows
before admission; the report is retained. READY_TO_RECORD means missing media
must be recorded/acquired by that shared runner, not accepted by the lifecycle.

For an explicitly assigned same-host pair, set `pair_authorized:true` and add
key `b` with a distinct display/source/inputs. One lifecycle claim covers the
pair; memory/disk and foreign-process checks repeat before EACH launch. The
second guest is the sole deliberate exception to one Eden per host. Different
owners' existing Eden/compiler processes always refuse Up; open claims are
strictly blocking unless the optional policy below applies.
The caller is responsible for obtaining its normal scheduling/queue turn.

### Optional stale-claim admission (2026-11-03)

`stale_claim_hours` has **no default**. Omit it to keep strict open-claim
blocking. To opt in, add a finite positive number, e.g. `"stale_claim_hours": 24`;
zero, negative, null, boolean and nonnumeric values are refused.

After the existing parser pairs releases with claims, Up may skip an open
**foreign-owner** claim only when its scope explicitly identifies this host,
its timezone-aware timestamp is parseable and its age is **greater than** the
configured hours, and executor admission found no foreign Eden/compiler
processes. Memory/disk gates still apply. Unknown/conflicting scopes, missing
owners, malformed/naive timestamps, future/young claims and this owner's own
claims keep blocking. Missing process-check evidence also keeps blocking.
The policy and admission result cross the authoritative-ledger SSH boundary;
the ledger host's own process state does not substitute for the executor's.

Each skipped claim gets a separate `actions.jsonl` entry with
`kind: "stale_claim_skipped"`; its value contains the original `line`,
`age_hours` and `checked_at` UTC timestamp. The same receipts appear in Up's
`stale_claim_skipped` list and the lifecycle journal. This is admission policy,
not ledger reconciliation: **no release is written for any foreign owner**,
and the original claim remains in history and in strict ledger readback.
Down still releases only the exact owned lifecycle claim after cleanup.

Optional `display_helper: PIN` points to the existing reviewed
`display-rootful.ps1`. It starts unique journalled weston/Xwayland units and
stops only those units/PIDs at Down. Without it, displays must already exist
and are explicitly **not owned or stopped** by this tool. Display61 and source
profile `a` are forbidden. This does not implement frontend LDN room setup.

## Safety and receipts

- Local/remote ledger claim uses append+fsync under flock, no automatic release,
  normalization on disk or default expiry. Optional stale-claim admission is
  described above. Historical fractional ISO
  timestamps and `CodexID`/`Codex ID` spellings are read consistently.
  Exact-token readback handles claim/release ACK loss without inventing an
  unclaimed state; ledger outage still attempts owned stop/restore but retains
  the claim until its history can be checked.
- Admission: MemAvailable >=10GiB and disk >=40GB; no foreign Eden or compiler.
- Fresh private profile: only game Contents/firmware are hard-linked. All
  mutable/unknown files are real copies; snapshots cover the full clone,
  including empty directories, unknown files, bytes and modes.
- Pristine snapshot precedes all staging/config changes. NAND/SDMC paths are
  rebased and checked; persisted Audio volume is zero, then CLI
  settings.set(core.volume,0)/settings.save/readback precede game.load.
- Guests run in unique 8GiB/swap0/Tasks256 systemd units with hard runtime caps,
  full console/control logs and no inherited LAN/runtime/seed environment.
- Accepted load/stop jobs require successful terminal responses, not mere ACK.
  Socket/load/job waits are bounded and journalled.
- Down always attempts the forced stop even if game.stop fails/times out. It
  checks the exact owned unit/cgroup and PID birth identity, never `pkill -f`.
  It preserves the post-run profile by rename, restores the full snapshot and
  performs a separate complete read/verify. Any leftover/restore/verify failure
  retains the claim. A verified retry can finish cleanup without restoring twice.
- `instances.json` is the shared runner's plain a/b mapping: socket,
  console_log, control_log, profile, settings, diag_log, detector_log; per-run
  profile source aliases may be added via `sources`.
- State: lifecycle.json; actions.jsonl; dry-run.json; per-instance
  snapshot/restore/verify JSON; run-* full console/control logs; post-run-*
  profile evidence. The original snapshot never becomes the output directory.

## Tests and open gaps

```text
python3 -B -I test_lifecycle.py
```

`offline-final-v2.log`: **49 tests**, all fake host/RPC/claims and temporary file trees,
including actual shared-runner dry-run integration, generated native launch
argv inspection, PID-reuse refusal and lost claim/release ACK recovery.
Journal unit/profile paths are checked against the owned token/config before
cleanup, including a planted foreign-unit tamper test.
Earlier `offline-1/2/3.log` drafts are retained. This is not native runtime
acceptance. No runtime up/down invocation is authorized by this development task.

Stale-policy follow-up base: `40fbe1d` (50 tests, including the real-ledger-shape
release-pairing regression). The parser itself is unchanged. Added offline
regressions cover old+clean, old+foreign-process, young and omitted-field
behavior, invalid configuration, fail-closed boundaries, and an in-memory
local/remote ledger dispatch round trip: **57 tests total**. No real ledger or
live profile is used. Native/SSH qualification of the new option remains UNRUN;
Primary review and commit are pending.

Open draft gaps: native Surface/Yoga systemd/Qt/AppImage/SSH integration is
UNRUN; no automatic remote file/socket forwarding or LDN frontend room setup;
only fresh isolated profile roots (not adoption of existing live runs); profile
symlinks/special files are refused rather than followed. Full profile hashing
includes immutable content and may be costly for large installations. Host
queue policy remains caller-owned. Crash-at-every-write fault injection and
independent source review are still needed before treating the draft as a
qualified replacement for existing live lifecycles. No push/tag/release/card
operation has been performed.

## Scoped playbook checklist

| Requirement | Tag | Disposition |
| --- | --- | --- |
| Claim/admission/one host or explicit pair | M | Fake success/refusal tests; native UNRUN. |
| Private copies/full restore/separate verify | M | Temp-tree bytes/modes/empty-dir/snapshot tests; live profile UNRUN. |
| Bounded gates/pre-Up shared dry-run/row ownership | M | Shared dry-run tested; row engine remains unchanged and separate. |
| Native health/evidence/cleanup | M | Full console path and explicit owned cleanup receipts implemented; actual native health UNRUN. |
| Short ruleset | M | Caller row/profile fixture responsibility; lifecycle never drives rules/input. |
| Commands/model/watcher | M | Bounded pwsh, Sol/high verified; one thread watcher, reaped before handoff. |
| Hook/NRO/build/online/card/release checks | M/C | N/A: tooling-only, no plugin/hook/NRO/device changes or native test. |
| Independent review/production READY | M | OPEN: working draft only; offline tests are not permission to operate hosts. |
