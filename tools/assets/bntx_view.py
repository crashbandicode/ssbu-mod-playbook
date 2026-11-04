"""Decode the first texture of a BNTX (Tegra block-linear, BCn) to PNG. Usage: bntx_view.py in.bntx out.png"""
import struct
import sys
from PIL import Image

src, out = sys.argv[1], sys.argv[2]
d = open(src, 'rb').read()
assert d[:4] == b'BNTX', d[:4]
nx = d.find(b'NX  ')
info_ptr = struct.unpack_from('<Q', d, nx + 8)[0]
brti = struct.unpack_from('<Q', d, info_ptr)[0]
assert d[brti:brti + 4] == b'BRTI', d[brti:brti + 4]
tile_mode, = struct.unpack_from('<H', d, brti + 0x12)
mips, = struct.unpack_from('<H', d, brti + 0x16)
fmt, = struct.unpack_from('<I', d, brti + 0x1C)
w, h = struct.unpack_from('<ii', d, brti + 0x24)
layout, = struct.unpack_from('<i', d, brti + 0x34)
mip_ptrs, = struct.unpack_from('<Q', d, brti + 0x70)
data_off, = struct.unpack_from('<Q', d, mip_ptrs)
bh_log2 = layout & 7
print(f'format=0x{fmt:x} size={w}x{h} mips={mips} tile_mode={tile_mode} block_height_log2={bh_log2} data@0x{data_off:x}')

kind = fmt >> 8
bcn = {0x1A: (1, 8), 0x1B: (2, 16), 0x1C: (3, 16), 0x1D: (4, 8), 0x1E: (5, 16), 0x20: (7, 16)}
if kind == 0x0B:
    n, bpe, bw = None, 4, 1
else:
    n, bpe = bcn[kind]
    bw = 4
wb, hb = (w + bw - 1) // bw, (h + bw - 1) // bw
row_bytes = wb * bpe
gobs_x = (row_bytes + 63) // 64
bh = 1 << bh_log2
lin = bytearray(wb * hb * bpe)
for y in range(hb):
    for xb in range(row_bytes):
        gob = (y // (8 * bh)) * 512 * bh * gobs_x + (xb // 64) * 512 * bh + ((y % (8 * bh)) // 8) * 512
        x, yy = xb % 64, y % 8
        off = gob + (x // 32) * 256 + (yy // 2) * 64 + ((x % 32) // 16) * 32 + (yy % 2) * 16 + (x % 16)
        lin[y * row_bytes + xb] = d[data_off + off]
if n is None:
    im = Image.frombytes('RGBA', (w, h), bytes(lin))
else:
    im = Image.frombytes('RGBA', (wb * 4, hb * 4), bytes(lin), 'bcn', n).crop((0, 0, w, h))
im.save(out)
print('saved', out)
