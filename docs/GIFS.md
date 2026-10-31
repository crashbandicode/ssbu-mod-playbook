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

## Put it in the README

Save the GIF as `docs/media/<name>.gif`. Write the caption in italics under
the image: what happens, then where the clip comes from, for example
*"Eden, 2026-10-29, build `15d3f31`."* Keep the raw recordings out of git.
