# Eden test: one command

From another box, use `/home/intpa/eden-runs/_eden-test/hosts.json` or add `--ssh`:
`/usr/bin/python3 -I -B tools/eden-test/eden_test.py go --host yoga --label my-run --owner "Claude devin-<lane>/my-run" --nro /path/libtraining_rooms.nro --elf /path/libtraining_rooms.so --trace-point libtraining_rooms.nro!edge_juggle_attempt --trace-point libtraining_rooms.nro!planner_chose --rows tools/eden-test/example`

On the Eden host, run the same command with its local host name:
`/usr/bin/python3 -I -B tools/eden-test/eden_test.py go --host yoga --label my-run --owner "Claude devin-<lane>/my-run" --nro /path/libtraining_rooms.nro --elf /path/libtraining_rooms.so --trace-point libtraining_rooms.nro!edge_juggle_attempt --trace-point libtraining_rooms.nro!planner_chose --rows tools/eden-test/example`
Each host needs `/home/intpa/eden-runs/_eden-test/base.json`; Yoga and XPS have one.
Each run gets its own bridge port and its own X display (when the base has a display helper; otherwise the base's display is kept).
The base names the host's network interface ("network_interface"); go refuses when that interface is missing, down, loopback or has no IPv4 address, unless --no-lobby. Override with `--network-interface <name>`.
Boot rows call routes:host_network (never set core.network_interface to "lo": Eden does not list loopback, so the game has no network and cannot open a local wireless room).
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
Park in Training for review; if no verdict arrives within 20 minutes, Down and say so.
Old runs with recovery files directly in row folders cannot distinguish them from row output.

For drill tracing, supply `--elf /path/libtraining_rooms.so` and both trace points:
`--trace-point libtraining_rooms.nro!edge_juggle_attempt --trace-point libtraining_rooms.nro!planner_chose`
The bridge base enables trace, so it requires the matching ELF and at least one trace point.
The bridge lineup is a TRACE run: the stub is off so tracing is ready before game load.
