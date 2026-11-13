# Ally backend: offline handoff — 2026-11-03

Branch `pkg6-ally-lifecycle`, base `ceae08f`, worktree
`/home/intpa/ssbu-mod-playbook-pkg6` (both gone by 2026-11-13; the work is on main as `128dce8` and `0d706a5`).
The scope then was offline only: the Ally
belonged to eden-tracing. No later Ally/ledger query, claim, launch or proof was
authorized then. Runtime code is unchanged from the base; this follow-up adds a
negative task-creation/rollback regression and corrects qualification notes.
Primary's `defer_load`, frontend-ready branch, instance `game` path and its test
remain intact. `runner.py` is unchanged. After the worktree move, no shared-clone
file is edited.

## Offline evidence

`test_lifecycle.py`: **71 tests** (58 existing/shared tests, including deferred
load, plus 13 Windows tests). Run with Python `-B -I`, under the normal 256MiB /
60s guard. Tests use temporary trees, fake RPCs/claims, and mocked task/pipe
processes. The new regression runs the real `WindowsHost.launch` task-building
failure and `force_stop` missing-Job path through `Lifecycle.up` cleanup. It
checks that no launcher/Eden is spawned, all private/source verification passes
and only the fake owned claim is released. A passing suite is not native
Windows acceptance.

The last native candidate used Eden SHA256
`45af318c93cbc00a2794421ee884ffbcb9dbec75bbdbdeef27af30d61cd2cbad`.
This identifies a staged input, **not an executed/qualified Eden**. Later
operators must re-pin their actual executable, Python and helper files.

## What the earlier authorized attempts established

| Attempt (UTC) | Observation | What it establishes |
| --- | --- | --- |
| 19:59:29–20:00:46, `C:\Eden\life-p6` | Staging rejected a native-backslash versus Qt-forward-slash check, before launch intent. Cleanup restored and separately verified the snapshot/source, then released. | Native private cloning, Linux-authoritative ledger bridge and pre-launch rollback; no Eden boot. The path check now uses the same Qt-normalized paths it persists. |
| **20:08:41–20:10:02**, `C:\Eden\life-p6b` | After normal foreign release, Up claimed and staged; `schtasks /create` failed with **No mapping between account names and security IDs … UserId**. No launcher.json or owned-PID record was created. Down restored and separately verified **1,344 snapshot entries**, plus unchanged **1,297 source entries**, and released its exact claim. | A real admitted pre-launch failure cleans up. It does **not** establish task startup, interactive Job membership, named-pipe RPC, CLI mute, game loading or a running-Job kill. Task XML now derives the current user SID instead of SSH USERDOMAIN; that repair was then offline-tested only (the 21:27Z proof in the README later started the SID-based task natively). |
| Subsequent `life-p6c` attempt | Another foreign claim won the atomic ledger admission. Up refused before its own claim or launch; Down reported down/unclaimed. | Foreign ownership still blocks. No claim override or foreign release occurred. |

Private raw receipts already preserved locally before the offline-only order:
`/tmp/opencode/pkg6-ally-live/attempt1-remote-evidence.json`,
`attempt2-remote-evidence.json`, `attempt1-proof.json`, `attempt2-proof.json`,
and `proof.json` (by 2026-11-13 moved to
`/home/intpa/smash-training-lobby/tmp-archive-20261113/tmp/opencode/pkg6-ally-live/`). The evidence JSON contains SHA256-bound file payloads, not an
inference from shell success. The failed Windows journals, pristine snapshots
and post-run private profiles were retained; there was no subsequent remote
cleanup or inspection after the offline-only order. On 2026-11-13 `C:\Eden\life-p6`
and `C:\Eden\life-p6b` are empty, and `C:\Eden\life-p6c\state` holds only its journal (phase `down`).

At 20:09:20.6411325Z an authorized inbound **Block** rule was created for
`C:\Eden\life-p6b\work\profiles\profile-a\eden.exe`, named
`EdenLife-57b7c5b41c074151b3eaf70e92daae1e-a`. It was observed enabled/inbound/Block
in the captured 20:11:07Z receipt. No Allow rule or prompt was changed. This
private-path Block was intentionally retained; its state was **not**
re-queried after the offline-only decision. Re-queried read-only on 2026-11-13, it is still
enabled/inbound/Block, and the executable it names no longer exists. The installed source inventory
verified unchanged. `C:\Eden\tracing-1103` was not modified.

## Still open / later proof

**Not qualified:** native SID-based task startup, Job handle/prototype behavior,
8GiB commit/256-process/kill-on-close enforcement, the owned named pipe and CLI
mute/save/readback, terminal game load plus fresh frames, capped game.stop and
actual session-scoped Job termination. Pair mode, native 1312 recovery and
transport-loss continuation are also unqualified. No gameplay, rows, input,
media, performance or production-READY claim is made. The initial offline
follow-up was reviewed/cherry-picked by Primary as `128dce8`; native
qualification was then open. The 21:27–21:29Z proof later that day (README "Ally live proof: PASS")
covered the task startup, Job limit readback, named pipe and CLI mute, game load with a fresh frame,
capped game.stop and session Job termination; the README lists what it did not cover.

After a new operator grant and the normal queue/claim turn, use entirely fresh
work/state paths (do not recycle the three failed journals). Revalidate the
interactive session/SID, executable/helper hashes, disk >=60GB (40GB before 2026-11-09), available
memory >=10GiB, absence of foreign native/WSL Eden/compiler, and the private
executable's inbound Block policy. Perform one no-input Up/game-loaded/Down
proof using the shared lifecycle. Retain actual source/binary hashes,
task/Job/named-pipe receipts, muted-before-load evidence, fresh frame status,
console/control logs, exact owned PID disappearance, full restore **and a
separate verification**, unchanged source inventory, and own release readback.
Only then can that narrow native lifecycle path be accepted.

No NRO/build, push, tag, release, Switch/card/DBI operation or new agent occurred.
There is no retained proof queue/job; only the model watcher is reaped as part
of the final local handoff. Phase2 product work was then paused at `4814381`.

## Proof preparation while on HOLD

No new Ally observation or re-pin had occurred then. `ally-proof-HOLD.template.json`
is a deliberately non-executable plan for one later no-input proof; it has
unresolved pins, session zero, no GO reference or observation timestamp, and
reserves new `life-p6-go01` work/state names without asserting remote absence
(the 21:27Z proof then used them).
All identities, paths and policy must be freshly verified only after Primary's
explicit GO. No queue, auto-start or availability watcher is armed.

The suite then had **74 offline tests**: the original 71 plus fail-closed SID
rejection, parsed task XML with spaced/Unicode/XML-sensitive Python paths, and
the HOLD template's invalid-pin/session and single-boot constraints. Runtime
`lifecycle.py`, `defer_load` and `runner.py` were then unchanged from `ceae08f`;
`lifecycle.py` and `runner.py` have changed since, and on 2026-11-13 the suite has 143 tests.
