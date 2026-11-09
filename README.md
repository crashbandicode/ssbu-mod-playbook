# ssbu-mod-playbook

Build, test, document, release and install Smash Ultimate 13.0.5 Skyline
plugins using this workspace's reviewed contracts and tools.

Every agent starts with [START](docs/START.md), [CHECKLIST](docs/CHECKLIST.md)
and its task's concern pages. START names the required pages per task;
history is read on demand. Workers perform their own reading and checklist.

| Concern | Pages |
| --- | --- |
| Implementation | [Design](docs/DESIGN.md), [build/runtime](docs/BUILD.md), [site safety](docs/HOOKS.md), [ownership ABI](docs/HOOK-SHARING.md), [foreign chains](docs/HOOK-CHAIN.md) |
| Host and isolation | [Admission](docs/HOSTS.md), [profiles](docs/PROFILES.md), [lifecycle](docs/LIFECYCLE.md), [Linux Down](docs/LINUX-DOWN.md), [Windows](docs/WINDOWS.md), [watchdog](docs/WATCHDOG.md) |
| Eden testing | [Plan](docs/TEST-PLAN.md), [execute](docs/TEST-RUN.md), [runner](docs/RUNNER.md), [macros](docs/MACROS.md), [tile cursor](docs/TILE-CURSOR.md), [workers](docs/WORKERS.md) |
| Acceptance | [Online](docs/ONLINE.md), [tracing](docs/TRACING.md), [evidence](docs/EVIDENCE.md), [GIFs](docs/GIFS.md), [checklist](docs/CHECKLIST.md) |
| External actions | [Release](docs/RELEASE.md), [card transactions](docs/INSTALL.md) |

[Tool provenance](tools/PROVENANCE.md) distinguishes current source identity
from historical origin hashes. Dated observations live in [history](docs/history/);
they do not establish present host state, tool qualification or authorization.

The [skill](skills/ssbu-mod-playbook/SKILL.md) and generated pointer blocks
connect this reading rule to Claude Code, Codex, Cursor and OpenCode:

```text
python3 -I tools/skill/install.py
python3 -I tools/skill/install.py --check
```

Install on each agent machine after cloning to `~/ssbu-mod-playbook`.
Review `--check` output: exit 0 alone does not prove pointers are current.
Push, tag, release and card writes each require the operator's order.
