#!/usr/bin/env python3
"""Measure a compile's memory for the lifecycle's compile reserve (lifecycle.py COMPILE_RESERVE, README "Compiles
beside an Up"). Runs one command and samples its whole process tree every 0.2 s. It records the peak of the summed RSS
of all descendants, the peak of the largest single process, the cgroup's memory.peak when the command runs in its own
scope (page cache included), and the host's lowest MemAvailable meanwhile. Writes a JSON summary.

  python3 -I compile_peak.py <out.json> -- <argv...>

The 2026-11-10 measurement ran each TrainingRooms gate from an empty target dir as
  systemd-run --user --scope -p MemoryMax=8G nice -n 19 python3 -I compile_peak.py out.json -- cargo test
with CARGO_BUILD_JOBS=2 (and likewise for `cargo skyline build --release --features diag`)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def children_map():
    kids = {}
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            stat = (entry / 'stat').read_text()
            ppid = int(stat.rsplit(')', 1)[1].split()[1])
            kids.setdefault(ppid, []).append(int(entry.name))
        except (OSError, ValueError, IndexError):
            pass
    return kids


def tree(root, kids):
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        out.append(pid)
        todo.extend(kids.get(pid, []))
    return out


def rss_and_name(pid):
    try:
        status = Path('/proc/%d/status' % pid).read_text()
    except OSError:
        return 0, None
    rss = next((int(line.split()[1]) * 1024 for line in status.splitlines() if line.startswith('VmRSS:')), 0)
    name = next((line.split(None, 1)[1].strip() for line in status.splitlines() if line.startswith('Name:')), None)
    return rss, name


def mem_available():
    return next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                if line.startswith('MemAvailable:'))


def main(argv):
    out_path = Path(argv[0])
    cmd = argv[argv.index('--') + 1:]
    cgroup = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().strip().split('::', 1)[-1].lstrip('/')
    start = time.time()
    proc = subprocess.Popen(cmd)
    peak_sum, peak_one, peak_one_name, peak_at, low_avail = 0, 0, None, 0.0, mem_available()
    peak_names = {}
    while proc.poll() is None:
        kids = children_map()
        total = 0
        names = {}
        for pid in tree(proc.pid, kids):
            rss, name = rss_and_name(pid)
            total += rss
            if name:
                names[name] = names.get(name, 0) + rss
            if rss > peak_one:
                peak_one, peak_one_name = rss, name
        if total > peak_sum:
            peak_sum, peak_at, peak_names = total, time.time() - start, names
        low_avail = min(low_avail, mem_available())
        time.sleep(0.2)
    peak_cg = None
    try:
        peak_cg = int((cgroup / 'memory.peak').read_text())
    except (OSError, ValueError):
        pass
    summary = {'cmd': cmd, 'exit': proc.returncode, 'seconds': round(time.time() - start, 1),
               'peak_tree_rss': peak_sum, 'peak_tree_rss_at_s': round(peak_at, 1), 'peak_tree_by_name': peak_names,
               'peak_single_rss': peak_one, 'peak_single_name': peak_one_name, 'cgroup': str(cgroup),
               'cgroup_memory_peak': peak_cg, 'lowest_mem_available': low_avail, 'nice': os.nice(0)}
    out_path.write_text(json.dumps(summary, indent=1) + '\n')
    print(json.dumps({k: summary[k] for k in ('exit', 'seconds', 'peak_tree_rss', 'peak_single_rss',
                                               'peak_single_name', 'cgroup_memory_peak', 'nice')}))
    return proc.returncode


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
