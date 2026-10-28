#!/usr/bin/env python3
"""Drive xnuloader/mkpayload.py input checks: single-read guarantee, item name
rules, flag-owned names, epoch value class, NUL refusals, and output clobbering.
Plain script: every check prints a PASS line; any failed assertion exits
non-zero. Builds stay inside a temporary directory."""
from pathlib import Path
import hashlib
import runpy
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MK = str(ROOT / "xnuloader/mkpayload.py")

def sha(b):
    return hashlib.sha256(b).hexdigest()

def entries(payload):
    magic, size = struct.unpack_from('<II', payload, 0)
    assert magic == 0x50554E58, magic
    assert size == len(payload) - 8, (size, len(payload))
    out, at = [], 8
    while payload[at]:
        name, esize, _ = struct.unpack_from('<16sII', payload, at)
        data = payload[at + 24:at + 24 + esize]
        out.append((name.rstrip(b'\0'), data))
        at += 24 + esize + (-esize % 16)
    assert payload[at:] == b'\0' * 24
    return out

def run(args):
    p = subprocess.run([sys.executable, MK] + args, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()

def run_nul(argv):
    """Run mkpayload with an injected argv so items can carry NUL bytes, and
    return (exit code, combined output, per-file open counts)."""
    script = f'''
import builtins, io, json, runpy, sys
from contextlib import redirect_stdout, redirect_stderr
real_open = builtins.open
counts = {{}}
def counting_open(file, mode='r', *a, **k):
    if 'b' in mode and 'r' in mode:
        key = str(file)
        counts[key] = counts.get(key, 0) + 1
    return real_open(file, mode, *a, **k)
builtins.open = counting_open
sys.argv = [{MK!r}] + {argv!r}
out, err = io.StringIO(), io.StringIO()
code = 0
try:
    with redirect_stdout(out), redirect_stderr(err):
        runpy.run_path({MK!r}, run_name="__main__")
except SystemExit as e:
    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    if not isinstance(e.code, int) and e.code:
        err.write(str(e.code) + "\\n")
print("EXIT", code)
print(out.getvalue(), end="")
print(err.getvalue(), end="")
print("OPENS", json.dumps(counts))
'''
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    lines = p.stdout.strip().splitlines()
    code = int(lines[0].split()[1])
    counts = json_line(lines[-1])
    return code, "\n".join(lines[1:-1]).strip(), counts

def json_line(line):
    import json
    assert line.startswith("OPENS "), line
    return json.loads(line[len("OPENS "):])

with tempfile.TemporaryDirectory(prefix="mkpayload-test-") as directory:
    d = Path(directory)
    kernel = d / "kernel"; kernel.write_bytes(b"K" * 4096)
    ramdisk = d / "ramdisk"; ramdisk.write_bytes(b"R" * 2048)
    other = d / "other"; other.write_bytes(b"O" * 32)
    approved = "debug=0x10e cpus=1 bootcpu=7 epoch=1792813370 adreno8=14 adreno8_keep=1"

    # Ordinary approved-style build: items, flags, require, exact-cmd, layout.
    out = d / "p.bin"
    rc, msg = run(["-o", str(out), f"kernel={kernel}", f"ramdisk={ramdisk}",
                   f"--cmdline={approved.replace('1792813370', '1792900000')}",
                   f"--require=kernel={sha(kernel.read_bytes())}",
                   f"--require=ramdisk={sha(ramdisk.read_bytes())}",
                   f"--exact-cmd={approved}", "--forbid-cmd", "loader_return_after",
                   "--rdfile=18:9220096", "--checkpoint=14"])
    assert rc == 0, msg
    got = entries(out.read_bytes())
    assert got == [(b"kernel", kernel.read_bytes()), (b"ramdisk", ramdisk.read_bytes()),
                   (b"cmdline", approved.replace('1792813370', '1792900000').encode() + b'\0'),
                   (b"rdfile", b"18:9220096\0"), (b"checkpoint", b"14\0")], got
    print("ordinary kernel/ramdisk + flags + require + exact-cmd build: PASS")

    # A 15-byte name is the maximum that fits char name[16] with its NUL.
    fifteen = d / "fifteen"; fifteen.write_bytes(b"F" * 8)
    rc, msg = run(["-o", str(d / "p15.bin"), f"aaaaaaaaaaaaaaa={fifteen}"])
    assert rc == 0 and entries((d / "p15.bin").read_bytes())[0][0] == b"aaaaaaaaaaaaaaa", msg
    print("15-byte name accepted: PASS")

    rc, msg = run(["-o", str(d / "p16.bin"), f"aaaaaaaaaaaaaaaa={fifteen}"])
    assert rc != 0 and "1..15 ASCII bytes" in msg, msg
    print("16-byte name refused (was silently truncated): PASS")

    rc, msg = run(["-o", str(d / "puni.bin"), f"keß={fifteen}"])
    assert rc != 0 and "1..15 ASCII bytes" in msg, msg
    print("non-ASCII name refused: PASS")

    rc, msg = run(["-o", str(d / "pempty.bin"), f"={fifteen}"])
    assert rc != 0 and "1..15 ASCII bytes" in msg, msg
    rc, msg = run(["-o", str(d / "pnoeq.bin"), str(fifteen)])
    assert rc != 0 and "expected name=path" in msg, msg
    print("empty name and missing =path refused: PASS")

    # NUL-bearing name: with require the refusal is the name rule itself.
    rc, msg, counts = run_nul(["-o", str(d / "pnul.bin"), f"kernel\x00suffix={kernel}",
                               f"--require=kernel={sha(kernel.read_bytes())}"])
    assert rc != 0 and "1..15 ASCII bytes without NUL" in msg, (rc, msg)
    rc, msg, counts = run_nul(["-o", str(d / "pnul2.bin"), f"kernel\x00suffix={kernel}"])
    assert rc != 0 and "1..15 ASCII bytes without NUL" in msg, (rc, msg)
    print("NUL in item name refused with and without require: PASS")

    # Flag-owned names cannot arrive through items.
    for owned in ("cmdline", "physbase", "rdres", "rdfile", "usb", "checkpoint"):
        rc, msg = run(["-o", str(d / f"pown-{owned}.bin"), f"kernel={kernel}", f"{owned}={other}"])
        assert rc != 0 and f"item: {owned} comes from the --{owned} flag" in msg, (owned, rc, msg)
    print("items cannot supply flag-owned entry names: PASS")

    # Duplicate names are refused; both paths are named.
    rc, msg = run(["-o", str(d / "pdup.bin"), f"kernel={kernel}", f"kernel={ramdisk}"])
    assert rc != 0 and "duplicate name kernel" in msg and str(kernel) in msg and str(ramdisk) in msg, msg
    print("duplicate item name refused (last-wins hidden override removed): PASS")

    # require messages keep their strings.
    rc, msg = run(["-o", str(d / "pbad.bin"), f"kernel={kernel}", "--require=kernel=" + "0" * 64])
    assert rc != 0 and f"require: kernel ({kernel}) is {sha(kernel.read_bytes())}, wanted " in msg and "0" * 64 in msg, msg
    rc, msg = run(["-o", str(d / "pnosuch.bin"), f"kernel={kernel}", "--require=nosuch=" + "0" * 64])
    assert rc != 0 and msg.endswith("require: no input named nosuch"), msg
    print("require mismatch and unknown-name messages unchanged: PASS")

    # The single-read guarantee: each input is opened for reading exactly once.
    rc, msg, counts = run_nul(["-o", str(d / "pone.bin"), f"kernel={kernel}", f"ramdisk={ramdisk}",
                               f"--require=kernel={sha(kernel.read_bytes())}"])
    assert rc == 0, (rc, msg)
    assert counts == {str(kernel): 1, str(ramdisk): 1}, counts
    assert entries((d / "pone.bin").read_bytes())[0][1] == kernel.read_bytes()
    print("each input file read exactly once (require and assembly share one read): PASS")

    # cmdline checks keep their behaviour and messages.
    rc, msg = run(["-o", str(d / "prc.bin"), f"kernel={kernel}", "--cmdline=debug=0x10e",
                   "--require-cmd", "cpus=1"])
    assert rc != 0 and msg.endswith("require-cmd: cmdline lacks cpus=1"), msg
    rc, msg = run(["-o", str(d / "pfc.bin"), f"kernel={kernel}",
                   "--cmdline=debug=0x10e loader_return_after=1", "--forbid-cmd", "loader_return_after"])
    assert rc != 0 and msg.endswith("forbid-cmd: cmdline carries loader_return_after=1"), msg
    print("require-cmd and forbid-cmd messages unchanged: PASS")

    # --forbid-cmd matches the exact key, bare or with =value; the help says so and
    # a longer key merely starting with the token stays buildable.
    rc, msg = run(["-o", str(d / "pfc2.bin"), f"kernel={kernel}",
                   "--cmdline=debug=0x10e adreno8_keep=1", "--forbid-cmd", "adreno8"])
    assert rc == 0, msg
    rc, msg = run(["-o", str(d / "pfc3.bin"), f"kernel={kernel}",
                   "--cmdline=debug=0x10e adreno8", "--forbid-cmd", "adreno8"])
    assert rc != 0 and msg.endswith("forbid-cmd: cmdline carries adreno8"), msg
    rc, msg = run(["-o", str(d / "pfc4.bin"), f"kernel={kernel}",
                   "--cmdline=debug=0x10e adreno8_keep=1", "--forbid-cmd", "adreno8_keep"])
    assert rc != 0 and msg.endswith("forbid-cmd: cmdline carries adreno8_keep=1"), msg
    helptext = " ".join(subprocess.run([sys.executable, MK, "--help"], capture_output=True, text=True).stdout.split())
    assert "exact boot-arg key" in helptext and "with this prefix" not in helptext
    print("--forbid-cmd matches the exact key (bare or =value), prefix look-alikes build: PASS")

    # NUL in boot-arg strings is refused before any comparison.
    rc, msg, counts = run_nul(["-o", str(d / "pcnul.bin"), f"kernel={kernel}",
                               "--cmdline=debug=0x10e\x00cpus=1"])
    assert rc != 0 and "cmdline: contains a NUL byte" in msg, (rc, msg)
    rc, msg, counts = run_nul(["-o", str(d / "pecnul.bin"), f"kernel={kernel}",
                               f"--cmdline={approved.replace('1792813370', '1792900000')}",
                               f"--exact-cmd={approved}\x00"])
    assert rc != 0 and "exact-cmd: contains a NUL byte" in msg, (rc, msg)
    print("NUL bytes in cmdline and exact-cmd refused: PASS")

    # exact-cmd epoch value class.
    rc, msg = run(["-o", str(d / "pex1.bin"), f"kernel={kernel}",
                   "--cmdline=debug=0x10e cpus=1 epoch=abc", f"--exact-cmd={approved}"])
    assert rc != 0 and "epoch value must be non-negative decimal: 'epoch=abc'" in msg, msg
    rc, msg = run(["-o", str(d / "pex2.bin"), f"kernel={kernel}",
                   "--cmdline=debug=0x10e cpus=1 epoch=-1", f"--exact-cmd={approved}"])
    assert rc != 0 and "non-negative decimal" in msg, msg
    rc, msg, counts = run_nul(["-o", str(d / "pex3.bin"), f"kernel={kernel}",
                               "--cmdline=debug=0x10e cpus=1 epoch=1792813370\x002", f"--exact-cmd={approved}"])
    assert rc != 0 and "contains a NUL byte" in msg, (rc, msg)
    print("NUL inside the epoch value refused: PASS")
    rc, msg = run(["-o", str(d / "pex4.bin"), f"kernel={kernel}",
                   f"--cmdline={approved}", "--exact-cmd=debug=0x10e cpus=1 adreno8=14"])
    assert rc != 0 and "need one epoch token" in msg, msg
    rc, msg = run(["-o", str(d / "pex5.bin"), f"kernel={kernel}",
                   "--cmdline=debug=0x10e cpus=1 epoch=1792813370 epoch=1792813371",
                   f"--exact-cmd={approved}"])
    assert rc != 0 and "duplicate token epoch=1792813371" in msg, msg
    print("epoch token presence and duplication messages unchanged: PASS")

    # --forbid-entry: explicit scope control over the final serialized entries.
    rc, msg = run(["-o", str(d / "pfe0.bin"), f"kernel={kernel}", f"ramdisk={ramdisk}",
                   f"--cmdline={approved.replace('1792813370', '1792900000')}",
                   f"--require=kernel={sha(kernel.read_bytes())}",
                   f"--require=ramdisk={sha(ramdisk.read_bytes())}", f"--exact-cmd={approved}",
                   "--rdfile=18:9220096", "--rdres=0x900000", "--usb", "--checkpoint=14"])
    assert rc == 0, msg  # historical calls keep working; require never forbids rd by default
    print("rdfile/rdres flags stay accepted with require + exact-cmd (no implicit ban): PASS")
    for name in ("rdfile", "rdres", "usb", "checkpoint", "cmdline", "physbase",
                 "ramdisk", "kernel"):
        rc, msg = run(["-o", str(d / f"pfe-{name}.bin"), f"kernel={kernel}", f"ramdisk={ramdisk}",
                       "--cmdline=debug=0x10e", "--rdfile=18:9220096", "--rdres=0x900000",
                       "--usb", "--checkpoint=14", "--physbase=0x80000000", f"--forbid-entry={name}"])
        assert rc != 0 and msg.endswith(f"forbid-entry: the payload would carry {name}"), (name, rc, msg)
    rc, msg = run(["-o", str(d / "pfe-ok.bin"), f"kernel={kernel}", "--cmdline=debug=0x10e",
                   "--forbid-entry=rdfile", "--forbid-entry=rdres", "--forbid-entry=nosuch"])
    assert rc == 0, msg  # absent names (item or flag) never trigger
    print("--forbid-entry rejects each present entry and ignores absent ones: PASS")
    rc, msg = run(["-o", str(d / "pfe-bad.bin"), f"kernel={kernel}",
                   "--forbid-entry=abcdefghijklmnop"])
    assert rc != 0 and "forbid-entry: name" in msg, msg
    print("--forbid-entry validates its own name form: PASS")

    # A forbidden entry refuses without touching an existing output.
    out3 = d / "fe-clobber.bin"
    rc, msg = run(["-o", str(out3), f"kernel={kernel}"])
    assert rc == 0, msg
    before3 = out3.read_bytes()
    rc, msg = run(["-o", str(out3), f"kernel={kernel}", "--rdfile=18:9220096",
                   "--forbid-entry=rdfile"])
    assert rc != 0 and out3.read_bytes() == before3, (rc, msg)
    print("forbid-entry refusal leaves the previous payload untouched: PASS")

    # A failing check never rewrites an existing output.
    out2 = d / "clobber.bin"
    rc, msg = run(["-o", str(out2), f"kernel={kernel}"])
    assert rc == 0, msg
    before = out2.read_bytes()
    rc, msg = run(["-o", str(out2), f"kernel={kernel}", "--require=kernel=" + "1" * 64])
    assert rc != 0 and out2.read_bytes() == before, (rc, msg)
    print("failed run leaves the previous payload untouched: PASS")

print("mkpayload input checks: all PASS")
