# TrainingInkGateTests fixtures (test_routes.py)

These are vendored 2026-11-18 by judge-devin. The test used to read them from
/home/intpa/smash-training-lobby/artifacts by composed paths, so the 15:04Z artifact slim's reference scan could not
see them. That slim deleted run-1 and nav/, and the test errored.

Each file is pixel-identical to its source (RGB, lossless PNG re-encode). Sizes are listed with each file. All are
1280x720 switch-hw stills.

**Positive cases** (the hw Training gate must pass):
- `pos-other-stage-plate.png` (1,341,616 B): Training on another stage with the translucent menu plate. It replaces
  the deleted run-1/out/S-B07/sw-193412Z-029675-FAIL.png (19:34Z), which showed the same case.
  - Gate reading: ink 0.027 against ink_limit 0.3; `match_frame` diff 0.0 with `ink_mismatch`, which is exactly what the
    test asserts.
  - Source: /home/intpa/smash-training-lobby/artifacts/switch-hw-feature-ledger/run-16/out/CPU-F011/sw-121341Z-140046-LNG-0.6.png,
    sha256 df0080f938d397c135eb10d3c2f0b65299ed2b95ed2a305d8f531938f054f9ff.
- `pos-t01-closed.png` (363,298 B): the original fixture, unchanged.
  - Source: /home/intpa/smash-training-lobby/artifacts/switch-hw-session-1/out/T-01/sw-150743Z-533735-T-01-closed.png,
    sha256 68812120c7780bc04d9d56bc9c65ac4eb0148e5bf88effeb3803190ed833b936.

**Negative cases** (the gate must fail):
- `neg-t01-rec-reset-row.png` (130,423 B): the original fixture.
  - Source: /home/intpa/smash-training-lobby/artifacts/switch-hw-session-1/out/T-01/sw-150658Z-029949-T-01-rec-reset-row.png,
    sha256 3d899dcc0ebdd18296a1c2a1c4f401aea514f216b664283c2c4290d24284f1a3.
- `neg-up-still.png` (684,568 B): the `switch_hw up` still. It replaces the deleted run-1/up-still.png, which was the
  same kind of frame.
  - Source: /home/intpa/smash-training-lobby/artifacts/switch-hw-feature-ledger/run-16/up-still.png,
    sha256 f0253f7586273b8e5e5a6db5df98f7bab8763ff82efae8407387e9d7810ec101.
  - run-17/up-still.png was not used: it is a Training frame (ink 0.005).
- `neg-css.png` (1,212,754 B): the character select screen. It replaces the deleted nav/css-css.png.
  - Source: /home/intpa/smash-training-lobby/artifacts/switch-hw-feature-ledger/run-16/out/S-BOOT.prev-130741/sw-111909Z-526019-gate-routes.css.png,
    sha256 43b73c8e4fcbbdd1353e7b8da00459b32194df8a53c07ea9859ba16e8d695727.
- `neg-tr-menu.png` (218,860 B): the open TrainingRooms menu (CPU > Ledge & Tech). This is a new negative.
  - Source: /home/intpa/smash-training-lobby/artifacts/switch-hw-feature-ledger/run-16/out/CPU-F012/sw-121634Z-173110-nav-section-0.png,
    sha256 c41697c476e00f0efe4f2a5eed776a3253dd6dde2569ba8dabf529d8a9d713ed.

**Lost, with no surviving copy:**
- nav/boot-skip-a.png (boot);
- nav/sss-bf-sel.png (stage select);
- nav/shots/01-s00-start.png.

No equivalent survived the slim. Those negative screen kinds are covered by CSS, the up still and the TR menu above.
The assertions themselves are unchanged.
