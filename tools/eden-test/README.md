# Eden test: one command

From another box, use `/home/intpa/eden-runs/_eden-test/hosts.json` or add `--ssh`:
`/usr/bin/python3 -I -B tools/eden-test/eden_test.py go --host yoga --label my-run --owner "Claude devin-<lane>/my-run" --nro /path/libtraining_rooms.nro --elf /path/libtraining_rooms.so --trace-point libtraining_rooms.nro!edge_juggle_attempt --trace-point libtraining_rooms.nro!planner_chose --rows tools/eden-test/example`

Use `tr-yoga` for the Yoga SSH host and `ledger_ssh`. Yoga DNS returns its
Wi-Fi address first; that path lost up to 36% of packets on 2026-11-24 and
logins stalled before authentication. Yoga Wi-Fi was switched off at 02:57Z
on 2026-11-24; DNS may still answer old Wi-Fi addresses and wired IPv4
`192.168.0.166` is not in DNS. The Claw alias must not be used for TR runs.
The external `/home/intpa/eden-runs/_eden-test/hosts.json` must have
`"yoga": "tr-yoga"`; this repository does not change that file.

On the Eden host, run the same command with its local host name:
`/usr/bin/python3 -I -B tools/eden-test/eden_test.py go --host yoga --label my-run --owner "Claude devin-<lane>/my-run" --nro /path/libtraining_rooms.nro --elf /path/libtraining_rooms.so --trace-point libtraining_rooms.nro!edge_juggle_attempt --trace-point libtraining_rooms.nro!planner_chose --rows tools/eden-test/example`
By default, `go` stages the full card lineup from the newest stamp and verifies its `SHA256SUMS.txt`.
Each `--nro` replaces the card file of the same name; a new name is added.
Base plugins not on the card, including the bridge, are added; card files supersede matching base pins.
Use `--card-lineup <dir>` for another lineup or `--no-card-lineup <reason>` to opt out with a recorded reason.
The receipt is in `config.json` (`card_lineup`, `staged_plugins`, `superseded_base_pins`) and `REPORT.md`.
On an Eden host the default folder does not exist, so `go` needs `--card-lineup <dir>`; remote dispatch copies the lineup and passes it.
Each host needs `/home/intpa/eden-runs/_eden-test/base.json`; Yoga and XPS have one.
The repository does not ship a `base.json` template; it is maintained on
each host. Add `"notify": {"file": "/home/intpa/eden-run-notify/NOTIFY.md",
"to": "tr-primary-2"}` to that base, without an `ssh` key. Notifications
append to this file locally on the run host; the coordinator's Surface
monitor relays new lines to the inbox. On the Ally use the run user's
Windows profile path instead: `C:\Users\intpa\eden-run-notify\NOTIFY.md`
for the `intpa` user.
Each run gets its own bridge port and its own X display (when the base has a display helper; otherwise the base's display is kept).
The base names the host's network interface ("network_interface"); go refuses when that interface is missing, down, loopback or has no IPv4 address, unless --no-lobby. Override with `--network-interface <name>`.
Boot rows call routes:host_network (never set core.network_interface to "lo": Eden does not list loopback, so the game has no network and cannot open a local wireless room).
Lobby rows: load and bring the bridge up first, then routes:frontend_room in its own row, then routes:to_main, routes:bridge_alive, and routes:wireless_room. The block creates a Local Wireless room and reaches its waiting room as host; it requires a connected frontend room and the main menu. A frontend room hosted BEFORE load makes the bridge unreachable (Eden routes every game socket through the room).
TrainingRooms lobby practice needs `lobby_practice` On before the room: the go base `practice-settings.json` stores it Off, and the bridge refuses a settings write inside the room (`req set result=refused reason=scene`); set it in Training with `routes:tr_settings` first.
`wireless_room` uses the flown 9-frame recipes from stage select onward; its L3+Up chord waits for `tile.cursor` screen `css` with `p1.token` held, then places whichever fighter it lands on (Simon, tile 66, in `tss-lobby-17060a5e-3`); the route was proven for a host with one member only.
In `tss-lobby-17060a5e-2`, hosting left `ssbu.ui` at `screen=unknown` and `observation.active=no`; `to_main` restored it.
go refuses rows that set core.network_interface to anything but the run's checked interface; load_game checks it again just before loading.
A run with --no-lobby on a host without a usable interface must not call host_network.

What it does:
1. Copies and pins the NROs and rows.
2. Selects a free bridge port for this run.
3. Checks the source profile for the trace stub rule.
4. Starts Eden with deferred game load.
5. Runs the rows and writes the report.
6. Leaves Eden up after Up succeeds, whether the rows pass or fail.

Read the printed `REPORT.md` for PASS, FAIL and NOT RUN.
`go` exits 0 only when every row passed, and 3 otherwise.
Rows continue after a failure; a row whose recovery fails is FAILED without running.
Rerun one row: `eden_test.py run /home/intpa/eden-runs/my-run --rows r02a`.
Refresh the report: `eden_test.py report /home/intpa/eden-runs/my-run`.
Down: `eden_test.py down /home/intpa/eden-runs/my-run` (also printed in the report).
If local cleanup succeeds while the ledger is unreachable, Down succeeds and
reports the pending receipt's last line and the settle unit, if started. The claim stays open until
settlement; running Down again completes settlement when the ledger returns.
Park in Training for review; if no verdict arrives within 20 minutes, Down and say so.
Old runs with recovery files directly in row folders cannot distinguish them from row output.

For drill tracing, supply `--elf /path/libtraining_rooms.so` and both trace points:
`--trace-point libtraining_rooms.nro!edge_juggle_attempt --trace-point libtraining_rooms.nro!planner_chose`
The bridge base enables trace, so it requires the matching ELF and at least one trace point.
The bridge lineup is a TRACE run: the stub is off so tracing is ready before game load.
