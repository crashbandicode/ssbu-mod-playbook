# Linux AppImage shutdown

Apply this page to Linux lifecycle Down and fresh-process recovery, including
older journals. [LIFECYCLE](LIFECYCLE.md) owns restoration/release and
[HOSTS](HOSTS.md) owns admission. This is process ownership and mount lifetime
safety; cleanup success and healthy shutdown are separate acceptance checks.

**AppImage backing processes must outlive Eden's main process.** New guest
units set and read back `KillMode=mixed`, `KillSignal=SIGTERM`, and
`FinalKillSignal=SIGKILL`, so initial systemd TERM targets main only.
Down additionally enforces ordering explicitly rather than trusting timeout
behavior, numeric PID order or a unit's old stop policy:

1. Attempt bounded CLI `game.stop`; retain terminal failure/timeout evidence.
2. Read the exact owned unit's current MainPID. Require its birth identity
   in the owned cgroup and, when available, original launch receipt.
3. Through an identity-rechecked pidfd, TERM **only that main** and wait.
   Half of `force_cap` is reserved for this phase; TERM gets its first half,
   then KILL targets only the same main identity if needed.
4. Confirm that original main identity is gone **before** unit stop or any
   remaining owned member/backing-process signals. FUSE/helper PIDs sorting
   lower never determine the order.
5. Confirm full owned-process absence; preserve post-run evidence, restore,
   separately verify, then release the exact owned claim.

If identity/ownership is ambiguous, or main survives, retain the claim:
no cgroup stop, profile restore or release is permitted. Do not adopt a live
run, kill by process name, or leave backing processes outside owned cleanup.
Ordering needs no AppImage extraction, binary-pin or profile-layout change;
none substitutes for the required identity and stop order.
Display units and the Windows Job backend follow their own cleanup paths.
Retain `main_pid_stopped`, `main_stop_signals`, cgroup/launch identities,
native unit/mount timeline, complete console and final unit status.

`game.stop` timeouts can precede successful bounded cleanup, restore, verify
and release; they do not prove graceful stop and do not waive fatal logs.
Inspect SIGBUS/panic/fatal tails separately. Nonempty `cleanup_failures`,
unequal verify or an unreleased claim always fail cleanup.

The main-first correction was tested offline against synthetic graphs,
including lower-numbered FUSE/helpers, escalation, old units, partial launch,
PID reuse, ambiguous/stubborn main, unsafe readback and retained ownership.
The exact old `705cc21` method must fail the original main-before-group
counterexample. Offline tests do not prove native mount lifetime, clean logs
or hard-cap/crash interruption behavior. At the dated 2026-11-03 checkpoint,
the live check was assigned to the next lead batch's post-merge Down. That
historical assignment is not evidence of a present host state or later pass.
The dated incident, raw receipts and test bases/counts are preserved in
[qualification history](../tools/eden-lifecycle/README.md).
Implementation: [Host stop methods](../tools/eden-lifecycle/lifecycle.py).
