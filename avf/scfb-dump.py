#!/usr/bin/env python3
"""Save the SCFB framebuffer as a PNG.

  avf/scfb-dump.py --qemu ADDR OUT.png        from a running avf/qemu-scfb.sh (QMP port 4455)
  avf/scfb-dump.py FILE FILE_BASE ADDR OUT.png from a dump of guest memory starting at FILE_BASE
"""
import struct, sys, zlib
import os, socket, time
if sys.argv[1] == "--qemu":
    addr, out = int(sys.argv[2], 0), sys.argv[3]
    ram, base = os.path.abspath(out + ".bin"), addr
    import json
    m = socket.create_connection(("127.0.0.1", int(os.environ.get("QMP", "4455")))); f = m.makefile("rw")
    f.readline(); f.write('{"execute": "qmp_capabilities"}\n'); f.flush(); f.readline()
    f.write(json.dumps({"execute": "pmemsave", "arguments": {"val": addr, "size": 0x2000000, "filename": ram}}) + "\n"); f.flush()
    while '"return"' not in (r := f.readline()) and '"error"' not in r:
        pass
else:
    ram, base, addr, out = sys.argv[1], int(sys.argv[2], 0), int(sys.argv[3], 0), sys.argv[4]
with open(ram, 'rb') as f:
    f.seek(addr - base)
    h = f.read(0x70)
    magic, ver, w, hgt, stride, fmt, off0, off1, nbufs, flags, front, drawing = struct.unpack('<12I', h[:48])
    seq, = struct.unpack('<Q', h[0x30:0x38])
    print(f"magic={magic:#x} {w}x{hgt} stride={stride} fmt={fmt:#x} nbufs={nbufs} flags={flags} front={front} drawing={drawing} frame_seq={seq}")
    if magic != 0x42464353:
        sys.exit(1)
    f.seek(addr - base + (off1 if front else off0))
    data = f.read(stride * hgt)
rows = []
for y in range(hgt):
    r = data[y * stride:y * stride + w * 4]
    if fmt == 0x34325258:  # XR24: B,G,R,X -> RGB
        px = bytearray(w * 3); px[0::3] = r[2::4]; px[1::3] = r[1::4]; px[2::3] = r[0::4]
    else:
        px = bytearray(w * 3); px[0::3] = r[0::4]; px[1::3] = r[1::4]; px[2::3] = r[2::4]
    rows.append(b'\0' + bytes(px))
def chunk(t, d): return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, hgt, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b'')
open(out, 'wb').write(png)
print("wrote", out)
