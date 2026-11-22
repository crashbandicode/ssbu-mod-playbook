# The tile cursor: exact picks on the character and stage select

2026-10-31. `ssbu-tile-cursor` (https://git.syntaxbyexample.com/intpa/ssbu-tile-cursor)
is a Skyline plugin for Eden automation. On the character select (CSS) and
the stage select (SSS), **L3 + D-pad** moves P1's hand exactly one tile. Stick
pushes overshoot there, and the plain D-pad moves the hand only a little.
Install it next to the plugin under test. It does nothing on other screens or
without L3, so it doesn't change what you are testing.

- **Accepted NRO:** frame-repeat build `8f830a2`, based directly on `15d3f31`; full pin and hash below.
  Re-pin existing Eden configs explicitly. `pair.ps1` installs the configured aid; with `solo.ps1`, add it to `-Nro`.
  The historical v0.1.0 pin (`b8313ca2…`) repeats by wall time. Source `main` (`a052fd5`) builds a different,
  Eden-unproven NRO; do not rebuild that into the accepted pin.
- **Log:** `<sdmc>/ultimate/ssbu-tile-cursor/events.log`. It is written every
  100 ms, so the host copy is current.

## Accepted frame-repeat pin (2026-11-16)

Source `8f830a2` on `frame-repeat`, based directly on `15d3f31`, replaces the wall-clock repeat with one step on
press and another every 15 elapsed CSS/SSS frame updates. Host speed cannot add repeats to a 2-frame hold.
Build host: XPS; host Rust 1.98.1, Skyline Rust 1.95.0-nightly (`a423f68a0`, 2026-02-13).

- New isolated pin on Surface/XPS/Yoga: `/home/intpa/trainingrooms-eden-pkg6/nro/aid/frame-repeat-8f830a2/libssbu_tile_cursor.nro`.
- SHA256: `66f00442eb7a93de3196714aebba85807e106663e4e9d5dbb3894d7c5e071ecd`.
- Host tests: 13 pass; all-target clippy with `-D warnings`, no-default-features build and Skyline release pass.
- Surface `tile-frame-surface-2`: **5/5 PASS**, muted, GDB stub on before load, detached controller and ledger.
  On both SSS and CSS, held 32 frames logged exactly `0→1→2→3`, with settled checks (CSS hover equals tile).
  Shared 2-frame walks settled Battlefield 1 and Mario 0, ending at Ready to Fight; stills visually checked.
- Surface calibration: the sole approved retry used `pick_stage(transition_cap=180)` for native CSS loading,
  with unchanged image/log discrimination. The first run exhausted the default 60 seconds during loading;
  its partial SSS evidence and failed receipt remain intact. No further extension or boot was used.
- Evidence: `smash-training-lobby/artifacts/harness-lib/tile-frame-surface-{1,2}/`; full build provenance in
  `tile-frame-PROVENANCE.json` beside them. This proves cursor selection, not a post-load stage-id assertion.
Down returned 0: all 2016 profile entries matched the snapshot, cleanup failures were empty, the ledger claim
was released and copies were pruned. No owned process remained, and the console had no panic/abort/crash/Unmapped
matches. The terminal `game.stop` timeout required a forced stop; the receipts retain that shutdown detail.


### Native selection and accepted card build

The Switch card carries the accepted frame-repeat NRO from `8f830a2`, SHA-256
`66f00442eb7a93de3196714aebba85807e106663e4e9d5dbb3894d7c5e071ecd`.
Its SD event log is available after the session. A bridge-enabled Tile Cursor
candidate adds live `tile.cursor/1` observations and has its own release and
card qualification.

The provider-backed runner checks `ssbu.ui/1` native focus and the local
controller's tile state. Each short macro uses player 0, L3 on frame 0,
direction on frames 1–2, and neutral from frame 3. The runner waits for exactly
one handled ACK and target write with the expected direction, source and
destination. It then waits for native settled hover before continuing.

A named pick checks the exact stock kind, name and form. Movement requires
directed neighbors for the active layout fingerprint, including a verified
off-grid entry when needed. A fresh native observation of the intended target
authorizes the A for selection. CSS confirms the local placement separately,
including a Local Wireless guest's native P2 token. Stage select confirms the
requested next native screen.

## Stepping

```text
gv.py press e19 lstick,right 150     # one tile right
gv.py press e19 lstick,left 1100     # held: repeats every 15 game frames; wall-time count varies
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

2026-11-22: shared tile-direction macros hold L3 plus direction for six frames and release at frame 7.
