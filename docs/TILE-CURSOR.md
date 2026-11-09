# Exact fighter and stage picks

`ssbu-tile-cursor` is an **Eden-only** Skyline automation plugin, never a
Switch-card install. L3 + D-pad moves P1's CSS/SSS hand one tile; it does
nothing on other screens or without L3. Ordinary stick overshoots and plain
D-pad moves only a little. Include it in the run's approved pinned NROs.
Historical test identity: 0.1.0, `15d3f31`, `b8313ca2…`, formerly
`/home/intpa/ssbu-tile-cursor/dist/libssbu_tile_cursor.nro`. Freshly verify the
actual file/hash; historical paths and wrapper auto-install are not current pins.

The log `<sdmc>/ultimate/ssbu-tile-cursor/events.log` flushes every 100 ms.
Read its actual neighbors for this save/DLC, not an assumed roster index,
row width or historical fighter route. Tile numbers are positions in the
game's tile vector, consecutive within a row; edge steps do not wrap and
log `to=none` without moving the hand.

Use recorded **same-frame L3 + direction** events and reuse the segment.
Legacy `gv.py press <label> lstick,right 150` sends listed buttons in order:
L3 first prevents an early ordinary D-pad press, but sequential calls are
not a same-frame chord guarantee. `lstick` means L3 click here. Held tile
steps repeat every 250 ms; hand easing takes about 8 frames and longer wall
time below 60 fps. Gate on the settled line rather than assuming 0.6 seconds.

```text
step screen=css dir=left from=85 to=84 token=p2 x=288.0 y=-77.0
settled screen=css at=84 hovered=84 x=288.0 y=-77.0
```

On CSS, require `settled hovered=<to>`: the game itself confirms the hovered
tile. On SSS, `hovered` is always `none` because Lua owns that hover; require
`settled at=<to>` instead. Split picks at real settled boundaries and check
before pressing A. [MACROS](MACROS.md) owns segment/chord/input policy.

The CSS hand **must carry a token**. Initially it holds P1's below the grid;
L3+Up moves onto a first-row tile (`from=none`). Step to the fighter, then A
places the token. Empty-hand tile steps intentionally do nothing; B takes
the token back. To choose a CPU, first place P1's token, move to the CPU's
coin, A picks it up, then require `token=p2` as steps preview the CPU's
fighter. A drops it again. Read the actual hovered fighter/state rather than
assuming a fixed Random-to-character sequence.

On SSS, use the current log to choose the stage. Form-tab/page changes do
not change that tile list; Up/Down move one row. Historical Random→Final
Destination and CPU Joker routes are examples in
[route history](MACROS.md), not fixtures for all saves.

The plugin's `docs/EDEN-TEST.md` has its procedure/criteria and `docs/HOOKS.md`
its sites: [ssbu-tile-cursor](https://git.syntaxbyexample.com/intpa/ssbu-tile-cursor).
Keep the NRO pin, step/settled lines and final state evidence with the row.

## Transitional baseline reference

Current concern policy is above; baseline detail remains during the migration for linked concerns and evidence extraction. The operator-approved task reading and screenshot policy take precedence.

# The tile cursor: exact picks on the character and stage select

2026-10-31. `ssbu-tile-cursor` (https://git.syntaxbyexample.com/intpa/ssbu-tile-cursor)
is a Skyline plugin for Eden automation. On the character select (CSS) and
the stage select (SSS), **L3 + D-pad** moves P1's hand exactly one tile. Stick
pushes overshoot there, and the plain D-pad moves the hand only a little.
Install it next to the plugin under test. It does nothing on other screens or
without L3, so it doesn't change what you are testing.

- **NRO:** `/home/intpa/ssbu-tile-cursor/dist/libssbu_tile_cursor.nro`
  (0.1.0, build `15d3f31`, sha256 `b8313ca2…`). This is the build the Eden
  tests used. `pair.ps1` installs it automatically. With `solo.ps1`, add it
  to `-Nro`.
- **Log:** `<sdmc>/ultimate/ssbu-tile-cursor/events.log`. It is written every
  100 ms, so the host copy is current.
- **Not on the Switch card.** It is an Eden tool.

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

Each step writes a line, and so does the hand when it stops:

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

From Random, six L3 + Right steps landed on Final Destination (tiles 0→6).
Down and Up move one row and back. Switching the form tab or the page
doesn't change the tile list.

The repo's `docs/EDEN-TEST.md` has the whole test procedure and pass
criteria, and `docs/HOOKS.md` has the hook sites.
