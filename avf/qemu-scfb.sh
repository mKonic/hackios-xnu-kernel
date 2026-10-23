#!/bin/bash
# The AVF layout on QEMU virt, with the Scion shared framebuffer (SCFB) as the only display:
# avfboot + an XNUP payload, SYSTEM (iOS's system.apfs) as a read-only virtio-blk-pci disk (md0=rvblk0),
# virtio-multitouch-pci for touch, and an SCFB region 36 MB below the top of RAM, as the Scion app
# lends it. Read the screen with avf/scfb-dump.py, tap with avf/qemu-tap.py.
#
#   avf/qemu-scfb.sh PAYLOAD SYSTEM [seconds]
# env: MEMG (default 4), EXTRA (more boot-args), HMP (monitor port, 4454), QMP (4455)
# Note: under hvf, an extra virtio-tablet-pci made QEMU abort ("!s1ptw" in hvf.c); keep the device set as is.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PAYLOAD=${1:?usage: qemu-scfb.sh PAYLOAD SYSTEM [seconds]}
SYSTEM=${2:?usage: qemu-scfb.sh PAYLOAD SYSTEM [seconds]}
SECS=${3:-600}
MEMG=${MEMG:-4}
IMAGE=$HERE/shim/out/Image
[ -f "$IMAGE" ] || "$HERE/shim/build.sh"
REGION=$(( 0x40000000 + (MEMG << 30) - (36 << 20) ))
PARAMS="scfb.addr=$(printf 0x%x $REGION) scfb.size=0x2000000 scfb.w=1125 scfb.h=2436 md0=rvblk0 ${EXTRA:-} -v"
echo "scfb region $(printf 0x%x $REGION); boot-args: $PARAMS" >&2
exec timeout "$SECS" qemu-system-aarch64 -M virt,gic-version=3 -accel "${ACCEL:-hvf}" -cpu host -smp 1 -m "${MEMG}G" \
	-kernel "$IMAGE" -initrd "$PAYLOAD" -append "$PARAMS" \
	-drive "file=$SYSTEM,format=raw,if=none,id=sys,readonly=on" -device virtio-blk-pci,drive=sys \
	-device virtio-multitouch-pci \
	-nographic -serial stdio -no-reboot \
	-monitor "tcp:127.0.0.1:${HMP:-4454},server,nowait" -qmp "tcp:127.0.0.1:${QMP:-4455},server,nowait"
