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
change are in [media history](history/ROUTES-AND-MEDIA-2026-10--11.md).

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
