# The tile cursor: exact picks on the character and stage select

2026-10-31. `ssbu-tile-cursor` (https://git.syntaxbyexample.com/intpa/ssbu-tile-cursor)
is a Skyline plugin for Eden automation. On the character select (CSS) and
the stage select (SSS), **L3 + D-pad** moves P1's hand exactly one tile. Stick
pushes overshoot there, and the plain D-pad moves the hand only a little.
Install it next to the plugin under test. It does nothing on other screens or
without L3, so it doesn't change what you are testing.

- **NRO:** 0.1.0, build `15d3f31`, sha256 `b8313ca2…`, the same file as
  release v0.1.0. This is the build the Eden tests used, and the one the
  Eden configs pin. `pair.ps1` installs it automatically. With `solo.ps1`,
  add it to `-Nro`. As of 2026-11-13, `main` (`a052fd5`, 2026-10-30) builds
  a different NRO that hasn't run in Eden, so don't rebuild it into `dist/`.
  - On 2026-11-16 the old path `/home/intpa/ssbu-tile-cursor/dist/` was gone on the Yoga. The Yoga and XPS configs
    pin `/home/intpa/trainingrooms-eden-pkg6/nro/aid/libssbu_tile_cursor.nro`, the same sha256.
- **Log:** `<sdmc>/ultimate/ssbu-tile-cursor/events.log`. It is written every
  100 ms, so the host copy is current.
- **Not on the Switch card.** It is an Eden tool.

## Frame-repeat candidate (2026-11-16)

Source `8f830a2` on `frame-repeat`, based directly on `15d3f31`, replaces the wall-clock repeat with one step on
press and another every 15 elapsed CSS/SSS frame updates. Host speed cannot add repeats to a 2-frame hold.
Build host: XPS; host Rust 1.98.1, Skyline Rust 1.95.0-nightly (`a423f68a0`, 2026-02-13).

- New isolated pin on Surface/XPS/Yoga: `/home/intpa/trainingrooms-eden-pkg6/nro/aid/frame-repeat-8f830a2/libssbu_tile_cursor.nro`.
- SHA256: `66f00442eb7a93de3196714aebba85807e106663e4e9d5dbb3894d7c5e071ecd`.
- Host tests: 13 pass; all-target clippy with `-D warnings`, no-default-features build and Skyline release pass.
- Surface `tile-frame-surface-1`: SSS held 32frames gave exactly `0→1→2→3`, then the shared 2-frame walk settled at
  Battlefield 1. The existing 60 s CSS gate expired during native loading; CSS appeared later in events.log.
  CSS walking/held-repeat remains **unverified**, so this candidate has not replaced the established default pin.
- Evidence: `smash-training-lobby/artifacts/harness-lib/tile-frame-surface-1/`, full build provenance in
  `tile-frame-PROVENANCE.json` beside it. Down restored/verified/released and pruned the owned profile.

## Stepping

```text
gv.py press e19 lstick,right 150     # one tile right
gv.py press e19 lstick,left 1100     # held: repeats every 250 ms
step.ps1 -Label e19 -Steps 'b:lstick+up:700,b:lstick+right:700'
```

- **Send L3 first.** `gv.py press` presses its buttons in the order listed. A
  D-pad press that arrives before L3 is an ordinary D-pad press.
- **Wait about 0.6 s between steps.** The hand eases over about 8 frames, and
  longer when Eden runs below 60 fps.
- **Edges:** steps don't wrap. At the end of a row the line reads `to=none`
  and the hand stays.
- **Tile numbers:** they are positions in the game's tile vector,
  consecutive within a row. Read the actual neighbours from the log instead
  of assuming a row width; the CSS tile count depends on the save's DLC.

Each step writes a line, and so does the hand when it stops (each line
starts with a tick count, left out here):

```text
step screen=css dir=left from=85 to=84 token=p2 x=288.0 y=-77.0
settled screen=css at=84 hovered=84 x=288.0 y=-77.0
```

`settled … hovered=<i>` is the game's own record of the hovered tile. When it
equals `to`, the pick is right, with no screenshot needed. On the SSS
`hovered` is always `none`, because the game keeps that hover in Lua; check
that `at` equals `to` instead.

## On the character select

- **The hand must carry a token** for steps to happen. At the start it holds
  P1's token, below the grid, so `lstick,up` puts it on the first row
  (`from=none`). `to-css.ps1` ends this way.
- **To pick:** step to the fighter, then press `a` to place the token. With
  an empty hand, steps do nothing, which is intended; press `b` to take the
  token back.
- **To set a CPU:** with P1's token placed, move onto the CPU's coin and
  press `a` to pick it up. Steps then log `token=p2`, and the CPU's panel
  previews each fighter. Press `a` again to drop the coin.
- **Example from Eden on 2026-10-29:** the CPU's coin was on Random. Three
  L3 + Left steps and one L3 + Up stepped 86→85→84→83→72. Dropping the coin
  then set the CPU to Joker.

## On the stage select

On Training's stage select (2026-11-16), the grid is 11 tiles wide. Tile 0 is the Training stage. Tile k is
TrainingRooms' `lobby_stages::STAGES[k]`: Battlefield 1, Small Battlefield 2, Big Battlefield 3, Final
Destination 4, Peach's Castle 5, Kongo Jungle 6.
- **Evidence:** the hover on entering is tile 0; the XPS stills show Battlefield at 1; a Yoga still shows Kongo
  Jungle at 6.
- **Not this screen:** the older note "from Random, six L3 + Right steps landed on Final Destination (tiles 0→6)"
  came from another stage select. A walk to tile 6 for Final Destination therefore picked Kongo Jungle.
- **Picking by name:** `routes:pick_stage` takes the stage's name and checks its id from game state after the load
  ([ROUTES](ROUTES.md)).
- **Rows and pages:** Down and Up move one row and back. Switching the form tab or the page doesn't change the
  tile list.
- **Leaving:** a held B leaves the stage select and the character select. A 4-frame B tap does nothing on either.

The repo's `docs/EDEN-TEST.md` has the whole test procedure and pass
criteria, and `docs/HOOKS.md` has the hook sites.
