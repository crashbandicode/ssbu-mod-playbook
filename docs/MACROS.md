# Reusable input segments and macros

Test-run rules **1–15** now live in [TEST-PLAN](TEST-PLAN.md), with execution
in [TEST-RUN](TEST-RUN.md), schema in [RUNNER](RUNNER.md) and persistent
coordination in [WORKERS](WORKERS.md). Tests use the shared runner; manual
exploration is only for a missing segment. One driver owns each instance.

Split routes at loads: record each segment from its **settled start screen**
through the last input, leaving load slack out. Keep independent settled
segments in `eden/` with SHA256. A marker branch replays its prefix and
therefore is not independent. Between every segment use a capped log or
cheap screenshot gate, polling every second or two; retry a screenshot that
fails before the render window exists. Fixed sleeps only when neither gate
exists, rounded up for slower runs/recording. On cap: screenshot, stop,
report. Never play the next segment onto an unseen/unchecked screen.

Use tile-cursor/logged settled checks for exact CSS/SSS picks. Menu stick
bursts can explore a missing segment, then record it once. Do not extend the
press/screenshot exploration loop into the test itself. For a missing segment,
`step.ps1` accepts `b:<button>[+<button>][:wait]`, `s:<x>:<y>:<ms>[:wait]`,
and `w:<ms>`; inspect its screenshot after every segment. Menu stick pushes
are 250 ms; `gv.py press` holds for its time plus 500 ms, then releases.
`gv.py keepalive <label> <stop-file> <seconds>` holds down and taps ZL every
0.7 s until the stop file; keep it in the one driver's scripted route. Historical sleep
routes retain only route-shape/discovery value in
[route history](history/ROUTES-AND-MEDIA-2026-10--11.md).

Record with Tools → Input Macros (Record new; countdown/red recording), or
control `macro.record`, `macro.marker`, `macro.stop`. Record from marker
creates a new branch file and leaves the original intact. Runner
`record-start`/`record-stop --segment <name> [--start-gate <gate>]` stores
the discovered file/hash in rows. Eden's pinned build `docs/InputMacros.md`
and `docs/InputMacrosTutorial.md` define its format/API.

Macros count displayed emulation frames, not wall time; slower game replay
keeps game pace. They restore no savestate: start from the same scene, since
loading/random behavior/menu changes still alter outcomes. Play an explicit
file, poll `macro.status` until finished with empty error, then reset input.
Use a single long macro only for a sequence needing exact human timing;
otherwise retain segment gates at loads.
Playback owns controller input (`input_busy` rejects other calls) until
finish or `macro.stop`; API playback avoids GUI focus-dependent shortcuts.
Held inputs are leases, not permanent state; `input.reset` releases all.
Runner renews long holds within Eden's 1..10000-ms lease bound.

**Chords share one poll frame.** Put every required button in the same macro
event/frame, record once and reuse. Sequential `input.button` calls, even
L3-first, can straddle frames. `lstick` as a button is L3 click, not steering.
Use logged neighbors/settled gates rather than guessed CSS/SSS positions.
Idle online players can be dropped after about 30 seconds; planned keepalive
belongs in the one driver's scripted route. Walking off is a self-destruct.

Macro JSON has name/game_id/player/duration_frames/events/markers/bindings/
parent/format/version. Events sparsely store changed frame/button-mask,
sticks (lx/ly/rx/ry in -1..1), triggers and motion. A held press is a set sample
and later clear sample; playback runs through duration_frames.

Safe edits: remove idle frames by subtracting the same amount from **all**
later samples; remove both press/release of a wrong input; move release
earlier to shorten hold; trim duration a few frames beyond the last event.
Keep ascending distinct frames, paired presses/releases, recorded game_id
and player, and at least one displayed frame between samples. Eden has no
sub-frame timing and can drop shorter inputs. Save a **new file/name** (the
library remembers paths), replay from the same scene, inspect its end
screenshot, then record SHA256 before trusting it. Record every played hash.
Recording auto-saves at **60,000 changed samples or 216,000 frames**.

Working routes/chords/fixtures enter the shared project index immediately;
harvest only reviewed passing segments. Reviewed evidence/reference crops
may be committed; extracted stock/mod assets, data.arc and dumps may not.
