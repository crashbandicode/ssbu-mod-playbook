# Requirements, fixes and recorded decisions

Before coding, write a design report beside the handoff. Include a table
headed `| Operator clause | Design | Test |`: one row per clause of the
request, quoted verbatim, its implementing design element, and proving tests
as backticked function names. Every row names a test and every test exists.
For a requirement to behave the same as an existing path, a parity test runs
both paths through one table of cases. Preserve this pre-code timing.

The working design trace is the scoped exception to the no-operator-quotes
rule. The README and shipped docs carry no operator quotes. Run
`tools/check/compliance.py <repo> --base <branch-start> --trace <design.md>`
with the other [CHECKLIST](CHECKLIST.md) inputs. Its parser checks table and
name presence; review clause completeness, meaningful assertions, parity and
actual test passage yourself. It does not prove those properties.

Fix the root cause. A guard, skip, player restriction, relaxed tolerance,
lint `allow`, warning hidden behind a feature, or disabled setting that
leaves the root in place is a workaround. Before shipping one, give the
operator the root cause or remaining unknowns, why it is not fixed now,
the cost to the player, and your recommendation. Ship only after approval;
list each approved workaround with its approval date in the evidence doc.

When defects share a native field, assumption or lifecycle step, first
consider one abstraction that owns the cause. Report whether that
abstraction or separate fixes were chosen, and why. The dated copy-registry
example is in [incident history](WORKFLOW.md).

Every user-facing refusal added by the branch cites an approved restriction.
This includes ordinary refusals such as busy or a loading save state, as well
as strings that say unavailable, only when, requires, cannot, or tell the
player to turn a feature on to proceed. List ordinary refusals once each.
Put `// restriction: <ID>` on the string's line or within the preceding
three lines. In the plugin's `docs/RESTRICTIONS.md`, add a row shaped
`| <ID> | message | why | approved |`; the last cell names the operator's
approval date or decision ID. Pending approval does not count.
The `--base` scan covers recognized committed Rust additions; it cannot
establish genuine authorization or find every refusal form. Review semantics.

Before changing something covered by an existing spec or decision log,
find and cite that decision in the commit and report: document, item and
date. An order citation identifies its message and time. A newer operator
order wins, but disclose which earlier decision it reverses, so the conflict
is visible, and record the new decision where the old one lives.

Acceptance evidence: pre-code trace, test outputs, restriction registry and
approval records, dated workaround list, shared-cause choice, and any
decision/reversal citations. Keep claims within what those tests establish.
