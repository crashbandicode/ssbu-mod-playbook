"""Read-only: look for references to given main-image offsets inside plugin
NROs, as little-endian u32/u64 constants or as a MOVZ/MOVK (or MOVZ/ORR-free)
pair building the value in one register within 8 instructions.
Usage: scan-offsets.py <dir_with_nros> <hex_offset>..."""
import struct
import sys
from pathlib import Path

targets = [int(v, 16) for v in sys.argv[2:]]
# Also near misses inside the same functions (any offset in the hooked
# function's first 0x40 bytes counts as the same site).
windows = {t: range(t - 0x40, t + 0x40) for t in targets}
for nro in sorted(Path(sys.argv[1]).glob('*.nro')):
    data = nro.read_bytes()
    hits = []
    for t in targets:
        for width, fmt in ((4, '<I'), (8, '<Q')):
            needle = struct.pack(fmt, t)
            start = data.find(needle)
            if start >= 0:
                hits.append(f'const{width * 8}:{t:#x}@{start:#x}')
    words = struct.unpack_from(f'<{len(data) // 4}I', data)
    for i, w in enumerate(words):
        if (w & 0x7f800000) == 0x52800000 and (w >> 21) & 3 == 0:      # MOVZ, hw=0
            rd, lo = w & 31, (w >> 5) & 0xffff
            for j in range(i + 1, min(i + 9, len(words))):
                k = words[j]
                if (k & 0x7f800000) == 0x72800000 and (k >> 21) & 3 == 1 and k & 31 == rd:  # MOVK hw=1
                    value = ((k >> 5) & 0xffff) << 16 | lo
                    for t, window in windows.items():
                        if value in window:
                            hits.append(f'movz/movk:{value:#x}@{i * 4:#x}')
                    break
    print(f'{nro.name}: {", ".join(hits) if hits else "no reference"}')
