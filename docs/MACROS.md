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
7. **Keep what the run built.** At `Down`, commit the recorded segments
   (with sha256), the runner and its row list to the repo's `eden/` folder.
   The next run starts from them and records only what is still missing.

#### The shared runner: write a row list, not a runner

`tools/eden-runner/runner.py` is the runner. A test run writes only
`rows.json` (segments, gates, rows) and, at `Up`, `instances.json` (each
instance's control socket and the logs or files its gates read). Don't write
another runner. If a step type is missing, add it to this tool with a test in
`test_runner.py`.

```text
python3 -I tools/eden-runner/runner.py dry-run eden/rows.json
python3 -I tools/eden-runner/runner.py run eden/rows.json --instances runs/eNN/instances.json
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
  them again. At `Down`, add the segments a run proved (rule 7), with the
  run label.
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
