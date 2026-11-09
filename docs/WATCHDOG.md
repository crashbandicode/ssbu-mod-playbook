# Idle and demo watchdog

Read this page when configuring the opt-in lifecycle watchdog. The normal
Up/Down/status path sends no project input; only an explicitly declared,
authorized `park` sequence sends its declared buttons/stick steps and then
`input.reset`. A config field does not establish new input authorization.
The watchdog implements no product rows or macros.

`lifecycle.py watchdog config.json --execute` observes control-log activity.
Idle beyond `watchdog.idle_s` (default 300 seconds), outside a declared safe
state, or a detector hit triggers `action` (default `down`). If `control.jsonl`
is missing, idle starts at journal Up time, not the epoch. `--rounds` and
`--pause` override `rounds` and `poll_s`.

`safe_states` alone has no effect. A `state_probe` rule must identify the
state from its declared source. The probe sees only the **last 40 lines**:
a change-only line does not establish an indefinitely safe state. The
matching line must remain in that window; use a suitably recurring line.
Safe states suppress **idle only**, never a demo detector. A waiting Training
instance can therefore Down after `idle_s` when the probe cannot identify it.
TrainingRooms release builds print no such diag state line: use demo detectors
and a longer declared idle interval rather than assuming the diag probe works there.

The TrainingRooms diag recipe uses `practice-diagnostics.log` (`diag_log`):

```json
"watchdog": {
  "idle_s": 300,
  "safe_states": ["training"],
  "state_probe": {"source": "diag_log", "rules": [
    {"pattern": "\\[FrameStrip\\] p1 status=", "state": "training"}
  ]}
}
```

It prints when P1's busy status changes; it establishes safety only while
the line remains in the last-40-line window, never for an indefinite wait.

| Detector | Semantics |
| --- | --- |
| `new-files` | Directory/pattern baseline persists across sidecar polls: first look records names, later new names hit. |
| `log-line` | Searches the probe source's last 40 lines, not a consumed delta. A matching line keeps hitting until it scrolls out or action ends the sidecar. |
| `screen-class` | Matches declared `demo_states`. |
| `hook` | Calls the declared module/function. |

Declare state source/rules, safe states, demo detectors, idle interval and
action together. Extra instance sources can be relative profile paths,
`{"sdmc":"ultimate/TrainingRooms/practice-gates.log"}`, or
`{"beside":"diag_log","name":"practice-gates.log"}`. Run/instance
`sources` are written into `instances.json` for gates and probes.

Declaring `watchdog` starts the loop from Up unless `watchdog.auto` is false.
Linux uses `eden-life-<token>-watchdog.service`; Ally uses the token-derived
`EdenLife-<token>-watchdog` scheduled task. Journal and `status` expose the
sidecar identity. Down stops it. It also exits after its action, when the
phase is down, or when the journal disappears, so a persistent detector line
does not repeatedly execute an action after that exit.

Retain `state_dir/watchdog-receipt.json`, state/detector evidence and sidecar
status. Preflight scope and cleanup are in [LIFECYCLE](LIFECYCLE.md);
implementation/config validation are in [lifecycle.py](../tools/eden-lifecycle/lifecycle.py).
The observed TrainingRooms line availability and 2026-11-07 failed probe
remain in [qualification history](../history/LIFECYCLE-QUALIFICATION-2026-11.md).
