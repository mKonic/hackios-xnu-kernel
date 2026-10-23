#!/bin/bash
# Boot avfboot + an XNUP payload on QEMU virt the way crosvm boots it: -kernel Image -initrd payload,
# FDT in x0, PSCI over hvc. The console is the PL011 (crosvm's is an ns16550a at 0x3f8).
#
#   avf/qemu-avf.sh PAYLOAD [seconds] [extra qemu args...]
# env: ACCEL=hvf|kvm|tcg (default: hvf on macOS, kvm if /dev/kvm, else tcg), MEM (default 2G), SMP (default 1)
#
# Payloads: python3 xnuloader/mkpayload.py -o payload.bin kernel=KERNEL [ramdisk=IMAGE] [--cmdline=...]
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
PAYLOAD=${1:?usage: qemu-avf.sh PAYLOAD [seconds] [qemu args...]}
SECS=${2:-60}
shift $(( $# >= 2 ? 2 : 1 ))
IMAGE=$HERE/shim/out/Image
[ -f "$IMAGE" ] || "$HERE/shim/build.sh"
if [ -z "${ACCEL:-}" ]; then
	if [ "$(uname -s)" = Darwin ]; then ACCEL=hvf; elif [ -w /dev/kvm ]; then ACCEL=kvm; else ACCEL=tcg; fi
fi
case "$ACCEL" in
tcg) CPU="-cpu cortex-a76" ;;
*) CPU="-accel $ACCEL -cpu host" ;;
esac
exec timeout "$SECS" qemu-system-aarch64 -M virt,gic-version=3 $CPU -smp "${SMP:-1}" -m "${MEM:-2G}" \
	-kernel "$IMAGE" -initrd "$PAYLOAD" -nographic -serial stdio -monitor none -no-reboot "$@"
