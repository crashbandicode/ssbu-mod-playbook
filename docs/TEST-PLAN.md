# Plan the batch before Up

Read [START](START.md), [CHECKLIST](CHECKLIST.md), [HOSTS](HOSTS.md),
[PROFILES](PROFILES.md), [LIFECYCLE](LIFECYCLE.md) and this page for an Eden
run. [TEST-RUN](TEST-RUN.md) governs execution; [RUNNER](RUNNER.md) describes
schema and evidence limits. These paragraphs preserve test-run rules 1–15
from the former MACROS page; their homes are listed below.
Read [HOOK-CHAIN](HOOK-CHAIN.md) when the batch has sharing or stand-in rows.

1. **Runner ready before Up.** Use the existing shared
   `tools/eden-runner/runner.py`, the project's `rows.json` and optional
   `eden/steps.py`; never copy/fork the runner. One plan covers the whole row
   list. Each row names segments with SHA256, capped automated screen/log
   gates and its saved screenshots/logs/receipt. Offline dry-run checks files,
   hashes, gate loading and start/end chaining. Use actual shared `dry_run`,
   not a replacement validator. Review real states and evidence separately.
2. **Chain end states.** Order rows so each starts where the previous ends;
   restore between rows only when needed. Full profile restore happens once
   at Down. Name recovery/start gates where a failed row can be bypassed.
3. **Manual input only for a missing segment.** Record that bounded route,
   add its hash and return to automated rows. Native menu steering one input
   at a time is appropriate only when that menu itself is under test.
4. **Review row checkpoints.** The runner drives segments/checks gates;
   models review receipts/screens at row completion or a gate cap, not after
   every input. Scripted project-hook input is allowed inside logged/paced rows.
5. **First-settled pace.** If no row has finished 15 minutes after the first
   settled game screen, stop input and fix the runner. A compliant control
   log shows `macro.play`, not hundreds of hand-driven presses.
6. **Up deadline.** Up within 15 minutes of the order when segments exist,
   30 minutes when some need recording. Boot with dry-run-passing rows;
   remove failures and list them deferred with reasons. Add them back
   between rows in that same boot after their own dry-run passes. Before Up
   do only the required dry-run; extra offline suites, input-pin inventories
   and code review wait until after the run. Required safety admission stays.
   Evidence: runner path, dry-run receipt and log, order and Up times, and
   `macro.play` calls in `control.jsonl`.
7. **Keep what worked.** At Down commit project rows/hooks, segments and
   reviewed reference screenshots/crops with SHA256 into `eden/`, plus the
   shared runner's playbook commit. Do not vendor `runner.py`. Harvest proven
   routes and reuse them next time; [TEST-RUN](TEST-RUN.md) covers review.
8. **One boot covers every reachable open row.** Split only for a different
   build, plugin set, host/profile, or when fresh boot/first load is itself
   tested. Fix/defer a failing row and continue that same boot when reachable;
   recovery, summary and stop conditions are in [TEST-RUN](TEST-RUN.md).
   Cheap independent rows go first, wedge/crash/teardown/long matches last.
   Evidence: run labels against the row list.
9. **Short reports.** Per-run RESULTS <=20 lines, coordination <=5 lines;
   full compliance once per phase/candidate, with all manual dispositions.
10. **One entry.** Use `tools/eden-test/eden_test.py` on the executor, with
    fresh wrapper-created paths/config and validated pins; no hand-built
    caller scripts. Current flow and immutable-config rules are in TEST-RUN.
11. **Read actual game state.** Read roster indexes, fighter kinds and
    prefetch targets from settings/roster JSON, plugin state logs or tracing;
    never assume fixture indexes. Evidence waits for stable HUD or ready
    line, not merely first body-ready frame; body and HUD can disagree.
12. **Standing-goal workers.** Keep batching until each open row is closed
    or deferred with a reason. Persistent sessions, <=60-second polls,
    deadline enforcement and direct acceptance are in [WORKERS](WORKERS.md).
13. **Plan starting states before Up.** In priority order use a pinned
    fixture (config `instances[].files` with `to`, or settings-reload fixture),
    existing segment/library recipe, state-reading project hook, then one
    bounded recording that becomes a segment. Declare each in `setup`, e.g.
    `{"state":"Training, P1 Kirby, CPU Kirby","via":"fixture","ref":"files: TrainingRooms/settings.json"}`.
    `via` is `fixture`, `segment`, `hook` or `record`. Up requires setup;
    `--skip-setup-plan` is only for rerunning rows whose segments exist.
    Shape checks do not prove references exist or cover every row: review it.
14. **Same-frame chords.** Every chord button goes down in one poll frame.
    Record/reuse a macro with events sharing that frame; sequential control
    calls do not guarantee it. See [MACROS](MACROS.md) and [TILE-CURSOR](TILE-CURSOR.md).
15. **Pass on behavior.** Gate the tested event via trace argument/return,
    log, HUD number or an event frame (hit/death/respawn). Applying a fixture
    or recording a clip proves setup only; bare-True hooks need evidence too.

Set batch `runtime` deliberately: default 5400 seconds, maximum 10800;
batch boots use 5400 or more. A cloned config retains its old cap, including
short GIF caps. Give each row `budget_min`; sum rows + about 10 minutes for
boot/navigation must fit below runtime minus the 5-minute pace margin.
Route building consumes this budget. Plan excess rows into another boot
before Up, instead of hitting the hard cap mid-batch.

Use a named shortest ruleset in the private profile: **1 stock, 1:00,
items/hazards/FS meter off**, Final Destination or Battlefield. Save it in
the copy, never profile `a`, and pick by name each time. End deliberately
with one-stock walk-off when suitable. A longer match needs a test-specific
reason in the handoff. Retain ruleset screenshot or logged match length.
Hazards are under the rules editor's collapsed **Advanced** row.
The caller owns this fixture: the lifecycle does not drive rule menus.

Reuse the shared navigation index before building anything. Generic runner
needs get a shared test; plugin navigation/checks belong in project hooks.
Change shared tools **in a worktree**, never the clone used by live runs.
Current dry-run/setup mechanics: [runner.py](../tools/eden-runner/runner.py),
[eden_test.py](../tools/eden-test/eden_test.py). Examples/incidents remain in
[route history](history/ROUTES-AND-MEDIA-2026-10--11.md).
