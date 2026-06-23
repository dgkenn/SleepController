#!/usr/bin/env python3
"""Generate minimal PNG icons for the SleepCtl PWA."""

import struct
import zlib
import os

def make_png(width: int, height: int, r: int, g: int, b: int) -> bytes:
    """Create a minimal valid PNG with a solid color."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack('>I', len(data)) + tag + data
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return c + struct.pack('>I', crc)

    # PNG signature
    sig = b'\x89PNG\r\n\x1a\n'

    # IHDR: width, height, bit depth=8, color type=2 (RGB), compression=0, filter=0, interlace=0
    ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    ihdr = chunk(b'IHDR', ihdr_data)

    # IDAT: raw scanlines, each prefixed with filter byte 0
    raw = b''
    for _ in range(height):
        row = b'\x00' + bytes([r, g, b] * width)
        raw += row

    compressed = zlib.compress(raw, 9)
    idat = chunk(b'IDAT', compressed)

    # IEND
    iend = chunk(b'IEND', b'')

    return sig + ihdr + idat + iend


# Background color: dark navy #0f0f14
R, G, B = 0x0f, 0x0f, 0x14

script_dir = os.path.dirname(os.path.abspath(__file__))
public_dir = os.path.join(script_dir, '..', 'public')
os.makedirs(public_dir, exist_ok=True)

for size in [192, 512]:
    path = os.path.join(public_dir, f'icon-{size}.png')
    data = make_png(size, size, R, G, B)
    with open(path, 'wb') as f:
        f.write(data)
    print(f'Created {path} ({len(data)} bytes)')

print('Done.')
