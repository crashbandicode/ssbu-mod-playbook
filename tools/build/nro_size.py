"""Resident size of NROs from their headers: .text, .rodata, .data, .bss."""
import hashlib
import struct
import sys

for path in sys.argv[1:]:
    data = open(path, 'rb').read()
    assert data[0x10:0x14] == b'NRO0', path
    text, ro, rw = (struct.unpack_from('<II', data, 0x20 + 8 * i)[1] for i in range(3))
    bss = struct.unpack_from('<I', data, 0x38)[0]
    total = text + ro + rw + bss
    print(f'{hashlib.sha256(data).hexdigest()[:8]} file={len(data)} text={text} ro={ro} '
          f'data={rw} bss={bss} resident={total} ({total / 1024:.0f} KiB) {path}')
