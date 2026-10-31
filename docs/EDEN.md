# One Eden instance you can drive

2026-10-31. Tools: `tools/eden-solo/` (gsp-visualizer's copies). Each run has
a label `eNN`, and every file a run writes is named after it.

## What is installed

- **Eden:** the Eden-GIF build at
  `/home/intpa/Applications/Eden-GIF/current/Eden.AppImage`.
  - `current` points at `20261030-a1796fe59ffe-elite-ratings-v8`.
  - Its `tools/` has `edenctl.py`, the control client, and `eden-run.py`, the
    bounded launcher.
  - Its `docs/` has `CLIControl.md` (the whole control API), `InputMacros.md`
    and `ELITE_RATING_TESTS.md`.
- **Profile:** `gv.py` copies source profile `b` of the Yoga
  (`/home/intpa/ssbu-desync-1029/profiles/b`) to
  `/home/intpa/gsp-visualizer-work/profiles/y`. Game content is hard-linked
  and everything else is copied. Only the copy is ever changed. Profile `s`
  is a copy of `y`, for the second instance of a pair.

## Up

```text
pwsh -File tools/eden-solo/solo.ps1 -Action Up -Label e19 -Nro a.nro,b.nro [-Diag diag.txt] [-Seeds 82:1700]
```

`solo.ps1` does these steps in order, and stops at the first one that fails:

1. Checks the coordination file for another owner's open claim.
2. Checks for an Eden already running on this host, that display `:68` is
   free, and for at least 10 GiB of MemAvailable.
3. Writes a `CLAIMED:` line naming the run and the profile.
4. Runs `gv.py prep`:
   - moves every NRO already in the profile's `plugins/` into
     `plugins-set-aside-gsp-visualizer/`, then installs the `-Nro` list;
   - renames each plugin's earlier output folder to `.before-eNN`, so each
     run starts with fresh logs;
   - copies `-Diag` to `ultimate/<plugin>/diag.txt`;
   - writes `PREP-eNN.json`.
5. Starts a rootful X display `:68` and takes the Yoga GPU lock.
6. Runs `gv.py launch` (bounded `eden-run.py`: 8 GiB, 5,400 s, background
   mode, socket `runs/eNN/control.sock`) and writes `LAUNCH-eNN.json`.
7. Calls `game.load` with the game's XCI.

`-Seeds slot:rating` seeds the rating runtime, for example to put a fighter
in Elite Smash. The details are in Eden's `ELITE_RATING_TESTS.md`.

## Drive

```text
gv.py shot  e19 css            # screenshot of the game -> tools/eden-solo/shots/e19/<HHMMSS>-css.png
gv.py press e19 a              # tap (120 ms); press e19 lstick,up 150 = L3 then Up, held 150 ms
gv.py stick e19 0 -1 250       # left stick x y for ms (positive y is up)
gv.py tree  e19                # visible Qt widgets
gv.py call  e19 status         # running/loading, fps; any control method by name
```

`shot` takes a name, not a path. Look at the screenshot before sending more
input: menus load at different speeds, and one missed press puts every later
input on the wrong screen. `step.ps1 -Label e19 -Steps 'b:a:4000,s:0:-1:250'`
sends a short burst of input and then takes one screenshot. That is the
basic loop. [MACROS](MACROS.md) has whole routes.

## Read the results

- **Plugin logs:** under
  `gsp-visualizer-work/profiles/y/user/sdmc/ultimate/<plugin>/`, on the
  host, while the game runs.
- **Eden's console:** `gsp-visualizer-work/runs/e19/console.log`. Search it
  for `panic`, `abort` and `crash`.
- **The run's end state:** `run.json` and `memory.peak` in the same folder.
- **Timestamps:** compare them as UTC (`[DateTimeOffset]::Parse(…)`). In
  PowerShell, `[DateTime]'…Z'` converts to local time.

## Down

```text
pwsh -File tools/eden-solo/solo.ps1 -Action Down -Label e19
```

Stops Eden, the display and the GPU lock, then writes `RELEASED:`. Always run
it, even after a failed run. The profile is kept.

## Queue runs

`Up` stops when the host is busy. Don't keep checking by hand: start a
runner in the background that waits, then runs.
`tools/eden-pair/examples/queued-run.ps1` (elite-rematch run e26) does this:

- **Wait:** it checks every 10 s, up to a deadline, until three things are
  true: no other owner holds an open claim, no Eden runs on the host, and
  MemAvailable is at least 10 GiB.
- **Claim:** if another owner claims first, `Up` fails with `active claim`;
  the runner goes back to waiting instead of stopping.
- **Run:** route, test, logs, then always `Down`. Put several runs in a list,
  each with its own Up, test and Down.
- **Start it** with the usual memory and time guard, in the background, with
  a `timeout` longer than the deadline, and write its output to a file.

## Two instances (online)

- **Same PC:** `pair.ps1` runs instances A (profile `y`, display `:68`) and B
  (profile `s`, display `:69`). Both carry the tile cursor. Both use the
  rating runtime and TLS, so they can only pair with each other.
- **Surface and Yoga:** `tools/eden-pair/` (elite-rematch's copies) uses
  instance `s` on the Surface, with its socket forwarded, and `y` on the
  Yoga. `drive.py <eNN> <s|y> …` takes the same commands as `gv.py`.
- **A won match:** `match.ps1 -Winner eA -Loser eB` has the loser walk off
  the stage repeatedly while the winner holds still.
