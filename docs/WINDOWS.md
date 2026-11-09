# Native Windows lifecycle

This page adds to [HOSTS](HOSTS.md), [PROFILES](PROFILES.md) and
[LIFECYCLE](LIFECYCLE.md). Every live Ally campaign needs the operator's grant
and normal queue/ledger turn. Offline preparation includes **no host query,
claim or launch**. Fresh identity observations follow GO; old pins, paths,
session numbers and HOLD plans confer no current authority.

Use `lifecycle.py {up,down,status} config.json --ssh intpa@dreamland-ally`
(`--execute` for Up/Down). SSH uses non-login exec, no PTY and explicit
`C:\Program Files\PowerShell\7\pwsh.exe -NoLogo -NoProfile -NonInteractive`;
the cmd-default host receives UTF-16LE EncodedCommand, then Python312.
No WindowsApps, Windows PowerShell 5.1 or alternate shell. A
`CreateProcessAsUserW failed: 1312` launch failure is retried once through
that same MSI executable with shell login disabled, **only before any ledger
operation**. Never replay an admitted Up. The retry was fault-tested offline;
native triggering remains unqualified.

The Linux controller services only token-bound read/claim/release requests
against the authoritative ledger, with no ledger copy or Windows SSH
credential needed. Keep it alive until Up/Down returns. Transport loss leaves
the Windows journal/task/Job recoverable and independently lifetime-bounded;
recover with Down using the same config, not a fresh Up.

After GO, verify/bind `host:"ally"`, actual kernel hostname, active interactive
session, current user's SID, executable/Python/helper hashes, game/source
paths, absent fresh roots, resource gates and firewall policy. Preserve the GO
reference, observation time and source/config/binary identities in the proof.
Use new disjoint native work/state roots. Never use installed/foreign roots
or reuse failed or previously successful proof journals. The checked-in
`ally-proof-HOLD.template.json` deliberately has invalid pins/session and is
historical planning evidence; its `life-p6-go01` names have already been used.
Do not change that sibling file merely to modernize prose: tests depend on it.

`source` is the read-only user-data directory, separate from the runtime
directory containing pinned `eden.exe`. A fresh portable `<profile>\user`
links immutable content/runtime dependencies, copies mutable data and
snapshots the whole private profile before staging. Eden executes from that
copy with private APPDATA/LOCALAPPDATA fallbacks. Down separately verifies
the restored private snapshot **and unchanged installed source inventory**.

Pin `windows_job` (`winjob.py`), asynchronous `windows_control`
(`Invoke-EdenControl.ps1`) and `windows_python` (`pythonw.exe`) by SHA256.
Known historical locations are `C:\Eden\win\winjob.py`,
`C:\Eden\scripts\Invoke-EdenControl.ps1` and
`C:\Program Files\Python312\pythonw.exe`; freshly verify them. Helpers are
read/imported, never changed. Loading the older Job API pins HANDLE argument
prototypes at full pointer width. No Linux display/helper is used.

A unique InteractiveToken scheduled task owns the launcher and survives SSH.
Use the current user's SID, not unreliable SSH USERDOMAIN. A disabled future
trigger and exclusive launch-once marker prevent a second start; Task
Scheduler adds its execution-time cap. The launcher joins the kill-on-close
Job **before** creating Eden, so the child inherits membership: 8 GiB total
Job **commit**, 256 processes and finite wall lifetime. Commit is not Linux
RSS; this does not disable Windows paging. Retain task/Job limit readbacks.

Require an enabled **inbound Block** rule for the private executable.
`allow_inbound_block:true` permits creation of only that token-named Block
where operator policy authorizes it. Retain rule name/program/time in
`run-*/firewall.json`. Existing Allow rules are never modified; no firewall
prompt is answered or dismissed. The Block stays scoped to that private
executable path; pruning grants no rule changes. Hosting peers needs separate
authorization/policy because this
Block prevents inbound hosting.

`lifecycle_only:true` is explicit no-row proof mode: receipt says
`LIFECYCLE_ONLY`, **zero rows**, runner is neither imported nor executed,
and no input is authorized. It cannot satisfy product-row acceptance.
Otherwise use the normal pinned shared-runner dry run. Windows instances
declare a unique `\\.\pipe\eden-life-<token>-<key>` control endpoint and
`screenshot_target:"screen"`. The runner must use its named-pipe backend;
all instance paths remain Windows-host paths.

Attempt capped terminal `game.stop` first. If it hangs, terminate only the
exact `Session\<id>\<owned-job>`. Recorded creation-time/image/session
identities and Job membership protect against PID reuse; no process-name
kill. End/delete only the token-derived task. Confirm all owned PIDs/tasks
absent **before** post-run preservation, full restore, separate private/source
verification and exact owned release. Keep console/control logs, pipe,
task/Job/firewall receipts and authoritative ledger readback. Cleanup and
graceful shutdown health remain separate.

Qualification is dated, not a claim about present host availability:

| Path | Observed status |
| --- | --- |
| Pre-launch failure/rollback | Native attempts preserved full snapshot/source and own release; no game boot was proven. |
| One no-input Up/load/Down | Passed 2026-11-03 21:27–21:29Z: session task, mute/readback, terminal load/fresh frame, exact Job fallback, restore/separate verify/release. |
| Graceful stop, pair mode | Not qualified by that proof. |
| Native 1312, transport loss, cap exhaustion/runtime expiry | Still require their own native qualification. |

A later narrow proof uses fresh identities/roots after a new grant, no
foreign native/WSL Eden/compiler, >=10 GiB available and current pre-Up disk
floor. Record terminal load plus fresh frame, limits and capped-stop → exact
Job fallback. **A fresh Down process** proves no owned PID/task, post-run
preservation, private/source equality and own release. Compare deployed
source/binary hashes with the tested candidate. Offline suites qualify no
additional live paths. Exact bases, counts, attempts, raw paths and HOLD
scope remain in [qualification history](../tools/eden-lifecycle/README.md).
Implementation: [WindowsHost and SSH bridge](../tools/eden-lifecycle/lifecycle.py).
