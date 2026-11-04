# Input routes and macros

2026-10-31. Two ways to drive the game to a screen. The plugin tests here
used **scripted routes**: `gv.py`/`drive.py` calls with sleeps in a short
PowerShell script. Eden also records and replays **input macros**
(`.edenmacro.json`). The best of the two is the hybrid described under
"Eden input macros": a recorded macro for each segment of the route,
replayed at game pace, with a log line or screenshot check between segments.
Use a plain scripted route while you are still building a route or when a
segment is one or two presses, and one long macro only for a sequence that
needs exact human timing. A test run never drives by hand: it plays a runner
that was ready before `Up` ("Test runs: the runner is ready before Up",
below).

## Scripted routes

A route is presses and stick pushes, each followed by a wait long enough for
the next screen to load. `tools/eden-solo/to-css.ps1` goes from the title
screen to the Quickplay character select, with the hand on a fighter:

```powershell
P a; Start-Sleep 4; P a; Start-Sleep 6                                     # title, main menu
S 0 -1 250; Start-Sleep -Milliseconds 600; S 1 0 250; Start-Sleep 1        # down, right: Online
P a; Start-Sleep 12   # Online
P a; Start-Sleep 12   # Smash
P a; Start-Sleep 15   # Quickplay
P a; Start-Sleep 15   # Solo
P 'lstick,up' 150; Start-Sleep 2                                           # tile cursor: onto a fighter
```

`P` is `gv.py press` and `S` is `gv.py stick`. `-BootSeconds` waits for boot
first. `tools/eden-pair/nav.ps1` is the same route on both instances of a
pair. The fixed sleeps in these two are the old form, kept for the route's
shape; the rule under "Rules learned" is to gate each wait on a log line or
a screenshot check and sleep a fixed time only where neither exists.

### Building a new route

This is for a segment nothing reaches yet. Build it once, record it and add
it to the runner. A test run does not proceed this way ("Test runs", below).

1. Send one segment at a time with `step.ps1`, then look at the screenshot it
   takes:
   ```text
   step.ps1 -Label e19 -Name online -Steps 'b:a:4000,b:a:6000,s:0:-1:250:600,s:1:0:250:1000'
   ```
   The step forms are `b:<button>[+<button>][:wait]`, `s:<x>:<y>:<ms>[:wait]`
   and `w:<ms>`. `b:lstick+up` is the tile cursor's L3 + Up.
2. When a segment lands where it should, add it to the route script with the
   same waits. Round each wait up, because Eden runs slower with more plugins
   and while it records, or replace the wait with a screenshot check (below).
3. End the script with a screenshot (`gv.py shot`), and check that screenshot
   before you trust anything after it.

### Rules learned

- **Menus:** short 250 ms stick pushes move one item at a time.
- **Character and stage select:** use the tile cursor
  ([TILE-CURSOR](TILE-CURSOR.md)). The stick overshoots, and the D-pad moves
  the hand only a little.
- **Held inputs are leases.** `gv.py press` holds for its time plus 500 ms,
  then releases. `call eNN input.reset` releases everything.
- **Idle players get dropped.** The game drops a player after about 30 s
  without input, and walking off the stage counts as a self-destruct.
  `gv.py keepalive eNN <stop-file> <seconds>` holds down and taps ZL every
  0.7 s until the stop file appears. `match.ps1` and
  `tools/eden-pair/chain.ps1` (several matches, answering each pop-up) are
  built on it.
- **Wait for a log line, not a fixed time, wherever one exists.** Online
  pairing took anywhere from 2 s to 125 s. In elite-rematch's run e25, a
  press timed by sleeps landed with 3 s left on a pop-up's countdown, and the
  rematch then failed with a communication error. `chain.ps1 -DetectStart`
  waits for the detector's next `session slot=` line, and `-Watch s,y` waits
  for the plugin's diag pop-up line. Keep fixed waits only where neither a
  log line nor a screenshot check exists (shipped builds that log no
  events: `-PopupWait`).
- **Gate menu waits on a screenshot check.** The fixed 12 to 15 s per menu
  in `to-css.ps1` adds about 70 s to a route and still fails when a screen
  is late. Instead, take a screenshot every second or two, test a cheap
  marker, and move on as soon as it appears, with a cap.
  `tools/eden-solo/examples/css_check.py` is such a marker: the fraction of
  near-black pixels in the P1 panel tells Game & Watch from any other
  fighter (0.35 against 0.05 for Meta Knight), and the runner pressed A
  only once it said so. Retry the screenshot itself when it fails: the
  first one after a load can find no render window yet.
- **One driver per instance at a time.**

## Eden input macros

Eden's own guide is `docs/InputMacros.md` in the Eden build, with a tutorial,
`InputMacrosTutorial.md`, at
`/home/intpa/eden-android-timer-build/stage/docs/user/`. In short:

- **Record:** use **Tools → Input Macros…** (*Record new*; a 3-2-1 countdown;
  red means recording), or use the CLI: `macro record "<name>"`, then
  `macro marker --name "<point>"`, then `macro stop`.
- **Branch:** *Record from marker* replays the prefix (shown in blue) up to a
  marker, then records a new ending. The branch is a new file and the
  original is unchanged.
- **Timing:** macros count displayed emulation frames, not wall time, so a
  slow game replays them at the same game pace. They don't restore
  savestates. Loading times, random behaviour and changed menus can still
  change the outcome, so start from the same scene.
- **Play** from an explicit file, which needs no library entry, and wait for
  it to finish:
  ```text
  gv.py call e19 macro.play '{"path":"/abs/route.edenmacro.json"}'
  gv.py call e19 macro.status        # repeat until state is finished; check error
  gv.py call e19 macro.stop
  gv.py call e19 input.reset
  ```
  While a macro plays, any other input call is rejected with `input_busy`.
- **File format:** JSON with `name`, `game_id`, `player`, `duration_frames`,
  `events`, `markers`, `bindings`, `parent`, `format` and `version`.
- **Examples:** earlier automation saved many of them under
  `/home/intpa/automation01/`. For example,
  `run60/main-to-training-fast01.edenmacro.json` goes from the main menu to
  Training.
- **Receipts:** the training-lobby drivers (for example
  `/home/intpa/training-lobby-preload169/drive.py`) save each played file's
  sha256 with the run. Do the same.

### Why they are fast, and where they break

A macro replays at game pace: the route to the character select that
`to-css.ps1` walks with about 70 s of sleeps takes a macro only as long as
the game itself needs. But a macro cannot wait. Its frames count on, so when
a load takes longer than it did while recording, the next press lands on the
wrong screen; Eden's own doc lists loading times and performance among the
things that change a replay's outcome. One macro for a whole route is fast
until the day it is not, and then everything after the missed press is
wrong.

### Hybrid: one macro per segment, a check between

1. **Split the route at the loads.** Title to main menu; main menu through
   Online, Smash and Quickplay to Solo; the fighter pick; the stage pick.
   Record each segment from its settled start screen up to the last press,
   not through the wait after it, so the file holds inputs and no slack.
   Keep the files in the repo's `eden/` folder with their sha256.
2. **Play a segment:** `macro.play` with the file's path, poll
   `macro.status` until it is finished and its `error` is empty, then
   `input.reset`.
3. **Check before the next one.** Poll a cheap screenshot marker
   (`tools/eden-solo/examples/css_check.py` is one) or a log line every
   second or two until the expected screen is there, with a cap. When the
   cap passes: screenshot, stop, report. Never play the next segment onto a
   screen you have not seen.
4. **Keep segments independent.** A segment that starts from a settled
   screen can be reused in any route; a branch made with "Record from
   marker" shares its prefix with the original and cannot. Picks on the
   character and stage select stay tile-cursor steps with their `hovered=`
   check ([TILE-CURSOR](TILE-CURSOR.md)), inside a segment or as their own
   step.

### Test runs: the runner is ready before Up

2026-11-03. On the clean-room RC8 runs (lr-01, lr-02 and a Yoga pair), the
agents spent most of each hour sending one press at a time, with a model
reading a screenshot after each, and restored the whole menu state after
every row. They had applied "Building a new route" and EDEN's `step.ps1`
loop to the whole run. lr-02's batch sent 884 single presses and no macro.
The rules:

1. **Before `Up`, the runner exists and has had a dry run.** One script
   covers the whole row list. Each row names its segment files (with
   sha256), its gates (a marker or log line, with a cap) and the evidence it
   saves (screenshots, log lines, a receipt per row). The dry run happens
   offline and checks that every file exists and matches its hash, that every
   gate loads, and that each row starts on the screen the row before it ends
   on.
2. **Chain rows on their end state.** Order the rows so each one starts
   where the last one ended. Restore state between rows only where the next
   row needs it. The full restore happens once, at `Down`.
3. **Manual input only for a missing segment.** When a row needs a screen no
   segment reaches, build that one segment ("Building a new route"), record
   it, add it to the runner with its hash, and go back to the runner. Don't
   drive the rest of the row by hand.
4. **A model reviews at row checkpoints, not after each input.** The runner
   plays segments and checks gates by itself. The agent reads the row's
   receipt and screenshots when the row ends or a gate's cap passes.
5. **Pace check.** If no row has finished 15 minutes after the game reached
   its first settled screen, stop sending input and fix the runner. More
   presses won't fix it. A coordinator can see it in the run's
   `control.jsonl`: a run that follows these rules shows `macro.play` calls,
   not hundreds of single presses.
6. **Runner prep is time-boxed.** Up comes at most 15 minutes after the
   order when the segments exist, and 30 minutes when some must be recorded.
   Boot with the rows whose dry run passes. A dry run that fails on some
   rows is not a reason to wait: take those rows out of `rows.json`, list
   them in the handoff as deferred with the reason, and boot. Add them back
   between rows in the same boot once each one passes its own dry run. The dry run is the checks in rule 1
   and no more: extra test suites, input-pin sets and code review wait until
   after the run. (2026-11-03: the first runs under these rules spent 30
   minutes before any boot. Each agent wrote its own runner from scratch,
   one with 14 offline tests and 86 pinned input hashes.)
7. **Keep what the run built.** At `Down`, commit the run's `rows.json`,
   recorded segments and reference shots (with sha256) to the repo's
   `eden/` folder, with the playbook commit of `tools/eden-runner` it ran
   on. Don't copy `runner.py` into the repo; a copy forks the tool. Then
   harvest the proven segments into the shared library (`harvest.py`,
   below). The next run starts from them and records only what is still
   missing.
8. **Group rows into one boot.** A run's `rows.json` covers every open row
   that host and build can reach, chained on their end states. Don't run one
   row family per boot. Split into another boot only when the next rows need
   a different build, plugin set, host or profile, or when a fresh boot is
   itself the thing under test (boot-time install, first load).
   - **When a row fails**, fix it (its data, a gate, a hook), delete
     `STOP-INPUT` and run the same `rows.json` again on the same boot.
     Passed rows are skipped. If a row can't be fixed within a few minutes,
     take it out (list it as deferred, with the reason). Add a step that
     brings the game to the next row's start (a library segment or a
     `recover`), and carry on. Don't shut down to retry one row.
   - **Order the batch:** cheap, independent rows first. Rows that may wedge
     the game (crash checks, teardown, long matches) go last.
   - **Budget the boot against the runtime cap before Up.** Give each row a
     minute estimate in `rows.json` (`"budget_min"`). Keep the sum, plus
     about 10 minutes for boot and navigation, under the cap minus `pace`'s
     5-minute margin. Rows over budget go to a planned second boot, not a
     forced one. Route-building inside a boot counts against the budget too.
     (2026-11-04: a Yoga boot spent its time building routes and hit the cap
     before its redo rows ran.)
   - 2026-11-03: Phase 1 ran 10 boots of 2–5 minutes each over four hours,
     each followed by 10–15 minutes of write-up. Rows passed but almost none
     closed.
9. **Keep the write-up per run light; do compliance once per phase.**
   - **Per run:** `RESULTS.md` of 20 lines at most: the build (resolved
     path + sha256); each row's PASS, FAILED or deferred with its receipt path
     and a one-line cause; and the `down` result (`cleanup_failures`, verify
     equal, released). Add the harvest line and the next action. The
     runner's receipts and the lifecycle journal are the evidence; don't
     restate them.
   - **Not per run:** checklist dispositions, extra inventories beyond the
     lifecycle's own verify, archive essays.
   - **Once per phase or release candidate:** the full `compliance.py`
     report with every manual row disposed, over all of the phase's runs.
   - **Coordination posts:** an inbox or status post is 5 lines at most. It
     names the run, the rows that changed state, and the next action; paths
     point to the rest.
10. **One command per run: `tools/eden-test/eden_test.py`.** Don't hand-build
    configs, state paths or caller scripts. Run it on the host that runs Eden
    (on the Yoga: `git -C ~/ssbu-mod-playbook pull --ff-only`, copy the rows
    folder over, then call it through `ssh`).
    - `new --from <last config.json> --label <new> --rows <folder>` makes a
      fresh run folder (`/home/intpa/eden-runs/<label>/`) with new state and
      work paths. It keeps the previous run's pins and checks each one's
      sha256; `--app` switches the Eden build.
    - `up <run>` does the dry run, then the lifecycle `up`. `run <run>` runs
      the rows; after a fix, `run <run> --retry`, on the same boot. `down
      <run>` does the lifecycle `down`, the harvest, a `RESULTS.md` skeleton
      of 20 lines or fewer (build, rows, the unit's exit result, harvest,
      timing) and `timing.json`.
    - `all` does new, up, run and down in one go, for a batch you don't
      expect to fix mid-boot.
    - `--plugin <new build>` on `new` swaps the pinned plugin with that file
      name; `--defer-load` leaves the frontend ready for boot tracing.
    - `pace <run>` reports hand-driven inputs (control requests outside
      macro recordings, minus the runner's own hook inputs), the minutes
      since the last newly passed row and the minutes left before the run's
      runtime cap (`runtime` in the config). It exits 2 and prints `STOP`
      past 40 inputs, 15 minutes without progress, or 5 minutes before the
      cap. `run` prints it after every call. When it says STOP: Down if the
      cap is near (Eden has crashed on the cap's SIGTERM, 2026-11-03/04);
      otherwise stop steering, use a fixture, record the route once, or Down
      and fix the rows (rule 13). Retakes of clips go on a fresh boot.
    - Mid-boot fixes change `rows.json` and hooks only, never `config.json`
      or the lifecycle's `state/lifecycle.json`. If `config.json` was edited
      after Up anyway, `down` restores the Up-time config (the lifecycle
      refuses a mismatch) and keeps the edited copy as
      `config.after-up.<time>.json`.
11. **Read the game's state; don't assume it.** A row doesn't hard-code a
    fixture's roster index, fighter kind or prefetch target. It reads them
    first, from the settings or roster JSON (`json` gate, `copy`), the
    plugin's state lines (`log` gate), or a trace point. Evidence shots are
    taken after the screen settles: a `change` gate with `"stable": true` on
    the HUD, or the plugin's ready line. A first-ready frame can show the new
    body with the old HUD. (2026-11-03: lr-11 lost three rows to a hard-coded
    prefetch, a wrong roster index and a route that skipped a step. lr-12's
    Kirby/R.O.B. frames showed body and HUD disagreeing at first-ready.)
12. **Brief workers with a standing goal.** For example: "keep batching the
    open rows until each one is closed or deferred with a reason". A worker
    keeps working through its batches. It waits for a busy host with polls of
    60 s or less instead of ending its turn, and posts only at row checkpoints.
    Restarting an agent that ended its turn costs minutes of re-reading.
    This includes a run in progress: poll `eden_test.py pace` and the runner
    log until it finishes. (2026-11-04: an OpenCode worker ended its turn
    "until the retry completes"; the retry failed a minute later, and the
    worker sat idle for 11 minutes until the primary re-sent it.)
13. **Reach the starting state with a fixture, and plan it before Up.** A
    row's starting state (which fighters, which stage, which settings, which
    screen) comes from, in this order: a **fixture** (a pinned file in the
    run config, `instances[].files` with `to`, such as the plugin's settings
    JSON, or the plugin's own settings-reload fixture), an existing recorded
    **segment** or library recipe, a project **hook** that reads state and
    steps to it, or one bounded **record**ing (`record-start`, the inputs,
    `record-stop`) that then becomes a segment. Steering native menus one input
    at a time is for the case where the menu itself is under test. `rows.json`
    carries the plan, and `eden_test.py up` refuses without it:
    `"setup": [{"state": "Training, P1 Kirby, CPU Kirby", "via": "fixture",
    "ref": "files: TrainingRooms/settings.json"}]` (`via` is `fixture`,
    `segment`, `record` or `hook`; `--skip-setup-plan` only to re-run rows
    whose segments already exist). (2026-11-04: a worker spent a 20-minute
    boot steering the character select to make the CPU Kirby, 139 single
    inputs and no finished row, until the runtime cap stopped Eden; the
    plugin's settings fixture reached the same state in one step.)
14. **Press a chord in one frame.** A plugin that reads a button combination
    (a menu chord such as `B`+`Up`, a shortcut such as `ZL`+`Left`, the
    `L`+`R`+`A` start) sees it only when every button goes down in the same
    poll frame. Sequential `input.button` calls straddle frames and the chord
    never fires. Put chords in a macro whose events share one frame, record
    it once as a segment, and reuse it. (2026-11-04: the Ally batch lost its
    first 15 minutes to menu chords that never registered, until the worker
    built same-frame chord macros.)
15. **A row passes on the behaviour, not on its setup.** A row's gate checks
    that the behaviour under test happened. Use a machine-readable value where
    one exists: a guest tracepoint on the plugin's function (its return or
    argument per event), a log line, a HUD number. Otherwise use a frame that
    shows the event itself: the hit, the death, the respawn. "Fixture applied"
    and "clip recorded" are setup checks only. (2026-11-04: five TrainingRooms
    rows passed on their setup gates while their clips showed no respawn, no
    hit during playback, and no difference between SDI on and off. The
    coordinator's frame review caught it.)

#### The shared runner: a helper, extended per project

`tools/eden-runner/runner.py` runs the rows; `tools/eden-lifecycle` does
`Up` and `Down` and writes `instances.json` (each instance's control socket
and the files its gates read). A run writes `rows.json` and, for anything
its plugin needs that the runner doesn't have, **project step hooks**: plain
Python in the plugin's repo, `eden/steps.py`.

- **Hooks are the flexible part.** A step `{"run": "eden/steps.py:open_defaults",
  "args": {...}}` gets the runner's tools: `call`, `press`, `stick`, `play`,
  `gate`, `ui`, `shot`, `wait_log` and `log`. Write the plugin's own menu
  navigation and checks there, freely. They still run inside the row, so
  stop files, the pace cap and `actions.jsonl` apply. Add `"check": true`
  when the hook verifies the screen it leaves.
- **Bring only generic needs to the shared tool**: something any plugin's
  test would want (a new gate kind, a frontend operation). Add it with a
  test in `test_runner.py`. Don't copy or fork `runner.py`.
- **Change the shared tools in a worktree** (`git worktree add`), never in
  the playbook clone itself: live runs execute the tools from that clone,
  so a half-edited file there breaks someone's `up` or `down`.
- **What stays fixed:** a test run doesn't drive one input at a time with a
  model looking after each. Scripted inputs in a hook are fine. Lifecycle
  hygiene and the pace check also stay.
- **Record while exploring:** `runner.py record-start rows.json --instances
  … --segment <name>`, then drive by hand (`step.ps1`, the GUI), then
  `record-stop … --segment <name> [--start-gate <gate>]`. The segment lands
  in `rows.json` with its sha256, ready for `play` steps and harvest.
  Exploration stays interactive; what it finds becomes reusable.

```text
python3 -I tools/eden-runner/runner.py dry-run eden/rows.json
python3 -I tools/eden-runner/runner.py run eden/rows.json --instances <state_dir>/instances.json
```

- **Steps:** `play` a segment (`macro.play`, wait for it to finish, then
  `input.reset`), `gate`, `shot`, `copy` (save an instance file into the row
  folder), and `wait_ms` (only where no gate exists).
- **Recording:** a segment whose file is missing but which has a `recipe` is
  recorded on its first `play` (`macro.record`, then the recipe's inputs,
  then `macro.stop`). Its sha256 is written back into `rows.json`, and
  every later use plays the file. Recipe inputs are the only single inputs
  the runner ever sends.
- **Gates:** `log` (a regex over lines the row wrote), `json` (expected
  values in a file such as the settings), `image` (a region compared with a
  reference shot) and `hook` (a Python check such as `css_check.py`).
  - Prefer a log line.
  - For an image gate, pick a region that doesn't animate. The default
    tolerance is a mean difference of 8 per colour byte; an exact match is
    only allowed with `"static": true`. The title screen's attract mode
    failed two exact-match attempts on 2026-11-03.
  - Capture reference shots from the run itself, at the same resolution.
- **Start checks:** every `play` needs one. That's a gate on the same
  instance since the last play, a `start_gate` on the segment, or an
  explicit `"unchecked": true`. On 2026-11-03 lr-03 recorded title→main
  while the game sat in the attract-mode intro movie.
- **Idle game:** left idle on the title (or during a long hold), the game
  plays its attract-mode intro movie. Both runs on 2026-11-03 hit it. Give
  the first gate of a run, and the first gate after any pause in driving, a
  `"recover": "<intro-cancel segment>"`. A failed gate then plays that
  segment once and checks again.
- **References not taken yet:** an image gate with `"acquire": true` and no
  reference file saves the settled screen as its reference on first visit.
  The runner then stops after that row with `REVIEW` (exit 4). Look at the
  new references once, then run again.
- **Shared library:** `tools/eden-runner/library/index.json` holds proven
  segments and gate references for the game, with sha256 and start/end
  screens. Rows use them with `{"library": "<name>"}` instead of recording
  them again. At `Down`, add the segments a run proved (rule 7):
  `python3 -I tools/eden-runner/harvest.py eden/rows.json --label eNN`
  copies every segment and gate used in a PASS row, with sha256, the gates
  before and after each segment, and the run label. It leaves existing
  entries alone unless you pass `--replace`.
- **Stopping:** the first failure writes `STOP-INPUT` and the runner exits.
  Fix the row list or the tool, delete `STOP-INPUT`, and run again. Rows
  with a PASS receipt are skipped. A `STOP` file in the output folder stops
  the runner before its next input; a coordinator can use it. The 15-minute
  pace cap is built in (`--pace-minutes`).
- **Evidence:** every control call goes to `out/actions.jsonl`. Each row
  writes `out/<row>.json`, and its screenshots and copies go to
  `out/<row>/`.

### Editing a recording

The file is plain JSON. `events` is a sparse list of changed samples, each
with `frame`, `buttons` (a bit mask), `sticks` (`lx, ly, rx, ry` in −1..1),
`triggers` and `motion`; only changes are stored, so a held button is one
sample with its bit set and a later one with it cleared. `duration_frames`
is the total length and playback runs to it.

- **Safe edits:** cut idle time by subtracting the same number from every
  later `frame` (the example file under `/home/intpa/automation01/peer10/`
  waits 2,068 frames, 34 s, before its first input); drop a wrong press by
  removing both its press and its release sample; shorten a hold by moving
  its release sample earlier; trim the tail by setting `duration_frames` a
  few frames past the last event.
- **Keep:** frames ascending and distinct, every press paired with its
  release, `game_id` and `player` as recorded, and at least one displayed
  frame between samples. Eden keeps no sub-frame timing and may drop an
  input shorter than one displayed frame.
- **Then:** save it as a new file with a new `name` (the library remembers
  paths), replay it from the same scene with a screenshot at the end before
  trusting it, and record its sha256 like any played file.
- **Limits from Eden's doc:** recording auto-saves at 60,000 changed samples
  or 216,000 frames; macros do not restore savestates; playback owns the
  controller until it finishes or `macro.stop`; keyboard and gamepad
  shortcuts need Eden focused, so drive playback through the control API.
