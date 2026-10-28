#!/usr/bin/env python3
"""
Pack the redwood UEFI (with the hook that starts the XNU loader) and an optional payload
into an Android boot image for `fastboot boot`.

  kernel = gzip(BootShim + UEFI FD + ["XNUP" container]) + DTB

The boot shim copies the FD to its base and the payload to PAYLOAD_BASE.

The UEFI pieces are taken from a Mu-Silicium tree built here (Mu-Silicium/, see uefi/README.md)
or, when there is none, from prebuilt/uefi/.

usage: mkxnuimg.py [-p payload.bin] [-o out.img]

Another device (common/devices/<name>.conf, read by common/build.sh) sets these in the environment:
  UEFI_TARGET   Mu-Silicium device name (default redwood)    UEFI_FD, DTB   files to use instead
  FD_BASE, FD_SIZE, PAYLOAD_BASE                              addresses from its UEFI port's memory map
  PAYLOAD_CAP   strict payload contract (annibale): container limit C in bytes, passed to the
                shim as -DPAYLOAD_CAP. Set = strict checks below; unset = legacy shape (redwood,
                QEMU) keeps its original container check and byte stream.
"""
import argparse, gzip, os, struct, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MU = os.path.join(HERE, 'Mu-Silicium')
PREBUILT = os.path.join(HERE, 'prebuilt', 'uefi')

XNUP_MAGIC = b'XNUP'
ENTRY_HDR = 24  # char name[16], u32 size, u32 reserved (see xnuloader/mkpayload.py)
# Entries whose consumers read them as C strings; mkpayload.py writes a NUL inside the
# data of every one, so the strict packer rejects the container without it.
STRING_NAMES = ('cmdline', 'physbase', 'rdres', 'rdfile', 'usb', 'checkpoint')

def env_int(name, default):
    v = os.environ.get(name, '')
    return int(v, 0) if v else default

def align16(n):
    return (n + 15) & ~15

def validate_container(data, cap):
    """Strict "XNUP" container check for the PAYLOAD_CAP contract.

    data is a complete container file. Returns (N, L): body bytes and the
    16-aligned length the shim will copy. Raises ValueError on any defect;
    a rejected container must not reach the image.
    """
    if len(data) < 8 or data[:4] != XNUP_MAGIC:
        raise ValueError('not an XNUP container')
    n, = struct.unpack_from('<I', data, 4)
    if n > cap - 8:
        raise ValueError(f'body {n:#x} exceeds C-8 = {cap - 8:#x}')
    if 8 + n != len(data):
        raise ValueError(f'header size {n:#x} + 8 != file length {len(data):#x}')
    length = align16(8 + n)
    if length > cap:
        raise ValueError(f'copy length {length:#x} exceeds C = {cap:#x}')
    if n < ENTRY_HDR:
        raise ValueError('body too short for the 24-byte terminator')
    seen = set()
    pos, end = 8, 8 + n
    while True:
        if pos + ENTRY_HDR > end:
            raise ValueError(f'entry at +{pos - 8:#x} truncated')
        raw = data[pos:pos + ENTRY_HDR]
        if raw[0] == 0:
            if raw != b'\0' * ENTRY_HDR:
                raise ValueError(f'terminator at +{pos - 8:#x} is not all-zero')
            if pos + ENTRY_HDR != end:
                raise ValueError(f'{end - pos - ENTRY_HDR} trailing bytes after the terminator')
            return n, length
        name_field = raw[:16]
        esize = struct.unpack_from('<I', raw, 16)[0]
        if b'\0' not in name_field:
            raise ValueError(f'entry at +{pos - 8:#x} has a 16-byte name without NUL')
        name = name_field.split(b'\0', 1)[0]
        if not name.isascii():
            raise ValueError(f'entry at +{pos - 8:#x} has a non-ASCII name')
        if name in seen:
            raise ValueError(f'duplicate entry {name.decode()!r}')
        seen.add(name)
        padded = align16(esize)
        if pos + ENTRY_HDR + padded > end:
            raise ValueError(f'entry {name.decode()} size {esize:#x} runs past the container end')
        if name.decode() in STRING_NAMES:
            if b'\0' not in data[pos + ENTRY_HDR:pos + ENTRY_HDR + esize]:
                raise ValueError(f'string entry {name.decode()} has no NUL inside its data')
        pos += ENTRY_HDR + padded

def build_blob(shim, fd, container, cap):
    """Assemble the boot shim + FD + payload image with the strict padding rules.

    container is the validated container bytes, or None for no payload. The
    shim needs at least 16 readable bytes at the source container start, and
    copies align16(8 + N) bytes, so the container is padded to that length;
    a no-payload image carries 16 zero bytes instead of trusting whatever
    the decompression buffer happens to hold there.
    """
    blob = shim + fd
    assert len(blob) % 16 == 0, 'shim + FD must be 16-aligned for the padding rule'
    if container is None:
        return blob + b'\0' * 16
    n, length = validate_container(container, cap)
    return blob + container + b'\0' * (length - (8 + n))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-p', '--payload')
    ap.add_argument('-o', '--output', default=os.path.join(HERE, 'xnu-redwood.img'))
    ap.add_argument('--mode', default='RELEASE')
    args = ap.parse_args()

    target = os.environ.get('UEFI_TARGET') or 'redwood'
    fd_base, fd_size = env_int('FD_BASE', 0x9FD00000), env_int('FD_SIZE', 0x200000)
    payload_base, payload_max = env_int('PAYLOAD_BASE', 0xF0000000), 0x04000000
    payload_cap = env_int('PAYLOAD_CAP', 0)
    strict = payload_cap != 0
    if strict:
        if payload_cap % 16 or payload_cap < 32:
            sys.exit(f'PAYLOAD_CAP {payload_cap:#x}: must be a multiple of 16 and at least 32')

    fd_path = os.environ.get('UEFI_FD') or os.path.join(MU, f'Build/{target}Pkg/{args.mode}_CLANGPDB/FV/SILICIUM_UEFI.fd')
    use_mu = os.path.isfile(fd_path) and os.path.isdir(os.path.join(MU, 'BootShim'))
    container = open(args.payload, 'rb').read() if args.payload else None

    if strict:
        # Reject every bad input before the first unlink/make/write: a refused
        # container must leave the previous BootShim.bin/.elf and the output
        # image exactly as they were (zero build side effects).
        if fd_size <= 0 or fd_size % 16:
            sys.exit(f'FD_SIZE {fd_size:#x}: the strict contract needs a positive multiple of 16')
        if fd_base < 0 or fd_base + fd_size > 0xFFFFFFFF:
            sys.exit(f'FD_BASE {fd_base:#x} + FD_SIZE {fd_size:#x}: must stay inside the low 4 GiB')
        # Same expressions and endpoints as the BootShim.S compile guards, so
        # every config the strict shim refuses to build is refused here, while
        # BootShim.bin/.elf and the output image are still untouched.
        if payload_base < 0:
            sys.exit(f'PAYLOAD_BASE {payload_base:#x}: must be non-negative')
        if payload_base + payload_cap > 0xFFFFFFFF:
            sys.exit(f'the payload window must stay inside the low 4 GiB '
                     f'(PAYLOAD_BASE {payload_base:#x} + PAYLOAD_CAP {payload_cap:#x} = '
                     f'{payload_base + payload_cap:#x})')
        if fd_base + fd_size > payload_base and payload_base + payload_cap > fd_base:
            sys.exit(f'the FD destination [{fd_base:#x}, {fd_base + fd_size:#x}) overlaps the payload window '
                     f'[{payload_base:#x}, {payload_base + payload_cap:#x})')
        if not use_mu:
            sys.exit('PAYLOAD_CAP needs the BootShim built from Mu-Silicium/ '
                     '(prebuilt/uefi/BootShim.bin predates the strict contract)')
        fd_len = os.path.getsize(fd_path)
        if fd_len != fd_size:
            sys.exit(f'FD file {fd_path}: {fd_len:#x} bytes != FD_SIZE {fd_size:#x}')
        if container is not None:
            try:
                validate_container(container, payload_cap)
            except ValueError as exc:
                sys.exit(f'payload rejected: {exc}')

    if use_mu:
        shim_dir = os.path.join(MU, 'BootShim')
        for f in ('BootShim.elf', 'BootShim.bin'):
            try: os.unlink(os.path.join(shim_dir, f))
            except FileNotFoundError: pass
        cap_flag = f' -DPAYLOAD_CAP={payload_cap:#x}' if strict else ''
        subprocess.check_call(['make', 'REQUIRES_KERNEL_HEADER=1', f'FD_BASE={fd_base:#x}', f'FD_SIZE={fd_size:#x}',
                               f'CFLAGS=--target=aarch64-linux-gnu -DPAYLOAD_BASE={payload_base:#x}{cap_flag}'],
                              cwd=shim_dir, stdout=subprocess.DEVNULL)
        parts = {'shim': os.path.join(shim_dir, 'BootShim.bin'), 'fd': fd_path,
                 'dtb': os.path.join(MU, f'Resources/DTBs/{target}.dtb'), 'ramdisk': os.path.join(MU, 'Resources/ramdisk'),
                 'mkbootimg': os.path.join(MU, 'Resources/Scripts/mkbootimg.py')}
    else:
        parts = {'shim': os.path.join(PREBUILT, 'BootShim.bin'), 'fd': os.path.join(PREBUILT, 'SILICIUM_UEFI.fd'),
                 'dtb': os.path.join(PREBUILT, 'redwood.dtb'), 'ramdisk': os.path.join(PREBUILT, 'ramdisk'),
                 'mkbootimg': os.path.join(PREBUILT, 'mkbootimg.py')}
        if os.environ.get('UEFI_FD'): parts['fd'] = os.environ['UEFI_FD']
    shim = open(parts['shim'], 'rb').read()
    fd = open(parts['fd'], 'rb').read()
    assert len(fd) == fd_size, hex(len(fd))

    if strict:
        try:
            blob = build_blob(shim, fd, container, payload_cap)
        except ValueError as exc:
            sys.exit(f'payload rejected: {exc}')
        if container is not None:
            n, length = validate_container(container, payload_cap)
            print(f'payload: {len(container)} bytes (N={n:#x}, shim copy {length:#x}), '
                  f'bytesum={sum(container) & 0xffffffff:08x}')
    else:
        blob = shim + fd
        if container is not None:
            data = container
            # the payload file is a complete "XNUP" container (see xnuloader/mkpayload.py)
            assert data[:4] == XNUP_MAGIC and len(data) <= payload_max - 0x1000, 'bad or oversized payload'
            blob += data
            blob += b'\0' * (-len(blob) % 16)
            print(f'payload: {len(data)} bytes, bytesum={sum(data) & 0xffffffff:08x}')

    if os.environ.get('DTB'): parts['dtb'] = os.environ['DTB']
    dtb = open(parts['dtb'], 'rb').read()
    kernel = args.output + '.kernel~'
    open(kernel, 'wb').write(gzip.compress(blob, 6) + dtb)
    try:
        subprocess.check_call([sys.executable, parts['mkbootimg'], '--kernel', kernel, '--ramdisk', parts['ramdisk'],
                               '--header_version', '1', '-o', args.output])
    finally:
        os.unlink(kernel)
    print(f'{args.output}: {os.path.getsize(args.output)} bytes (shim {len(shim)}, uncompressed {len(blob)})')

if __name__ == '__main__':
    main()
