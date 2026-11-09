# Card transactions and connection ownership

Write to the SD only on the operator's explicit order for that action.
The user connects/disconnects the Switch, starts/stops DBI or hekate UMS,
and mounts/unmounts/ejects the card. The agent uses only the MTP endpoint
or mount point the user exposed. Never mount, unmount, eject, power off or
remount the card, or start/stop DBI/UMS. If it is not visible, stop and ask
the user. Compliance scans scripts for forbidden disk commands; passing
that scan does not establish actual connection ownership or authorization.

Read the selected script before use. Verify its current identity and the
current operator grant, endpoint, files, paths and hashes. Approval booleans
or a historical example are not a new grant. One writer owns the card
across all tools and output directories; per-folder locks do not coordinate
separate transactions automatically.

| Change | Tool under `tools/switch/` | Phases |
| --- | --- | --- |
| Replace plugin over DBI MTP | `upgrade2-mtp.py` | `preflight`, `install` |
| Add plugin over DBI MTP | `deploy-mtp.py` | `preflight`, `install`; inspect its configured name/data/hash |
| Remove plugin over DBI MTP | `remove-mtp.py` | `preflight`, `remove`, archiving first |
| Replace plugin on exposed mounted card | `upgrade2-ums.py` | `preflight`, `install`, `verify` |
| Swap mutually exclusive plugins on mounted card | `swap-ums.py` | `preflight`, `install`, `verify` |
| Replace Skyline loader over MTP or mounted card | `replace-exefs.py` | `preflight`, `install`, `verify` |

Use a transaction folder containing the NRO where applicable and
`operator-approval.json` (`operator_order`, `operator_install_authorized`,
`name`, `data`, `old`, `new`; mounted transport also names `mount`). Follow
the selected tool's schema; do not assume every tool has identical fields.

Preflight lists the card, keeps local copies of every plugin and protected
config, and checks the installed file's SHA-256 against `old`. Install
keeps a card rollback copy, normally
`<data>/backups/pre-<new16>.nro`, writes the new NRO, then reads back it,
every other plugin and protected files. Mounted-card tools re-read in
place using cache-bypass reads: inspect the recorded O_DIRECT or documented
fallback method rather than promising every read was hardware-direct.
The card stays mounted; never remount it for verification.

Keep every JSON phase receipt. An existing receipt means inspect the
operation/recovery state; do not re-run that phase or delete its receipt as
a retry recipe. A mismatch requires rollback and readback, with retained
failure evidence. Failed rollback is recovery-required, never success.
MTP add/replace/remove read back during their operation and have no separate
`verify` CLI; do not invent a phase.

`replace-exefs.py` targets only `exefs/subsdk9` or `exefs/main.npdm`.
It snapshots the whole `exefs/` folder, plugins, protected configs and
`extra_protected` files; verifies target and every copied file; and restores
and reads back the old file if one differs. Keep rollback at
`<data>/backups/pre-<new16>.<name>`, with `data` outside `exefs/` (for
example `ultimate/skyline_exefs`). Use its MTP or `transport: ums` schema.

`swap-ums.py` archives/readbacks the old NRO as
`<data>/backups/removed-<old16>.nro`, stages the new one, removes the old,
renames the staged file into place, and adds new data files under
`ultimate/<folder>/<file>`. Never overwrite data or add an import marker
`migration.json`. Snapshot/readback coverage includes plugins, `exefs/`,
protected configs, `extra_protected`, and `watch` trees (hash every file
up to 30 MB and list larger ones); check free space.
On mismatch/interrupt after removal, undo in reverse: data made by this run,
new NRO, then old NRO restored and read back. Accept only fully swapped or
verified original state. Exchanging `remove` and `add` in a fresh authorized
transaction reverses a completed swap.

For several files, use one transaction folder per file, with a fresh
preflight immediately before each install so it snapshots earlier files'
new bytes. Write Skyline's loader last, after the other files are verified.
Do not tell the user to disconnect on an intermediate tool's closing line;
wait for the final required verify/readback. Then report all writes complete
and that the user can disconnect. Keep per-file phases, rollback/readback
and final verification evidence. [RELEASE](RELEASE.md) covers publication.

The Switch scripts' fake-card suites and historical read-only/loop-image
checks are documented in [provenance history](history/PROVENANCE-2026-10-31--11-07.md).
They do not prove a new physical-card operation or grant current authority.
Remount-era provenance is superseded by the no-remount rule above.
