"""One gsp-visualizer Eden instance on this Yoga (adapted from elite-rematch/eden).

Usage: gv.py prep <eNN> [--diag-file <path>] <nro>...
       gv.py launch <eNN> <:display> [<slot:value,...>]
       gv.py shot <eNN> <name>
       gv.py press <eNN> <button>[,<button>...] [hold_ms]
       gv.py stick <eNN> <x> <y> <ms>
       gv.py call <eNN> <method> [json]
       gv.py stop <eNN>
The private profile is a copy of the Yoga source profile b (never a profile
named a); its read-only game content (installed titles, system titles,
firmware) is hard-linked, everything else copied. Only the copy is modified.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/intpa/gsp-visualizer-work")
# Profile y is a copy of the Yoga source profile b; profile s (the second
# instance of a same-PC pair) is a copy of y. --profile picks one.
SOURCES = {"y": Path("/home/intpa/ssbu-desync-1029/profiles/b"), "s": ROOT / "profiles" / "y"}
PROFILES = {tag: ROOT / "profiles" / tag for tag in SOURCES}
SOURCE, PROFILE = SOURCES["y"], PROFILES["y"]
APP = Path("/home/intpa/Applications/Eden-GIF/current/Eden.AppImage")
RUNTIME = Path("/home/intpa/elite-rematch-work/eden-lan-runtime")
RUNTIME_SHA = "ee3c2e953a0b87541c29eb8918ce0354489035296ac5eaf060b3444d06a8bbcb"
OWNER = "Claude 88c0f449-28a7-479d-ad98-1721e2fad049 gsp-visualizer"
READ_ONLY = ["user/nand/user/Contents", "user/nand/system/Contents", "user/firmware"]
PLUGINS = Path("user/sdmc/atmosphere/contents/01006A800016E000/romfs/skyline/plugins")
OUTPUT = Path("user/sdmc/ultimate/gsp-visualizer")
OTHER_OUTPUTS = [Path("user/sdmc/ultimate/ssbu-tile-cursor"),
                 Path("user/sdmc/ultimate/ssbu-quiet-elite-switch"),
                 Path("user/sdmc/ultimate/elite-rematch")]
CONFIG = Path("user/config/qt-config.ini")
FORCED = {"lan_online": "true", "lan_online_tls": "true", "airplane_mode": "false",
          "pauseWhenInBackground": "false"}
SHOTS = Path(__file__).resolve().parent / "shots"

sys.path.insert(0, str(APP.parent / "tools"))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def label_ok(label):
    assert re.fullmatch(r"e\d{2,}", label), f"bad label: {label}"
    return label


def run_dir(label):
    return ROOT / "runs" / label


def prep(args):
    receipt = {"label": args.label, "time": time.time(), "source": str(SOURCE),
               "profile": str(PROFILE), "set_aside": [], "archived": []}
    assert SOURCE.name != "a" and SOURCE.is_dir()
    if not PROFILE.exists():
        temp = PROFILE.with_name(f".{PROFILE.name}-{args.label}-tmp")
        assert not temp.exists(), f"temporary path exists: {temp}"
        temp.parent.mkdir(parents=True, exist_ok=True)
        skip = {SOURCE / rel for rel in READ_ONLY}

        def ignore(directory, names):
            return [n for n in names if n in ("profile.lock", "crash_dumps", "log")
                    or Path(directory) / n in skip]

        shutil.copytree(SOURCE, temp, symlinks=True, ignore=ignore)
        for rel in READ_ONLY:
            if (SOURCE / rel).is_dir():
                shutil.copytree(SOURCE / rel, temp / rel, symlinks=True, copy_function=os.link)
        text = (temp / CONFIG).read_text(encoding="utf-8").replace(str(SOURCE), str(PROFILE))
        (temp / CONFIG).write_text(text, encoding="utf-8")
        temp.rename(PROFILE)
        receipt["created"] = True
    lines = []
    for line in (PROFILE / CONFIG).read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0]
        if key == "nand_directory":
            line = f"nand_directory={PROFILE / 'user/nand'}"
        elif key == "sdmc_directory":
            line = f"sdmc_directory={PROFILE / 'user/sdmc'}"
        elif key in FORCED:
            line = f"{key}={FORCED[key]}"
        lines.append(line)
    (PROFILE / CONFIG).write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = (PROFILE / CONFIG).read_text(encoding="utf-8")
    assert str(SOURCE) not in config and "/profiles/a/" not in config, "config points outside"
    for key, value in FORCED.items():
        assert f"{key}={value}" in config.splitlines(), f"config lacks {key}={value}"

    plugins = PROFILE / PLUGINS
    plugins.mkdir(parents=True, exist_ok=True)
    aside = plugins.parent / "plugins-set-aside-gsp-visualizer"
    aside.mkdir(exist_ok=True)
    for path in sorted(plugins.glob("*.nro")):
        target = aside / f"{path.stem}.{args.label}-{int(time.time())}.nro"
        path.replace(target)
        receipt["set_aside"].append(target.name)
    for nro in map(Path, args.nros):
        assert nro.suffix == ".nro" and nro.is_file(), f"missing NRO: {nro}"
        shutil.copyfile(nro, plugins / nro.name)
    receipt["plugins"] = {p.name: digest(p) for p in sorted(plugins.glob("*.nro"))}
    for rel in [OUTPUT, *OTHER_OUTPUTS]:
        previous = PROFILE / rel
        if previous.exists():
            archived = previous.with_name(f"{previous.name}.before-{args.label}")
            assert not archived.exists(), f"archive exists: {archived}"
            previous.replace(archived)
            receipt["archived"].append(str(archived))
    if args.diag_file:
        # gsp-visualizer and ssbu-quiet-elite-switch each read their own keys.
        for rel in (OUTPUT, Path("user/sdmc/ultimate/ssbu-quiet-elite-switch")):
            out = PROFILE / rel
            out.mkdir(parents=True)
            shutil.copyfile(args.diag_file, out / "diag.txt")
        receipt["diag"] = Path(args.diag_file).read_text(encoding="utf-8")
    (ROOT / f"PREP-{args.label}.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({k: receipt.get(k) for k in ("label", "plugins", "archived", "diag")}))


def launch(args):
    display, seeds = args.display, args.seeds or ""
    assert re.fullmatch(r"(\d+:\d+(,\d+:\d+)*)?", seeds), f"bad seeds: {seeds}"
    assert re.fullmatch(r":\d{1,3}", display) and display != ":61", f"display not allowed: {display}"
    run = run_dir(args.label)
    assert (PROFILE / "user").is_dir(), f"profile missing: {PROFILE}"
    assert not run.exists(), f"run exists: {run}"
    assert Path(f"/tmp/.X11-unix/X{display[1:]}").exists(), f"display not up: {display}"
    mem_kib = next(int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                   if line.startswith("MemAvailable:"))
    assert mem_kib >= 10 * 1024 ** 2, f"need 10 GiB available, have {mem_kib} KiB"
    assert digest(RUNTIME) == RUNTIME_SHA, "runtime hash mismatch"
    env = dict(os.environ, DISPLAY=display, WAYLAND_DISPLAY="", XAUTHORITY="", QT_QPA_PLATFORM="xcb",
               XDG_DATA_HOME=str(PROFILE / "xdg-data"), XDG_CONFIG_HOME=str(PROFILE / "xdg-config"),
               XDG_CACHE_HOME=str(PROFILE / "xdg-cache"),
               EDEN_LOG_FILTER="*:Warning Frontend:Info Service.Sockets:Info Network:Info")
    for key in ("EDEN_NSO_LAN_CONFIG", "EDEN_LAN_RUNTIME", "EDEN_LAN_ASSETS", "EDEN_LAN_PROFILE_DIR",
                "EDEN_LAN_FIGHTER_SEEDS"):
        env.pop(key, None)
    (PROFILE / "ratings").mkdir(mode=0o700, exist_ok=True)
    child = ["/usr/bin/env", f"EDEN_LAN_RUNTIME={RUNTIME}", f"EDEN_LAN_PROFILE_DIR={PROFILE / 'ratings'}"]
    if seeds:
        child.append(f"EDEN_LAN_FIGHTER_SEEDS={seeds}")
    child.append(str(APP))
    (ROOT / "runs").mkdir(parents=True, exist_ok=True)
    command = ["/usr/bin/python3", str(APP.parent / "tools/eden-run.py"), "--memory-gib", "8",
               "--runtime", "5400", "--startup-timeout", "180", "--hang-timeout", "60",
               "--stall-timeout", "0", "--background", "--output", str(run), "--", *child]
    receipt_path = ROOT / f"LAUNCH-{args.label}.json"
    assert not receipt_path.exists(), f"receipt exists: {receipt_path}"
    with (ROOT / f"launch-{args.label}.log").open("x", encoding="utf-8") as log:
        process = subprocess.Popen(command, env=env, cwd=PROFILE, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                   close_fds=True)
    receipt = {"owner": OWNER, "label": args.label, "display": display, "pid": process.pid,
               "app_sha256": digest(APP), "runtime": str(RUNTIME), "seeds": seeds or None,
               "mem_available_kib": mem_kib, "time": time.time(), "profile": str(PROFILE),
               "run": str(run)}
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))


def client_call(label, method, params=None, timeout=15):
    from edenctl import Client
    response = Client(str(run_dir(label) / "control.sock"), timeout=timeout).request(method, params or {})
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


def shot(args):
    target = {"id": args.target} if args.target else next(
        n for n in nodes(client_call(args.label, "ui.tree", {"brief": True, "visible_only": True}))
        if n["class"] == "GRenderWindow")
    out = SHOTS / args.label
    out.mkdir(parents=True, exist_ok=True)
    path = out / f'{time.strftime("%H%M%S", time.gmtime())}-{args.name}.png'
    client_call(args.label, "ui.screenshot", {"path": str(path), "target": target["id"]}, timeout=30)
    print(path)


def tree(args):
    """Visible widgets: class, id and any short text/title, one per line."""
    for n in nodes(client_call(args.label, "ui.tree", {"brief": True, "visible_only": True})):
        words = [str(n.get(k))[:80] for k in ("text", "title", "windowTitle", "objectName") if n.get(k)]
        print(n["class"], n["id"], *words)


def press(args):
    buttons = args.buttons.split(",")
    for button in buttons:
        client_call(args.label, "input.button", {"button": button, "state": "press", "lease_ms": args.hold + 500})
    time.sleep(args.hold / 1000)
    for button in buttons:
        client_call(args.label, "input.button", {"button": button, "state": "release"})
    print("ok")


def stick(args):
    client_call(args.label, "input.stick", {"stick": "left", "x": args.x, "y": args.y, "lease_ms": args.ms})
    time.sleep(args.ms / 1000)
    print("ok")


def call(args):
    print(json.dumps(client_call(args.label, args.method, json.loads(args.json) if args.json else {})))


def keepalive(args):
    """Keep the fighter in place (walking off counts as a self-destruct; the
    game drops players after ~30 s without input), as elite-rematch drive.py."""
    from edenctl import Client
    socket = str(run_dir(args.label) / "control.sock")
    deadline, step, errors = time.monotonic() + args.seconds, 0, 0
    while time.monotonic() < deadline and not Path(args.stop_file).exists():
        for method, params in (("input.stick", {"stick": "left", "x": 0, "y": -1, "lease_ms": 300}),
                               ("input.button", {"button": "zl", "state": "tap", "duration_ms": 80})):
            try:
                Client(socket, timeout=5).request(method, params)
            except Exception:  # a dropped call must never end the loop
                errors += 1
        step += 1
        time.sleep(0.7)
    try:
        Client(socket, timeout=5).request("input.reset", {})
    except Exception:
        errors += 1
    print(json.dumps({"steps": step, "errors": errors,
                      "reason": "stop" if Path(args.stop_file).exists() else "deadline"}))


def stop(args):
    receipt = json.loads((ROOT / f"LAUNCH-{args.label}.json").read_text(encoding="utf-8"))
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
    print(json.dumps({"label": args.label, "pid": pid, "alive": alive(), "state": s.get("state"),
                      "reason": s.get("reason"), "exit_code": s.get("exit_code")}))
    sys.exit(1 if alive() else 0)


parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="op", required=True)
p = sub.add_parser("prep")
p.add_argument("label", type=label_ok)
p.add_argument("--profile", choices=sorted(PROFILES), default="y")
p.add_argument("--diag-file")
p.add_argument("nros", nargs="*")
p.set_defaults(fn=prep)
p = sub.add_parser("launch")
p.add_argument("label", type=label_ok)
p.add_argument("--profile", choices=sorted(PROFILES), default="y")
p.add_argument("display")
p.add_argument("seeds", nargs="?")
p.set_defaults(fn=launch)
p = sub.add_parser("shot")
p.add_argument("label", type=label_ok)
p.add_argument("name")
p.add_argument("target", nargs="?")
p.set_defaults(fn=shot)
p = sub.add_parser("tree")
p.add_argument("label", type=label_ok)
p.set_defaults(fn=tree)
p = sub.add_parser("press")
p.add_argument("label", type=label_ok)
p.add_argument("buttons")
p.add_argument("hold", nargs="?", type=int, default=120)
p.set_defaults(fn=press)
p = sub.add_parser("stick")
p.add_argument("label", type=label_ok)
p.add_argument("x", type=float)
p.add_argument("y", type=float)
p.add_argument("ms", type=int)
p.set_defaults(fn=stick)
p = sub.add_parser("call")
p.add_argument("label", type=label_ok)
p.add_argument("method")
p.add_argument("json", nargs="?")
p.set_defaults(fn=call)
p = sub.add_parser("keepalive")
p.add_argument("label", type=label_ok)
p.add_argument("stop_file")
p.add_argument("seconds", type=float)
p.set_defaults(fn=keepalive)
p = sub.add_parser("stop")
p.add_argument("label", type=label_ok)
p.set_defaults(fn=stop)
args = parser.parse_args()
if getattr(args, "profile", None):
    SOURCE, PROFILE = SOURCES[args.profile], PROFILES[args.profile]
args.fn(args)
