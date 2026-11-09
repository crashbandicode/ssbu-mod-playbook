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
[route history](../history/ROUTES-AND-MEDIA-2026-10--11.md), not fixtures for all saves.

The plugin's `docs/EDEN-TEST.md` has its procedure/criteria and `docs/HOOKS.md`
its sites: [ssbu-tile-cursor](https://git.syntaxbyexample.com/intpa/ssbu-tile-cursor).
Keep the NRO pin, step/settled lines and final state evidence with the row.
