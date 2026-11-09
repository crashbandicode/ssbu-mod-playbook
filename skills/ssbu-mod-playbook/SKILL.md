---
name: ssbu-mod-playbook
description: Use for creating, changing, testing, validating, releasing or installing a Super Smash Bros. Ultimate (13.0.5) Skyline plugin, or driving Eden to test one. Follow the playbook's task reading and completion checklist.
---

# SSBU Skyline plugin work

Use the authoritative clone at `~/ssbu-mod-playbook`
(`/home/intpa/ssbu-mod-playbook` on Ubuntu,
`%USERPROFILE%\ssbu-mod-playbook` on Windows; origin
`https://git.syntaxbyexample.com/intpa/ssbu-mod-playbook`).

1. Update the authorized clone with `git pull --ff-only` and record its
   commit in the project's handoff before changing anything.
2. Every agent reads `docs/START.md`, `docs/CHECKLIST.md`, and the concern
   pages its task touches completely. START provides the task matrix;
   Top-level `history/` is read on demand. A parent's read does not cover workers.
   Keep any broader global full-read requirement alongside these task pages.
3. Read each tool script before running or copying it. Build a handoff
   checklist of every M, applicable C and reason for each N/A C.
4. Every delegated brief names the clone, commit and reading order, requires
   that worker's own checklist/compliance before READY, and ends with a
   done-checklist. If the worker cannot read, it stops and reports that.

Follow the concern pages and reuse their tools. Record evidence as gates
pass, and revisit the checklist after sizeable changes. Keep the handoff
current. Global instructions and `/home/intpa/.codex/DELEGATION_ROUTING.md`
own shell, delegation, model/effort and watcher policy.

Before completion, run `tools/check/compliance.py <plugin-repo>` with the
branch `--base`, design `--trace` and explicit shipped `--nro`. Resolve every
agent row as pass, fail, `N/A: <reason>` or not authorized, with evidence.
Keep the report beside the evidence doc and disclose failed, skipped and
unverified rows in the final message. Claim success only with evidence.

Push, tag, release and card writes each require the operator's explicit
order; report when none was given. The user connects/disconnects the Switch
and controls DBI/UMS and mounting. Never mount, unmount, eject or remount the
card, or start/stop DBI/UMS; use only what the user exposed. Follow
`docs/RELEASE.md` and `docs/INSTALL.md` for those actions.

Leave all outside-repo instructions, including `CLAUDE.md` and `AGENTS.md`, unchanged.
Do not run `tools/skill/install.py` to write their pointer blocks; `--check` is permitted.
