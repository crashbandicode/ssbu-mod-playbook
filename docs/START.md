# Start every plugin task here

These rules apply to every agent creating, changing, testing, validating,
releasing or installing a Smash Ultimate 13.0.5 Skyline plugin, including
agents driving Eden. A parent's reading does not cover its workers.

Before work, update the authorized playbook clone with `git pull --ff-only`
and record its commit in the project's handoff. Every agent reads this page,
[CHECKLIST](CHECKLIST.md), and the concern pages its task touches completely.
Read top-level `history/` on demand; historical observations are not current host
state or authorization. Read each tool script before running or copying it.
Any broader global full-read instruction remains in force alongside the task pages.
No agent watches, reports, measures or acts on its own or another session's context;
none compacts, recycles, replaces or hands off because of it ([WORKERS](WORKERS.md), rule 2).

| Task | Additional required pages |
| --- | --- |
| Code change | [DESIGN](DESIGN.md), [BUILD](BUILD.md), [HOOKS](HOOKS.md), [TRACING](TRACING.md), [EVIDENCE](EVIDENCE.md); [HOOK-SHARING](HOOK-SHARING.md) and [HOOK-CHAIN](HOOK-CHAIN.md) for shared or possibly shared sites |
| Eden run | [HOSTS](HOSTS.md), [PROFILES](PROFILES.md), [LIFECYCLE](LIFECYCLE.md), [WATCHDOG](WATCHDOG.md), [TEST-PLAN](TEST-PLAN.md), [TEST-RUN](TEST-RUN.md), [RUNNER](RUNNER.md), [MACROS](MACROS.md), [TILE-CURSOR](TILE-CURSOR.md), [TRACING](TRACING.md), [EVIDENCE](EVIDENCE.md), and the host's [LINUX-DOWN](LINUX-DOWN.md) or [WINDOWS](WINDOWS.md); [HOOK-CHAIN](HOOK-CHAIN.md) when the batch has sharing or stand-in rows |
| GIF | [GIFS](GIFS.md), [EVIDENCE](EVIDENCE.md), and the Eden-run pages when capturing in Eden |
| Release | [BUILD](BUILD.md), [EVIDENCE](EVIDENCE.md), [TEST-RUN](TEST-RUN.md), [RELEASE](RELEASE.md) |
| Card install, removal or swap | [INSTALL](INSTALL.md), [TEST-RUN](TEST-RUN.md), [RELEASE](RELEASE.md) |
| Online behavior | [ONLINE](ONLINE.md), [HOOKS](HOOKS.md), and the code-change/Eden-run pages |
| Host or lifecycle change | [HOSTS](HOSTS.md), [PROFILES](PROFILES.md), [LIFECYCLE](LIFECYCLE.md), [WATCHDOG](WATCHDOG.md), [LINUX-DOWN](LINUX-DOWN.md) or [WINDOWS](WINDOWS.md), [TEST-PLAN](TEST-PLAN.md) (shared-tool worktrees), [EVIDENCE](EVIDENCE.md) |
| Several workers or hosts | [WORKERS](WORKERS.md), plus each worker's task pages |

Build a task checklist in the handoff: every **M** rule, every applicable
**C** rule, and a reason for each inapplicable C. The checklist summarizes
every M/C rule and links to its complete contract. Recheck after sizeable
changes and record evidence as gates pass, rather than from memory.

Every delegated brief gives the clone path, reading order and playbook
commit, requires the worker's own checklist and compliance run before READY,
and ends in a done-checklist. A worker unable to read stops and reports it.
For any delegated work, coordinator and worker also read [WORKERS](WORKERS.md)
completely: keep turns open during builds/runs/busy-host waits, poll in ≤60 s
steps, and name the actual navigation kit in every Eden brief.

Wrap every local command in a memory and time guard appropriate to it:
`systemd-run --user --scope -p MemoryMax=… /usr/bin/timeout …`.
Put commands containing `${var}` in a script file: `systemd-run` expands
those expressions on its command line. Follow the global host shell rules.
The model/effort gate, watcher and delegation routing come from
`/home/intpa/.codex/DELEGATION_ROUTING.md`; do not restate another policy here.

Use Forgejo (`git.syntaxbyexample.com/intpa`), never GitHub. Write commit
messages from a UTF-8 file. Push, tag, release and card writes each require
the operator's order for that action; [RELEASE](RELEASE.md) and
[INSTALL](INSTALL.md) retain the separate authorization boundaries.

Run [compliance](CHECKLIST.md) once per phase or release candidate, with
the branch base, design trace and explicit shipped NRO. Resolve every agent
row with evidence before completion and disclose failed, skipped and
unverified rows. A green process exit alone is not acceptance.

`tools/skill/install.py --check` reports installed links and pointers without
writing. Inspect every printed status: exit 0 can include stale pointers or
an obstructing real directory. Leave all outside-repo instructions unchanged,
including `CLAUDE.md` and `AGENTS.md`. Do not run the installer to write their
pointer blocks; `--check` is permitted.
