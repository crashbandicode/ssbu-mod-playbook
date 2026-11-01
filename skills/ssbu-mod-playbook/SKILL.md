---
name: ssbu-mod-playbook
description: Use for creating, changing, testing, validating, releasing or installing a Super Smash Bros. Ultimate (13.0.5) Skyline plugin, or for driving Eden to test one. Points at the operator's playbook and its completion checklist.
---

# SSBU Skyline plugin work

The authoritative instructions are the playbook cloned at
`~/ssbu-mod-playbook` (`/home/intpa/ssbu-mod-playbook` on the Ubuntu
machines, `%USERPROFILE%\ssbu-mod-playbook` on Windows; origin
`https://git.syntaxbyexample.com/intpa/ssbu-mod-playbook`). This skill does
not restate them. It makes sure they are read and proven.

This applies to every agent that touches the plugin, including every
subagent, teammate or worker you spawn: a parent's read does not cover its
workers. Put the read order (steps 1–4 below, with the clone path) in every
delegated brief, and require the worker's own compliance run before its
READY.

## Before changing anything

1. Bring the clone up to date (`git -C ~/ssbu-mod-playbook pull --ff-only`),
   then record the commit you follow
   (`git -C ~/ssbu-mod-playbook rev-parse --short HEAD`) in the project's
   handoff file.
2. Read, completely: `README.md`, every file in `docs/`, and
   `tools/PROVENANCE.md`. Do not skim and do not stop at the short version.
3. Read the scripts under `tools/` that the task will run or copy, before
   running or copying them.
4. Build the task's checklist from `docs/CHECKLIST.md`: every **M** item,
   every **C** item whose condition holds, and for each **C** item that does
   not hold the reason it is N/A. Keep it in the handoff file.

## While working

- Follow the playbook's layout, guards, hook rules and Eden rules; reuse its
  tools instead of writing new ones.
- Record evidence for each gate as you pass it (file, log line or command
  output), not from memory at the end.
- After any sizeable change, go back over the checklist.
- The shell, delegation, model-gate and watcher rules come from the global
  instructions and `/home/intpa/.codex/DELEGATION_ROUTING.md`, not from
  here.

## Before saying it is done

1. Run `python3 -I ~/ssbu-mod-playbook/tools/check/compliance.py
   <plugin repo> [--nro <shipped NRO>]` and replace every `agent` row with
   `pass`, `fail`, `N/A: <reason>` or `not authorized`, each with evidence.
   Keep the finished report next to the evidence doc.
2. List the failed, skipped and unverified rows in the final message.
3. Never push, tag, release or write to the Switch card without the
   operator's explicit order for that action, and say so in the report when
   the order was not given. The user connects and disconnects the Switch:
   never mount, unmount, eject or remount the card, or start or stop DBI;
   read and write the SD only through what the user exposed.
4. Do not claim success for anything without the evidence that shows it.
