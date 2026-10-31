# Input routes and macros

2026-10-31. Two ways to drive the game to a screen. The plugin tests here
used **scripted routes**: `gv.py`/`drive.py` calls with sleeps in a short
PowerShell script. Eden also records and replays **input macros**
(`.edenmacro.json`). The best of the two is the hybrid described under
"Eden input macros": a recorded macro for each segment of the route,
replayed at game pace, with a log line or screenshot check between segments.
Use a plain scripted route while you are still building a route or when a
segment is one or two presses, and one long macro only for a sequence that
needs exact human timing.

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
