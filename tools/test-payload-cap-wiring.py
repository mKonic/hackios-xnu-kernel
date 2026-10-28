#!/usr/bin/env python3
"""PAYLOAD_CAP wiring test (host, no device, no real build).

Proves the annibale wiring end to end through the real entry points on an
isolated fixture tree:
  common/devices/annibale.conf  ->  the mkxnuimg invocation line of
  common/build.sh (extracted verbatim, not a copy)  ->  mkxnuimg's
  environment  ->  BootShim `make` macros (stub make records argv).
Stub make and stub mkbootimg keep the fixture build in Python; the real
mkxnuimg.py runs (a copy of the frozen file). No SKIP flags, no device, no
writes outside the temp fixture; prebuilt/ and Mu-Silicium/ stay untouched.

Run: python3 tools/test-payload-cap-wiring.py
"""
import gzip
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MKXNUIMG = ROOT / 'mkxnuimg.py'
BUILD_SH = ROOT / 'common/build.sh'
DEVICES = ROOT / 'common/devices'
M2_TERM = b'\0' * 24
DTB_STUB = b'DTB!' + b'\x11' * 60
SHIM_STUB = b'SHIM' + b'\x00' * 12  # 16 bytes: shim+FD prefix stays 16-aligned


def die(msg):
    print(f'FAIL {msg}')
    sys.exit(1)


def ok(name):
    print(f'PASS {name}')


def conf_vars(name):
    """Read conf values through bash itself, exactly as load_device would."""
    script = ('set -e; . "$1"; for v in UEFI_TARGET FD_BASE FD_SIZE PAYLOAD_BASE PAYLOAD_CAP; do '
              "printf '%s=%s\\n' \"$v\" \"${!v}\"; done")
    r = subprocess.run(['bash', '-c', script, 'bash', str(DEVICES / f'{name}.conf')],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f'source {name}.conf: {r.stderr[:200]}')
    out = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition('=')
        out[k] = v
    return out


def mkxnuimg_line():
    """The real invocation lines of common/build.sh, joined, `|| die` dropped."""
    lines = BUILD_SH.read_text().splitlines()
    for i, l in enumerate(lines):
        if 'FD_BASE=$FD_BASE' in l and 'python3 mkxnuimg.py' not in l:
            first = l.rstrip().rstrip('\\').rstrip()
            second = lines[i + 1].strip()
            if not second.startswith('python3 mkxnuimg.py'):
                die('build.sh: mkxnuimg env line is not followed by the python call')
            return first, second
    die('build.sh: mkxnuimg invocation line not found')


def container():
    # one 16-byte entry (the loader's data region is 16-aligned) + the terminator
    entry = struct.pack('<16sII', b'x', 16, 0) + b'\xAA' * 16
    body = entry + M2_TERM
    return struct.pack('<II', 0x50554E58, len(body)) + body


MAKE_STUB = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
rec = {'argv': sys.argv[1:], 'env': {k: os.environ.get(k, '<unset>')
                                     for k in ('PAYLOAD_CAP', 'PAYLOAD_BASE')}}
Path(os.environ['CAP_RECORD']).write_text(json.dumps(rec))
Path('BootShim.bin').write_bytes(bytes.fromhex(os.environ['SHIM_HEX']))
'''

MKBOOTIMG_STUB = '''#!/usr/bin/env python3
import sys
a = sys.argv[1:]
out = a[a.index('-o') + 1]
open(out, 'wb').write(open(a[a.index('--kernel') + 1], 'rb').read())
'''


def fixture(base, target, fd_size, strict_no_fd=False):
    fx = Path(base)
    (fx / 'bin').mkdir(parents=True)
    shutil.copy(MKXNUIMG, fx / 'mkxnuimg.py')
    shim_dir = fx / 'Mu-Silicium/BootShim'
    shim_dir.mkdir(parents=True)
    (fx / 'out').mkdir()
    (fx / 'out/usb-payload.bin').write_bytes(container())
    (fx / 'Mu-Silicium/Resources/DTBs').mkdir(parents=True)
    (fx / f'Mu-Silicium/Resources/DTBs/{target}.dtb').write_bytes(DTB_STUB)
    (fx / 'Mu-Silicium/Resources/ramdisk').write_bytes(b'ramdisk')
    (fx / 'Mu-Silicium/Resources/Scripts').mkdir(parents=True)
    (fx / 'Mu-Silicium/Resources/Scripts/mkbootimg.py').write_text(MKBOOTIMG_STUB)
    make = fx / 'bin/make'
    make.write_text(MAKE_STUB)
    make.chmod(0o755)
    if not strict_no_fd:
        fd = fx / f'Mu-Silicium/Build/{target}Pkg/RELEASE_CLANGPDB/FV/SILICIUM_UEFI.fd'
        fd.parent.mkdir(parents=True)
        fd.write_bytes(b'\xEE' * fd_size)
    return fx


def run_device(name):
    """Reproduce build.sh's mkxnuimg call for device `name` on a fresh fixture."""
    vars_ = conf_vars(name)
    fd_size = int(vars_['FD_SIZE'], 0)
    first, second = mkxnuimg_line()
    call = second.split(' || ')[0]
    # build.sh runs without `set -u`: an unset profile variable expands to empty,
    # which is exactly the "unset = legacy" contract mkxnuimg implements
    cmd = (f'set -e; . "{DEVICES}/{name}.conf"; DEVICE={name}; '
           f'export CAP_RECORD="$PWD/make-record.json" SHIM_HEX="{SHIM_STUB.hex()}"; '
           f'export PATH="$PWD/bin:$PATH"; {first} {call}')
    with tempfile.TemporaryDirectory(prefix='capwire-') as td:
        fx = fixture(td, vars_['UEFI_TARGET'] or 'redwood', fd_size)
        r = subprocess.run(['bash', '-c', cmd], cwd=fx, capture_output=True, text=True)
        if r.returncode != 0:
            die(f'{name}: build.sh line failed: {r.stderr[-300:]}')
        rec = json.loads((fx / 'make-record.json').read_text())
        img = (fx / f'out/{name}-boot.img').read_bytes()
    dtb_len = len(DTB_STUB)
    blob = gzip.decompress(img[:len(img) - dtb_len])
    return vars_, rec, blob


def test_annibale():
    vars_, rec, blob = run_device('annibale')
    assert vars_['PAYLOAD_CAP'] == '0x03700000', vars_
    argv = ' '.join(rec['argv'])
    for want in ('REQUIRES_KERNEL_HEADER=1', 'FD_BASE=0xa7100000', 'FD_SIZE=0x300000',
                 '-DPAYLOAD_BASE=0xf0000000', '-DPAYLOAD_CAP=0x3700000'):
        if want not in argv:
            die(f'annibale make macros missing {want}: {argv}')
    if rec['env']['PAYLOAD_CAP'] != '0x03700000':
        die(f"annibale env PAYLOAD_CAP: {rec['env']}")
    cont = container()
    L = (len(cont) + 15) & ~15
    if blob != SHIM_STUB + b'\xEE' * int(vars_['FD_SIZE'], 0) + cont + b'\0' * (L - len(cont)):
        die('annibale image: strict blob (shim+FD+container padded to L) mismatch')
    ok('wiring: annibale.conf PAYLOAD_CAP reaches mkxnuimg env and the make macros')


def test_legacy_redwood():
    vars_, rec, blob = run_device('redwood')
    if 'PAYLOAD_CAP' not in vars_ or vars_['PAYLOAD_CAP'] != '':
        die(f'redwood conf gained a CAP: {vars_}')
    argv = ' '.join(rec['argv'])
    if '-DPAYLOAD_CAP' in argv:
        die(f'redwood make got a CAP macro: {argv}')
    if '-DPAYLOAD_BASE=0xf0000000' not in argv or 'FD_BASE=0x9fd00000' not in argv:
        die(f'redwood make macros wrong: {argv}')
    if rec['env']['PAYLOAD_CAP'] != '':
        die(f"redwood env PAYLOAD_CAP not empty-as-forwarded: {rec['env']}")
    cont = container()
    fd = b'\xEE' * int(vars_['FD_SIZE'], 0)
    pad = (-len(SHIM_STUB + fd + cont)) % 16
    if blob != SHIM_STUB + fd + cont + b'\0' * pad:
        die('redwood image: legacy blob (container appended unvalidated) mismatch')
    ok('wiring: legacy device stays CAP-free in env, make macros, and blob shape')


def test_prebuilt_refused():
    """Strict CAP must not fall back to the 176-byte prebuilt shim."""
    vars_ = conf_vars('annibale')
    first, second = mkxnuimg_line()
    call = second.split(' || ')[0]
    cmd = (f'set -u; . "{DEVICES}/annibale.conf"; DEVICE=annibale; '
           f'export CAP_RECORD="$PWD/make-record.json" SHIM_HEX="{SHIM_STUB.hex()}"; export PATH="$PWD/bin:$PATH"; '
           f'{first} {call} && echo SHOULD-NOT-REACH')
    with tempfile.TemporaryDirectory(prefix='capwire-') as td:
        fixture(td, vars_['UEFI_TARGET'], int(vars_['FD_SIZE'], 0), strict_no_fd=True)
        r = subprocess.run(['bash', '-c', cmd], cwd=td, capture_output=True, text=True)
    if r.returncode == 0 or 'SHOULD-NOT-REACH' in r.stdout or 'predates the strict contract' not in r.stdout + r.stderr:
        die(f'strict prebuilt fallback not refused: rc={r.returncode}')
    ok('wiring: PAYLOAD_CAP refuses the legacy prebuilt shim path')


def test_conf_audit():
    annibale = (DEVICES / 'annibale.conf').read_text().splitlines()
    hits = [l for l in annibale if l.split('#')[0].startswith('PAYLOAD_CAP=')]
    if len(hits) != 1 or hits[0].split('#')[0].strip() != 'PAYLOAD_CAP=0x03700000':
        die(f'annibale.conf CAP line: {hits}')
    for conf in sorted(DEVICES.glob('*.conf')):
        if conf.name == 'annibale.conf':
            continue
        if any(l.split('#')[0].startswith('PAYLOAD_CAP=') for l in conf.read_text().splitlines()):
            die(f'{conf.name} gained a CAP default')
    line = mkxnuimg_line()[0]
    if line.count('PAYLOAD_CAP=$PAYLOAD_CAP') != 1:
        die(f'build.sh env line CAP forwarding: {line}')
    ok('wiring audit: only annibale defines CAP; build.sh forwards it once')


def load_device_env(pre_export, seq):
    """Run the real common/lib.sh load_device for the device sequence `seq`,
    returning PAYLOAD_CAP after each step. `pre_export` is a PAYLOAD_CAP value
    placed in the parent environment first (empty string = none). Uses the live
    common/lib.sh and common/devices/*.conf; sets variables only, no build."""
    lib = ROOT / 'common/lib.sh'
    body = ''.join(f'load_device {d} >/dev/null; echo "STEP${{PAYLOAD_CAP-<unset>}}"; ' for d in seq)
    script = f'. "{lib}"; {body}'
    env = dict(os.environ, HOME=os.environ.get('HOME', '/'))
    env.pop('PAYLOAD_CAP', None)
    if pre_export:
        env['PAYLOAD_CAP'] = pre_export
    r = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        die(f'load_device {seq}: {r.stderr[-200:]}')
    steps = {}
    for line in r.stdout.splitlines():
        if line.startswith('STEP'):
            steps[len(steps)] = line[4:]
    return steps


def test_load_device_inherit():
    # inherited annibale CAP, then pick a legacy device: stays legacy
    s = load_device_env('0x03700000', ['redwood'])
    if s[0] != '<unset>':
        die(f'inherited CAP not cleared for redwood: {s[0]!r}')
    ok('load_device: a CAP inherited in the environment clears when redwood is selected')
    # same shell: annibale then redwood -> the annibale CAP does not leak
    s = load_device_env('', ['annibale', 'redwood'])
    if s[0] != '0x03700000' or s[1] != '<unset>':
        die(f'annibale->redwood same shell: {s}')
    ok('load_device: annibale then redwood in one shell clears the CAP')
    # redwood then annibale -> annibale's own CAP applies
    s = load_device_env('', ['redwood', 'annibale'])
    if s[1] != '0x03700000':
        die(f'redwood->annibale lost the CAP: {s[1]!r}')
    ok('load_device: redwood then annibale selects the 0x03700000 CAP')
    # a CAP inherited from the parent, then annibale -> the profile CAP applies
    s = load_device_env('0x09990000', ['annibale'])
    if s[0] != '0x03700000':
        die(f'inherited CAP overrode the annibale profile: {s[0]!r}')
    ok('load_device: the device profile CAP overrides an inherited CAP value')
    # only PAYLOAD_CAP is cleared: an unrelated parent variable survives
    script = (f'. "{ROOT}/common/lib.sh"; load_device redwood >/dev/null; '
              'printf "%s|%s" "${MYSENT-<gone>}" "${FD_BASE-<gone>}"')
    env = dict(os.environ, MYSENT='keep-me')
    env.pop('PAYLOAD_CAP', None)
    r = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0 or r.stdout != 'keep-me|0x9FD00000':
        die(f'load_device touched other variables: rc={r.returncode} out={r.stdout!r}')
    ok('load_device: only PAYLOAD_CAP is cleared, other environment variables survive')


def main():
    if sys.version_info < (3, 8):
        die('python3 >= 3.8')
    test_conf_audit()
    test_annibale()
    test_legacy_redwood()
    test_prebuilt_refused()
    test_load_device_inherit()
    print('ALL CAP WIRING CHECKS PASSED')


if __name__ == '__main__':
    main()
