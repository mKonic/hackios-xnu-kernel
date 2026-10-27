#!/usr/bin/env bash
# Transfer system.apfs into the resident loader's RAM before staging a kernel payload.
# Images over 0xffffffff bytes require protocol 1 and STAGE_SYSTEM_WINDOW (hex bytes).
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$#" -eq 0 ] || { echo "usage: tools/stage-system.sh" >&2; exit 2; }
exec python3 - <<'PYTHON'
import os
import pathlib
import re
import subprocess
import sys
import tempfile

image = pathlib.Path(os.environ.get('STAGE_SYSTEM_IMAGE', 'userland/system.apfs'))
serial = 'xnuloader'
fastboot_cmd = os.environ.get('STAGE_SYSTEM_FASTBOOT', 'fastboot')
MAX_DOWNLOAD = 0xffffffff
BLOCK_BYTES = 256 * 1024 * 1024  # Bound the temporary file and fastboot's host-side buffer.

def fastboot(*args, seconds=10):
    result = subprocess.run([fastboot_cmd, '-s', serial, *args],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=seconds)
    print(result.stdout, end='', flush=True)
    if result.returncode or 'FAILED' in result.stdout:
        raise RuntimeError('fastboot command failed')
    return result.stdout

def variable(name):
    output = fastboot('getvar', name)
    match = re.search(r'(?:^|\n)(?:\(bootloader\)[ \t]*)?' + re.escape(name) + r':[ \t]*([^\r\n]*)', output)
    if not match:
        raise RuntimeError(f'loader did not report {name}')
    return match.group(1).strip()

def check_progress(state, received, total, window):
    fields = variable('ramdisk-state').split()
    if (len(fields) != 4 or fields[0] != state or
            [int(v, 16) for v in fields[1:]] != [received, total, window]):
        raise RuntimeError(f'unexpected ramdisk progress: {" ".join(fields)}')

def transfer_large(total, window):
    if variable('ramdisk-protocol') != '1':
        raise RuntimeError('loader lacks RAM upload protocol 1')
    fastboot('oem', 'ramdisk', 'begin', f'{total:x}', f'{window:x}')
    check_progress('receiving', 0, total, window)
    with image.open('rb') as source, tempfile.TemporaryDirectory(prefix='hackios-ramdisk-') as temporary:
        before = os.fstat(source.fileno())
        if before.st_size != total:
            raise RuntimeError('system image changed before upload')
        block = pathlib.Path(temporary) / 'block.bin'
        received = 0
        while received < total:
            length = min(BLOCK_BYTES, total - received)
            with block.open('wb') as destination:
                left = length
                while left:
                    data = source.read(min(left, 1024 * 1024))
                    if not data:
                        raise RuntimeError('system image ended during upload')
                    destination.write(data)
                    left -= len(data)
            # A failed or ambiguous block is never retried in the same session.
            fastboot('stage', str(block), seconds=180)
            received += length
            check_progress('ready' if received == total else 'receiving', received, total, window)
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            fastboot('oem', 'ramdisk', 'none')
            raise RuntimeError('system image changed during upload; resident image cancelled')

try:
    if not image.is_file() or image.stat().st_size == 0:
        raise RuntimeError(f'missing or empty image: {image}')
    total = image.stat().st_size
    window = 0
    if total > MAX_DOWNLOAD:
        value = os.environ.get('STAGE_SYSTEM_WINDOW', '')
        if not re.fullmatch(r'(?:0x)?[0-9a-fA-F]{1,16}', value):
            raise RuntimeError('large image requires STAGE_SYSTEM_WINDOW as hexadecimal bytes')
        window = int(value, 16)
        if window <= total:
            raise RuntimeError('STAGE_SYSTEM_WINDOW must exceed the system image size')
    devices = subprocess.run([fastboot_cmd, 'devices'], check=True,
                             capture_output=True, text=True, timeout=5).stdout
    if not any(line.split()[:1] == [serial] for line in devices.splitlines()):
        raise RuntimeError('xnuloader is not connected; boot the resident loader first')
    if 'product: xnuloader' not in fastboot('getvar', 'product'):
        raise RuntimeError('unexpected fastboot product')
    state = fastboot('getvar', 'result')
    # Any held state (kernel or loader keeps the cores and resources) and a
    # firmware fault refuse staging; the loader would only answer a generic
    # error later. Words match the set_result strings in xnuloader/loader.c.
    if ('result:' not in state
            or any(word in state for word in ('running', 'hung', 'no-such-core', '-held', 'firmware-fault'))):
        first = state.strip().splitlines()
        raise RuntimeError('kernel state does not allow replacing its system image: '
                           + (first[0] if first else '(empty response)'))
    if total > MAX_DOWNLOAD:
        transfer_large(total, window)
    else:
        fastboot('oem', 'ramdisk')
        fastboot('stage', str(image), seconds=180)
    print(f'Transferred {total} bytes into loader RAM.')
except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
    print(f'stage-system: {error}', file=sys.stderr)
    sys.exit(1)
PYTHON
