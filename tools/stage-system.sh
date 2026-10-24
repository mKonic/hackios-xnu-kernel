#!/usr/bin/env bash
# Transfer system.apfs into the resident loader's RAM before staging a kernel payload.
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$#" -eq 0 ] || { echo "usage: tools/stage-system.sh" >&2; exit 2; }
exec python3 - <<'PYTHON'
import pathlib
import subprocess
import sys

image = pathlib.Path('userland/system.apfs')
serial = 'xnuloader'

def fastboot(*args, seconds=10):
    result = subprocess.run(['fastboot', '-s', serial, *args],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=seconds)
    print(result.stdout, end='', flush=True)
    if result.returncode or 'FAILED' in result.stdout:
        raise RuntimeError('fastboot command failed')
    return result.stdout

try:
    if not image.is_file() or image.stat().st_size == 0:
        raise RuntimeError(f'missing or empty image: {image}')
    devices = subprocess.run(['fastboot', 'devices'], check=True,
                             capture_output=True, text=True, timeout=5).stdout
    if not any(line.split()[:1] == [serial] for line in devices.splitlines()):
        raise RuntimeError('xnuloader is not connected; boot the resident loader first')
    if 'product: xnuloader' not in fastboot('getvar', 'product'):
        raise RuntimeError('unexpected fastboot product')
    state = fastboot('getvar', 'result')
    if 'result:' not in state or any(word in state for word in ('running', 'hung', 'no-such-core')):
        raise RuntimeError('kernel state does not allow replacing its system image')
    fastboot('oem', 'ramdisk')
    fastboot('stage', str(image), seconds=180)
    print(f'Transferred {image.stat().st_size} bytes into loader RAM.')
except (OSError, subprocess.SubprocessError, RuntimeError) as error:
    print(f'stage-system: {error}', file=sys.stderr)
    sys.exit(1)
PYTHON
