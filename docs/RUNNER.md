# Shared runner reference

Use [TEST-PLAN](TEST-PLAN.md) for policy, [TEST-RUN](TEST-RUN.md) for execution,
[MACROS](MACROS.md) for recording/editing and [GIFS](GIFS.md) for media.
The current [runner source/help](../tools/eden-runner/runner.py) and
[tests](../tools/eden-runner/test_runner.py) define field mechanics.

```text
python3 -I tools/eden-runner/runner.py dry-run eden/rows.json
python3 -I tools/eden-runner/runner.py run eden/rows.json --instances <state_dir>/instances.json
```

Paths in rows are relative to that plan's folder. The lifecycle writes the
plain a/b instance mapping: endpoint, profile, game, console/control logs,
settings, diag/detector/tile logs and declared source aliases. Steps select
`on:<key>`; default is the first instance. Gate sources can omit `_log`.
Unix runner client selection currently uses the `current/tools` alias; record
actual client identity separately from the pinned AppImage. Windows uses its
named-pipe client and executor-host paths, as [WINDOWS](WINDOWS.md) requires.

| Step | Meaning |
| --- | --- |
| `play`, `library` | Play hashed segment/shared index entry, await finished/empty error, reset input. |
| `gate`, `shot`, `mark` | Check a named gate; save a screenshot; mark a before-frame for change checks. |
| `copy`, `json_diff` | Save a source file; compare with prior copy using allowed/must-change/must-keep. |
| `run` | Project `module:function` hook with args, optional `check:true`. |
| `ui`, `call` | Reviewed frontend action or allowlisted control method. |
| `wait_ms` | Fixed wait only where no log/screen gate exists. |
| `gif` | Record nested steps with reviewed preset/output; see GIFS. |

Each step has exactly one kind; generic additions register in
`Runner.step_handlers` and need shared tests. Plugin specifics belong in
`eden/steps.py`. StepContext supplies call/press/stick/play/gate/ui/shot/
wait_log/log, instance and row directory. `check:true` means the hook verifies
its final screen and can establish the next start check. Scripted hook inputs
are allowed inside logged/paced rows; model-driven input-by-input tests are not.

Every play needs a **real same-instance start check since the previous play**
or its segment's `start_gate`. Code also accepts `unchecked:true`, and library
play checks only declared plan gates; those flags alone do not prove the state.
Verify and retain actual scene evidence before each segment. Dry-run validates declared
hashes/game IDs/events, caps/references, hooks, start/end strings and failure/
GIF shapes; it cannot prove live route, setup coverage, behavior or review.

Missing segment + recipe is READY_TO_RECORD: first play records recipe,
resets input, stops macro and stores SHA256 back in rows. Every later use
plays that file. Failed recording deletes partial output, leaves no hash and
fails the row; playing an unhashed file also fails explicitly. Recipe buttons,
stick and `with_stick` holds can use `frames`/`gap_frames` for game timing.
Input leases are **1..10000 ms**; long wall/frame holds renew before expiry.
New input replaces expiry; `with_stick` adds no margin above 10000.

Gates are capped `log`, `json`, `image`, `hook`, `change`, `ui` or `status`.
Prefer row-written log lines, then machine-readable values. `change` with
`stable:true` waits for a changed HUD to settle after a `mark`. Multi-instance
log gates can use `on_all`, `equal` and `expect_groups` for matching frames.
Status gates can require running/loading_complete and fresh frame age.
UI dict targets poll the tree until exactly one widget matches (or explicit
index); operations include trigger/click/set/key/select/items/context/close.
Room setup precedes game load: lifecycle `defer_load`, frontend UI, then
allowlisted `game.load` with `@game`, terminal-job cap and fresh-frame gate.

Image gates use non-animating ROIs, default mean difference **8 per color
byte**. Tolerance 0 requires `static:true`; the old source-header sample omits
it and is not a valid exact-match recipe. `scale:"fit"` on gate/plan opts in
to same-aspect live-frame scaling to reference dimensions; ROI stays in
reference coordinates. ROI-only references need `reference_is_roi` plus
`frame:[width,height]`. Half-size crop comparison is narrower: ROI reference,
declared frame, fit enabled and supplied tolerance, after excessive diff.
Receipts disclose scaling; tolerances stay as written. Capture references at
run resolution when possible. A hook returning bare `True` can tool-PASS with
a warning; return line/count/dict and retain behavior evidence instead.

`acquire:true` with missing image reference captures its settled region,
stores hash and stops after the row as REVIEW (exit 4). Inspect references
before running again. A gate `recover:<segment>` handles a known detour once
by default (`recover_times`), then rechecks. Give the first gate of a run and
the first gate after any pause or long hold a `recover` naming the intro-cancel
segment (library `attract-cancel`), because title idle can start the attract movie.
Row/batch recovery and policy remain in TEST-RUN.

`replay rows.json --fixture <dir>` replays one passing row's gates/hooks
offline, no Eden/play steps. `capture_fixtures`/`--capture-fixtures` or
`capture-fixture` saves `out/fixtures/<row>/fixture.json` plus screen/source
files. `self-test` checks each declared start gate and each `screen_classes`
reach/gate/recover/start cycle; classes/routes are plan data. Default
`early_stop=3` repeated same-cause failures stops the run with `probable shared cause`;
false disables it. Neither mode proves plugin behavior on unexercised paths.

Failure saves screenshot, relevant log tails and trace counters/read when
tracing; `failure_evidence`/`on_failure_hook` add evidence. A `STOP` file in the
run's `out/` (a coordinator may create it) halts the runner before its next
input; failed recovery/crash writes STOP-INPUT. No auto-reboot. Every
call enters `out/actions.jsonl`; row receipt `out/<row>.json`, artifacts
`out/<row>/`, summary every row. Runner nonzero signals failed rows, but wrapper
exit/pace limitations and required human inspection remain in TEST-RUN.
Harvest preserves reviewed hashes/start/end provenance; stale historical
reference-status labels do not override present file/hash validation.
