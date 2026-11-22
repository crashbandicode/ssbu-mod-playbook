# Eden test: one command

From another box, use `/home/intpa/eden-runs/_eden-test/hosts.json` or add `--ssh`:
`/usr/bin/python3 -I -B tools/eden-test/eden_test.py go --host yoga --label my-run --owner "Claude devin-<lane>/my-run" --nro /path/libtraining_rooms.nro --rows tools/eden-test/example`

On the Eden host, run the same command with its local host name:
`/usr/bin/python3 -I -B tools/eden-test/eden_test.py go --host yoga --label my-run --owner "Claude devin-<lane>/my-run" --nro /path/libtraining_rooms.nro --rows tools/eden-test/example`
Each host needs `/home/intpa/eden-runs/_eden-test/base.json`; Yoga and XPS have one.

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
Every run gets its own bridge port; never hard-code 6979.
The bridge lineup is a TRACE run: the stub is off so tracing is ready before game load.
