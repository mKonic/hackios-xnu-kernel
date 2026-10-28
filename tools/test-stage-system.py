#!/usr/bin/env python3
"""Drive tools/stage-system.sh against a fake fastboot.

The script honours STAGE_SYSTEM_FASTBOOT (command used for every fastboot
call) and STAGE_SYSTEM_IMAGE (image path), so the fake can answer every
getvar and record every call in order. Every held state, the firmware fault
and the running/hung states must be refused before `oem ramdisk` is sent;
idle and non-held finished states must reach `oem ramdisk` and `stage` in
that order. Plain script: every check prints a PASS line; failures exit
non-zero."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = str(ROOT / "tools/stage-system.sh")

FAKE = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_FASTBOOT_LOG"
case "$*" in
  "devices")            echo "xnuloader         fastboot any" ;;
  "-s xnuloader getvar product") echo "product: xnuloader" ;;
  "-s xnuloader getvar result")  [ -n "$FAKE_FASTBOOT_STATE" ] && echo "result: $FAKE_FASTBOOT_STATE" ;;
  "-s xnuloader oem ramdisk")    echo "OKAY" ;;
  -s\ xnuloader\ stage\ *)       echo "OKAY [  0.001s ]" ;;
  *)                     echo "FAILED (unknown)"; exit 1 ;;
esac
exit 0
'''


def run(state, *, image_ok=True):
    with tempfile.TemporaryDirectory(prefix="stage-system-test-") as td:
        d = Path(td)
        fake = d / "fastboot"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        log = d / "calls.log"
        image = d / "system.apfs"
        if image_ok:
            image.write_bytes(b"S" * 4096)
        env = dict(os.environ,
                   STAGE_SYSTEM_FASTBOOT=str(fake),
                   STAGE_SYSTEM_IMAGE=str(image),
                   FAKE_FASTBOOT_LOG=str(log),
                   FAKE_FASTBOOT_STATE="" if state is None else state)
        p = subprocess.run(["bash", STAGE], capture_output=True, text=True, env=env)
        calls = log.read_text().splitlines() if log.exists() else []
        return p.returncode, p.stdout + p.stderr, calls


rc, out, calls = run("idle")
assert rc == 0, out
assert calls[:4] == ["devices", "-s xnuloader getvar product", "-s xnuloader getvar result",
                     "-s xnuloader oem ramdisk"] and calls[4].startswith("-s xnuloader stage "), calls
assert "Transferred 4096 bytes" in out, out
print("idle state stages in order devices/product/result/oem/stage: PASS")

REFUSE = ("returned-held", "checkpoint-held", "early-exception-held", "selftest-ok-held",
          "hung-held", "no-such-core-held", "firmware-fault", "running", "hung", "no-such-core")
for state in REFUSE:
    rc, out, calls = run(state)
    assert rc == 1, (state, out)
    assert "does not allow replacing its system image: result: " + state in out, (state, out)
    assert calls == ["devices", "-s xnuloader getvar product", "-s xnuloader getvar result"], (state, calls)
print("every held state, firmware-fault, running/hung/no-such-core refused before oem ramdisk: PASS")

for state in ("returned", "checkpoint", "selftest-ok", "early-exception"):
    rc, out, calls = run(state)
    assert rc == 0 and any(c.startswith("-s xnuloader oem ramdisk") for c in calls), (state, rc, out)
print("non-held finished states keep staging (semantics unchanged): PASS")

rc, out, calls = run(None)
assert rc == 1 and "(empty response)" in out and len(calls) == 3, (out, calls)
print("missing result line refused before oem ramdisk: PASS")

rc, out, _ = run("idle", image_ok=False)
assert rc == 1 and "missing or empty image" in out, out
print("missing image refuses before any fastboot call: PASS")

print("stage-system state guard: all PASS")
