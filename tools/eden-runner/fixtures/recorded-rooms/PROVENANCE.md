# Recorded bridge streams of two stalled room entries (2026-11-23/24)

Each file is the head of a run's `out/bridge-run/bridge.log` on `ubuntu-xps.lan`, lines unmodified, cut after the
`state end` line of the first `ssbu.ui` block that reads `screen=css.smash`. Gzip with mtime 0.

| File | Run | Lines | Raw bytes | Raw sha256 | Whole log sha256 |
| --- | --- | ---: | ---: | --- | --- |
| `xps-stop-1.bridge.log.gz` | `/home/intpa/eden-runs/rc15-i2-71158421-xps-stop-1` | 2,686 of 49,100 | 219,994 | `a31a826e57dbb45bd01aac56888e55f5228a8ec117c9c0ddcd0f0deb6bd371f4` | `9e05019c483095d87136bfb7877c9af4237e05f32d6e8ad8f667f0f9562b77e0` |
| `xps-1.bridge.log.gz` | `/home/intpa/eden-runs/rc15-i2-71158421-xps-1` | 2,971 of 71,380 | 245,795 | `f15060a8119ff3215fe7013fd31778ec78d54c0c904febaa1053a4729c445351` | `11ef053cdcdc9b5ea38dacf64177296637c0d3b092ed362f056b5c3e2392ddf5` |

Both runs used the runner at `setup-blocks` `216a6b2`, whose `wireless_room` pressed A on the screen name. What the
streams hold, by bridge frame (the `frame=` suffix of each line):

| | `xps-stop-1` (boot `0x022b82b302fcdd39`) | `xps-1` (boot `0x41ebfad78f601a4b`) |
| --- | ---: | ---: |
| the only `ldn.room_list` block (`focus.id=unknown`) | 618 | 772 |
| first `stage_select` block | 840 | 992 |
| the old route's A, lost (pressed before the picker took input) | about 847 | about 1004 |
| `stage_select` blocks read by the old route's 15 s wait | 840 to 1783 | 992 to 1931 |
| first `tile.cursor` block with `screen=sss`, both tokens held, `settled=1`, `at == hovered` | 6617 | 7230 |
| the same qualifying block just before the A that worked | 13973 | 12845 |
| the A that worked (the recovery's `ldn-open-create`, a 9-frame A) | about 13986 | about 12862 |
| first `css.smash` block (the stream ends here) | 14061 | 12936 |

The press frames are from `smash-training-lobby/artifacts/phase23/route-hardening/stage-stall/episodes.txt` (clock
aligned from request and answer pairs); the block frames are read from the streams themselves.

Limits of these streams as test input:
- The old route asked for `tile.cursor` only in the recovery, so the streams hold no tile block in the early window.
  The `provider name=tile.cursor` lines show its revision first changing at frame 989 and 1140.
- After the first press the hardened route sends that the recorded run did not send at that point, the stream no
  longer says what the game would have done. Tests replay only up to such a point.
