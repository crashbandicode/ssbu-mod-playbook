# Tool provenance

Copied 2026-10-31 from the paths below, unchanged unless the doc that uses
them says otherwise. Paths, hosts, displays and labels inside are this
workspace's. None of these files was in a git repository before.

| Copy | Source | SHA-256 (first 16) |
| --- | --- | --- |
| `eden-solo/gv.py` | `/home/intpa/gsp-visualizer/eden/gv.py` | `f3ca9c324fe2423b` |
| `eden-solo/solo.ps1` | `/home/intpa/gsp-visualizer/eden/solo.ps1` | `8b365ac7fd44984f` |
| `eden-solo/pair.ps1` | `/home/intpa/gsp-visualizer/eden/pair.ps1` | `d033b2ecd0ef1bc0` |
| `eden-solo/to-css.ps1` | `/home/intpa/gsp-visualizer/eden/to-css.ps1` | `5e82b2786bc21401` |
| `eden-solo/record.ps1` | `/home/intpa/gsp-visualizer/eden/record.ps1` | `aabff8a090f0731b` |
| `eden-solo/match.ps1` | `/home/intpa/gsp-visualizer/eden/match.ps1` | `72d685ba4c2b1685` |
| `eden-solo/encode.py` | `/home/intpa/gsp-visualizer/eden/encode.py` | `bd905898b89a7b05` |
| `eden-pair/launch.py` | `/home/intpa/elite-rematch/eden/launch.py` | `95f162242a18d533` |
| `eden-pair/prep.py` | `/home/intpa/elite-rematch/eden/prep.py` | `4e801a4afd4deede` |
| `eden-pair/stop.py` | `/home/intpa/elite-rematch/eden/stop.py` | `81d0da398a526613` |
| `eden-pair/drive.py` | `/home/intpa/elite-rematch/eden/drive.py` | `463dab468b4ad11e` |
| `eden-pair/local_pair.ps1` | `/home/intpa/elite-rematch/eden/local_pair.ps1` | `e8a0f8c9283efc14` |
| `eden-pair/pair.ps1` | `/home/intpa/elite-rematch/eden/pair.ps1` | `c1a281cb3794e267` |
| `eden-pair/solo_surface.ps1` | `/home/intpa/elite-rematch/eden/solo_surface.ps1` | `220e929ad98ed084` |
| `eden-pair/nav.ps1` | `/home/intpa/elite-rematch/eden/nav.ps1` | `e26ecc7bea6cb1cb` |
| `eden-pair/match.ps1` | `/home/intpa/elite-rematch/eden/match.ps1` | `3f6ee6d97253231f` |
| `eden-pair/chain.ps1` | `/home/intpa/elite-rematch/eden/chain.ps1` | `ddddab8032dcf741` |
| `eden-pair/rematch.ps1` | `/home/intpa/elite-rematch/eden/rematch.ps1` | `347c75817d083b1b` |
| `eden-pair/ratings.py` | `/home/intpa/elite-rematch/eden/ratings.py` | `375f3c38880d5285` |
| `eden-pair/grid.py` | `/home/intpa/elite-rematch/eden/grid.py` | `4add710d0325e905` |
| `eden-pair/thumb.py` | `/home/intpa/elite-rematch/eden/thumb.py` | `ca893182c72d494d` |
| `eden-pair/trim_gif.py` | `/home/intpa/elite-rematch/eden/trim_gif.py` | `0e13705c4251cbc3` |
| `switch/deploy-mtp.py` | `/home/intpa/gsp-visualizer/eden/deploy-mtp.py` | `08e1119abd593b80` |
| `switch/upgrade2-mtp.py` | `/home/intpa/gsp-visualizer/eden/upgrade2-mtp.py` | `7d6c49570eefa73a` |
| `switch/remove-mtp.py` | `/home/intpa/gsp-visualizer/eden/remove-mtp.py` | `227616979ccf2604` |
| `switch/upgrade2-ums.py` | written here 2026-11-01 (upgrade2-mtp.py's phases over a mounted card: hekate Nyx UMS or a card reader; adds a remount readback phase) | `0ac7a2ce3a044cf7` |
| `switch/replace-exefs.py` | written here 2026-11-01 (upgrade2-mtp.py's and upgrade2-ums.py's phases for Skyline's `exefs/subsdk9` / `exefs/main.npdm`, one shared transaction over an MTP or mounted-card transport; adds `verify`, `extra_protected` and a readback of the whole `exefs/` folder). Tested against a fake card; one read-only `preflight` ran on the real card 2026-11-01 16:02 ET (`preflight_verified`: `subsdk9` 9959ecb13eab6db8, `main.npdm` 0953885117632821), then an MTP `install` at 22:42Z (the card's `exefs/subsdk9` is ca627e6a91e4a6df, its rollback copy `ultimate/skyline_exefs/backups/pre-ca627e6a91e4a6df.subsdk9`). Added DBI FTP 2026-11-19 using the session and listing patterns from `ssbu-switch-remote/tools/card/own_install.py` at `70e7aa33450de124aea2a580f922da09f3963525` (MIT). The same transaction uses anonymous sessions, double readbacks and its verified archive and a fresh destination listing before any FTP delete fallback. The FTP change was exercised against a loopback fake card. The DBI 912 follow-up accepts 553 from the CWD file probe and verifies the file through SIZE or binary readback. | `d7c166dc8e676809` |
| `switch/tests/fake_gio.py` | written here 2026-11-01: runs the real `gio` and injects faults from a JSON plan (test double for `replace-exefs.py`) | `eae91c5ce8f06d86` |
| `switch/tests/test_replace_exefs.py` | written here 2026-11-01: 38 tests of `replace-exefs.py` on a fake card (success, readback mismatch → rollback, failed rollback, re-run refusal, refusals before card access, both transports); 10 mutations of the script's safety steps each make it fail. On 2026-11-19 added 18 FTP cases, including read-only phases, generic rename refusals, a fresh destination listing before deletion, and verified restoration. The full suite covers 57 cases. The DBI 912 preflight regression verifies CWD followed by SIZE and makes no writes. | `d4495551adaea540` |
| `switch/tests/fake_ftp.py` | adapted 2026-11-19 from `ssbu-switch-remote/tools/card/tests/fake_ftp.py` at `70e7aa33450de124aea2a580f922da09f3963525` (MIT) for serial anonymous loopback sessions, command capture and fault injection Models DBI 912's distinct CWD replies for files and missing directories and its missing-file SIZE reply. | `20e7714fb1accb09` |
| `switch/swap-ums.py` | written here 2026-11-02 (upgrade2-ums.py's and replace-exefs.py's transaction for swapping one installed plugin NRO for another on a mounted card, as one transaction: archive the old NRO on the card, stage the new one, remove, rename in, add data-folder files, read everything back, undo in reverse on a mismatch or interrupt; `watch` trees and free space; refuses the import marker `migration.json`). Tested only against a fake card tree and a read-only copy of the real card (never run on the real card) | `8f505c73dbc670a6` |
| `switch/tests/test_swap_ums.py` | written here 2026-11-02: 76 tests of `swap-ums.py` on a fake card tree (success, reverse swap, readback mismatch → rollback, interrupt before and after the rename, rollback that fails, foreign files left alone, re-run refusal, every refusal before card access); the O_DIRECT path runs with `TMPDIR` on a disk and `SWAP_UMS_EXPECT_DIRECT=1` | `24a82f28145bfb1c` |
| `switch/tests/mutate_swap_ums.py` | written here 2026-11-02: 32 mutations of the script's safety steps; each makes the suite fail (2026-11-02: 32 killed, 0 survived) | `1c9f5cd4c12e5041` |
| `switch/examples/prep-install.ps1` | `<session scratchpad>/prep-install.ps1` | `9f95ef3a514e5061` |
| `switch/examples/mtp-run.ps1` | `<session scratchpad>/mtp-run.ps1` | `3dbbfd6933fbaddd` |
| `switch/examples/prep-remove.ps1` | `<session scratchpad>/prep-remove.ps1` | `82b4a9475663ed55` |
| `switch/examples/mtp-remove-run.ps1` | `<session scratchpad>/mtp-remove-run.ps1` | `7781a8aa785ad526` |
| `build/nro_size.py` | `<session scratchpad>/nro_size.py` | `91f40ad3459e01ca` |
| `build/scan-offsets.py` | `<session scratchpad>/scan-offsets.py` | `2b3281b3de9d2e1a` |
| `release/examples/make_assets.py` | `<session scratchpad>/make_assets.py` | `b91a03bf80cf85e7` |
| `release/examples/publish-releases.ps1` | `<session scratchpad>/publish-releases.ps1` | `bcbb511788bcaaa4` |

`eden-solo/step.ps1` is new: `<session scratchpad>/e18-step.ps1` with the run label as a parameter and `+` for button combinations.

Changed later on 2026-10-31:

| Copy | Change | SHA-256 (first 16) |
| --- | --- | --- |
| `eden-solo/encode.py` | fps `0` keeps the source GIF's frame times | `4fdfec3d1e60f546` |
| `eden-pair/chain.ps1` | copied again: `-GifWidth`, `-GifFps`, `-GifLead` | `fb12cddc7379d9bf` |
| `eden-pair/examples/queued-run.ps1` | new: `<session scratchpad>/online-release.ps1` (elite-rematch e26), plus a header comment and the MemAvailable check | `f1fa430873fb7d97` |
| `eden-solo/examples/retry-batch.ps1` | new: `/home/intpa/ssbu-gnw-judge-log/eden/run_free_batch.ps1` (runs g39–g49), plus a header comment | `0289c04e1564ec0b` |
| `eden-solo/examples/css_check.py` | new: `/home/intpa/ssbu-gnw-judge-log/eden/css_check.py`, plus a paragraph in the docstring | `35a5938b7f5051cc` |

Changed on 2026-11-01:

| Copy | Change | SHA-256 (first 16) |
| --- | --- | --- |
| `switch/upgrade2-ums.py` | verify remounts through the user's systemd manager when polkit refuses `udisksctl mount` over ssh, and checks with `pkcheck` that a way is authorized before it unmounts (the first version left the card unmounted over ssh). Tested on the Surface over ssh with a vfat loop image: the first version failed `NotAuthorizedCanObtain`, this one reached `verified_after_remount` | `0fc2930e1fd9849a` |
| `switch/upgrade2-ums.py` | the card is never unmounted (operator rule): every card read is O_DIRECT (page-cache drop where refused), and verify re-reads in place, checking the mount id is unchanged. Tested on the Surface over ssh with a vfat loop image: every read `o_direct`, `verified_in_place` with the same mount id; after three bytes of the installed NRO were changed in the image behind the page cache, a normal read still showed the old bytes and a second verify failed on that file | `8fba8d5e74772bb6` |
| `switch/upgrade2-ums.py` | after a successful install or verify, prints that the card is still mounted and that the user unmounts or ejects it (the user connects and disconnects the Switch) | `9c4b67412863c078` |
| `switch/upgrade2-mtp.py` | after a successful install, prints that the Switch is still connected and that the user closes DBI | `041b9fa731a9784e` |
| `switch/deploy-mtp.py` | the same closing line after a successful install | `ff82b5ac2b123df1` |
| `switch/remove-mtp.py` | the same closing line after a successful remove | `560194b44417cdde` |
| `switch/upgrade2-mtp.py` | the approval's `data` folder may hold upper-case letters (`ultimate/[A-Za-z0-9_-]+`, as `upgrade2-ums.py` already allowed): the Training Modpack's folder is `ultimate/TrainingModpack`, which the lower-case check refused before any device access | `909b531078fa11cd` |
| `switch/remove-mtp.py` | the same `data` check | `f9b175ac8a7232bf` |

`check/compliance.py` gained a machine row on 2026-11-01: it fails when a
tracked script in the plugin repo, or a script under the playbook's
`tools/switch`, mounts, unmounts, ejects or powers off a disk (`udisksctl
mount|unmount|power-off`, `gio mount`, `umount`, `eject`, `mount`), as a
shell line or an argv list. It flagged the `48b1471` `upgrade2-ums.py`, which
unmounted the card; the current tools and the 24 local plugin repos pass.

On 2026-11-02 `check/compliance.py` reads `src/` recursively for the version, guard, allocator and test rows, and finds the
version in `src/model.rs` or any file under `src/model/`: plugins that split model and runtime into folders
(TrainingRoomsForUltimate: `src/model/`, `src/runtime/`) were reported as failing rows they meet.

On 2026-11-11 (the commit asked for the operator's approval; merged into main as 913fa6e, 2026-11-13) `check/compliance.py`'s release-profile row accepts `opt-level = "z"`
as well as `"s"`; CHECKLIST says when `"z"` may be used (measured). TrainingRooms rc.8 R6: −741,376 B resident, the
timed Eden rows held, and its stack analysis had to read prologues the machine outliner split. Test:
`test_compliance.py` `ReleaseProfile`.

On 2026-11-11 `check/compliance.py`'s refusal row compares refusal text, not touched lines. The lines the branch
removes that the row would count (not comments, assertions, test files or `#[cfg(test)]` items, read in the merge
base's files) give each refusal literal a budget, and an added line whose literals all have budget adds no refusal. A
touched call (`f(b, "…")` to `f(&b, "…")`) or a move between files adds none. A further copy of an existing message
still counts, and so does a refusal offset only by a removed comment or assertion. A base file git can't show gives
no budget. TrainingRooms rc.8's by-reference change was reported as two new refusals. Tests: `test_compliance.py`
`Refusals` (four new cases).

Written for this repo on 2026-10-31, not copies: `check/compliance.py` (fills
the machine rows of `docs/CHECKLIST.md`), `skill/install.py` (links the skill
into the four agent tools and writes the pointer blocks) and
`../skills/ssbu-mod-playbook/SKILL.md`.

Written here 2026-11-04 (TrainingRooms primary), not copies: `assets/bntx_view.py` (decodes the first texture of a BNTX:
Tegra block-linear deswizzle + Pillow's BCn decoder; BC1–BC5, BC7 and RGBA8) `53bdd2c078f1da4e`; `assets/arc_get.py`
(extracts named files from the local stock 13.0.5 `data.arc` with `/home/intpa/ssbu-recomp/tools/nro/arc_extract.py`)
`6d2a2e46f0c3b37f`; `assets/zshim/zstandard.py` (the two calls `arc_extract.py` needs, over Python 3.14's stdlib
`compression.zstd`) `1fb083fa75a184aa`. Checked by decoding stock and Cosmic Destination `stage_1_end` / `stage_2_end`
(BC7, 108x90 and 512x256) and matching them to Eden frames; no host tests yet.

Written here 2026-11-16 (harness-lib), not copies:
- den-runner/routes.py, outes_online.py and 	est_routes.py: the shared setup blocks ([ROUTES](../docs/ROUTES.md)).
- den-runner/library/routes/:
  - index.json and macros/: the tile, tap and coin macros were authored there. eset-lra, 	r-save and
    	r-load are byte copies of remaining-2's eset-center, chord-save and chord-load.
  - 	ables.json: generated by uild_tables.py from TrainingRooms' oster_grid.rs and lobby_stages.rs.
- **Proof:** the blocks were booted on the XPS as harness-lib-xps-1, with 11/11 rows passing (ROUTES  Proof).
- **Not booted:** the online blocks restate checks from runtime-calibration-4 and eden-ldn-coexistence, and were not
  booted again.

den-runner/library/routes/refs/main-recorder.png (2026-11-16): the sidebar icons, ROI [1150,160,1230,640], cut from a main-menu recorder frame of harness-lib-ally-1 (reviewed); 1f11f1a2355a14dc.

den-runner/cut_session.py and 	est_cut_session.py (2026-11-16): copied from ssbu-switch-remote ebde113 	ools/host/cut_session.py and 	ools/host/tests/test_cut_session.py; the test's import path now points beside the module. They cut the gif-start/gif-end clips, per-row clips and shot stills out of the one session recording (GStreamer imported lazily). Live proof: live-rec-1 (client 91512f5, playbook 16cc318; artifacts/harness-lib/live-rec-1). 31/31 tests.

2026-11-16 (harness-lib-sol): per-row OBS files imported from switch-remote-2 patch `ebf4d8e2b6d7e25dcad837422b78482763569997`
(base `d77d0b2`): `eden-runner/{runner.py,switch_hw.py,cut_session.py,test_routes.py,test_cut_session.py}` and ROUTES.
The row-start split waits for `RecordFileChanged`; the next split/stop links the closed file into its row and receipt.
Source packet: `smash-training-lobby/artifacts/switch-hw-session-1/playbook-patch/`; live proof supplied by that lane:
`artifacts/switch-hw-clip-proof-1` (three rows, three files). This review made no live OBS or console calls.
Review correction: the still cutter closes the prior decoder before opening the next, reusing one within each file.
`StillResources.test_per_row_stills_keep_only_one_decoder_open` failed with three open decoders on the imported patch
and passes with a peak of one. Validation: test_routes 44/44, test_runner 44/44, test_cut_session 34/34 (with GStreamer).

2026-11-16 (harness-lib-sol), tile-cursor accepted frame-repeat fix: `ssbu-tile-cursor` source `8f830a2`, branch
`frame-repeat` from `15d3f3153278e62510252ed7ce35581af6a5c73d`; compiled on XPS, never Surface. Host Rust 1.98.1
(`48a229cea`), Skyline Rust 1.95.0-nightly (`a423f68a0`, 2026-02-13), Skyline 0.6.0. NRO SHA256
`66f00442eb7a93de3196714aebba85807e106663e4e9d5dbb3894d7c5e071ecd`; new pin
`/home/intpa/trainingrooms-eden-pkg6/nro/aid/frame-repeat-8f830a2/libssbu_tile_cursor.nro` (Surface/XPS/Yoga).
13 host tests, zero-warning clippy, no-default build and release pass. Surface proof `tile-frame-surface-2`: 5/5 PASS;
SSS and CSS held repeats exactly 0->1->2->3 plus shared 2-frame walks to Battlefield 1/Mario 0, Ready to Fight.
Sole approved Surface-calibrated CSS transition deadline 180 s; same image/log discrimination. Earlier run 1 retained
its native-loading timeout at 60 s. Source branch head 84211bc adds only evidence-checker tests after build 8f830a2.
Full provenance and source patch: `smash-training-lobby/artifacts/harness-lib/{tile-frame-PROVENANCE.json,tile-frame-source.patch}`.

2026-11-22 (fusion-g4), setup-call CPU coin runner: `tools/eden-runner/test_setup_calls.py` appends modelled
`tile.cursor/1` CPU fields to the recorded l8w fixtures. `library/routes/topologies/sss-settled-topology.json` is a
byte copy of bridge load 9's settled SSS topology, SHA-256
`b2823df6e24a55f0fe8dc13c54a26299ba50b475b74ea57038de9cbd59c9e3a2`. The four existing tile macros hold their
direction for six frames as approved by the operator; no runtime acceptance is claimed here.

2026-11-22 (fusion-g4), Training setup calls: `test_setup_calls.py` uses modelled `tr.menu/1` input/ACK
transitions; `library/routes/build_tables.py --catalog-only` derives `roster_catalog` from TrainingRooms'
`src/model/swap/choices.rs`, with source checkout, commit and SHA-256 recorded in `tables.json`. The authored
`tap-l1` and `tap-r1` macros use the existing `tap-a1` frame shape and plain L/R masks. No runtime acceptance is claimed.
`test_setup_calls_rework.py` adds modelled race, timeout, settings and compatibility probes following lead review.

2026-11-22: `eden-runner/test_harvest.py` tests no-op, new-segment, new-gate and dry-run harvests against a
temporary library (5 host tests); `2eb13ee5204f28cc`.

2026-11-22: `eden-runner/trace_text.py` and `test_trace_text.py` port the read side of compact-strip's
`strip_trace.py` and notice-ui's `notice_steps.py`; `cb1781bf25583f07` and `2c7547a9f442f262`.
The 15-line real trace fixture is `eden-runner/fixtures/trace-text/set-text-excerpt.jsonl` (`418ecc8cb4c813ce`);
its source and sequences are in `eden-runner/fixtures/trace-text/PROVENANCE.md`. Twenty-six reader host tests; no live boot
proof yet.

2026-11-22: `eden-runner/trace_attempts.py` ports marker decoding from edge-juggle
`evidence/tools/attempt_boundaries.py` and plan registers from `compare_attempts.py`. The Pyra and Hero excerpts
in `eden-runner/test-data/` are identified with their full source paths and SHA-256 in `test_trace_attempts.py`.
The marker's form field is the stage form, not the CPU form. No live acceptance is claimed.

2026-11-22: `eden-runner/trace_attempts.py` also reads the separately armed `planner_chose` point in trace
order. `test-data/cp1b-hero-level9-none.jsonl` is an exact release/terminal excerpt from the Hero trace named
in `test_trace_attempts.py`; its source interval 312411–312685 contains no planner entry. These are host
decoder and control-flow probes, not runtime acceptance.

2026-11-22: the 12 `library/routes/macros/wizard-{direction}-{coarse,medium,fine}` analog pulses
are authored from Eden's `macro_file.h` full-state JSON schema: full tilt for 10 or 2 frames and 0.35 tilt
for 1 frame. Actual TrainingRooms controller-poll counts and wizard behavior need runtime proof.

2026-11-22: `library/routes/tr-menu-kinds.json` records supported direct row control kinds from
TrainingRooms `aad616bc` `src/model/menu/table.rs` (full source SHA-256 in the file), excluding
recording-owned `playback_loop`; the `tap-x1` macro is `tap-a1`'s full-state shape with X's mask.
Retired practice-mode and hitbox slots are handled from `practice_games.rs` and `legend.rs`.

2026-11-22: `library/routes/tr-setting-widths.json` records the product row widths from TrainingRooms
`7d962416` `src/model/factory.rs` DEFS, cross-checked against its product-written settings fixture.

2026-11-24: `routes.LogCursor` returns each line once. The held cursor compared the paths size, taken before its
read, with its position after it, so an append during the read looked like a truncation and the log came back from
its first line (A1 trace boot `rc15-a1-trace-2ddba1f-xps-2`: eleven times in one receipt, the file itself only
appended to). The cursor now checks the open file after the read, knows a file by device and inode, and finds the
text it read last in a replaced file. A replaced file it cannot place is left out and counted (`unqualified`); only
lines appended to it afterwards are returned, and what the old handle still had is dropped with it. `rotates=True`
keeps the read from the first byte for a plugins own rotating logs; `routes._log_cursor` chooses it by the file, never
for the bridge captures log (a Switch instance names that one file `diag_log` and `bridge_log`). `test_routes.py` `LogCursorTests` are host tests on temporary files, one with a writer thread and one
with `ui` lines in the shape of that boots; no runtime acceptance is claimed.

2026-11-24: `tools/eden-runner/packet_gate.py` and its tests use throw-away Git repositories shaped like
TrainingRooms `71158421` `src/model/bridge_state.rs` and `src/model/practice_state.rs`. The button table maps the
plugin's logical order in `src/model/chords.rs` at `71158421` to `recipe_macro.BITS`. The optional witness test
compares the extractor with a recorded `tr.menu` snapshot when supplied. Host tests only; no runtime acceptance.

2026-11-24: `eden-runner/fixtures.py` ports the Recording page role switch from
`dispatch-pack-v2/xps/waiting_room_docs.py` and `rm_lobby_retake_steps.py`, the popup role from `repro_hw.py`, and
the first-use Guard sequence from `f15_room_steps.py` and `edge_steps.py` onto the canonical state readers.
The waiting-room mark and readback, including the waiting-battle predicate, and the offline seed settings follow
`install2/flight-prep/xps_fd_room.py` and `xps-stop-71158421/xps_fd_room.py`; the latter seeds Behavior Stop rather
than Attack. Panel readback follows `dispatch-pack-v2/xps/docs_lobby_tail.py`.
`take-p1-ground-jump` and `take-p1-jump-only` are compiled from `f10/fixtures/take-T.json`; X as Jump has not been
checked against a run profile. `tap-zr1` and `recording-popup-chord` reproduce the flown `rm-tap-zr` and
`rm-popup-chord` frame shapes. Host tests only; no runtime acceptance.

2026-11-24: `eden-runner/test-data/i2-play-p1-chord.edenmacro.json` is copied from
`install2/flight-prep/dispatch-pack-v2/yoga/segments/i2-play-p1-chord.edenmacro.json`, SHA-256
`437b038c2da32690c3ee43c923ca3c9d9e0fe97a70eb69bfdb75eeb523e66cb3`. The event extractor tests model
TrainingRooms `71158421` `src/model/bridge_events.rs` lines 23–26, 45 and 132. Host tests only; no runtime acceptance.

2026-11-24: `eden-runner/fixtures.py` ports finite watch qualification from
`dispatch-pack-v2/xps/bridge_watch_steps.py`, field parsing from `f16_judge.py` and effective partner readback
from `f15_room_steps.py`, using canonical native state, log cursor and bridge request helpers. The fighter
lines in `test_fixtures.py` are modelled, not recorded. Host tests only; no runtime acceptance.

2026-11-24: `eden-runner/runner.py` separates visual and deterministic blockers, binds visual reads to
runner captures, and ties producer receipts to packet and canonical runner inputs. Offline fake-client
regressions model the reviewer and F10 missing-delivery controls; host tests only, no runtime acceptance.
Named GIF outputs are excluded from the input identity, including nested and recovery steps; collisions
with packet inputs are refused and do not gain that exclusion.

2026-11-24: `eden-runner/runner.py` binds visual reads to the still's own capture, an explicit source,
or a unique matching capture; duplicate hashes no longer select an arbitrary receipt. Judge verdicts
clear visual blockers only, leaving deterministic blockers unresolved. Host tests only.

2026-11-24: `eden-runner/fixtures/bridge-on/stub-on-yoga-71158421/bridge.log` copies lines 1–40 from
`rc15-i2-71158421-yoga-1/out/bridge-run/bridge.log`; SHA-256
`0f47489cad7530d5e5f6e1aefb95399f6cc13ee0265dfd72b84bbabb0f6ca076`.
`events.log` copies lines 1–4 from that run's `out/post-run-offline/user/sdmc/ultimate/ssbu-hw-bridge/events.log`;
SHA-256 `c80f086732a0ef13253a2edccb7d66ce1b07d525768278a5e0ae73a90992804d`.
These are host test inputs, not runtime acceptance of this change.
