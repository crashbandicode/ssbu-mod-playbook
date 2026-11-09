# Private profiles and pinned inputs

Read this page for any Eden profile, fixture, pin or mod change. Use the
current [lifecycle](LIFECYCLE.md) private clone, not the historical pair
`prep.py` full-copy recipe. Never change profile `a`, installed profiles,
another worker's roots or any profile outside the run's ownership.

**Link what is identical; copy what differs or is written in place.** Never
copy the whole game-content tree for each profile. Immutable byte-identical
game content, firmware, NROs, archives and unchanged sources may be links.
Saves, replays, rewritten INI/config, shader caches, logs and unknown mutable
files need real private copies. A write through a hard link changes every
copy sharing it. After any `cp -al`, break writable links before first boot.
Compare `du -sh` with `du -sh --apparent-size` and retain the output;
the first (allocated-size) number should stay small.

The lifecycle's conservative clone links game Contents/firmware and copies
mutable/unknown files. Its pristine snapshot precedes **all** staging and
config changes and covers the complete clone: bytes, modes, unknown files
and empty directories. Symlinks and special files are refused, not followed.
Full hashing includes immutable content and can be costly.

Use absolute executor-host paths without symlink ancestors. Resolve the
release's `current` alias before making a pin; keep the resolved path for
Down. Pin each input as `{"path":"/absolute/file","sha256":"64hex"}`:
application, shared runner, rows, NROs, fixtures and platform helpers as
applicable. Verify before claim and check staged copies again. Results name
the resolved application path and SHA256; disclose different builds rather
than pooling their results silently. An optional expected game version or
playbook commit is a declaration check, not proof of all executed bytes.

Application and control-client identities are separate. The lifecycle uses
the pinned AppImage's adjacent client; the Unix runner currently defaults to
`/home/intpa/Applications/Eden-GIF/current/tools`. Record the actual helper
identity too; the application pin alone does not pin that client.

Stage only the approved pinned plugins and files. The lifecycle sets all
pre-existing NROs aside, installs the pin list, rebases/verifies NAND and
SDMC paths and starts fresh output folders. Fixtures belong in the plan
before Up; see [TEST-PLAN](TEST-PLAN.md). Keep original snapshots separate
from run outputs: the original snapshot never becomes the output directory.

If a row depends on any `ultimate/mods` stage, fighter, UI or sound
replacement, pin the **card's `libarcropolis.nro`**. Historical card copies
are under `/home/intpa/ssbu-cpu-pilot-install/`,
with digest prefix `92171b2f…`; freshly hash/pin the actual card copy.
Without it the isolated plugin list loads stock assets. Before judging the
plugin's view, prove the mod loaded on the game's own screen in the **same boot**; a stage uses the
native stage select. Retain the config pin and native-screen proof. Decode
expected and stock art for visual claims as required by [EVIDENCE](EVIDENCE.md).

Down stops all owned processes before restoring set-aside NROs, saves,
replays and the complete snapshot; a separate full verification precedes
release. Windows also verifies the installed source inventory unchanged;
see [WINDOWS](WINDOWS.md). Cleanup failure prunes nothing.

**After a clean Down/release**, export new/changed post-run files against the
snapshot inventory, then prune work-root profiles, `state/snapshot-*` and
`state/post-run-*`. The wrapper sends exports to `out/post-run-<key>/`;
direct lifecycle Down defaults to `state/evidence/`, or `--evidence-dir`.
Evidence cannot be the state directory itself or lie inside a pruned tree.
Each `post-run-<key>.json` manifest records copied path/size/SHA256, listed
caches, known-content count and removed snapshot files (`gone`). Copies are
hash-verified. Same-size files sharing snapshot inode or copy2 modification
time skip hashing; others compare bytes. Already-known snapshot/pinned app,
plugin and fixture bytes, including set-aside originals, are not new output.

Only rebuilt caches `user/cache/shader`, `user/shader`, and `xdg-cache` are
**listed, not copied**. Deletion refuses symlinks/junctions rather than following
them. Windows clears read-only attributes only on private directories or
single-link files; it leaves shared installed-app/source links unchanged and
records skipped paths. Empty work roots/source-inventory copies are removed;
journal, actions, snapshot/restore/verify receipts and `run-*` logs remain.

Inspect journal `prune`: copied/listed counts, total `bytes`, `unique_bytes`
(files with **every link** counted in the pruned trees), df before/after, removed/skipped/left
paths and error/done state. RESULTS keeps its `Prune:` line. A prune error
does **not undo release**: retry Down with the same config; saved evidence
(`evidence_saved`) is not copied again while interrupted deletion finishes.
`--keep-state` is only for an explicit request; it skips export/prune for
debugging, and a later Down without it prunes. These flags are Down-only.
Keep NROs, exported run files, receipts/logs/reports under their run label.

Implementation: [clone, pin, stage and snapshot functions](../tools/eden-lifecycle/lifecycle.py).
Historical paths, digests and the ARCropolis incident are in
[route/media history](history/ROUTES-AND-MEDIA-2026-10--11.md).
