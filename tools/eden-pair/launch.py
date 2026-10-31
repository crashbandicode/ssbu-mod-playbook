"""Launch this host's elite-rematch Eden instance, detached, bounded, receipted.

Usage: launch.py <eNN label> <s|y> <:display> [<runtime path> [<slot:value,...>]]
With a runtime, Eden LAN Online uses that companion (the Eden agent's
rating-storing candidate) with ratings kept in <profile>/ratings, optionally
seeded per fighter slot. eden-run.py forwards a limited environment, so these
go inside the bounded child command through /usr/bin/env.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/intpa/elite-rematch-work")
APP = Path("/home/intpa/Applications/Eden-GIF/current/Eden.AppImage")
label, tag, display = sys.argv[1], sys.argv[2], sys.argv[3]
runtime = Path(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
seeds = sys.argv[5] if len(sys.argv) > 5 else ""
assert re.fullmatch(r"(\d+:\d+(,\d+:\d+)*)?", seeds), f"bad seeds: {seeds}"
assert runtime is None or runtime.is_file(), f"runtime missing: {runtime}"
assert runtime is not None or not seeds, "seeds need the rating runtime"
assert re.fullmatch(r"e\d{2,}", label) and tag in ("s", "y"), "bad label/tag"
assert re.fullmatch(r":\d{1,3}", display) and display != ":61", f"display not allowed: {display}"
profile, run = ROOT / "profiles" / tag, ROOT / "runs" / f"{label}-{tag}"
assert (profile / "user").is_dir(), f"profile missing: {profile}"
assert not run.exists(), f"run exists: {run}"
assert Path(f"/tmp/.X11-unix/X{display[1:]}").exists(), f"display not up: {display}"
mem_kib = next(int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
               if line.startswith("MemAvailable:"))
assert mem_kib >= 10 * 1024 ** 2, f"need 10 GiB available, have {mem_kib} KiB"
config_lines = (profile / "user/config/qt-config.ini").read_text().splitlines()
offline = "lan_online=false" in config_lines
for line in ("lan_online=false", "airplane_mode=true") if offline else ("lan_online=true", "airplane_mode=false"):
    assert line in config_lines, line
assert runtime is None or not offline, "the rating runtime is for LAN Online"
assert "lan_online_tls=true" in config_lines or "lan_online_tls=false" in config_lines, "TLS unset"
with APP.open("rb") as app:
    app_sha = hashlib.file_digest(app, "sha256").hexdigest()

env = dict(os.environ, DISPLAY=display, WAYLAND_DISPLAY="", XAUTHORITY="", QT_QPA_PLATFORM="xcb",
           XDG_DATA_HOME=str(profile / "xdg-data"), XDG_CONFIG_HOME=str(profile / "xdg-config"),
           XDG_CACHE_HOME=str(profile / "xdg-cache"),
           EDEN_LOG_FILTER="*:Warning Frontend:Info Service.Sockets:Info Network:Info")
for key in ("EDEN_NSO_LAN_CONFIG", "EDEN_LAN_RUNTIME", "EDEN_LAN_ASSETS", "EDEN_LAN_PROFILE_DIR",
            "EDEN_LAN_FIGHTER_SEEDS"):
    env.pop(key, None)
child = [str(APP)]
runtime_sha = None
if runtime is not None:
    (profile / "ratings").mkdir(mode=0o700, exist_ok=True)
    with runtime.open("rb") as stream:
        runtime_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    child = ["/usr/bin/env", f"EDEN_LAN_RUNTIME={runtime}", f"EDEN_LAN_PROFILE_DIR={profile / 'ratings'}"]
    if seeds:
        child.append(f"EDEN_LAN_FIGHTER_SEEDS={seeds}")
    child.append(str(APP))
(ROOT / "runs").mkdir(exist_ok=True)
command = ["/usr/bin/python3", str(APP.parent / "tools/eden-run.py"), "--memory-gib", "8",
           "--runtime", "5400", "--startup-timeout", "180", "--hang-timeout", "60",
           "--stall-timeout", "0", "--background", "--output", str(run), "--", *child]
receipt_path = ROOT / f"LAUNCH-{label}-{tag}.json"
assert not receipt_path.exists(), f"receipt exists: {receipt_path}"
with (ROOT / f"launch-{label}-{tag}.log").open("x", encoding="utf-8") as log:
    process = subprocess.Popen(command, env=env, cwd=profile, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
receipt = {"owner": "Claude 9311e6e4-66cd-4cd9-9aba-988997d3aa75 elite-rematch", "label": label,
           "tag": tag, "display": display, "pid": process.pid, "app_sha256": app_sha,
           "runtime": str(runtime) if runtime else None, "runtime_sha256": runtime_sha,
           "seeds": seeds or None,
           "mem_available_kib": mem_kib, "time": time.time(), "profile": str(profile), "run": str(run)}
receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print(json.dumps(receipt))
