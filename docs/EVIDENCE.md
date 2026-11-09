# Claims and retained evidence

A plugin README contains one paragraph on its behavior, a full-screen GIF,
the install path, build commands, a license line and the AI-assistance
disclaimer. [GIFS](GIFS.md) requires readable 1280-wide media strictly under
10 seconds, with the date and build in its caption.

Keep a dated evidence doc (`docs/EDEN-RESULTS.md`, `VALIDATION.md` or
`docs/SAFETY.md`): run labels, build commit or NRO SHA-256, what was done,
and what the logs showed. Online work includes both detectors' results under
[ONLINE](ONLINE.md). For Eden batches, the run's short `RESULTS.md` and
runner receipts are that doc; the phase compliance report collects them.

Keep outputs under their run label. Preserve row receipts and summary,
screenshots, log/trace evidence, `run.json`, `memory.peak`, build identity
and lifecycle cleanup/verify/release receipts. Record resolved build paths
and hashes; do not silently pool runs made on different Eden builds. Record
the actual playbook commit and selected script/binary hashes with each task;
old origin hashes do not identify current source. Resolve external binaries,
clients and extractor dependencies afresh, rather than from source-era paths.
Explicit artifact paths outrank newest-file inference; a tag is required only
for an authorized release, not ordinary build/run evidence. Search
the console for panic, abort, crash and unmapped accesses, and apply
[TRACING](TRACING.md)'s failure and pointer-review rules. A clean search
does not establish absence of an unlogged null read.

Each run's `RESULTS.md` is at most 20 lines: build identity, row states with
receipt paths and one-line causes, Down result (`cleanup_failures`, verify
equal, released), harvest and next action. Coordination posts are at most
five lines and link to the full receipts. Grouping and continuation rules
remain in [TEST-PLAN](TEST-PLAN.md) and [TEST-RUN](TEST-RUN.md).

Every numerical claim computed from logs comes from a committed script
over those logged files, with host tests. Name its command beside the
numbers. Test the scripts on the host with synthetic fair data and a planted bias so the
test proves the calculation can distinguish them; an unrelated test file
does not establish the calculation's correctness.

Write short factual claims within the tests' limits. Shipped docs and
README contain no operator quotes; verbatim request clauses belong only
in the pre-code working [design trace](DESIGN.md). Keep the handoff current
at each checkpoint: objective, branch/commit, evidence, live state, risks
and next commands. Include no secrets.

For a claim that a particular picture, icon or mod asset is visible, decode
the expected source image and the stock alternative. Put the captured
frame beside both, retain the decoded file paths and side-by-side evidence,
and rule out the stock case. The side-by-side contains decoded textures:
keep it outside git and record its path and hash. Use `tools/assets/arc_get.py` for stock files
and `tools/assets/bntx_view.py` for BNTX decoding after reading the scripts.
These decoders have no host tests; review each decoded image yourself.
This source comparison and [PROFILES](PROFILES.md)'s ARCropolis pin plus
native-screen proof in the same boot are separate necessary gates.

Reviewed evidence screenshots and the runner reference crops its checks
need may be committed, with their hashes and provenance. Extracted game or
mod textures, models, sound, `data.arc` files and dumps stay out of every
git repository; keep decoded extraction files outside git. Raw recordings
also stay out of git. The screenshot exception does not permit extracted
assets to be committed as reference material.

The coordinator views frames and log lines itself before accepting a row,
and reviews source comparisons for asset claims. A worker's visual PASS is
a request for that review. A setup check, fixture applied or clip recorded
does not prove behavior; require the event, return/argument, log value or
settled HUD that demonstrates it. Resolve every [CHECKLIST](CHECKLIST.md)
row with evidence and disclose failures, skips and unverified work.
