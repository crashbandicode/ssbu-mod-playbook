# Eden lifecycle — 2026-11-03

**Live status (2026-11-03):**
- **Linux:** proven on real runs. The Yoga over `--ssh` ran lr-04 to lr-09 (single
  instance); the Surface ran locally, and the Yoga over `--ssh`, ran pkg4's pairs
  (`pair_authorized` + `defer_load`). Each did up, rows, down, full restore,
  separate verify and release. lr-09 also exposed a teardown SIGBUS in Eden and
  its LAN helper; cleanup success was not clean shutdown health. The main-first
  correction below is offline-tested; its live check belongs to the next lead run.
- **Ally (Windows):** one no-input proof passed at 21:27–21:29Z: up, game loaded,
  down through the Job fallback, restore, separate verify and release (below).
  Graceful `game.stop`, pairs, native error 1312 and transport loss aren't
  qualified there yet.

Companion to `../eden-runner/runner.py`; the lifecycle never sends input, plays
macros or implements project rows.

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
For local Surface or XPS with the authoritative ledger on Yoga, set `ledger_ssh` to
that SSH host. Hosts are `surface`, `yoga`, `xps` (hostname `ubuntu-xps`) and
`ally`; the XPS runs one Eden at a time. Claims/releases run under a remote file lock, preserving raw
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

### Linux AppImage Down: main before FUSE (2026-11-03)

lr-09's old `KillMode=control-group` sent SIGTERM to Eden **and** its AppImage
FUSE daemon together. The package mount disappeared while Eden still executed
mapped files: its main PID exited with SIGBUS and helper stderr contained a Go
fault. This is lifecycle teardown failure, not an accepted plugin-log exception.

New guest units use and read back `KillMode=mixed`, `KillSignal=SIGTERM` and
`FinalKillSignal=SIGKILL`: systemd initially signals only the main process.
Down additionally enforces ordering itself, including for older journals/units:
after the capped CLI `game.stop`, read the exact unit's current MainPID, require
its birth identity in the owned cgroup and (when available) original launch
receipt, signal it through an identity-rechecked pidfd, and wait. Half of
`force_cap` is reserved for this main-only phase: SIGTERM gets its first half,
then SIGKILL targets **only that same main process** if necessary. Only after
the original main identity is gone may Down stop the unit and kill any remaining
owned members. Lower-numbered FUSE/helper PIDs never determine stop order.
Display units and the Windows Job backend keep their existing cleanup paths.

If main identity/ownership is ambiguous or the main process survives, no cgroup
stop or profile restore/release is permitted; retain the claim for recovery.
The forced-stop receipt records `main_pid_stopped` and `main_stop_signals`.
Full owned-process absence, post-run preservation, restore, **separate** verify
and exact release remain in that order. A stop timeout alone is not a clean-log
waiver: inspect SIGBUS/fatal/panic tails and unit exit status separately. No
AppImage extraction, binary-pin or profile-layout change is needed. Native
mount lifetime/clean-log confirmation is pending the lead's post-merge Down;
offline tests do not establish it.

The suite at this fix is **89 offline tests** (75 on base `705cc21`, plus 14
regressions), including main-only escalation, old control-group units,
lower-numbered FUSE/helper members, PID reuse, partial launch, unsafe policy
readback, retained-claim failure and stop → restore → separate verify → release.

## Ally / Windows backend (2026-11-03)

Use `lifecycle.py {up,down,status} config.json --ssh intpa@dreamland-ally`
(`--execute` for up/down). The cmd-default SSH host receives the explicit MSI
PowerShell7 executable with a UTF-16LE EncodedCommand, then Python312. The
Linux controller services only token-bound read/claim/release requests against
the configured authoritative ledger; no ledger copy or Windows SSH credential
is needed. Keep that controller running until Up/Down returns. If transport is
lost, use Down with the same config: its Windows journal and scheduled Job
remain recoverable, with a separate hard lifetime bound.

Windows config differences:

- `host: "ally"`, exact kernel `hostname`, and `interactive_session: 1` (verify
  the actual active session before each campaign).
- Disjoint native `work_root` / `state_dir`, e.g. `C:\Eden\life-p6\work` and
  `C:\Eden\life-p6\state`. Never use an installed or another worker's root.
- `source` is the read-only **user data** directory (e.g. the installed
  `%APPDATA%\eden`); `app` pins `eden.exe` in its separate runtime directory.
  The backend wraps the source in a fresh portable `<profile>\user`, links
  immutable game content/runtime dependencies, copies mutable data, and
  snapshots the entire private profile before staging. The executable runs
  from that copy, with isolated APPDATA/LOCALAPPDATA fallbacks. Down separately
  verifies both restored private profile and unchanged source inventory.
- `windows_job`, `windows_control`, `windows_python` are SHA256 pin objects
  for the reviewed `winjob.py` API, asynchronous `Invoke-EdenControl.ps1`, and
  console-less `pythonw.exe`. Known Ally paths are `C:\Eden\win\winjob.py`,
  `C:\Eden\scripts\Invoke-EdenControl.ps1`, and
  `C:\Program Files\Python312\pythonw.exe`; verify hashes, do not use old notes
  as binary identity. Helpers are read/imported, never changed. HANDLE argument
  prototypes are pinned at full pointer width when loading the older Job API.
- No Linux display/helper. A uniquely named interactive-token scheduled task
  owns the launcher; it survives SSH. Its disabled future trigger prevents an
  automatic second start, its action has an exclusive launch-once marker, and
  Task Scheduler adds an execution-time cap. The launcher joins a kill-on-close
  Job **before** creating Eden (the child inherits membership): 8GiB total Job
  **commit**, 256 processes, finite wall lifetime. Windows commit is not Linux
  RSS and there is no claim of disabling Windows paging.
- An enabled inbound Block rule for the private executable is required.
  `allow_inbound_block: true` explicitly permits creating only that token-named
  Block rule where operator policy authorizes it. The rule name/program/time
  receipt is retained as `run-*/firewall.json`; existing Allow rules are never
  modified. The Block remains associated with the retained private executable.
  No firewall prompt is answered or dismissed. Hosting peers needs separate
  authorization/policy because inbound Block prevents it.
- `lifecycle_only: true` is the explicit Windows no-row proof mode. It records
  `LIFECYCLE_ONLY` / **zero rows**, does not import or execute a runner, and
  authorizes no input. Otherwise the ordinary pinned shared-runner dry run is
  required. This field is not a bypass for product row acceptance.

Instances use `socket: "\\\\.\\pipe\\eden-life-<token>-<key>"` (unique per run),
plus `screenshot_target: "screen"`; paths are Windows-host paths. The shared
runner must use its named-pipe implementation on Windows; this change never
edits runner.py. A capped game.stop is still attempted first; the expected
Windows power-off hang then takes the exact `Session\<id>\<owned-job>` fallback.
No process-name kill is used. Recorded creation-time/image/session identities
and Job membership protect against PID reuse. Only the token-derived task is
ended/deleted, then all owned PIDs must be absent before restore/release.

Offline suite at the follow-up checkpoint: **74 tests**, including sixteen Windows
fake/command/bridge tests and Primary's concurrent deferred-load regression.
Native single-boot qualification is tracked separately in the Primary inbox;
pair mode and transport-loss recovery remain unqualified. SSH always uses a
non-login exec, no PTY and explicit `-NoProfile -NonInteractive` MSI PowerShell;
`CreateProcessAsUserW failed: 1312` is retried once only before any ledger
operation, never by replaying an admitted Up. That retry contract is fault-tested
offline, not natively triggered. Tasks use the current user's SID rather than
SSH's unreliable USERDOMAIN string. No alternate shell is substituted.

The follow-up branch starts at `ceae08f`; `lifecycle.py`, `defer_load`, its
frontend-ready behavior, the `game` field in instances.json and the original
deferred-load regression are unchanged. One added offline regression exercises
the actual Windows task-creation failure path through Up's cleanup, verifying
restore/source equality and own release without spawning a launcher or Eden.
The earlier 20:08–20:10 native attempt proved **pre-launch rollback only**:
Task Scheduler rejected UserId, no launcher receipt existed, and the full
1,344-entry private snapshot plus 1,297-entry source inventory verified before
release. It did not prove a game-loaded or Job-kill path. Exact observations,
remaining artifacts and the later-proof checklist are in
[ALLY-OFFLINE-2026-11-03.md](ALLY-OFFLINE-2026-11-03.md).

### Ally live proof: PASS (2026-11-03 21:27:09–21:29:34Z, pkg6)

One no-input proof after the primary's GO; source `bd13a46` (runtime
`lifecycle.py` identical to main's apart from Linux-only tracing), fresh
`C:\Eden\life-p6-go01\{work,state}`, lifecycle token
`9b042b5a61da406d98dfc83dc9659ecc`.
- **Up:** strict claim and admission (no foreign native/WSL processes, memory
  and disk gates). The current-SID InteractiveToken task started in session 1.
  Eden `a3a27006…` (build `063f1b80bb2c`, SSBU 13.0.5). Mute was persisted and
  read back through the CLI before the only `game.load`. Game loaded at
  21:28:38Z, running, `loading_complete`, no modal, fresh frame.
- **Down:** the graceful `game.stop` hit its cap (`Deadline: terminal
  game.stop`, the same as on Linux). The exact session-scoped Job was
  terminated at 21:29:02Z, and the task and its process identities were gone
  before restore. Job limits read back as 8 GiB commit, 256 processes and
  kill-on-close (peak commit 5.56 GB).
- **Restore and release:** full private restore, then separate verify (1,347
  private entries; the installed source's 1,297 entries unchanged), then
  release. The authoritative readback has `owned_release=true` and `open=[]`.
- **Not yet qualified on the Ally:** graceful shutdown, pairs, native error
  1312 handling, transport-loss recovery, cap exhaustion and runtime expiry.

The text below is the checklist that the proof followed; reuse it for the next
Ally qualification.

### Ally qualification checklist

**Each live Ally run needs the operator's grant and the Ally's normal
queue/ledger turn.** No host query, claim or launch is part of offline
preparation. The checked-in
`ally-proof-HOLD.template.json` reserves `C:\Eden\life-p6-go01\{work,state}` but
does not assert those paths are currently unused. Its pins and session are
deliberately invalid, so its draft cannot execute as delivered. It is a plan,
not a launcher or an authorization token; no queue worker is armed.

After GO, freshly observe and bind all executable/helper hashes, active session
and current user SID, hostname, game/source paths, root absence and firewall
policy before making an executable config from `draft_config`. Old hashes are
historical, **not re-pinned current identities**. Preserve the GO reference and
observation timestamp with that proof's source/config receipt. The source
already uses the SID-based task principal; offline tests also reject an invalid
SID before any task command and parse XML containing spaced/Unicode Python
paths. Native startup remains unqualified.

Obtain a new operator grant and the Ally's normal queue/ledger turn; it currently
belongs to eden-tracing. Do not reuse the failed `life-p6`, `life-p6b` or
`life-p6c` journals, change tracing-1103, or touch the installed profile in place.
Use fresh work/state paths, current binary/helper pins and the actual interactive
session/SID. Confirm no foreign native/WSL Eden/compiler, >=10GiB available and
>=40GB free, and the private executable's inbound Block policy. Then perform one
no-row up/game-loaded/down proof: record the named pipe, terminal load plus fresh
frame status, 8GiB/256-process/kill-on-close Job limits, and capped game.stop →
exact session Job fallback. A fresh Down process must leave no owned PID/task,
preserve the post-run profile, restore and separately verify the full private
snapshot and unchanged source inventory, and append only its own release.
Retain console/control logs, Job/task/firewall receipts and authoritative ledger
readback; compare the actually deployed source/binary hashes with the tested
candidate. Pair mode, native 1312 handling and transport-loss recovery need their
own later qualification; offline tests are not permission to run these now.

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
