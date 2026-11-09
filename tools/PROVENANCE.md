# Tool identity and provenance

The checked-out git commit and freshly measured file hashes identify the
tools in use. Record the actual playbook commit and selected script/binary
hashes with each task; old origin hashes do not identify current source.

[The historical origin/change ledger](../docs/history/PROVENANCE-2026-10-31--11-07.md)
preserves the 2026-10-31 copies, later changes, dates, hashes, source paths
and qualification limits. Remount/polkit-era card recipes there are
superseded by [INSTALL](../docs/INSTALL.md)'s no-remount contract.

Current entrypoints are [START](../docs/START.md) and the
[completion checklist](../docs/CHECKLIST.md). Use `tools/eden-test/eden_test.py`
with [LIFECYCLE](../docs/LIFECYCLE.md) for current Eden runs; solo/pair and
release examples preserve historical recipes and do not confer current
claims, restoration, authorization or qualification guarantees.

Read a script before running or copying it. Resolve external binaries,
clients and extractor dependencies afresh rather than inferring them from
source-era paths. Keep executable source, tests and data at their tracked
paths; each concern page states the tool's policy limits and human checks.
