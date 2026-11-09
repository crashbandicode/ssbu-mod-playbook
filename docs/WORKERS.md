# Persistent workers and coordinator acceptance

Apply this page when several workers or Eden hosts participate. Bootstrap is
per worker: [START](START.md), [CHECKLIST](CHECKLIST.md) and task concern pages,
the recorded playbook commit, used scripts and the worker's own checklist.
Routing/model/effort and watcher rules come from
`/home/intpa/.codex/DELEGATION_ROUTING.md`, not a duplicate local model table.

1. Give each Eden host one **persistent worker** with a standing goal: keep
   closing assigned rows until each is PASS, FAIL or BLOCKED with a reason.
   Send follow-ups/new rows to that same session. Coding follow-ups return
   to the original author. Keep primary and workers for the campaign.
2. **Ignore context entirely.** No agent watches, reports, measures or acts
   on its own or another session's context; none compacts, recycles, replaces
   or hands off because of it. Tools compact automatically. Context belongs
   to the operator; ignore broker `contextPercent`.
3. Keep a handoff current at row checkpoints: completed/open/deferred rows,
   routes/segments/fixtures, branch/commit, evidence and live claims, run
   folder, pinned build hashes and exact next commands. No secrets.
4. Every brief **ends in a done-checklist**. Every item receives PASS, FAIL
   or BLOCKED and exact reason. End only when all items are disposed or the
   real time-box is spent. Do not end while a build/run is in progress or
   merely while waiting for a busy host; poll in <=60-second steps. Continue
   polling `eden_test.py pace` and runner logs until completion, and act on STOP.
5. Name the actual navigation kit in every Eden brief: tile cursor plugin,
   shared runner segment library, project's hook sets and settings fixtures.
   A generic playbook link is insufficient. `lstick` in a button list means
   **L3 click**, not steering; same-frame chord policy still applies.
6. Commit a working chord macro, route, fixture or hook to the project's
   shared library immediately (for example `eden/lr*-shared`), with a one-line index. Workers read/reuse that
   index first; harvest reviewed proven routes to the shared runner library.
7. The coordinator enforces Up in **15 minutes**, or **30** when segments need
   recording. At the deadline interrupt/redirect: Up now with rows needing
   no new route, record missing routes in that boot, mark unstaged rows BLOCKED.
   [TEST-PLAN](TEST-PLAN.md) owns full grouping/setup/budget requirements.
8. Watch for stalls with these exact triggers: direct input outside runner;
   provider/turn error **and no worker activity for the following 2 minutes**;
   worker quiet **12 minutes without updating its report**. Other transient
   blips need no intervention. Ordinary provider retries can recover themselves.
9. Hosts take turns under [HOSTS](HOSTS.md): Surface/Yoga/XPS two instances,
   Ally one; manual/foreign claims still block. Builders wait while Eden
   workers hold their Up/Down lock; Up refuses compilers. Gate a new head into
   a **new output folder**, preserving running workers' pinned paths/hashes.
10. The coordinator accepts only evidence it personally reviews: frames and
    log lines, plus decoded expected/stock source comparisons for asset claims.
    A worker's visual PASS requests review; it does not close that criterion.
    Behavior gates, not fixture/clip setup, determine row acceptance.

Keep coordination posts <=5 lines: label, changed row states and next action,
with paths to receipts. Per-run RESULTS <=20 lines; phase compliance resolves
every manual row. Preserve session-to-host mapping and route-library commits
as evidence of this coordination contract. Do not infer native acceptance
from a worker narrative, wrapper exit 0 or offline tests.

Incidents motivating persistence, navigation reuse, chord handling and pacing
are retained in [host history](../history/PLUGIN-AND-HOST-INCIDENTS-2026-10--11.md)
and [route history](../history/ROUTES-AND-MEDIA-2026-10--11.md).
