"""Create or reuse this host's private elite-rematch profile and stage plugins.

Usage: prep.py <eNN label> <s|y> [--diag-file <path>] <nro>...
Source profiles are read-only: Surface ssbu-desync-1029/profiles/g, Yoga
ssbu-desync-1029/profiles/b (never a profile named a). Only the private copy
under /home/intpa/elite-rematch-work/profiles/<s|y> is modified.
"""
import argparse
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

ROOT = Path("/home/intpa/elite-rematch-work")
SOURCES = {"s": Path("/home/intpa/ssbu-desync-1029/profiles/g"),
           "y": Path("/home/intpa/ssbu-desync-1029/profiles/b")}
PLUGINS = Path("user/sdmc/atmosphere/contents/01006A800016E000/romfs/skyline/plugins")
OUTPUTS = [Path("user/sdmc/ultimate/elite-rematch"), Path("user/sdmc/ultimate/ssbu-desync-detector")]
FORCED = {"lan_online": "true", "lan_online_tls": "false", "airplane_mode": "false",
          "pauseWhenInBackground": "false"}

parser = argparse.ArgumentParser()
parser.add_argument("label")
parser.add_argument("tag", choices=["s", "y"])
parser.add_argument("--diag-file")
# Same-PC pairs: the second profile on this host is copied from another
# private copy (never from a profile named a).
parser.add_argument("--source")
# Peers only pair with the same TLS setting; a private TLS-on pair stays apart
# from other owners' TLS-off instances on the LAN.
parser.add_argument("--tls", choices=["true", "false"], default="false")
# Boot checks without any network: LAN Online off, airplane mode on.
parser.add_argument("--offline", action="store_true")
parser.add_argument("nros", nargs="+")
args = parser.parse_args()
if args.offline:
    FORCED.update({"lan_online": "false", "airplane_mode": "true"})
assert re.fullmatch(r"e\d{2,}", args.label), f"invalid run label: {args.label}"
source = Path(args.source) if args.source else SOURCES[args.tag]
profile = ROOT / "profiles" / args.tag
assert source.name != "a" and source.is_dir(), f"bad source profile: {source}"
assert source != profile, "source and target profile are the same"
FORCED["lan_online_tls"] = args.tls


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            value.update(block)
    return value.hexdigest()


receipt = {"label": args.label, "tag": args.tag, "time": time.time(), "source": str(source),
           "profile": str(profile), "set_aside": [], "archived": []}
config_rel = Path("user/config/qt-config.ini")
if not profile.exists():
    temp = ROOT / "profiles" / f".{args.tag}-{args.label}-tmp"
    assert not temp.exists(), f"temporary path exists: {temp}"
    temp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, temp, symlinks=True,
                    ignore=shutil.ignore_patterns("profile.lock", "crash_dumps", "log"))
    text = (temp / config_rel).read_text(encoding="utf-8").replace(str(source), str(profile))
    lines = []
    for line in text.splitlines():
        key = line.split("=", 1)[0]
        if key == "nand_directory":
            line = f"nand_directory={profile / 'user/nand'}"
        elif key == "sdmc_directory":
            line = f"sdmc_directory={profile / 'user/sdmc'}"
        elif key in FORCED:
            line = f"{key}={FORCED[key]}"
        lines.append(line)
    (temp / config_rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.rename(profile)
    receipt["created"] = True
# Re-apply the forced settings every time (TLS may change between runs).
lines = []
for line in (profile / config_rel).read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0]
    lines.append(f"{key}={FORCED[key]}" if key in FORCED else line)
(profile / config_rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
config = (profile / config_rel).read_text(encoding="utf-8")
assert str(source) not in config and "/profiles/a/" not in config, "config points outside the copy"
assert f"sdmc_directory={profile / 'user/sdmc'}" in config.splitlines(), "private SDMC path missing"
for key, value in FORCED.items():
    assert f"{key}={value}" in config.splitlines(), f"config lacks {key}={value}"

plugins = profile / PLUGINS
plugins.mkdir(parents=True, exist_ok=True)
aside = plugins.parent / "plugins-set-aside-elite-rematch"
aside.mkdir(exist_ok=True)
for path in sorted(plugins.glob("*.nro")):
    target = aside / f"{path.stem}.{args.label}-{int(time.time())}.nro"
    path.replace(target)
    receipt["set_aside"].append(target.name)
for nro in map(Path, args.nros):
    assert nro.suffix == ".nro" and nro.is_file(), f"missing NRO: {nro}"
    name = nro.name.replace(".diag.nro", ".nro")
    shutil.copyfile(nro, plugins / name)
receipt["plugins"] = {p.name: digest(p) for p in sorted(plugins.glob("*.nro"))}
assert len(receipt["plugins"]) == len(args.nros), "unexpected plugin set"

for rel in OUTPUTS:
    out = profile / rel
    if out.exists():
        archived = out.with_name(f"{out.name}.before-{args.label}")
        assert not archived.exists(), f"archive exists: {archived}"
        out.replace(archived)
        receipt["archived"].append(str(archived))
if args.diag_file:
    out = profile / OUTPUTS[0]
    out.mkdir(parents=True)
    shutil.copyfile(args.diag_file, out / "diag.txt")
    receipt["diag"] = Path(args.diag_file).read_text(encoding="utf-8")

(ROOT / f"PREP-{args.label}-{args.tag}.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps({k: receipt[k] for k in ("label", "tag", "plugins", "archived")} | {"diag": receipt.get("diag")}))
