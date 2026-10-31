"""Drive one elite-rematch Eden instance through its control socket.

Usage: drive.py <eNN> <s|y> shot <name>
       drive.py <eNN> <s|y> press <button>[,<button>...] [hold_ms]
       drive.py <eNN> <s|y> stick <x> <y> <ms>
       drive.py <eNN> <s|y> call <method> [json]
       drive.py <eNN> <s|y> keepalive <stop-file> [seconds] [--attack]
Instance s runs on the Surface (control socket forwarded to
/run/user/1000/elite-rematch-<eNN>-s.sock), y on this Yoga. Screenshots are
copied to eden/shots/<eNN>/.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/intpa/Applications/Eden-GIF/current/tools")
from edenctl import Client  # noqa: E402

ROOT = "/home/intpa/elite-rematch-work"
label, inst, op = sys.argv[1], sys.argv[2], sys.argv[3]
# Instance s runs on the Surface (forwarded socket) unless this host runs it
# too (same-PC pair): then its own run directory is here.
SAME_HOST = Path(f"{ROOT}/runs/{label}-s").is_dir()
SOCKET = {"s": f"{ROOT}/runs/{label}-s/control.sock" if SAME_HOST
          else f"/run/user/1000/elite-rematch-{label}-s.sock",
          "y": f"{ROOT}/runs/{label}-y/control.sock"}[inst]
REMOTE_SHOTS = f"{ROOT}/shots/{label}"
LOCAL = Path(__file__).resolve().parent / "shots" / label


def call(method, params=None, timeout=15):
    response = Client(SOCKET, timeout=timeout).request(method, params or {})
    if not response.get("ok"):
        raise SystemExit(json.dumps(response))
    return response["result"]


def nodes(tree):
    if isinstance(tree, dict):
        if "class" in tree and "id" in tree:
            yield tree
        for key, value in tree.items():
            if key != "properties":
                yield from nodes(value)
    elif isinstance(tree, list):
        for value in tree:
            yield from nodes(value)


if op == "shot":
    name = f'{inst}-{time.strftime("%H%M%S", time.gmtime())}-{sys.argv[4]}.png'
    target = next(n for n in nodes(call("ui.tree", {"brief": True, "visible_only": True}))
                  if n["class"] == "GRenderWindow")
    remote = f"{REMOTE_SHOTS}/{name}"
    if inst == "s" and not SAME_HOST:
        subprocess.run(["/usr/bin/ssh", "-o", "BatchMode=yes", "intpa@dreamland-surface.lan",
                        f"New-Item -ItemType Directory -Force {REMOTE_SHOTS} | Out-Null"],
                       check=True, capture_output=True, timeout=30)
    else:
        Path(REMOTE_SHOTS).mkdir(parents=True, exist_ok=True)
    call("ui.screenshot", {"path": remote, "target": target["id"]}, timeout=30)
    LOCAL.mkdir(parents=True, exist_ok=True)
    local = LOCAL / name
    if inst == "s" and not SAME_HOST:
        subprocess.run(["/usr/bin/scp", "-q", f"intpa@dreamland-surface.lan:{remote}", str(local)],
                       check=True, timeout=60)
    else:
        local.write_bytes(Path(remote).read_bytes())
    print(local)
elif op == "press":
    hold = int(sys.argv[5]) if len(sys.argv) > 5 else 120
    buttons = sys.argv[4].split(",")
    for button in buttons:
        call("input.button", {"button": button, "state": "press", "lease_ms": hold + 500})
    time.sleep(hold / 1000)
    for button in buttons:
        call("input.button", {"button": button, "state": "release"})
    print("ok")
elif op == "stick":
    x, y, ms = float(sys.argv[4]), float(sys.argv[5]), int(sys.argv[6])
    call("input.stick", {"stick": "left", "x": x, "y": y, "lease_ms": ms})
    time.sleep(ms / 1000)
    print("ok")
elif op == "call":
    print(json.dumps(call(sys.argv[4], json.loads(sys.argv[5]) if len(sys.argv) > 5 else {})))
elif op == "keepalive":
    stop = Path(sys.argv[4])
    seconds = float(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5][0].isdigit() else 400
    attack = "--attack" in sys.argv
    deadline, step, errors = time.monotonic() + seconds, 0, 0
    while time.monotonic() < deadline and not stop.exists():
        # Stay in place (walking off counts as a self-destruct; the game drops
        # players for excessive self-destructs and for ~30 s without input).
        for method, params in (("input.stick", {"stick": "left", "x": 0, "y": -1, "lease_ms": 300}),
                               ("input.button", {"button": "a" if attack else "zl", "state": "tap",
                                                 "duration_ms": 80})):
            try:
                Client(SOCKET, timeout=5).request(method, params)
            except Exception:  # a dropped call must never end the loop
                errors += 1
        step += 1
        time.sleep(0.7)
    try:
        Client(SOCKET, timeout=5).request("input.reset", {})
    except Exception:
        errors += 1
    print(json.dumps({"steps": step, "errors": errors, "reason": "stop" if stop.exists() else "deadline"}))
