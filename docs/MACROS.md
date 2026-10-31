# Input routes and macros

2026-10-31. Two ways to drive the game to a screen. The plugin tests here
used **scripted routes**: `gv.py`/`drive.py` calls with sleeps in a short
PowerShell script. Eden also records and replays **input macros**
(`.edenmacro.json`). Use scripted routes for routes you build yourself, and a
recorded macro when someone hands you one or a sequence needs exact human
timing.

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
pair.

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
   and while it records.
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
