# Execute and finish an Eden batch

Prepare [TEST-PLAN](TEST-PLAN.md) before Up and use the existing
`tools/eden-test/eden_test.py` on the host running Eden. For a remote executor,
refresh its playbook, transfer project rows, then call the wrapper through
the required PowerShell/SSH transport. Config and `instances.json` paths are
executor-local; no implicit forwarding. The wrapper is the current recipe;
old solo/pair wrappers are historical and do not prove current cleanup.

| Command | Purpose |
| --- | --- |
| `new --from <last config.json> --label <new> --rows <folder>` | New `/home/intpa/eden-runs/<label>/` state/work roots; retained pins are hash-checked. `--app` changes Eden; `--plugin` replaces the named plugin pin. |
| `up <run>` | Required setup review/dry-run, then lifecycle Up; honors the defer_load configured by new/all `--defer-load`. |
| `run <run>` | Execute rows; after a fix use `run <run> --retry` on the same boot. |
| `pace <run>` | Inspect manual-input/progress/runtime budgets. |
| `down <run> [--keep-state]` | Lifecycle Down/prune, harvest, <=20-line RESULTS skeleton and `timing.json`; keep-state only when asked. |
| `all` | New/Up/run/Down for a batch not expected to need mid-boot fixes. |

Always arrange Down in cleanup/finally, also after failed rows. Failed-Up
cleanup is lifecycle-owned; the wrapper's `all` enters its run/finally only
after Up returns. **`all` exit 0 is not proof all rows passed**: current code
can ignore row-run failure when Down succeeds. Read every row summary,
receipt and cleanup result.

Continue after failure when the next row is reachable. Save the failed row's
evidence, run its recovery and check the next start gate. A row `recover`
replaces the plan list; recovery may contain a segment, library route, hook,
UI or call. `on_failure` is plan/row `continue` or `stop`, with CLI override.
Without recovery the runner stops at the first failure. Failed recovery or
non-running/crashed game writes `STOP-INPUT` and stops; it never auto-reboots.
Fix/defer rows in minutes with reasons, keeping the same boot for remaining
reachable work. After fixing, inspect/remove the stop marker and retry failed
rows (`--rows failed` / wrapper `--retry`); PASS receipts are skipped.
`out/summary.json` must account for every row: pass, fail, skipped or not run,
with reasons and evidence paths. Fix the batch together; do not reboot per bug.

Poll `pace` and the runner log until completion, in steps <=60 seconds.
Retain the `setup` list and `pace:` output as evidence of the pace budget.
`pace` reports hand-driven inputs outside recording, minus runner hook inputs;
time since a newly passed row; and time left to the config runtime cap.
It prints STOP/exits 2 past **40 inputs**, **15 minutes without progress**,
or **5 minutes before the cap**. On any pace STOP, **stop steering**. Near the
cap, Down; otherwise use a fixture, record the route once, or Down and fix rows.
Clip retakes use a fresh boot. Reporting is partly informational: a wrapper
run printing pace does not universally enforce its stop. The runner's built-in
first-row timer starts at runner start and disables after a pass; it does not
replace the separate first-settled-screen and since-new-pass human clocks.

Mid-boot edits change `rows.json` and project hooks only, **never config.json
or state/lifecycle.json**. If config was edited anyway, wrapper Down restores
the Up-time config and preserves `config.after-up.<time>.json`; lifecycle
otherwise refuses a mismatch. New dry-run-passing rows can join between rows.

For a missing route, bound exploration, record the segment, hash it and return
to the runner. Review at row checkpoints/caps, not after every input. Use actual
game state and stable HUD, and pass on behavior, as TEST-PLAN rules 11/15 require.
Capture documentation GIFs during the boot that already reaches the moment;
[GIFS](GIFS.md) governs envelopes, finalization and review.

Before shipping, sanity-check the **full card plugin set**: play one match
through results and back, then inspect every plugin's install line and the
full Eden console. Keep that run's pinned set and evidence.

An absent required plugin precondition line aborts that attempt; retry under
the next unique label. Queue/retry with a deadline and capped tries, one line
per start/end; count successful precondition-qualified runs, not attempts.
Historical examples keep their original paths but not the old admission rule.

At Down, commit project rows/hooks, segments, reviewed references and hashes
to `eden/`, recording the shared runner's playbook commit. Harvest reviewed
PASS routes using `tools/eden-runner/harvest.py <rows> --label <label>`.
It copies used segments/gates with SHA256, start/end gates and label; existing
names stay unless `--replace`. Harvest trusts PASS receipts: review behavior
before accepting it. Allow reviewed evidence screenshots/reference crops;
keep extracted game/mod textures/models/sound, `data.arc` and dumps out of git.

Clean Down exports changed files into `out/post-run-<key>/` with manifests,
then prunes work and state snapshot/post-run copies. [PROFILES](PROFILES.md)
owns cache/known-input exclusions, no-follow safety, retention and retry;
inspect prune errors/skipped/left paths separately from the completed release.
`--keep-state` only when asked; a later Down without it finishes pruning.

Complete RESULTS in <=20 lines: resolved build path/hash, each row's state,
receipt and one-line cause/defer reason, Down cleanup/verify/release, **Prune:**
bytes/df/export path or retained/error reason, harvest and next action.
The generated skeleton can omit failures beyond its first
six and leave Next blank; summary/receipts remain authoritative. Coordination
posts <=5 lines name changed rows and next action. No per-run compliance essay
or duplicate inventories; full compliance/dispositions once per phase/candidate.
Keep labelled receipts/logs and inspect shutdown health separately from cleanup.
