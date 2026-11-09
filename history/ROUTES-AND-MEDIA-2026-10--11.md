# Historical route and media observations, October–November 2026

Historical examples from `87b3de0`; these are not present host state,
current sleep-based recipes, permission to boot, or substitutes for gates.
Current contracts: [MACROS](../docs/MACROS.md), [TILE-CURSOR](../docs/TILE-CURSOR.md),
[TEST-PLAN](../docs/TEST-PLAN.md), [TEST-RUN](../docs/TEST-RUN.md),
[RUNNER](../docs/RUNNER.md), [GIFS](../docs/GIFS.md) and [EVIDENCE](../docs/EVIDENCE.md).
Source references below name original files and line spans at that commit.

## Route shape and discoveries

`docs/MACROS.md:28–49` retained the old `tools/eden-solo/to-css.ps1`
Quickplay route; `P` meant `gv.py press`, `S` meant `gv.py stick`, and
`-BootSeconds` added a boot wait. `tools/eden-pair/nav.ps1` used the same
shape on both instances. These fixed sleeps are archived shape examples:

```powershell
P a; Start-Sleep 4; P a; Start-Sleep 6   # title, main menu
S 0 -1 250; Start-Sleep -Milliseconds 600; S 1 0 250; Start-Sleep 1 # Online
P a; Start-Sleep 12 # Online
P a; Start-Sleep 12 # Smash
P a; Start-Sleep 15 # Quickplay
P a; Start-Sleep 15 # Solo
P 'lstick,up' 150; Start-Sleep 2 # tile cursor onto a fighter
```

MACROS:76–98 recorded idle-player drop at about 30 s and self-destruct on
walk-off. `gv.py keepalive eNN <stop-file> <seconds>` held Down and tapped
ZL every 0.7 s; `match.ps1` and `tools/eden-pair/chain.ps1` used it.
Online pairing ranged **2–125 s**. elite-rematch **e25** timed a press
with sleeps at three seconds left on a pop-up, then hit a communication
error. `chain.ps1 -DetectStart` watched the next detector `session slot=`;
`-Watch s,y` watched the plugin's diagnostic pop-up line, with `-PopupWait`
only for builds lacking event lines. The 12–15 s menu waits added about
70 s and still missed late screens. The old `css_check.py` recognized
Game & Watch through the fraction of near-black P1-panel pixels: **0.35**
versus **0.05** for Meta Knight. Screenshots sometimes needed retry after
load because no render window existed yet.

MACROS:128–145 named `/home/intpa/automation01/` and
`run60/main-to-training-fast01.edenmacro.json` as reusable examples.
`/home/intpa/training-lobby-preload169/drive.py` saved played-file hashes.
The observed speed benefit came from emulated-frame playback rather than
70 s of route slack; variable loads/menus could still invalidate a long
ungated macro. MACROS:481–486's `/home/intpa/automation01/peer10/` example
waited **2,068 frames, 34 s**, before its first input.

`docs/TILE-CURSOR.md:3–16,59–67` identified the Eden-only
`/home/intpa/ssbu-tile-cursor/dist/libssbu_tile_cursor.nro`: version
**0.1.0**, build `15d3f31`, SHA-256 prefix `b8313ca2…`;
`<sdmc>/ultimate/ssbu-tile-cursor/events.log` updated every 100 ms.
The **2026-10-29** CPU example picked up the Random coin and stepped
86→85→84→83→72, then dropped it to choose Joker. In that source setup,
SSS Random→Final Destination took six Right steps (0→6); form tab/page
did not change the tile list. These indices describe that save, not a
stable fixture contract. `docs/EDEN.md:268–282` observed X opening the
ruleset Edit/Delete pop-up and A opening its editor on **2026-10-30**;
Judge-log's level-9-CPU 3-stock data matches lasted **44–80 s**.

## Runner and worker incidents, 2026-11-03–04

MACROS:171–175 recorded clean-room RC8 **lr-01**, **lr-02** and a Yoga
pair spending much of each hour on model-driven inputs and whole-menu
restores after each row. **lr-02 sent 884 single presses and no macro**.
MACROS:207–209 noted early agents then spent 30 minutes before boot,
writing separate runners; one added 14 offline tests and 86 input hashes.
MACROS:255–257 recorded Phase 1's **10 boots of 2–5 minutes over four
hours**, each followed by **10–15 minutes** of write-up: rows passed but
few closed. MACROS:248–254 recorded the Yoga boot consuming its cap
while building routes, before redo rows ran; a cloned **1200 s** GIF cap
also cut the November 4 batch boots short (MACROS:239–245,289–297).

MACROS:309–311 recorded **lr-11** losing three rows to hard-coded
prefetch, wrong roster index and a skipped step; **lr-12**'s Kirby/R.O.B.
frames first showed new body with old HUD. MACROS:318–320 recorded an
OpenCode worker ending its turn during a retry that failed one minute later,
then remaining idle **11 minutes** until the primary sent another message.
MACROS:334–345 recorded a **20-minute**, **139-input** CPU-Kirby setup
with no completed row before the cap, although a settings fixture reached
it directly; the Ally's first **15 minutes** also went to sequential menu
chords until same-frame macros were recorded.

MACROS:351–354 recorded five TrainingRooms rows passing setup gates while
clips showed no respawn, no hit during playback and no SDI-on/off difference.
Coordinator frame review caught the false passes. MACROS:415–416 recorded
two exact-match failures against the animated title on **2026-11-03**;
MACROS:438–444 recorded **lr-03** acquiring title→main from the attract
movie and both runs hitting that idle movie. These incidents explain the
current behavior-gate, stable-HUD, recovery and reference-review contracts.

## Media observations and source-art incident

`docs/GIFS.md:3–5,14–15,24–26,109–130,159–168` records Eden's built-in
recorder making README clips for gsp-visualizer, quiet-elite-switch,
elite-rematch and tile cursor. quiet-elite-switch used `before.gif` /
`after.gif`. On **2026-11-04**, 30 fps evidence takes up to 60 s were
**24–217 MB**; reviewed oversized raw takes used
`gifsicle -O3 --lossy=80`. The old `record.ps1` output default was
`/home/intpa/gsp-visualizer/eden/captures/<label>-<name>.gif`;
`chain.ps1 -GifName` on instance `s` defaulted to 15 s/640/12 fps and
supported `-GifWidth`, `-GifFps`, `-GifLead`, pop-up log or `-PopupWait`.

Eden's 1280-wide 8 s GIFs were **15–20 MB**. On **2026-10-31**,
elite-rematch **e13/e14** palette experiments (first/middle/last, nine
spread frames, all quarter-size frames) made no visible difference; all
frames' mean color error stayed below **1.1/255**. Full-frame outputs
were **3–5 MB**. Historical command
`encode.py e08-in-elite-real.gif in-elite.gif 0,0,1280,720 10 128`
reduced **20.4 MB→3.3 MB**, but its original 110 ms frames at 10 fps played
about **10% faster**. Current publication uses fps 0 to preserve times;
`trim_gif.py`'s first-frame-delay reuse remains a documented limitation.

On **2026-11-04**, a reported Cosmic Destination tile was stock Final
Destination because those TrainingRooms RC10 runs omitted ARCropolis.
The card `libarcropolis.nro` prefix was `92171b2f…`, seen in snapshots
under `/home/intpa/ssbu-cpu-pilot-install/` (EDEN:77–87). Historical
comparison decoded `ui/replace/stage/stage_1/stage_1_end.bntx` via
`tools/assets/arc_get.py` and `bntx_view.py`, compared stock/mod images
to the frame, and ruled out stock. The caption example was Eden,
**2026-10-29**, build `15d3f31`. Current evidence permits reviewed
screenshots/reference crops, while extracted assets, data.arc and dumps
remain excluded from git; the old blanket game-derived-image wording is
superseded by the approved scope in [EVIDENCE](../docs/EVIDENCE.md).

## Precondition retry and replay examples

`docs/EDEN.md:373-380` at `87b3de0` recorded Judge-log's 30-second
wait for `gamewatch session=` and exit 2 when absent, so a wrong fighter
cost a short abort rather than a full match. The capped retry batch used
the next label and obtained **10 valid matches in 11 tries on 2026-10-30**;
`tools/eden-solo/examples/retry-batch.ps1` retained that historical loop.
`docs/CHECKLIST.md:108-109` recorded **four replay A/Bs**, including a
replay without the plugin's fighter that emitted only its install line.
These are examples, not replacement coverage requirements or current results.
