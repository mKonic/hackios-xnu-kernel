#!/usr/bin/env python3
"""Strict payload-contract test for BootShim.S, mkxnuimg.py and the mu-silicium patch.

Coverage (annibale PAYLOAD-MEM contract, 2026-10):
  * packer: strict XNUP container validation and the 16-byte padding rule;
  * shim compile: the legacy (redwood/QEMU) forms stay byte-identical to the
    pre-change baseline, reconstructed from the committed patch hunk;
  * shim statics: label order, stores only after every range check, an 8-byte
    (not 16-byte) target header clear, literal pools ending at _Payload,
    #if guards refusing broken builds;
  * patch: the BootShim section of uefi/mu-silicium.patch reproduces the
    working-tree BootShim.S when applied to the Mu baseline;
  * shim execution: the compiled .text runs in qemu-system-aarch64 (only when
    already installed; nothing is installed here) through its GDB remote stub.
    Layouts cover normal copy, no-payload / bad-magic / oversized-header
    clear, FD in-place (S == D), FD-copy overlap, container/window alias,
    code-in-window and the conservative P == B / P == D refusals; a rejected
    layout must stop inside the _Dead loop with every target byte untouched.
    The 64-bit P wrap cannot run in qemu (instruction fetch outside RAM
    faults first), so it is covered by the arithmetic model and the
    disassembly order check instead.

Host limits stated on purpose: no host run can prove that LK really reads the
source range the shim assumes (offline packer premise), and memory-map or
RamManager behaviour is out of scope. Bytecode beyond the listed layouts is
not executed; the machine code itself gets an independent audit (Astra).
"""
from pathlib import Path
import hashlib
import importlib.util
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SHIM_SRC = ROOT / 'Mu-Silicium/BootShim/BootShim.S'
PATCH = ROOT / 'uefi/mu-silicium.patch'
QEMU = shutil.which('qemu-system-aarch64')
OBJDUMP = shutil.which('objdump') or 'objdump'
CLANG = shutil.which('clang') or 'clang'
BASELINE_PATCH_REV = 'c0716bf'      # kernel commit carrying the pre-change BootShim hunk
BASELINE_SHA = '3898ce4bdf4507d5b22d5094eb252d6de9893768c8352f35fc2664396f4219c5'
ANN = dict(fd=0xA7100000, q=0x300000, b=0xF0000000, c=0x3700000)   # annibale.conf + ruling
FIX = dict(fd=0x48000000, q=0x1000, b=0x50000000, c=0x1000)        # low-capacity fixture
M2 = 1 << 64
passed = []

def ok(name):
    passed.append(name)
    print(f'PASS {name}')

def die(msg):
    print(f'FAIL {msg}')
    sys.exit(1)

# ---------------------------------------------------------------- ELF helpers

def elf_parse(path):
    d = Path(path).read_bytes()
    if d[:4] != b'\x7fELF':
        die(f'{path}: not ELF')
    shoff, = struct.unpack_from('<Q', d, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from('<HHH', d, 0x3a)
    raw = []
    for i in range(shnum):
        o = shoff + i * shentsize
        name, typ, flags, addr, off, size, link = struct.unpack_from('<IIQQQQI', d, o)[:7]
        raw.append((name, typ, off, size, link))
    so = raw[shstrndx]
    strt = d[so[2]:so[2] + so[3]]
    secs = {}
    for name, typ, off, size, link in raw:
        secs[strt[name:strt.index(b'\0', name)].decode()] = (off, size)
    off, size = secs['.text']
    text = d[off:off + size]
    syms = {}
    if '.symtab' in secs:
        off, size = secs['.symtab']
        strtab = d[secs['.strtab'][0]:secs['.strtab'][0] + secs['.strtab'][1]]
        for i in range(size // 24):
            st_name, _, _, _, st_value, _ = struct.unpack_from('<IBBHQQ', d, off + i * 24)
            syms[strtab[st_name:strtab.index(b'\0', st_name)].decode()] = st_value
    return text, syms

def compile_shim(src, out, macros):
    cmd = [CLANG, '--target=aarch64-linux-gnu', '-c', str(src), '-o', str(out)]
    return subprocess.run(cmd + [f'-D{k}={v}' for k, v in macros.items()],
                          capture_output=True, text=True)

# ---------------------------------------------------------------- containers

def entry(name, data):
    return struct.pack('<16sII', name, len(data), 0) + data + b'\0' * (-len(data) % 16)

TERM = b'\0' * 24

def container(body):
    return struct.pack('<II', 0x50554E58, len(body)) + body

# ---------------------------------------------------------------- packer unit tests

def load_packer():
    spec = importlib.util.spec_from_file_location('mkxnuimg', ROOT / 'mkxnuimg.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # main() must not run on import
    return mod

def packer_tests(mod):
    cap = FIX['c']
    small = container(entry(b'kernel', b'K' * 17) + entry(b'cmdline', b'boot=1\0') + TERM)
    n, length = mod.validate_container(small, cap)
    if (n, length) != (len(small) - 8, (len(small) + 15) & ~15):
        die('packer: valid container mis-measured')
    ok('packer: valid container accepted, L = align16(F)')
    if mod.validate_container(container(TERM), cap) != (24, 32):
        die('packer: minimum container rejected')
    ok('packer: minimum container (header + terminator) accepted')
    # caps stay multiples of 16 like every real PAYLOAD_CAP; boundary = L == C
    body = entry(b'x', b'\0' * 8) + TERM          # N = 0x40, F = 0x48, L = 0x50
    if mod.validate_container(container(body), 0x50) is None:
        die('packer: L == C boundary rejected')
    ok('packer: L == C accepted')
    try:
        mod.validate_container(container(body), 0x40)   # C below L
        die('packer: L > C accepted')
    except ValueError:
        ok('packer: L > C rejected')
    try:
        mod.validate_container(container(body + b'\0' * 9), 0x50)   # N = C-7
        die('packer: N > C-8 accepted')
    except ValueError:
        ok('packer: N = C-7 rejected')
    for bad, label in (
        (b'XNUQ' + struct.pack('<I', 24) + TERM, 'old/foreign magic'),
        (container(b''), 'N = 0 (no terminator)'),
        (container(TERM[:-1]), 'truncated terminator'),
        (struct.pack('<II', 0x50554E58, 0xFFFFFFFF) + TERM, 'N = max u32'),
        (container(TERM) + b'\0', 'file longer than header'),
        (container(TERM)[:-1], 'file shorter than header'),
        (container(entry(b'ab' + b'c' * 14, b'') + TERM), 'name without NUL'),
        (container(entry(b'\xffx', b'd') + entry(b'x', b'e') + TERM), 'non-ASCII name'),
        (container(entry(b'x', b'd') + entry(b'x', b'e') + TERM), 'duplicate name'),
        (container(struct.pack('<16sII', b'cmdline', 3, 0) + b'abc' + b'\0' * 13 + TERM),
         'string entry without NUL in data'),
        (container(TERM + b'JUNK' * 6), 'trailing bytes after terminator'),
        (container(struct.pack('<16sII', b'kernel', 0x100, 0) + b'\0' * 16 + TERM),
         'entry data past container end'),
        (container(b'\0kernel_pad' + TERM), 'not-all-zero terminator'),
    ):
        try:
            mod.validate_container(bad, cap)
            die(f'packer: {label} accepted')
        except ValueError:
            pass
    ok('packer: thirteen malformed containers rejected (magic, N, bounds, names, terminator)')
    prefix = b'\0' * 64
    blob = mod.build_blob(prefix[:16], prefix[:48], None, cap)
    if blob[64:] != b'\0' * 16:
        die('packer: no-payload tail must be 16 zero bytes')
    ok('packer: no-payload image carries an explicit 16-byte zero tail')
    blob = mod.build_blob(prefix[:16], prefix[:48], small, cap)
    if blob[:64] != prefix or blob[64:64 + len(small)] != small or \
       blob[64 + len(small):] != b'\0' * (mod.align16(len(small)) - len(small)):
        die('packer: container tail wrong')
    ok('packer: container padded exactly to align16(F)')
    try:
        mod.build_blob(b'\0' * 17, b'\0' * 48, None, cap)   # 65-byte prefix
        die('packer: 16-alignment invariant not enforced')
    except AssertionError:
        ok('packer: unaligned shim+FD prefix rejected')

# ---------------------------------------------------------------- patch <-> source

def extract_bootshim_section(patch_bytes):
    out, keep = [], False
    for line in patch_bytes.splitlines(keepends=True):
        if line.startswith(b'diff --git '):
            keep = b'a/BootShim/BootShim.S' in line
        if keep:
            out.append(line)
    return b''.join(out)

def git(*args):
    r = subprocess.run(['git'] + [str(a) for a in args], capture_output=True)
    if r.returncode != 0:
        die(f'git {" ".join(map(str, args))}: {r.stderr.decode()[:200]}')
    return r.stdout

def apply_section(tmp, section, baseline):
    work = Path(tempfile.mkdtemp(dir=tmp))
    (work / 'BootShim').mkdir()
    (work / 'BootShim/BootShim.S').write_bytes(baseline)
    (work / 'bs.diff').write_bytes(section)
    r = subprocess.run(['git', 'apply', 'bs.diff'], cwd=work, capture_output=True, text=True)
    if r.returncode != 0:
        die(f'git apply failed: {r.stderr.strip()[:200]}')
    return (work / 'BootShim/BootShim.S').read_bytes()

def patch_tests(tmp, baseline_src, prechange_src):
    old = extract_bootshim_section(git('-C', ROOT, 'show', f'{BASELINE_PATCH_REV}:uefi/mu-silicium.patch'))
    work = extract_bootshim_section(PATCH.read_bytes())
    for label, section in (('pre-change', old), ('working', work)):
        if not section.startswith(b'diff --git a/BootShim/BootShim.S'):
            die(f'patch[{label}]: BootShim section not found')
    if apply_section(tmp, old, baseline_src) != prechange_src:
        die('patch[pre-change]: no longer reproduces the pinned baseline')
    if apply_section(tmp, work, baseline_src) != SHIM_SRC.read_bytes():
        die('patch[working]: applied result differs from working-tree BootShim.S')
    if old == work:
        die('patch[working]: BootShim section identical to pre-change (not updated?)')
    if b'\r' not in PATCH.read_bytes()[PATCH.read_bytes().find(b'diff --git a/Silicon'):]:
        die('patch: BootManager CRLF sections lost their CR')
    ok('patch: BootShim section applies cleanly and reproduces the working file; CRLF sections intact')

# ---------------------------------------------------------------- arithmetic model

def model(A, shim_len, cfg, magic_ok, n):
    """Verdict from the implementation sheet's layout rules (§3), written from
    the sheet text and cross-checked against qemu running the real code."""
    B, C, D, Q = cfg['b'], cfg['c'], cfg['fd'], cfg['q']
    S = A + shim_len
    if S >= M2:
        return 'dead'
    P = S + Q
    if P >= M2:
        return 'dead'
    L = ((8 + n + 15) & ~15) if (magic_ok and n <= C - 8) else 0
    if P < B + C and B < P + C:          # container span vs window (conservative P+C)
        return 'dead'
    if A < B + C and B < S:              # running code vs window
        return 'dead'
    if S != D:
        if S < D + Q and D < S + Q:      # FD copy source vs destination
            return 'dead'
        if D + Q > P and D < P + C:      # FD destination vs unread container
            return 'dead'
        if D < S and A < D + Q:          # FD destination vs code
            return 'dead'
    return 'clear' if L == 0 else 'copy'

# ---------------------------------------------------------------- qemu harness

class Rsp:
    def __init__(self, sock):
        self.s = sock
        self.buf = b''
        self.s.settimeout(60)

    def packet(self):
        while True:
            if b'$' in self.buf:
                body = self.buf.split(b'$', 1)[1]
                if b'#' in body:
                    payload, rest = body.split(b'#', 1)
                    self.buf = rest[2:]
                    self.s.sendall(b'+')
                    if payload:
                        return payload
            chunk = self.s.recv(65536)
            if not chunk:
                die('qemu closed the gdb connection')
            self.buf += chunk

    def cmd(self, c):
        p = c.encode()
        self.s.sendall((b'$' + p + b'#%02x') % (sum(p) & 0xFF))
        while True:
            r = self.packet()
            if r.startswith(b'O') and len(r) > 2:   # console output, not the 'OK' reply
                continue
            return r

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass
        if getattr(self, 'proc', None):
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

def qemu_run(syms, mem_mb, A, blob, prewrites, sock, cfg):
    """Load blob at A, run until a gdb breakpoint at _Dead or the FD entry
    stops the core; return (stop_pc, rsp).  rsp stays attached so the caller
    can read target memory; the caller closes it.
    """
    Path(sock).unlink(missing_ok=True)
    p = subprocess.Popen(
        [QEMU, '-M', 'virt', '-cpu', 'max', '-m', str(mem_mb), '-S', '-display', 'none',
         '-monitor', 'none', '-serial', 'null', '-gdb', f'unix:{sock},server=on,wait=on'],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 30
    sock_o = socket.socket(socket.AF_UNIX)
    while True:
        try:
            sock_o.connect(sock)
            break
        except OSError:
            if time.time() > deadline or p.poll() is not None:
                err = p.stderr.read().decode()[:400]
                p.kill()
                die(f'qemu boot failed: {err}')
            time.sleep(0.1)
    rsp = Rsp(sock_o)
    rsp.proc = p
    if not rsp.cmd('?').startswith((b'S', b'T')):
        die('qemu did not report a stopped core')
    for addr, data in [(A, blob)] + prewrites:
        for i in range(0, len(data), 1024):
            chunk = data[i:i + 1024]
            if rsp.cmd(f'M{addr + i:x},{len(chunk):x}:{chunk.hex()}') != b'OK':
                die(f'qemu M write @{addr + i:x} failed')
    # aarch64 g/G layout: x0..x30, sp, pc, cpsr (34 regs, 268 bytes); splice pc
    g = bytearray(bytes.fromhex(rsp.cmd('g').decode()))
    if len(g) != 268:
        die(f'unexpected aarch64 register blob ({len(g)} bytes)')
    struct.pack_into('<Q', g, 256, A)
    if rsp.cmd('G' + g.hex()) != b'OK':
        die('qemu G failed')
    # TCG gdb breakpoints live outside guest memory, so the shim's own writes
    # to the FD destination cannot wipe them; a plain guest BRK would be
    # delivered to the EL3 vectors instead of stopping for gdb.
    for addr in (A + syms['_Dead'], cfg['fd']):
        if rsp.cmd(f'Z1,{addr:x},4') == b'':
            die(f'qemu Z1 rejected at {addr:x}')
    stop = rsp.cmd('c')
    if stop.startswith(b'W'):
        die('qemu exited early')
    g = bytes.fromhex(rsp.cmd('g').decode())
    return struct.unpack_from('<Q', g, 256)[0], rsp

def qemu_read(rsp, addr, n):
    out = b''
    while len(out) < n:
        r = rsp.cmd(f'm{addr + len(out):x},{min(n - len(out), 1024):x}')
        if not re.fullmatch(rb'[0-9a-f]+', r):
            die(f'qemu m read @{addr + len(out):x} -> {r[:60]!r}')
        out += bytes.fromhex(r.decode())
    return out

def qemu_tests(tmp, builds):
    if not QEMU:
        print('SKIP qemu layouts: qemu-system-aarch64 not installed (bytecode untested)')
        return
    texts = {}
    for key, (obj, mem_mb) in builds.items():
        text, syms = elf_parse(obj)
        texts[key] = (text, syms, mem_mb)

    def fd_pattern(q):
        return bytes((i * 7 + 3) & 0xFF for i in range(q))     # FD image body

    def scenario(name, key, A, tail, old_magic=False):
        text, syms, mem_mb = texts[key]
        cfg = ANN if key == 'ann' else FIX
        B, Q, D = cfg['b'], cfg['q'], cfg['fd']
        blob = text + fd_pattern(Q) + tail
        sentinel = bytes([0xA5]) * 0x220
        head = (b'XNUP' + struct.pack('<I', 0x9999)) if old_magic else sentinel[:8]
        # some layouts load the blob across the window; the pre-run state of
        # the checked window region is the sentinel with the blob overlaid
        exp = bytearray(0x240)
        exp[0:0x220] = head + sentinel[8:]
        for off in range(max(0, A - B), min(0x240, A - B + len(blob))):
            exp[off] = blob[off - (A - B)]
        prewrites = [(B, bytes(exp[:0x220]))]
        stop, rsp = qemu_run(syms, mem_mb, A, blob, prewrites,
                             str(Path(tempfile.gettempdir()) / f'bsc-{name}.sock'), cfg)
        verdict = model(A, len(text), cfg, tail[:4] == b'XNUP',
                        struct.unpack_from('<I', tail, 4)[0] if len(tail) >= 8 else 0)
        want = 'dead' if verdict == 'dead' else 'entry'
        got = 'dead' if stop == A + syms['_Dead'] else ('entry' if stop == D else f'pc={stop:x}')
        if got != want:
            die(f'qemu[{name}]: stopped at {got}, model says {want}')
        mem_b = qemu_read(rsp, B, 0x40)
        mem_b_after = qemu_read(rsp, B + 0x220, 32)
        mem_d = qemu_read(rsp, D, 64)
        mem_d_end = qemu_read(rsp, D + Q - 64, 64)
        rsp.close()
        if verdict == 'dead':
            if mem_b != bytes(exp[:0x40]):
                die(f'qemu[{name}]: rejected layout touched the target window')
        elif verdict == 'clear':
            if mem_b[:8] != b'\0' * 8:
                die(f'qemu[{name}]: container header not cleared')
            if mem_b[8:16] != bytes(exp[8:16]):
                die(f'qemu[{name}]: the clear wrote past the 8-byte header')
        else:
            L = (len(tail) + 15) & ~15
            padded = (tail + b'\0' * (L - len(tail)))
            if mem_b[:min(0x40, L)] != padded[:min(0x40, L)]:
                die(f'qemu[{name}]: window bytes differ from container + padding')
            pat = fd_pattern(Q)
            if mem_d != pat[:64] or mem_d_end != pat[-64:]:
                die(f'qemu[{name}]: FD copy destination wrong')
        if mem_b_after != bytes(exp[0x220:0x240]):
            die(f'qemu[{name}]: writes ran beyond the copy/clear length')
        ok(f'qemu[{name}]: {verdict} layout behaves per model')

    small = container(entry(b'kernel', b'K' * 17) + entry(b'cmdline', b'boot=1\0') + TERM)
    small += b'\0' * (-len(small) % 16)
    zeros = b'\0' * 16
    shim_fix, shim_ann = texts['fix'][0], texts['ann'][0]
    q = FIX['q']
    scenario('ok', 'fix', 0x42000000, small)
    scenario('nopayload', 'fix', 0x42000000, zeros, old_magic=True)
    scenario('badmagic', 'fix', 0x42000000, b'XNUQ' + struct.pack('<I', 0x20) + TERM[:16], old_magic=True)
    scenario('oversize', 'fix', 0x42000000, struct.pack('<II', 0x50554E58, FIX['c'] - 7) + TERM[:16], old_magic=True)
    scenario('maxu32', 'fix', 0x42000000, struct.pack('<II', 0x50554E58, 0xFFFFFFFF) + TERM[:16], old_magic=True)
    scenario('inplace', 'fix', FIX['fd'] - len(shim_fix), small)
    scenario('inplace-nopayload', 'fix', FIX['fd'] - len(shim_fix), zeros, old_magic=True)
    scenario('fdoverlap', 'fix', FIX['fd'] - len(shim_fix) + 16, small, old_magic=True)
    scenario('pay-in-window', 'fix', FIX['b'] + 0x10 - q - len(shim_fix), small, old_magic=True)
    scenario('pay-at-B', 'fix', FIX['b'] - q - len(shim_fix), small, old_magic=True)
    scenario('pay-at-D', 'fix', FIX['fd'] - q - len(shim_fix), small, old_magic=True)
    scenario('code-in-window', 'fix', FIX['b'] + 0x40, small, old_magic=True)
    scenario('ann-ok', 'ann', 0x80000000, small)
    scenario('ann-nopayload', 'ann', 0x80000000, zeros, old_magic=True)
    scenario('ann-inplace', 'ann', ANN['fd'] - len(shim_ann), small)

# ---------------------------------------------------------------- statics

# ---------------------------------------------------------------- packer CLI tests

def cli_tests(tmp):
    """Full mkxnuimg.py runs against a fixture Mu tree with a stubbed make.

    A strict rejection must have zero side effects: the stub make never runs,
    the pre-existing BootShim.elf/.bin sentinels and the pre-existing output
    image stay byte-identical. The accepted run calls make exactly once.
    """
    fx = Path(tempfile.mkdtemp(dir=tmp))
    (fx / 'Mu-Silicium/BootShim').mkdir(parents=True)
    (fx / 'Mu-Silicium/Resources/DTBs').mkdir(parents=True)
    (fx / 'Mu-Silicium/Resources/Scripts').mkdir(parents=True)
    (fx / 'bin').mkdir()
    shutil.copy2(ROOT / 'mkxnuimg.py', fx / 'mkxnuimg.py')
    stub = fx / 'bin/make'
    stub.write_text('#!/bin/sh\necho "$@" >> "$MAKES"\nprintf SHIM > BootShim.bin\n'
                    'dd if=/dev/zero bs=1 count=12 >> BootShim.bin 2>/dev/null\n')
    stub.chmod(0o755)
    (fx / 'Mu-Silicium/Resources/Scripts/mkbootimg.py').write_text(
        'import sys\nopen(sys.argv[sys.argv.index("-o") + 1], "wb").write(b"BOOTIMG")\n')
    (fx / 'Mu-Silicium/Resources/DTBs/fixt.dtb').write_bytes(b'DTB!' + b'\x11' * 60)
    (fx / 'Mu-Silicium/Resources/ramdisk').write_bytes(b'ramdisk')
    fd, pay = fx / 'fd.bin', fx / 'pay.bin'
    good = container(entry(b'kernel', b'K' * 16) + TERM)
    sentinel_bin = fx / 'Mu-Silicium/BootShim/BootShim.bin'
    sentinel_elf = fx / 'Mu-Silicium/BootShim/BootShim.elf'
    sentinel_bin.write_bytes(b'OLD-BIN-SENTINEL' * 4)
    sentinel_elf.write_bytes(b'OLD-ELF-SENTINEL' * 4)
    out = fx / 'out.img'
    out.write_bytes(b'PRE-EXISTING-IMAGE')
    snap = (sentinel_bin.read_bytes(), sentinel_elf.read_bytes(), out.read_bytes())
    base = dict(PATH=f'{fx / "bin"}:/bin:/usr/bin', MAKES=str(fx / 'makes.txt'),
                UEFI_TARGET='fixt', UEFI_FD=str(fd), DTB=str(fx / 'Mu-Silicium/Resources/DTBs/fixt.dtb'),
                FD_BASE=hex(FIX['fd']), FD_SIZE=hex(FIX['q']),
                PAYLOAD_BASE=hex(FIX['b']), PAYLOAD_CAP=hex(FIX['c']))

    def run(over, payload, fd_len=None):
        fd.write_bytes(b'\xEE' * (FIX['q'] if fd_len is None else fd_len))
        pay.write_bytes(payload)
        (fx / 'makes.txt').unlink(missing_ok=True)
        r = subprocess.run([sys.executable, str(fx / 'mkxnuimg.py'), '-p', str(pay), '-o', str(out)],
                           env={**base, **over}, capture_output=True, text=True)
        calls = (fx / 'makes.txt').read_text().splitlines() if (fx / 'makes.txt').exists() else []
        return r, calls

    bad = [
        ('PAYLOAD_CAP not 16-aligned', dict(PAYLOAD_CAP='0x1001'), good, None, 'multiple of 16'),
        ('PAYLOAD_CAP below 32', dict(PAYLOAD_CAP='0x10'), good, None, 'at least 32'),
        ('FD_SIZE zero (the copy16 underflow config)', dict(FD_SIZE='0x0'), good, None,
         'positive multiple of 16'),
        ('FD_SIZE not 16-aligned', dict(FD_SIZE='0x1001'), good, None, 'positive multiple of 16'),
        ('negative FD_BASE', dict(FD_BASE='-0x1000'), good, None, 'low 4 GiB'),
        ('FD end past the 4 GiB edge', dict(FD_BASE='0xFFFFF000', FD_SIZE='0x1000'), good, None,
         'low 4 GiB'),
        # Astra r2 repro trio: the same macro sets the compile-neg list refuses
        # to build must be refused before the first unlink/make/write.
        ('negative PAYLOAD_BASE', dict(PAYLOAD_BASE='-1', PAYLOAD_CAP='0x3700000'), good, None,
         'must be non-negative'),
        ('payload window past the 4 GiB edge', dict(PAYLOAD_BASE='0xf0000000',
         PAYLOAD_CAP='0x20000000'), good, None, 'the payload window must stay inside the low 4 GiB'),
        ('FD destination overlaps the payload window',
         dict(FD_BASE='0xa7100000', PAYLOAD_BASE='0xa7100000', PAYLOAD_CAP='0x3700000'), good,
         None, 'overlaps the payload window'),
        ('FD file shorter than FD_SIZE', {}, good, FIX['q'] - 16, 'bytes != FD_SIZE'),
        ('no Mu tree (prebuilt shim predates strict)', dict(UEFI_FD=str(fx / 'missing.fd')), good,
         None, 'predates the strict contract'),
        ('container without XNUP magic', {}, b'JUNK' * 16, None, 'not an XNUP container'),
        ('container body past C-8', {}, struct.pack('<II', 0x50554E58, FIX['c'] - 4) + TERM, None,
         'exceeds C-8'),
        ('header size against file length', {}, good[:-8], None, '!= file length'),
        ('string entry without NUL', {}, container(entry(b'cmdline', b'boot=1') + TERM), None,
         'has no NUL'),
    ]
    for label, over, payload, fd_len, want in bad:
        r, calls = run(over, payload, fd_len)
        now = (sentinel_bin.read_bytes(), sentinel_elf.read_bytes(), out.read_bytes())
        if r.returncode == 0 or want not in r.stderr:
            die(f'cli: {label} not refused as expected (rc={r.returncode}, want {want!r})')
        if calls or now != snap:
            die(f'cli: {label} left side effects (make calls {len(calls)}, '
                f'sentinels/output {"changed" if now != snap else "intact"})')
    ok(f'cli: {len(bad)} rejected inputs run zero side effects (no make, sentinels and output intact)')

    r, calls = run({}, good)
    if r.returncode != 0 or len(calls) != 1 or out.read_bytes() != b'BOOTIMG':
        die(f'cli: accepted run failed (rc={r.returncode}, make calls {len(calls)}): {r.stderr[:200]}')
    if sentinel_bin.read_bytes() != b'SHIM' + b'\0' * 12:
        die('cli: the accepted run did not rebuild BootShim.bin through the make stub')
    ok('cli: the accepted input calls make exactly once and writes the output')

    # Endpoint containment matches the compile guard exactly: an FD whose last
    # byte stops right below PAYLOAD_BASE is a legal layout, so the packer must
    # accept it and reach make once.
    r, calls = run({'FD_BASE': hex(FIX['b'] - FIX['q'])}, good)
    if r.returncode != 0 or len(calls) != 1 or out.read_bytes() != b'BOOTIMG':
        die(f'cli: touching-boundary input rejected (rc={r.returncode}, make calls '
            f'{len(calls)}): {r.stderr[:200]}')
    ok('cli: FD ending exactly at PAYLOAD_BASE is accepted (touching endpoints are disjoint)')

def static_tests(tmp, objs):
    d = subprocess.run([OBJDUMP, '-d', objs['ann']], capture_output=True, text=True).stdout
    labels = {}
    for line in d.splitlines():
        m = re.match(r'^[0-9a-f]+ <(.+)>:', line)
        if m:
            labels.setdefault(m.group(1), int(line.split(' ', 1)[0], 16))
    order = ['_Start', '_PayHeadDone', '_PayCode', '_PayFd', '_PayFdCode', '_PayFdEntry',
             '_PayFdCopy', '_PayFdLoop', '_PayCopy', '_PayLoop', '_PayClear', '_Entry', '_Dead']
    if any(x not in labels for x in order):
        die(f'statics: labels missing: {[x for x in order if x not in labels]}')
    if [labels[x] for x in order] != sorted(labels[x] for x in order):
        die('statics: check-chain label order broken')
    ok('statics: every range check precedes the copy/clear blocks')
    stores = [int(l.split(':')[0].strip(), 16) for l in d.splitlines()
              if re.match(r'^\s*[0-9a-f]+:\s+[0-9a-f]+\s+(stp|str)\t', l)]
    if not stores or min(stores) < labels['_PayFdLoop']:
        die('statics: a store runs before the range checks finish')
    ok('statics: first store is inside the FD copy loop')
    body = d[d.index('<_PayClear>:'):d.index('<_Entry>:', d.index('<_PayClear>:'))]
    if len(re.findall(r'\tstr\txzr, \[x5\]', body)) != 1 or 'stp' in body:
        die('statics: header clear must be exactly one 8-byte str of xzr')
    ok('statics: clear stores 8 bytes (str xzr), no 16-byte stp')
    seg = d[d.index('adds\tx10'):d.index('[x10]')]
    if '_Dead>' not in seg:
        die('statics: the P wrap check must branch out before the container probe reads')
    ok('statics: P overflow branches out before the container probe reads')
    if 'movk\tx12, #0x5055, lsl #16' not in d:
        die('statics: XNUP magic constant missing')
    ok('statics: XNUP magic compared against 0x50554E58')
    if not re.search(r'\tb\t0x[0-9a-f]+ <_Dead>', d[d.index('<_Dead>:'):]):
        die('statics: _Dead must be a self-loop')
    ok('statics: _Dead never returns')
    for key in ('ann', 'fix'):
        text, syms = elf_parse(objs[key])
        if syms['_Payload'] != len(text) or len(text) % 16:
            die(f'statics: {key} shim must end at _Payload and stay 16-aligned (len {len(text)})')
    ok('statics: literal pools end at _Payload; shim length 16-aligned')
    neg = [
        dict(FD_BASE='0xEFF00000', FD_SIZE='0x200000', PAYLOAD_BASE='0xF0000000',
             PAYLOAD_CAP='0x3700000', _want='overlaps the payload window'),
        dict(FD_BASE='0xA7100000', FD_SIZE='0x300001', PAYLOAD_BASE='0xF0000000',
             PAYLOAD_CAP='0x3700000', _want='multiple of 16'),
        dict(FD_BASE='0xA7100000', FD_SIZE='0x300000', PAYLOAD_CAP='0x3700000',
             _want='PAYLOAD_CAP requires PAYLOAD_BASE'),
        dict(FD_BASE='0xA7100000', FD_SIZE='0x300000', PAYLOAD_BASE='0xF0000000',
             PAYLOAD_CAP='0x1000000000', _want='low 4 GiB'),
        dict(FD_BASE='0xA7100000', FD_SIZE='0x0', PAYLOAD_BASE='0xF0000000',
             PAYLOAD_CAP='0x3700000', _want='FD_SIZE must be positive'),
        dict(FD_BASE='-0x1000', FD_SIZE='0x300000', PAYLOAD_BASE='0xF0000000',
             PAYLOAD_CAP='0x3700000', _want='must be non-negative'),
        dict(FD_BASE='0xA7100000', FD_SIZE='0x300000', PAYLOAD_BASE='-0x1000',
             PAYLOAD_CAP='0x3700000', _want='must be non-negative'),
        dict(FD_BASE='0xFFFF0000', FD_SIZE='0x300000', PAYLOAD_BASE='0xF0000000',
             PAYLOAD_CAP='0x3700000', _want='must end inside the low 4 GiB (FD_BASE + FD_SIZE)'),
        # Compile-side twins of the three new CLI rejections: identical macro
        # sets, so a config the packer refuses was already unbuildable.
        dict(FD_BASE='0xA7100000', FD_SIZE='0x1000', PAYLOAD_BASE='-0x1',
             PAYLOAD_CAP='0x3700000', _want='must be non-negative'),
        dict(FD_BASE='0xA7100000', FD_SIZE='0x1000', PAYLOAD_BASE='0xF0000000',
             PAYLOAD_CAP='0x20000000', _want='the payload window must stay inside the low 4 GiB'),
        dict(FD_BASE='0xA7100000', FD_SIZE='0x1000', PAYLOAD_BASE='0xA7100000',
             PAYLOAD_CAP='0x3700000', _want='the FD destination overlaps the payload window'),
    ]
    for macros in neg:
        want = macros.pop('_want')
        r = compile_shim(SHIM_SRC, Path(tmp) / 'neg.o', dict(macros, REQUIRES_KERNEL_HEADER='1'))
        if r.returncode == 0 or want not in r.stderr:
            die(f'statics: broken build ({want}) not refused as expected')
    ok(f'statics: {len(neg)} broken build configurations are refused')
    text, _ = elf_parse(objs['ann'])
    ANN_TEXT_SHA = '0a5cbe63026c7e372120a9ed55f4380e94e2842babc741f87f44f44130ffaa87'
    if hashlib.sha256(text).hexdigest() != ANN_TEXT_SHA:
        die('annibale strict .text changed: the range guards must stay compile-time only')
    ok('statics: annibale strict machine code is byte-identical to the frozen baseline')

# ---------------------------------------------------------------- main

def main():
    tmp = tempfile.mkdtemp(prefix='bootshim-contract-')
    baseline_src = git('-C', ROOT / 'Mu-Silicium', 'show', 'HEAD:BootShim/BootShim.S')
    work = Path(tempfile.mkdtemp(dir=tmp))
    (work / 'BootShim').mkdir()
    (work / 'BootShim/BootShim.S').write_bytes(baseline_src)
    (work / 'bs.diff').write_bytes(extract_bootshim_section(
        git('-C', ROOT, 'show', f'{BASELINE_PATCH_REV}:uefi/mu-silicium.patch')))
    r = subprocess.run(['git', 'apply', 'bs.diff'], cwd=work, capture_output=True, text=True)
    if r.returncode != 0:
        die(f'pre-change hunk no longer applies to Mu HEAD: {r.stderr[:200]}')
    prechange = (work / 'BootShim/BootShim.S').read_bytes()
    if hashlib.sha256(prechange).hexdigest() != BASELINE_SHA:
        die('reconstructed pre-change BootShim.S hash changed (baseline moved?)')
    ok('baseline: pre-change BootShim.S reconstructed from the committed patch (hash pinned)')
    (work / 'prechange.S').write_bytes(prechange)

    objs, builds = {}, {}
    for key, cfg, mem in (('ann', ANN, 2880), ('fix', FIX, 264)):
        out = str(Path(tmp) / f'{key}.o')
        r = compile_shim(SHIM_SRC, out, {'REQUIRES_KERNEL_HEADER': '1', 'FD_BASE': hex(cfg['fd']),
                                         'FD_SIZE': hex(cfg['q']), 'PAYLOAD_BASE': hex(cfg['b']),
                                         'PAYLOAD_CAP': hex(cfg['c'])})
        if r.returncode != 0:
            die(f'{key} strict build failed: {r.stderr[:300]}')
        objs[key] = out
        builds[key] = (out, mem)
    ok('compile: strict shim builds for annibale and the low-capacity fixture')

    for form, macros in (('legacy', {'REQUIRES_KERNEL_HEADER': '1', 'FD_BASE': '0x9FD00000',
                                     'FD_SIZE': '0x200000', 'PAYLOAD_BASE': '0xF0000000'}),
                         ('plain', {'REQUIRES_KERNEL_HEADER': '1', 'FD_BASE': '0x9FD00000',
                                    'FD_SIZE': '0x200000'})):
        for side, src in (('old', work / 'prechange.S'), ('new', SHIM_SRC)):
            r = compile_shim(src, Path(tmp) / f'{form}_{side}.o', macros)
            if r.returncode != 0:
                die(f'{form}/{side} compile failed: {r.stderr[:200]}')
        if (Path(tmp) / f'{form}_old.o').read_bytes() != (Path(tmp) / f'{form}_new.o').read_bytes():
            die(f'{form} form is no longer byte-identical for redwood/QEMU')
    ok('compile: legacy and plain shim objects stay byte-identical (redwood/QEMU unchanged)')

    static_tests(tmp, objs)
    packer_tests(load_packer())
    cli_tests(tmp)
    qemu_tests(tmp, builds)
    patch_tests(tmp, baseline_src, prechange)
    print(f'\nALL {len(passed)} CHECKS PASSED')

if __name__ == '__main__':
    main()
