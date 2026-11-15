# Harness lessons: what failed runs taught

These rules come from the 2026-11-15 TrainingRooms rc.14/rc.15 campaign. Each one cost at least one Eden attempt or a
build slot before it was written down. Read it before you write runner rows, macros or a two-PC room.

## Verify, don't recall

- **A log line you assert must exist in the source.** Before a row waits on a log line, grep the plugin's source for
  its format string, and check that the code path really emits it. A row once waited on
  `[Hitbox] command kind=attack fighter=true`, which the source returns before printing for fighters, so the row could
  never pass. Assert on a line the path really prints, or on a still.
- **Fighter, stage and item ids come from the project's own table,** never from memory. A row used Lucas's kind id
  (0x28) for Olimar (0x2b) and filmed the wrong fighter.
- **UI behaviour is checked on a still before a macro relies on it.** For example, the clip editor deselects both
  handles after a preview or a save; the help line says to press L or R again. A macro that assumed the handle was
  still selected moved nothing.
- **Spawning an object needs the real move, checked with a still.** "The move was pressed" is not "the object exists".
  R.O.B.'s Gyro needs a charge and a fire. If P1 dashes into the CPU first, the Gyro hits the CPU and never lies on the
  floor. Record per object: drawn, or spawned with no data, or not spawned. Never count "not spawned" as a result.

## Input macros

- **Use short taps (about 40 ms) to step anything that accelerates when held:** handles, sliders, list scrolls. A
  150 ms hold overshot a crop handle from frame 180 to 300.
- **Check each new macro against a still before the real take.** One wrong press costs a whole take.
- **Recording windows start at the observed event, not at the request.** A playback can start about 13 s after it was
  requested (the warp to the start save), so a 15 s window counted from the request ended before the take finished.
  Start the window from the `screen=playback` (or equivalent) receipt, within the runner's existing maximum.

## Rows inherit state

- **A row starts from whatever the previous row left.** Before its assertions, each row frees or sets the state it needs
  and checks it as a hard precondition, failing as FIXTURE if that isn't met. Examples from one pass:
  - a 3-body window left by an earlier row changed a cold-load count;
  - a Ready Fighters value left at 6 refused a later row's window;
  - a parked group served a pick warm, so a forced reload never happened;
  - a platform stage let a forward smash pass under the CPU.
- **Never mix plan families in one boot, and never run a sequenced row alone.** For example, a BTT G3 needs G1 and G2
  before it, and a clean-CPU row must run before anything loads a 999% save. A second boot is cheaper than a third
  rerun.
- **Don't change an assertion to fit an inherited state.** Fix the fixture. If the design really differs from the row,
  the row is re-derived (for example, an exact formula instead of a hard-coded count) and that goes to the operator.

## Two-PC local-wireless rooms

- **Connect the guest's frontend to the room BEFORE `game.load`.** A game loaded first never enters proxy mode
  ("ProxyPacket received, but not in Proxy mode!"), and its join fails.
- **On a multi-homed host, the room address is the host's default-route source address.** UDP replies leave from that
  address, so a guest that sent to the Wi-Fi address got replies from Ethernet and dropped them.
- **Use `lan_online=false` on both sides for cross-PC local wireless.**
- **Arm the host's acceptance before the guest requests.** The guest gives up after about 30 s, and a slow approval path
  made one join fail.
- **Windows (the Ally) screenshots:** use the Windows capture path. The Linux override's screenshot is black there.
- **Idle windows fall back to the attract demo.** Press Start in both and act promptly, or check the screen again
  before each step.

## Eden and the debugger

- **Breakpoints park a thread that Eden continues from them.** Prefer persistent `trace.*` points or the one-shot
  tracepoint ([EDEN](EDEN.md)). Step past every breakpoint hit.
- **Long debugger pauses during setup have crashed Eden's GPU path** (a `texture_cache` assertion, then a SIGSEGV).
  Keep halts short, or avoid them.
- **A host under heavy load** (for example a backup job) **can stop Eden drawing its first frame.** A boot that never
  draws is a host problem, so move it to another host rather than retrying there.

## For coordinators writing briefs

- **Separate facts from assumptions.** Give each fact its source (a file and line, or a receipt). Mark anything not
  verified as an assumption to check. Workers faithfully build on a wrong premise, such as a bar's cell count or a
  room's keyboard rule.
- **Measure size on a first working slice,** and post the number before building out. Targets set without a
  measurement overshot about 2×.
- **Red and green:** the red is the previous build's failing receipt (a run, a still, or the code or doc that refused
  it). The green is the new test passing in the gate, plus the rerun. Don't build stub commits just to watch a test fail.
- **Reports start with one plain sentence:** what passed, what failed, what is next. Details follow.
- **Message delivery differs by transport.** Native subagents receive a message at their next tool round. Claw
  codex-app sessions receive it only when the current turn ends. Lanes on Claw should end their turn when they ask
  for a ruling, so the answer is not queued behind work done without it.
