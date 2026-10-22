#!/bin/bash
# The AVF layout on QEMU virt, with the Scion shared framebuffer (SCFB) as the only display:
# avfboot + an XNUP payload, SYSTEM (iOS's system.apfs) as a read-only virtio-blk-pci disk (md0=rvblk0),
# virtio-multitouch-pci for touch, and an SCFB region 36 MB below the top of RAM, as the Scion app
# lends it. Read the screen with avf/scfb-dump.py, tap with avf/qemu-tap.py.
#
#   avf/qemu-scfb.sh PAYLOAD SYSTEM [seconds]
# env: MEMG (default 4), EXTRA (more boot-args), HMP (monitor port, 4454), QMP (4455)
#      CAM=1: and the camera region (SCCAM, avf/CAMERA.md) 16 MB below the framebuffer's, with guest RAM a
#      shared file (RAMFILE, default a temporary one) that avf/sccam-host.py serves a test pattern into
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
RAM=(-m "${MEMG}G")
if [ "${CAM:-0}" = 1 ]; then
	CAMREGION=$(( REGION - (16 << 20) ))
	RAMFILE=${RAMFILE:-$(mktemp -t sccam-ram)}
	rm -f "$RAMFILE"; truncate -s "${MEMG}G" "$RAMFILE"
	python3 "$HERE/sccam-host.py" "$RAMFILE" 0x40000000 $CAMREGION 0x1000000 --prime-only
	python3 "$HERE/sccam-host.py" "$RAMFILE" 0x40000000 $CAMREGION 0x1000000 --serve-only --secs "$SECS" >&2 &
	HOSTPID=$!
	trap 'kill $HOSTPID 2>/dev/null; rm -f "$RAMFILE"' EXIT
	PARAMS="sccam.addr=$(printf 0x%x $CAMREGION) sccam.size=0x1000000 $PARAMS"
	RAM=(-m "${MEMG}G" -object "memory-backend-file,id=ram,size=${MEMG}G,mem-path=$RAMFILE,share=on" -machine memory-backend=ram)
fi
echo "scfb region $(printf 0x%x $REGION); boot-args: $PARAMS" >&2
# (not exec: with CAM=1 the test pattern's server goes when QEMU does)
timeout "$SECS" qemu-system-aarch64 -M virt,gic-version=3 -accel "${ACCEL:-hvf}" -cpu "${CPU:-host}" -smp 1 "${RAM[@]}" \
	-kernel "$IMAGE" -initrd "$PAYLOAD" -append "$PARAMS" \
	-drive "file=$SYSTEM,format=raw,if=none,id=sys,readonly=on" -device virtio-blk-pci,drive=sys \
	-device virtio-multitouch-pci \
	-nographic -serial stdio -no-reboot \
	-monitor "tcp:127.0.0.1:${HMP:-4454},server,nowait" -qmp "tcp:127.0.0.1:${QMP:-4455},server,nowait"
