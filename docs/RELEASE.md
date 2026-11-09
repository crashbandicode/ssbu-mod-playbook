# Publish only with an action-specific order

Push, tag, release and Switch-card writes each need the operator's explicit
order for that action. A release order does not grant an install; a tool's
approval JSON does not independently establish a human grant. Without the
order, perform none of those actions and say so in the report. Use Forgejo
at `git.syntaxbyexample.com/intpa`, never GitHub.

For an authorized release:

1. Pass [BUILD](BUILD.md) and [CHECKLIST](CHECKLIST.md), retaining the
   actual shipped NRO identity and current evidence. Bump the minor version
   for every build that changes what is on the card.
2. Make an annotated tag and a Forgejo release with the NRO,
   `manifest.json`, `SHA256SUMS.txt`, and an SD-card zip where that repo
   calls for one. Keep manifest and asset identities consistent.
3. Obtain the Forgejo credential through `git credential fill` and keep it
   in memory only. Never persist it in artifacts, commands or reports.
4. Download every asset anonymously and compare its actual hash with the
   published manifest/sums and intended local asset. Inspect all results;
   missing or mismatched assets fail acceptance.
5. Rebuild the tagged commit and prove its NRO is byte-identical to the
   released file. Keep the rebuild and comparison output.

To identify an unknown NRO on the card, rebuild the suspected commit and
compare bytes; a name, old manifest or remembered tag is not proof.

`tools/release/examples/` contains historical worked examples. Their frozen
tags, hashes, counts, source-era paths and approval booleans are not current
inputs or authority. The publication example prints missing/mismatched
anonymous assets without failing and does not perform a reproducibility
rebuild; those manual acceptance steps remain required.

The final evidence names the order, version/tag/commit, shipped SHA-256,
gate results, anonymous download comparisons and byte-identical rebuild.
Follow [INSTALL](INSTALL.md) separately for an authorized card transaction.
