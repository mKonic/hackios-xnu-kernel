#!/usr/bin/env python3
"""The Scion app's side of the camera region (SCCAM, avf/CAMERA.md), for QEMU: a test pattern.

  avf/sccam-host.py RAMFILE RAM_BASE ADDR SIZE [--prime-only | --serve-only] [--secs N]

RAMFILE is guest RAM as QEMU's memory-backend-file (share=on) maps it, RAM_BASE its guest physical
address (0x40000000 on QEMU virt), ADDR/SIZE the region (sccam.addr/sccam.size). First the header is
written (before QEMU starts: avfboot and the kernel check it); then, whenever the guest asks for
frames, NV12 frames of the size it asks for go into the slots at 30 a second: eight colour bars
(white, yellow, cyan, green, magenta, red, blue, black), a white square moving across, a grey ramp
at the bottom; the front camera's bars come in the other order and its square is black, so the
two are told apart on the screen.
"""
import mmap, os, struct, sys, time

MAGIC, VERSION = 0x4D434353, 1
HEADER_BYTES, NSLOTS = 0x10000, 3
NV12 = 0x3231564E
READY = 1
SLOT0 = 0x100


def yuv(r, g, b):
    """full-range BT.601, as Camera2's YUV_420_888"""
    y = 0.299 * r + 0.587 * g + 0.114 * b
    u = 128 + (b - y) / 1.772
    v = 128 + (r - y) / 1.402
    return tuple(max(0, min(255, round(c))) for c in (y, u, v))


BARS = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]


def base_frame(w, h, front):
    """the frame without its square: Y plane then interleaved UV (NV12), strides = w"""
    bars = BARS[::-1] if front else BARS
    ramp_from = h * 3 // 4 & ~1
    yrow, uvrow = bytearray(w), bytearray(w)
    for x in range(w):
        y, u, v = yuv(*bars[x * 8 // w])
        yrow[x] = y
        if x % 2 == 0:
            uvrow[x], uvrow[x + 1] = u, v
    rrow = bytes(16 + x * 219 // w for x in range(w))
    gray_uv = bytes([128]) * w
    ypl = bytes(yrow) * ramp_from + rrow * (h - ramp_from)
    uvpl = bytes(uvrow) * (ramp_from // 2) + gray_uv * ((h - ramp_from) // 2)
    return ypl + uvpl


class Host:
    def __init__(self, path, ram_base, addr, size):
        self.f = open(path, "r+b")
        self.m = mmap.mmap(self.f.fileno(), 0)
        self.at = addr - ram_base
        self.size = size
        self.slot_bytes = (size - HEADER_BYTES) // NSLOTS & ~0xFFFF

    def u32(self, off, v=None):
        if v is None:
            return struct.unpack_from("<I", self.m, self.at + off)[0]
        struct.pack_into("<I", self.m, self.at + off, v)

    def u64(self, off, v=None):
        if v is None:
            return struct.unpack_from("<Q", self.m, self.at + off)[0]
        struct.pack_into("<Q", self.m, self.at + off, v)

    def prime(self):
        self.m[self.at:self.at + HEADER_BYTES] = bytes(HEADER_BYTES)
        for off, v in ((0, MAGIC), (4, VERSION), (8, HEADER_BYTES), (0xC, NSLOTS), (0x10, self.slot_bytes), (0x2C, 3)):
            self.u32(off, v)
        self.u32(0x14, READY)
        self.u64(0x20, time.monotonic_ns())
        self.m.flush()
        print(f"sccam-host: header at {self.at + 0x40000000:#x}: {NSLOTS} slots of {self.slot_bytes >> 10} KB", flush=True)

    def serve(self, secs):
        end = time.monotonic() + secs
        seen_req, on, facing, w, h, n = None, False, 0, 0, 0, 0
        frames = {}
        next_t = time.monotonic()
        while time.monotonic() < end:
            req = self.u32(0x40)
            if req != seen_req:
                seen_req = req
                on, facing = self.u32(0x44) == 1, self.u32(0x48)
                w, h = self.u32(0x4C) & ~1, self.u32(0x50) & ~1
                if w * h * 3 // 2 > self.slot_bytes or w < 2 or h < 2:
                    w, h = 1280, 960
                print(f"sccam-host: request {req}: {'on' if on else 'off'}, {'front' if facing else 'back'}, {w}x{h}", flush=True)
                self.u32(0x18, 2 if on else 0)
                self.u32(0x28, req)
            self.u64(0x20, time.monotonic_ns())
            if not on:
                time.sleep(0.05)
                continue
            key = (w, h, facing)
            if key not in frames:
                frames[key] = base_frame(w, h, facing == 1)
            latest, reading = self.u32(0x38), self.u32(0x58)
            s = next(i for i in range(NSLOTS) if i != latest and i != reading - 1)
            d = SLOT0 + 64 * s
            n += 1
            self.u64(d, 2 * n - 1)                      # writing
            off = HEADER_BYTES + s * self.slot_bytes
            base = self.at + off
            self.m[base:base + w * h * 3 // 2] = frames[key]
            # the square: 1/8 of the height, moving across in four seconds
            q = h // 8 & ~1
            x0 = (n * 4 * w // 120) % (w - q) & ~1
            y0 = h // 3 & ~1
            yv = 0 if facing else 235
            for y in range(y0, y0 + q):
                self.m[base + y * w + x0:base + y * w + x0 + q] = bytes([yv]) * q
            for y in range(y0 // 2, (y0 + q) // 2):
                p = base + w * h + y * w + x0
                self.m[p:p + q] = bytes([128]) * q
            struct.pack_into("<QIIIIIIIIII", self.m, self.at + d + 8, time.monotonic_ns(), off, w * h * 3 // 2, w, h, NV12,
                             w, w, w * h, facing, 90 if facing == 0 else 270)
            self.u64(d, 2 * n)                          # written
            self.u32(0x38, s)
            self.u64(0x30, n)
            if n == 1 or n % 300 == 0:
                print(f"sccam-host: frame {n} ({'front' if facing else 'back'} {w}x{h}); guest heartbeat "
                      f"{(time.monotonic_ns() - self.u64(0x60)) / 1e6:.0f} ms old", flush=True)
            next_t += 1 / 30
            time.sleep(max(0, next_t - time.monotonic()))
            if next_t < time.monotonic() - 1:
                next_t = time.monotonic()


def main():
    a = sys.argv[1:]
    if len(a) < 4:
        sys.exit(__doc__)
    path, ram_base, addr, size = a[0], int(a[1], 0), int(a[2], 0), int(a[3], 0)
    secs = float(a[a.index("--secs") + 1]) if "--secs" in a else 3600
    h = Host(path, ram_base, addr, size)
    if "--serve-only" not in a:
        h.prime()
    if "--prime-only" not in a:
        h.serve(secs)


if __name__ == "__main__":
    main()
