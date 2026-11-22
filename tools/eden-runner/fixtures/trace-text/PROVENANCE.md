# Text setter trace fixture

These 15 JSON lines were copied byte-for-byte from preview-vis-sol's boot
`intermediate-integrate-once-Cr` on round release `83eecc32` (2026-11-22).

- Source: `/home/intpa/smash-training-lobby/artifacts/phase23/compact-strip/evidence-boot/run-Cr/out/native-trace/strip-83eecc32.trace.jsonl`
- Source SHA256: `006ab29bde03aed32296a2bb4f7325dab933454ada6c7c109ec143a5ddd5e5f3`
- Sequences: `75, 82, 311, 505, 507, 744, 746, 987, 989, 1730, 1731, 3437, 3621, 3623, 3722`
- Fixture SHA256: `418ecc8cb4c813ce8622c49e395be8de9b21ca27d217f0480fe1a3a18203156c`

Sequence 75 is an empty-text request whose text capture failed at pointer `0x2`.
The remaining lines include
empty and changed menu text, HUD labels, a multiline notice and a private-use
glyph.
