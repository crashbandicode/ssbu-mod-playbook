# ssbu-mod-playbook

How the Smash Ultimate 13.0.5 Skyline plugins in this workspace get built,
tested in Eden and installed, written down for the next agent. Dated
2026-10-31. Paths, hosts and displays are this workspace's: the Yoga
(`/home/intpa`) runs Eden, and the Switch is plugged into the Surface
(`dreamland-surface.lan`) over USB.

| Doc | Read it to |
| --- | --- |
| [WORKFLOW](docs/WORKFLOW.md) | take a plugin from its first commit to the Switch card |
| [EDEN](docs/EDEN.md) | bring up one isolated Eden instance and drive it |
| [MACROS](docs/MACROS.md) | drive routes: macro segments with checks between, scripted routes, recording and editing Eden macros |
| [TILE-CURSOR](docs/TILE-CURSOR.md) | land on an exact fighter or stage |
| [GIFS](docs/GIFS.md) | make README GIFs with Eden's recorder |
| [HOOK-SHARING](docs/HOOK-SHARING.md) | keep two plugins working when they hook the same place |

| [CHECKLIST](docs/CHECKLIST.md) | prove the work is done: mandatory, conditional and example items, and the report |

HOOK-SHARING is a 2026-10-31 copy of `/home/intpa/SKYLINE-HOOK-SHARING.md`.
`tools/` has the scripts these docs use, copied as they ran (sources and
hashes: [tools/PROVENANCE.md](tools/PROVENANCE.md)).

## Skill and checklist

`skills/ssbu-mod-playbook/SKILL.md` is a thin skill that orders a full read
of this playbook, a checklist built from [CHECKLIST](docs/CHECKLIST.md) and
`tools/check/compliance.py` before completion. One copy serves the four
agent tools through symlinks:

```text
python3 -I tools/skill/install.py           # ~/.claude, ~/.agents, ~/.cursor, ~/.config/opencode
python3 -I tools/skill/install.py --check   # report only
```

It also writes a pointer block into `~/.claude/CLAUDE.md`,
`~/.codex/AGENTS.md`, `~/.config/opencode/AGENTS.md` and a Cursor rule, so
the read is required and not left to skill discovery. Run it on every
machine that runs an agent, after cloning this repo to `~/ssbu-mod-playbook`
(`/home/intpa/ssbu-mod-playbook` on the Ubuntu machines,
`%USERPROFILE%\ssbu-mod-playbook` on Windows, where it makes junctions).

## Delegated workers read it too

The read is per agent, not per project: a primary that has read this
playbook does not cover the workers it spawns. Every subagent, teammate or
worker that creates, changes, tests, validates, releases or installs a
plugin, or drives Eden to test one, reads this playbook completely first
(`README.md`, every file in `docs/`, `tools/PROVENANCE.md`), builds its own
checklist from [CHECKLIST](docs/CHECKLIST.md) and runs
`tools/check/compliance.py` before it reports READY. The brief that spawns
it says so explicitly, names the clone path and records the commit the worker
followed. A worker that cannot read the playbook stops and says so instead of
working from the brief alone.

## The short version

1. Copy a small plugin's layout: pure logic in `model.rs`, tested on the
   host, and hooks in `runtime.rs`. Build it as a cdylib only, so LTO applies.
2. Before you hook anything, guard the game version and the bytes at the site,
   and check whether another plugin on the card already hooks it.
3. Log one install line to `sd:/ultimate/<plugin>/events.log`. Lines per
   event belong in the `diag` build, which Eden tests read. The shipped NRO
   writes only its install line.
4. Run the gates: `cargo test`, then clippy with zero warnings, then the
   no-default-features build, then `cargo skyline build --release`.
5. In Eden, use a profile copy that has only your NRO and the tile cursor.
   Script the route to the screen, gate each wait on a log line or a
   screenshot check rather than a fixed sleep, and check the log. Abort a
   run whose precondition line never comes and retry it under the next
   label ([EDEN](docs/EDEN.md), "Queue runs"). If the plugin runs during
   online play, also run two consoles with the desync detector on both
   ([WORKFLOW](docs/WORKFLOW.md) §5).
6. Write a short README with a full-screen GIF, plus a dated evidence doc
   whose numbers a host-tested script regenerates from the logs.
7. Tag, release and install on the Switch only when the operator orders it.
   Installs go over DBI MTP, with readbacks and a rollback copy kept on the
   card.

## Rules that bite

- **Eden:**
  - Claim and release in `/tmp/deluxe-surface-1007/OWNER-COORDINATION.md`;
    `solo.ps1` does both.
  - Wait while anyone else holds a claim, run one Eden per host, and start
    only with at least 10 GiB of MemAvailable. Queue the run so it starts
    itself when the host is free ([EDEN](docs/EDEN.md), "Queue runs").
  - Never use profile `a`, display `:61`, or another agent's runs.
  - Profiles are hard-linked copies (`gv.py prep`); a full copy fills the
    filesystem.
  - Matches use a short ruleset (1 stock, 1:00, items and hazards off)
    unless the test needs a long one ([EDEN](docs/EDEN.md), "Keep matches
    short").
- **Fixes:** fix the root cause. A workaround (guard, skip, restriction,
  tolerance, lint `allow`, feature gate) ships only after the operator
  approves it ([WORKFLOW](docs/WORKFLOW.md), "Root cause, or the operator's
  approval").
- **Build:** `crate-type = ["cdylib"]` only. An `rlib` alongside turns LTO
  off and leaves code in the resident image the game pays for.
- **Disk:**
  - Give each source copy its own cargo target dir and never share one
    between copies of the same workspace (cargo would link one copy's crate
    into another's build). Share the cargo home per host and toolchain,
    delete targets when the work is done, and check `df -h /` before builds
    and Eden runs (stop below 40 GB).
  - In copies, link what is byte-identical and copy what differs or what
    the run writes in place ([WORKFLOW](docs/WORKFLOW.md), "Build caches and
    disk").
- **Git:** publish on Forgejo (`git.syntaxbyexample.com/intpa`), not GitHub.
  Never push, tag a release or write to the Switch without the operator's
  order.
- **Commands:** wrap every local command in a memory and time limit
  (`systemd-run --user --scope -p MemoryMax=… /usr/bin/timeout …`).
  `systemd-run` expands `${var}` on its command line, so put anything that
  uses it in a script file.
- **Docs:** short, dated and evidence only. Don't quote the operator.
