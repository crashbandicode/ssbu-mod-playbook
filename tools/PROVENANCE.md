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
| `switch/replace-exefs.py` | written here 2026-11-01 (upgrade2-mtp.py's and upgrade2-ums.py's phases for Skyline's `exefs/subsdk9` / `exefs/main.npdm`, one shared transaction over an MTP or mounted-card transport; adds `verify`, `extra_protected` and a readback of the whole `exefs/` folder). Tested only against a fake card; one read-only `preflight` ran on the real card 2026-11-01 16:02 ET (`preflight_verified`: `subsdk9` 9959ecb13eab6db8, `main.npdm` 0953885117632821) | `60cd5805592de2a6` |
| `switch/tests/fake_gio.py` | written here 2026-11-01: runs the real `gio` and injects faults from a JSON plan (test double for `replace-exefs.py`) | `eae91c5ce8f06d86` |
| `switch/tests/test_replace_exefs.py` | written here 2026-11-01: 38 tests of `replace-exefs.py` on a fake card (success, readback mismatch → rollback, failed rollback, re-run refusal, refusals before card access, both transports); 10 mutations of the script's safety steps each make it fail | `1e0fe2daa2f3eeb5` |
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

Written for this repo on 2026-10-31, not copies: `check/compliance.py` (fills
the machine rows of `docs/CHECKLIST.md`), `skill/install.py` (links the skill
into the four agent tools and writes the pointer blocks) and
`../skills/ssbu-mod-playbook/SKILL.md`.
