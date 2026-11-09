# Historical lifecycle qualification, November 2026

This records dated evidence at source commit `87b3de0`, not current host
state, permission to query/launch a host, or a production-READY claim.
Current contracts live in [LIFECYCLE](../LIFECYCLE.md),
[LINUX-DOWN](../LINUX-DOWN.md), [WINDOWS](../WINDOWS.md),
[HOSTS](../HOSTS.md) and [WATCHDOG](../WATCHDOG.md).
Old UNRUN/paused/owner statements below describe their own checkpoints;
later evidence supersedes them only for the exact path demonstrated.

Sources: `tools/eden-lifecycle/README.md` (R),
`tools/eden-lifecycle/ALLY-OFFLINE-2026-11-03.md` (A), and
`tools/eden-lifecycle/TEARDOWN-FIX-2026-11-03.md` (T), all at `87b3de0`.
Line ranges in this appendix refer to that original source, before migration.

## Draft and offline checkpoints

R:18–22 recorded a complete playbook read at
`c95d1f40255df6eca041eced2596d647c74dff5e`, plus pkg4 runner3 lifecycle,
lead lr03/runtime lifecycle, playbook gv/prep/launch/stop and shared runner
sources. Original files and `runner.py` were untouched. Phase2 was paused
at product `4814381`, with no-default/review/native acceptance pending and
no owned host claim/job. A:86–90 retained that paused product state.

R:423–466 preserved these distinct test checkpoints; counts are not added
together or presented as one present suite:

- `offline-final-v2.log`: **49 tests**, using fake host/RPC/claims and
  temporary trees. Coverage included actual shared-runner dry-run,
  generated native argv inspection, PID reuse, lost claim/release ACKs,
  token/config validation of cleanup paths and a planted foreign-unit
  tamper. `offline-1/2/3.log` drafts were retained. No native acceptance or
  runtime up/down authorization followed from these tests.
- Stale-policy follow-up base `40fbe1d`: **50 tests**, including a
  real-ledger-shape release-pairing regression, with the parser unchanged.
  Additional old+clean, old+foreign-process, young, omitted-field, invalid
  configuration and fail-closed tests plus an in-memory local/remote ledger
  round trip gave **57 tests**. No real ledger/live profile was used;
  native/SSH qualification of that option was UNRUN and primary review/commit
  pending at that checkpoint.
- A:12–20: **71 offline tests**, comprising 58 existing/shared tests
  (including deferred load) and 13 Windows tests. Python `-B -I` ran under
  the 256 MiB/60 s guard against temporary trees, fake RPC/claims and mocked
  task/pipe processes. The real `WindowsHost.launch` task-build failure and
  missing-Job `force_stop` paths ran through `Lifecycle.up` cleanup: no
  launcher/Eden spawned, private/source verify passed and only the fake owned
  claim released. This was not native Windows acceptance.
- A:87–90: **74 offline tests** after invalid-SID rejection before task
  creation, parsed task XML for spaced/Unicode/XML-sensitive Python paths,
  and HOLD invalid-pin/session/single-boot constraints. R:341–349 describes
  the same checkpoint as 16 Windows fake/command/bridge tests plus primary's
  concurrent deferred-load regression. Runtime `lifecycle.py`,
  `defer_load`, frontend-ready behavior, instance `game` and `runner.py`
  were unchanged from follow-up base `ceae08f`.
- R:278–280 and T:29–38: **89 offline tests**, 75 on `705cc21` plus
  14 teardown regressions, detailed under the Linux incident below.

The earlier R:442–466 draft limitations were native Surface/Yoga
systemd/Qt/AppImage/SSH integration UNRUN; no automatic remote file/socket
forwarding or LDN frontend room setup; fresh-root-only operation rather
than adoption of live runs; symlinks/special files refused; potentially
costly full-profile hashing including immutable content; caller-owned queue;
crash-at-every-write injection and independent review still needed.
Its scoped checklist described fake admission, temp-tree byte/mode/empty-dir
restore/verify, shared dry-run, owned cleanup receipts and console paths;
row execution and shortest-ruleset setup remained caller responsibilities.
It recorded bounded PowerShell, Sol/high verification and a reaped watcher;
hook/NRO/build/online/card/release were N/A for tooling-only work. Production
READY remained OPEN. Later native observations below do not erase the other
gaps or qualify unexercised paths.

R:282–339's Windows examples named source `%APPDATA%\eden`, separate
runtime `eden.exe`, native `C:\Eden\life-p6\{work,state}`, helpers
`C:\Eden\win\winjob.py`, `C:\Eden\scripts\Invoke-EdenControl.ps1`
and `C:\Program Files\Python312\pythonw.exe`. Its Linux controller
bridged token-bound ledger requests to `intpa@dreamland-ally` rather than
copying a ledger or Windows SSH credential. Those example locations and
Python312 selection do not establish current binaries or root availability.

## Earlier Ally attempts and offline-only follow-up, 2026-11-03

A:3–10 identifies branch `pkg6-ally-lifecycle`, base `ceae08f`, worktree
`/home/intpa/ssbu-mod-playbook-pkg6`. The Ally belonged to eden-tracing;
the follow-up authorized no later host/ledger query, claim, launch or proof.
It added the negative task-creation/rollback regression and corrected notes,
preserving primary's deferred load and shared runner. After moving the
worktree, no shared-clone file was edited.

A:22–26 recorded staged Eden candidate SHA-256
`45af318c93cbc00a2794421ee884ffbcb9dbec75bbdbdeef27af30d61cd2cbad`.
It identified an input, not an executed/qualified binary. Later campaigns
needed fresh executable, Python and helper pins.

- **19:59:29–20:00:46Z**, `C:\Eden\life-p6`: staging rejected a native
  backslash versus Qt forward-slash comparison before launch intent.
  Cleanup restored and separately verified the snapshot/source, then
  released. This proved native private cloning, Linux-authoritative ledger
  bridge and pre-launch rollback, not a boot. The path comparison was
  corrected to the same Qt-normalized paths that were persisted.
- **20:08:41–20:10:02Z**, `C:\Eden\life-p6b`: after a normal foreign
  release, Up claimed/staged but `schtasks /create` failed with
  `No mapping between account names and security IDs … UserId`.
  No `launcher.json` or owned-PID record existed. Down restored/verified
  **1,344 private snapshot entries** and unchanged **1,297 source entries**,
  then released its exact claim. This proved admitted pre-launch failure
  cleanup, not task startup, interactive Job membership, named-pipe RPC,
  CLI mute, game loading or running-Job termination. SID-derived task XML
  replaced unreliable SSH USERDOMAIN; the repair was then offline-tested only.
- Subsequent `life-p6c`: a foreign claim won atomic admission. Up refused
  before its own claim/launch; Down returned down/unclaimed. There was no
  claim override or foreign release. (A:28–36; R:351–361.)

Raw local receipts, captured before the offline-only order, were
`/tmp/opencode/pkg6-ally-live/attempt1-remote-evidence.json`,
`attempt2-remote-evidence.json`, `attempt1-proof.json`, `attempt2-proof.json`
and `proof.json`. Their SHA256-bound file payloads were evidence, rather
than shell success alone. Failed journals, pristine snapshots and post-run
private profiles were retained. No later remote cleanup/inspection was
performed under that offline-only follow-up. (A:38–45.)

At **20:09:20.6411325Z**, an authorized inbound Block rule was created
for `C:\Eden\life-p6b\work\profiles\profile-a\eden.exe`, named
`EdenLife-57b7c5b41c074151b3eaf70e92daae1e-a`. The **20:11:07Z** capture
showed enabled/inbound/Block. No Allow rule or prompt changed. This private
rule was intentionally retained, with no re-query after the offline-only
decision. The installed source verified unchanged;
`C:\Eden\tracing-1103` was untouched. (A:47–53.)

A:55–76 explicitly withheld qualification of SID task startup, full-width
Job HANDLE behavior, 8 GiB commit/256-process/kill-on-close, owned pipe,
mute/save/readback, terminal load/fresh frames and capped-stop/session-Job
termination. It also withheld pairs, native 1312 and transport-loss recovery,
gameplay, rows, input, media, performance and production READY. Primary
reviewed/cherry-picked the initial follow-up as `128dce8`; that was not
native qualification. No NRO/build, push/tag/release/card/DBI action or new
agent occurred, and no proof queue/job was left armed. Fresh paths/pins,
session/SID, foreign-process checks, then-40 GB/10 GiB gates and Block
policy were the later proof plan, not permission to run it.

## Later narrow Ally proof, 2026-11-03 21:27:09–21:29:34Z

R:363–386 records one authorized **no-input** proof after primary GO:
source `bd13a46`, runtime lifecycle identical to main except Linux-only
tracing; fresh `C:\Eden\life-p6-go01\{work,state}`; lifecycle token
`9b042b5a61da406d98dfc83dc9659ecc`.

Up passed strict claim/admission with no foreign native/WSL processes and
memory/disk gates. The current-SID InteractiveToken task started in session
1. Executed Eden identity was `a3a27006…`, build `063f1b80bb2c`, SSBU
13.0.5. CLI mute was persisted/read back before the only `game.load`.
At **21:28:38Z**, status showed running, `loading_complete`, no modal
and a fresh frame. Graceful stop hit `Deadline: terminal game.stop`.
At **21:29:02Z**, the exact session-scoped Job was terminated, and task
and process identities were absent before restore. Limits read back as
8 GiB Job commit, 256 processes and kill-on-close; peak commit **5.56 GB**.
Full restore then separate verify covered **1,347 private entries** and
unchanged **1,297 installed-source entries**, then release readback gave
`owned_release=true`, `open=[]`.

This superseded earlier UNRUN only for that narrow no-row lifecycle path.
Graceful shutdown, pairs, native 1312, transport-loss recovery, cap
exhaustion and runtime expiry remained unqualified. The 1312 once-before-
ledger retry was fault-tested offline and never natively triggered.

A:78–90 and R:388–422 preserve the earlier HOLD plan:
`ally-proof-HOLD.template.json` had unresolved pins, session zero, no GO
reference/observation timestamp and reserved `life-p6-go01` names without
asserting remote absence. The later proof used those names; the template
therefore cannot prove their freshness. It remained a deliberately
non-executable plan, with no queue/auto-start/availability watcher.
The post-GO plan required fresh helper/binary hashes, active session/SID,
hostname, game/source paths, root absence, firewall policy and GO timestamp;
an isolated no-row proof, fresh-process Down, exact PID/task absence,
post-run preservation, full restore/separate private-and-source verify,
own release and retained console/control/Job/task/firewall/ledger receipts.
Historical references to eden-tracing's turn, unqualified native startup
and 40 GB do not describe present ownership, late proof coverage or the
current 60 GB pre-Up floor.

## Linux evidence and teardown correction, 2026-11-03

R:3–13 records real Yoga `--ssh` single-instance runs **lr-04–lr-09**,
plus pkg4's Surface-local and Yoga-SSH pairs with `pair_authorized` and
`defer_load`. Each completed Up, rows, Down, full restore, separate verify
and release. lr-09 nevertheless exposed teardown SIGBUS and a LAN-helper
fault: cleanup success was not clean shutdown health. R/EDEN also recorded
30 s `game.stop` timeouts on **lr-02–lr-05** and pkg4's pair.

T:3–27 gives the correction worktree/branch above, rebased onto
`705cc21d1503648766663e78bd5d18c74a169efd`. Complete playbook reading at
that base was recorded; two equivalent Ally commits were skipped on rebase
because they were already cherry-picked. In lr-09, group-wide SIGTERM
deactivated the package FUSE mount at **21:33:30.552764Z** while Eden
PID **4042212** remained alive until SIGBUS. The combined console had a Go
companion instruction-fetch fault. Raw attribution:
`/tmp/opencode/pkg6-shutdown-sigbus/REPORT.md`; no plugin fix was implied.

The correction used/read back `KillMode=mixed`, main-only TERM/KILL and
explicit main-before-group order even for old control-group units and
partial launch. It preserved package pins, profile layout, limits and
full-cgroup ownership; extraction was not needed and would add space and
startup cost. Display and Windows cleanup paths were unchanged. Current
identity, bounded stop and fail-closed rules live in [LINUX-DOWN](../LINUX-DOWN.md).

The **89-test** checkpoint ran real Host stop methods on a synthetic
process graph with lower-numbered FUSE/helpers. It covered TERM success,
main-only KILL, stubborn main/claim retention, partial launch, PID reuse or
mismatch, old units, missing main, helper fallback, launch argv/readback,
unsafe stop-policy readback and stop → restore → separate verify → release.
The exact old `705cc21` force-stop method failed the main-before-group
counterexample. One intermediate test incorrectly inspected its pre-Down
dictionary after journal reload (`KeyError: verified`); the fixture was
fixed to read reloaded state without weakening production behavior.
Frozen source/validation/mutation receipts and scoped checklist were at
`/tmp/opencode/pkg6-teardown-fix/`. Only Linux Host implementation changed;
runner, Windows backend, deferred load/tracing and restore/verify/release
were preserved. (T:29–43; R:248–280.)

Independent review and live mount-lifetime/clean-log acceptance remained
pending primary. The lead's next post-merge Yoga batch Down was the planned
live check, not a new boot authorized by this task. No host/ledger query,
input, row, live-unit change or push/tag/release/card/DBI action occurred in
that correction task. Runtime hard-cap/crash interruption was not newly
qualified by offline tests. (T:44–48.)
