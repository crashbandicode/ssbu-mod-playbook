# Documentation clips, GIF previews and evidence

## Documentation and tutorial MP4s

A clip is legible and shows the effect. Use one short MP4 per behavior,
trimmed to the action, loop-friendly and about 10 seconds at most. Main
clips are about 1280×720. Name a close-up that makes a small detail readable
`<name>-zoom.mp4` and use whatever size makes the detail readable. For a
plugin that removes something, record the same route with and without the
plugin so the clips show the before and after.

Capture at 1920×1080 when possible, keeping the whole screen readable. For
a main clip, the recommended encode uses H.264 (`libx264`, `yuv420p`),
scaling to 1280×720 with Lanczos. Keep the source frame rate, including
60 fps when gameplay was recorded at 60 fps. Use CRF 23–24, the `slow`
preset, `+faststart` and no audio track. Adjust the in-point and duration
in this example to the action.

```powershell
ffmpeg -ss 2 -i /path/to/capture.mp4 -t 6 -map 0:v:0 -vf 'scale=1280:720:flags=lanczos' -fps_mode passthrough -c:v libx264 -pix_fmt yuv420p -crf 23 -preset slow -movflags +faststart -an /path/to/repo/docs/media/demo.mp4
```

Save the clip under `docs/media/` and embed it with a one-line caption
that gives the date and build. Forgejo does not play a `<video>` whose
`src` is a relative path: it serves the file page instead of the video.
Use this repository's forge media URL of the committed clip's blob as `src`:
`https://<forge>/<owner>/<repo>/media/blob/<blob id>`. Get the id with
`git ls-tree HEAD docs/media/demo.mp4` after committing the clip. The id
changes when the clip's bytes change, so update the `src` with the clip. A
relative `src` to a committed clip also resolves for renderers that support it.

Forgejo's sanitizer strips `muted`, `loop` and `playsinline`. Keep all five
attributes for other renderers; `controls` lets a viewer press play when
autoplay is blocked. Add a relative "Open the clip" link under the caption;
adjust its path for other pages. The video check fails only when a `<video>`
does not resolve. Missing attributes, size, frame rate, codec, fast start,
audio and length are reported as notes.

```html
<video src="https://git.example.com/owner/repo/media/blob/<blob id>" autoplay muted loop playsinline controls></video>

*Action. Eden, YYYY-MM-DD, build `commit`.* [Open the clip](docs/media/demo.mp4)
```

## Test evidence

Keep the 1080p evidence clip and a 2–3× zoom crop that shows the relevant
detail. MP4 is suitable for both. Keep the original recordings out of git.

## Film a 1080p60 take (2026-11-27)

One recorder for every host: `tools/eden-runner/film.py`. In a hook use
`with film.take(ctx, name, cap) as cue:`: `ctx` is the row context, `name`
names the take and `cap` is its time cap in seconds. Optional arguments are
`before=0.8`, `after=1.5`, `stop_after_body=False`, `fps=60` and
`on_started=None`. The row gets `<name>-1080p-capture.mkv`,
`<name>-1080p-capture.log` and `<name>-take.json`.
The receipt keeps `display`, `size`, `fps`, `source`, `cues_after_first_bytes`,
`capture_status` and `cap_s`, plus `backend`, `host_os`, `recorder`,
`film_probe` and `length_vs_cap`.

As row steps:

```json
[
  {"run": "film:fullscreen"},
  {"run": "film:record", "args": {"name": "...", "seconds": 10, "play": "<routes macro>"}}
]
```

`film_probe` (also `python3 film.py probe <film>…`) uses `ffmpeg -f framemd5`,
not ffprobe. It reports `duration`, `size`, `frames`, `width`, `height`,
`captured_fps`, `distinct_per_second` (frames whose hash differs from the one
before, per film second), `distinct_fps_min` and `distinct_fps_median`.
Repeated frames mean the screen had not changed; a still menu legitimately
repeats frames, so judge seconds of live gameplay.

The film lanes' published-clip numbers (2026-11-27) are at least 55 distinct
frames in each gameplay second and a file length equal to the take length.
The tool reports; it does not gate.

ffmpeg is found in order at `$EDEN_FFMPEG`, on `PATH`,
`~/eden-runs/_eden-test/ffmpeg`, then on Windows
`%USERPROFILE%\.local\bin\ffmpeg.exe`. On 2026-11-27 the Surface and Yoga had
none on `PATH`.

Check a host before a film run:
`python3 film.py smoke --out <dir> --seconds 3 [--display :N]`. Linux needs
the display; Windows refuses one. On 2026-11-27 the Ally's 3 s smoke used
`ddagrab` at 1920x1080: 179 frames, 59.67 captured frames/s. `gdigrab`
measured 47–55 frames/s there and is not offered. An Ally film run needs the
interactive session: the recorder starts through a one-shot scheduled task
with `pythonw.exe`. The Ally `film.py smoke` remains the Windows capture
evidence; no Ally boot has filmed yet (`xplat-ally-3` through `-7` stopped
before the film row).

Linux `x11grab` is host tests only; no boot has filmed through it yet. The
film lanes' own GStreamer capture remains what was flown.

## GIFs for image previews

Use a GIF only where an image is required, such as a README preview.
Use at least 12 fps for gameplay and 10 fps for menus, or keep the actual
source rate if it is lower. Never use keyframe-only clips or drop frames to
meet a size budget. Trim to the action, crop to the
useful region where the preview permits it, turn off dithering and try 128
colors if text stays readable. If the GIF is still over budget, report its
size for review. A README GIF is 1280 pixels wide and under 10 seconds.

Eden-GIF can record image previews directly. The gsp-visualizer,
ssbu-quiet-elite-switch, elite-rematch and ssbu-tile-cursor README previews
used its built-in recorder.

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

Record an image preview during its test row so the same boot captures the
action and runs the checks.

```json
{"gif": {"name": "hit", "preset": "preview", "out": "docs/media/hit.gif",
         "steps": [{"play": "show-hit"}]}}
```

`name` is the clip's name inside the row folder. `out`, when set, is a path
inside the repo. On Eden the runner calls `recording.start`, runs the nested
steps, then calls `recording.stop`. It encodes the full frame at the preset
width and trims to the preset duration. On Switch the runner marks the span
in the session recording for offline cutting.

| Preset | Use | Default width | Default fps | Duration cap |
| --- | --- | ---: | ---: | ---: |
| `preview` | Gameplay image preview | 1280 | 12 | 10 s |
| `preview-menu` | Menu image preview | 1280 | 10 | 10 s |
| `docs` | Legacy image preview recipe | 1280 | 12 | 10 s |
| `evidence` | Internal review GIF | 640 | 10 | 10 s |

The legacy `evidence` GIF preset also has a 10 MiB cap. Retain the 1080p
clip and zoom crop for test evidence. Use the original 1080p recording
for documentation and tutorial MP4s.

A plan or row may define more `gif_presets`. A step may override `width`,
`fps`, `length` (`duration_s` or `duration_ms`) and `size` (`max_bytes`
or `max_mb`). Overrides outside the full-frame or legacy evidence
capture limits need a `"reason"`, which the receipt records. Request at
least 12 fps for gameplay or 10 fps for menus. Set `"scene": "menu"` to
identify a menu clip when using a legacy or custom preset. The runner and
offline cutter enforce these minimums for published GIFs across all preset
names. They accept a lower cadence when independently measured from the
source. Automatic row review GIFs and `evidence` GIFs kept in the row folder
without `out` use the internal review settings.

Eden's capture limits are 1–30 fps, 160–1280 pixels wide and 100–120,000 ms.
The dry run checks the preset, the reason, the `out` path and any `recover` list.

The receipt holds the frame count, duration, size, SHA-256 and output
path. If the file still breaks its preset, the receipt gives the reason
and the output is refused.

## Trim and shrink a GIF preview

`tools/eden-solo/encode.py` uses one shared palette and no dithering, so
unchanged pixels stay identical and Pillow stores only changed rectangles.

```text
encode.py /path/to/raw-preview.gif /path/to/preview.gif 0,0,1280,720 0 128
```

The arguments are the input GIF or a folder of PNGs, the output path,
`x,y,w,h`, fps and the number of colors. Pass fps `0` to preserve an
Eden GIF's frame delays. A positive fps sets a constant frame delay.
Use one only for a known constant-rate frame sequence. Review the result
for text readability and the minimum rate for the scene.

To cut a time range with `tools/eden-pair/trim_gif.py`, pass
`<in> <out> <start fraction> <end fraction> 1 <width>`. The frame step of
`1` keeps every selected frame. This tool uses a constant delay based on
the first frame, so use it on constant-delay captures.

## Check before you use it

Watch the exported clip and inspect the first, middle and last frames.
For a GIF preview, the inspection tool extracts those frames.

```text
/usr/bin/python3 /home/intpa/.claude/skills/record-ui-demo/scripts/gif_tool.py inspect --input /path/to/preview.gif --output-dir /path/to/review-preview
```

This writes the first, middle and last frames and a `metadata.json` (size,
frame count, duration). View the PNGs. Then check:

- The start, the action and the result are all in the clip.
- The plugin's text is readable. A GIF preview plays smoothly at the
  required rate for its scene.
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

## Caption the README demo

Include one line below the embedded MP4 preview. Describe what
happens, then give the platform, date and build. For example, *The cursor
moves to the requested tile. Eden, 2026-10-29, build `15d3f31`.* Save the silent H.264 preview as
`docs/media/<name>.mp4`. Embed its Forgejo media blob in a video player and
add a relative “Open the clip” link below it.
