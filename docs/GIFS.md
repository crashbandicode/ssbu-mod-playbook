# Record and review Eden GIFs

Plan documentation capture during the existing test boot. Use Eden's
built-in recorder, or the runner's `gif` step enclosing the steps that show
the behavior. Clip retakes follow [TEST-RUN](TEST-RUN.md)'s fresh-boot rule.

| Use | Required envelope |
| --- | --- |
| README (`docs`) | Whole frame, 1280 wide, readable text, strictly under 10 seconds; one short clip per behavior and a one-line caption with date/build |
| Test evidence (`evidence`) | 640 wide, at most 15 fps, action only for a few seconds, at most 10 MB |

For a plugin that removes something, record the same route without and
with it and show before/after. README GIFs go in `docs/media/`; every GIF
directly there is checked as README media, so keep evidence clips in the
row output instead. The checker enforces strict `<10s`; runner tolerances
and envelope checks do not replace that publication requirement.

`recording.start` needs a loaded game and an absolute host path: its folder
exists and its output file does not. One recording runs at a time. Eden
accepts `fps` 1–30 (default 10), `width` 160–1280 (default 640), and
`duration_ms` 100–120000 (default 30000). GPU readback slows the game;
gate route waits accordingly. Dropped frames retain their timestamps.
The file appears only after finalization. Poll `recording.status` and check
final `state`, `error`, `frames` and `dropped_frames`; an accepted start is
not proof of an output file.

```json
{"gif": {"name": "hover", "preset": "docs", "out": "docs/media/hover.gif",
         "steps": [{"play": "hover"}]}}
```

`name` identifies the row clip; optional `out` is a path inside the repo.
Use `docs`, `evidence`, or a plan/row `gif_presets` name. The runner starts
recording, executes nested steps, stops, trims and encodes at the preset
width without cropping. Width, fps, length (`duration_s`/`duration_ms`) and
size (`max_bytes`/`max_mb`) may be overridden. An override outside the
two envelopes needs a `reason` copied into the receipt; it cannot widen
Eden's API limits. Apply the intended preset policy yourself: the current
union-envelope implementation does not fully enforce the named envelope.

Dry-run checks the preset, reason, output path and recovery list. Keep the
clip receipt's frame count, duration, size, SHA-256 and output path. A
rejected output or missing file is not accepted evidence.

Shrink reviewed large takes with the runner encoder or
`tools/eden-solo/encode.py`. It uses a shared palette from first/middle/last
frames, no dithering, and unchanged-pixel compression. For an Eden GIF,
set encoder fps to `0` to preserve each source frame's time. A fixed fps
changes playback speed. The old `trim_gif.py` uses the first frame's delay
for every frame; do not use that timing-losing output as faithful game-speed
evidence. Historical size experiments and the disclosed fixed-fps speed
change are in [media history](MACROS.md).

Inspect metadata and view first, middle and last frames, then the action:
the clip includes start, action and result; text reads; loading screens or
intervening menus do not invalidate the caption. Encoding success is not
behavior proof. Use the `record-ui-demo` review procedure where available.
For a particular art/icon claim, perform [EVIDENCE](EVIDENCE.md)'s decoded
expected/frame/stock comparison and same-boot mod proof before using it.

Write an italic caption below the README image: behavior, Eden, date and
build. Keep raw takes and extracted game/mod assets out of git. Reviewed
evidence screenshots and required runner reference crops are allowed under
[EVIDENCE](EVIDENCE.md), with hashes and provenance.

## Transitional baseline reference

Current concern policy is above; baseline detail remains during the migration for linked concerns and evidence extraction. The operator-approved task reading and screenshot policy take precedence.

# README GIFs from Eden

2026-10-31. Eden-GIF has a built-in GIF recorder, so no screen capture is
needed. The README GIFs of gsp-visualizer, ssbu-quiet-elite-switch,
elite-rematch and ssbu-tile-cursor were all made this way.

## What the operator wants

- **The whole screen, not cropped,** at a resolution where the plugin's text
  reads in the README: record 1280 wide.
- **One short clip per behaviour**, under 10 s, with a one-line caption below
  it.
- **For a plugin that removes something,** a before and an after clip.
  Record the same route twice, once without the plugin and once with it
  (ssbu-quiet-elite-switch's `before.gif`/`after.gif`).

## Evidence clips are not README GIFs

Clips recorded as **test evidence** (a hit, a respawn, a menu step inside an Eden row) stay small:
- `width` 640, `fps` ≤15;
- a `duration_ms` that covers only the action, a few seconds;
- ≤10 MB each.

A raw take that has to stay bigger is compressed after review (`gifsicle -O3 --lossy=80`). On 2026-11-04 full-size 30 fps
evidence takes of up to 60 s came out at 24–217 MB each, and the operator called that way too big. The full 1280 width
below is for README clips only.

## Record

`recording.start` takes:

| Field | Range | Default |
| --- | --- | --- |
| `path` | absolute path on the Eden host | required |
| `fps` | 1–30 | 10 |
| `width` | 160–1280 | 640 |
| `duration_ms` | 100–120,000 | 30,000 |

- **When:** it needs a game that has finished loading, and only one
  recording runs at a time.
- **Output file:** the folder must exist and the file must not. The GIF
  appears only after it is finalized.
- **Speed:** recording adds GPU readback, so the game runs slower and route
  waits need more slack. Dropped frames keep their timestamps, so the GIF
  plays at game speed.
- **Status:** `recording.status` reports `state`, `frames`,
  `dropped_frames`, `error` and the other fields. Check `state` and `error`
  at the end; an accepted start doesn't prove the file was written.

`tools/eden-solo/record.ps1` starts a recording, waits a lead-in, sends
moves, then polls the status until the recording finishes:

```text
record.ps1 -Label e19 -Name hover -DurationMs 8000 -Width 1280 -Fps 10 -Moves 'btn:lstick,right:700;btn:lstick,right:700;btn:lstick,right:700'
```

- **Moves:** separated by `;`. Each is either a stick push
  `x,y,ms,pause_ms` or a button press `btn:<buttons>:pause_ms`.
  `btn:lstick,right` is a tile-cursor step.
- **Output:** `/home/intpa/gsp-visualizer/eden/captures/<label>-<name>.gif`.
  This path is set in the script; edit it for another repo.

Other recorders:

- `tools/eden-pair/chain.ps1 -GifName x.gif` records on instance `s` after
  the first match. The defaults are 15 s, 640 wide and 12 fps (`-GifSeconds`,
  `-GifWidth`, `-GifFps`). Recording starts at the plugin's diag pop-up line
  or, with `-Watch @()`, after `-PopupWait` seconds. `-GifLead <n>` starts it
  n seconds earlier, so the clip leads in to the pop-up.
- The `record-ui-demo` skill (`~/.claude/skills/record-ui-demo`) records
  from frames and has a review procedure for previews of CPU input.

## A `gif` step in a test run

The operator, 2026-11-06: "during its boots grok should capture the gifs it
needs for the documentation as well so another boot isn't necessary". A row
records the moment while the other steps run:

```json
{"gif": {"name": "hover", "preset": "docs", "out": "docs/media/hover.gif",
         "steps": [{"play": "hover"}]}}
```

`name` is the clip's name inside the row folder. `out`, when set, is a path
inside the repo (the README's `docs/media/<name>.gif`). `preset` is `docs` or
`evidence`, or a name in the plan's or the row's `gif_presets`. The runner
calls Eden `recording.start`, runs the nested steps, then `recording.stop`,
trims to the preset's length and encodes the full frame at the preset width.
It does not crop.

| Preset | What the file must be |
| --- | --- |
| `docs` | full frame, 1280 wide, at most 10 s |
| `evidence` | 640 wide, at most 15 fps, at most 10 MB |

A step may override `width`, `fps`, `length` (`duration_s` or `duration_ms`)
and `size` (`max_bytes` or `max_mb`). Plugins differ, so those are defaults.
An override outside the two envelopes needs a `"reason"`, and the receipt
copies that reason. Eden's own limits still apply (fps 1–30, width 160–1280,
duration 100–120,000 ms); a reason does not widen them. The dry run checks
the preset, the reason, the `out` path and any `recover` list.

The receipt for the step holds the frame count, duration, size, sha256 and
the output path. A file that still breaks its preset is not written, and the
receipt says why.

## Shrink

Eden's 1280-wide GIFs are large (8 s clips: 15–20 MB). `tools/eden-solo/encode.py`
re-encodes them:

- It builds one shared palette from the first, middle and last frames. On
  elite-rematch's e13 and e14 clips (2026-10-31), palettes from 9 frames
  spread over the clip, or from every frame at quarter size, made no visible
  difference: with all three, every frame's mean colour error stayed under
  1.1 out of 255.
- It doesn't dither, so pixels that don't change stay identical, and Pillow
  stores only the changed part of each frame.

Full-frame clips came out at 3–5 MB with nothing lost that showed:

```text
encode.py e08-in-elite-real.gif in-elite.gif 0,0,1280,720 10 128    # 20.4 MB -> 3.3 MB
```

The arguments are the input GIF or a folder of PNGs, the output,
`x,y,w,h`, fps and the number of colours. Use fps `0` with a GIF from Eden:
each frame keeps the time Eden gave it, so the clip plays at game speed. A
fixed fps gives every frame 1/fps instead. That clip's frames were 110 ms
each in Eden, so at 10 fps it plays about 10% faster. To cut a clip, use
`tools/eden-pair/trim_gif.py <in> <out> <start fraction> <end fraction>
<frame step> <width>`.

## Check before you use it

A GIF that encodes without error doesn't prove the behaviour happened. Look
at the frames:

```text
python3 ~/.claude/skills/record-ui-demo/scripts/gif_tool.py inspect --input in-elite.gif --output-dir review-in-elite
```

This writes the first, middle and last frames and a `metadata.json` (size,
frame count, duration). View the PNGs. Then check:

- The start, the action and the result are all in the clip.
- The plugin's text is readable.
- No loading screen or menu in between makes the caption untrue.
- A claim about which picture is shown ("this tile is the mod's art", "these
  are the stock icons") is checked against the source, not by eye. Decode the
  expected image and the stock one, and put the frame between them:

  ```text
  python3 -I tools/assets/arc_get.py <out-dir> ui/replace/stage/stage_1/stage_1_end.bntx
  python3 -I tools/assets/bntx_view.py <out-dir>/stock-stage_1_end.bntx stock.png
  python3 -I tools/assets/bntx_view.py "<mod>/ui/replace/stage/stage_1/stage_1_end.bntx" mod.png
  ```

  On 2026-11-04 a worker reported a Cosmic Destination tile that was the stock
  Final Destination picture: the runs had no ARCropolis, so no mod loaded
  ([EDEN](EDEN.md), "Mods need ARCropolis"). Game-derived images stay out of
  every git repo.

## Put it in the README

Save the GIF as `docs/media/<name>.gif`. Write the caption in italics under
the image: what happens, then where the clip comes from, for example
*"Eden, 2026-10-29, build `15d3f31`."* Keep the raw recordings out of git.
