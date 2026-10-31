"""Print the rating records stored for this host's elite-rematch profiles.

Usage: ratings.py <s|y>...   (records: fighter slot, A, B, current C)
"""
import json
import struct
import sys
from pathlib import Path

ROOT = Path("/home/intpa/elite-rematch-work/profiles")
for tag in sys.argv[1:]:
    # The packaged Eden keeps the store per account and title under the
    # profile's user/lan-online; older launches used <profile>/ratings.
    found = sorted((ROOT / tag / "user/lan-online").glob("*/*/ratings.json")) + [ROOT / tag / "ratings/ratings.json"]
    path = next((p for p in found if p.is_file()), None)
    if path is None:
        print(f"{tag}: no store")
        continue
    data = json.loads(path.read_text())
    rows = []
    for item in data.get("records", []):
        raw = bytes.fromhex(item["record"])
        slot, a, b, c = raw[5], *struct.unpack_from("<III", raw, 6)
        rows.append(f"pid={item['pid']} slot={slot} A={a} B={b} C={c}")
    print(f"{tag}: " + ("; ".join(rows) if rows else "empty"))
