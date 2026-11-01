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
- **Never copy a profile in full.** The game content alone is tens of
  gigabytes, and one plain copy per profile or per run fills the filesystem.
  Hard-link it (`gv.py prep`, or `cp -al` for a copy by hand) and copy only
  the small parts that change: config, saves, the plugin folders. Check a
  copy with `du -sh` against `du -sh --apparent-size`; the first number
  should stay small.
- **Link what is identical, copy what is not.** This applies to any copied
  tree, not only game content:
  - Hard-link a file when it is byte-identical to its source and the run
    will not write it in place: game content, NROs, archives, unchanged
    sources.
  - Give a file its own copy when its content differs, or when the run
    writes it in place: saves, an INI or config the plugin or Eden rewrites,
    shader caches, logs. A write through a hard link changes every copy that
    shares it.
  - After `cp -al`, replace each file the run will write with a real copy
    before the first boot.
  - On 2026-10-31 a dedup pass hard-linked about 105 GiB of byte-identical
    files across this workspace's profile copies, with nothing lost.

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

## Keep matches short

A test match should last as long as the path under test needs and no
longer. The default Smash rules (3 stock, 7:00) make every run that has to
reach the results screen, a pop-up or a rematch wait for three KOs or the
clock, and a batch of ten pays that ten times.

- **Make one short ruleset in the test profile** and pick it by name every
  time: 1 stock, a 1:00 time limit, items off, stage hazards off (under the
  editor's collapsed "Advanced" row), FS meter off, Final Destination or
  Battlefield. Rulesets are save data, so a copy keeps it across runs; add it
  to the copy, never to profile `a`. In the ruleset list, X opens an
  Edit/Delete pop-up and A then opens the editor (Eden, 2026-10-30).
- **End it on purpose.** With one stock, `match.ps1`'s walk-off ends the
  match in one self-destruct instead of three, and the 1:00 limit ends a
  match nobody finishes.
- **Go long only when the test needs it,** and say why in the handoff: a
  dataset run that wants many events per match (ssbu-gnw-judge-log's
  free-play matches needed more Judges, and a level 9 CPU ended its 3-stock
  matches in 44 to 80 s anyway), or a check of behaviour late in a match.
- **Evidence:** the ruleset screenshot or the match length in the plugin's
  log (`session_end` frame count, or the detector's `frame=`).

## Read the results

- **Plugin logs:** under
  `gsp-visualizer-work/profiles/y/user/sdmc/ultimate/<plugin>/`, on the
  host, while the game runs.
- **Eden's console:** `gsp-visualizer-work/runs/e19/console.log`. Search it
  for `panic`, `abort` and `crash`, and for `Unmapped Read` / `Unmapped
  Write`. Eden returns 0 for a read through an unmapped guest address and
  keeps running where the console faults: on 2026-11-01 a group swap that had
  passed 878 Eden lanes with `HW.Memory … Unmapped Read32 @ 0x00000000000008`
  in the log crashed the Switch at that read. Any `Unmapped` line at an
  address below `0x1000`, or at any address inside the plugin's activity
  window, fails the lane. Attribute the others by timestamp against the
  plugin's own log lines before dismissing them; stock-game and emulator
  noise exists (repeated `Read64 @ 0x0` + `Read8 @ 0x38` pairs, for one).
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
- **Retry instead of counting a failed run.** Give each run a precondition
  the plugin's own log must show soon after the match starts, and abort
  when it doesn't: ssbu-gnw-judge-log's runner waits 30 s for its
  `gamewatch session=` line and exits 2 without it, so a wrong fighter pick
  costs 30 s rather than a match. The batch then runs again under the next
  label until the wanted number of runs succeeded, with a cap on tries and
  one line per start and end. `tools/eden-solo/examples/retry-batch.ps1` is
  that loop (2026-10-30: 10 valid matches in 11 tries).
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
