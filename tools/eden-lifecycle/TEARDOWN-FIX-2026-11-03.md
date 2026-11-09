# Linux AppImage teardown correction — 2026-11-03

Worktree `/home/intpa/ssbu-mod-playbook-pkg6`, branch `pkg6-ally-lifecycle`,
rebased onto main/playbook **705cc21d1503648766663e78bd5d18c74a169efd**.
README, every docs/ file and tools/PROVENANCE.md read at that base. Existing
Ally work was already cherry-picked; rebase skipped the two equivalent commits.

## Failure and contract

lr-09 used group-wide SIGTERM: its package FUSE mount deactivated at
21:33:30.552764Z while Eden's own PID4042212 remained alive until SIGBUS.
The combined console also held a Go companion instruction-fetch fault. Raw
attribution: `/tmp/opencode/pkg6-shutdown-sigbus/REPORT.md`; no plugin fix implied.

After a capped CLI stop, **main must be gone before group stop or backing-PID
signals**. Use the exact unit's MainPID, cgroup membership and launch birth
receipt, with the existing pidfd recheck. TERM then KILL target only main;
ambiguous identity or a surviving main blocks group stop and restore/release.
No new configuration/journal schema or saved PID role is introduced.

New units set/read back `KillMode=mixed`, TERM/9 final signals. Explicit ordered
Down also protects old control-group units and partial-launch recovery. The
installed systemd.kill(5) documents mixed's main-only initial TERM and remaining
group KILL after main exit/timeout; explicit ordering avoids depending on that
timeout for Down. No extraction is needed: keep existing package pins, profiles,
resource limits and full-cgroup cleanup instead of adding per-run extraction
space/startup costs or leaving FUSE/helper processes outside lifecycle ownership.

## Validation / scope

- **89 offline tests**: base75 +14 new. Real Host stop methods run against a
  synthetic process graph; FUSE/helper sort before main deliberately. Covers
  TERM success, main-only KILL escalation, stubborn main/claim retention,
  partial launch, PID reuse/mismatch, old units, missing main, helper fallback,
  launch argv/readback and stop → restore → SEPARATE verify → release.
- The regression is also challenged against the exact old force_stop method
  from705cc21; it must fail the main-before-group counterexample.
- One intermediate integration-test assertion used its pre-Down dictionary
  after Down reloaded the journal (`KeyError: verified`); fixed the fixture to
  inspect the reloaded controller state. No production behavior was weakened.
- Frozen source/validation/mutation receipts and scoped checklist:
  `/tmp/opencode/pkg6-teardown-fix/`. No runtime, runner.py or Windows backend
  edit beyond the Linux Host implementation; existing deferred-load/trace
  behavior and restore/verify/release implementations are preserved.
- **Independent review and live acceptance pending Primary.** No Eden boot,
  host/ledger query, input, row, live unit mutation, push/tag/release/card/DBI
  action in this task. Lead's next post-merge Yoga batch Down is the live check.
  Runtime hard-cap/crash interruption behavior is not newly qualified offline.
