"""Stop this host's detached elite-rematch Eden run recorded by launch.py.

Usage: stop.py <eNN label> <s|y>
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/intpa/elite-rematch-work")
label, tag = sys.argv[1], sys.argv[2]
assert re.fullmatch(r"e\d{2,}", label) and tag in ("s", "y"), "bad label/tag"
receipt = json.loads((ROOT / f"LAUNCH-{label}-{tag}.json").read_text(encoding="utf-8"))
pid, run = int(receipt["pid"]), Path(receipt["run"])


def alive():
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def state():
    try:
        return json.loads((run / "run.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


if alive():
    os.kill(pid, signal.SIGTERM)
deadline = time.monotonic() + 35
while time.monotonic() < deadline and state().get("state") != "stopped" and alive():
    time.sleep(1)
if alive() and state().get("unit"):
    subprocess.run(["systemctl", "--user", "stop", state()["unit"]], check=False, timeout=15)
    time.sleep(2)
s = state()
print(json.dumps({"label": label, "tag": tag, "pid": pid, "alive": alive(),
                  "state": s.get("state"), "reason": s.get("reason"), "exit_code": s.get("exit_code")}))
sys.exit(1 if alive() else 0)
