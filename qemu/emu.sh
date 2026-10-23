#!/bin/bash
# The system in a phone frame (tools/emu): run-ios.sh under the emulator instead of a bare QEMU window.
# usage: emu.sh [seconds] [extra qemu args...]      (seconds: a time limit; 0, the default, runs until the window is closed)      (env as for run-ios.sh: CMDLINE, QEMU_NET, AUDIO...)
# The mouse with the left button held is a finger; the toolbar has power, volume, home and screenshot.
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
E=out/emu/emu
if [ ! -x $E ] || [ -n "$(find tools/emu -newer $E -type f | head -1)" ]; then
	tools/emu/build.sh >/dev/null || exit 1
fi
SECS=${1:-0}; shift
# /private/var is a disk of its own, HFS+, written as iOS writes like a phone's data volume (run-ios.sh
# QEMU_VAR=hfs: qemu/var-hfs.img, made on first use; that first boot registers every application, later
# ones find everything as it was left). QEMU_VAR=1: the older snapshot (var_store); QEMU_VAR=0: empty each time.
export QEMU_VAR=${QEMU_VAR:-hfs}
[ "$QEMU_VAR" = 1 ] && [ ! -f "${QEMU_VAR_IMG:-qemu/var.img}" ] && export QEMU_VAR_SAVE_AFTER=${QEMU_VAR_SAVE_AFTER:-200}
# CoreAnimation draws with the host's GPU (run-ios.sh QEMU_GPU, boot-arg gl=1) where QEMU has gfxstream;
# QEMU_GPU=0 for its software renderer
if [ -z "$QEMU_GPU" ] && qemu-system-aarch64 -device help 2>/dev/null | grep -q '"virtio-gpu-rutabaga-device"'; then
	# the GPU's frames shown as they are with the QEMU built here (tools/qemu/build.sh: the distribution's
	# puts "Display output is not active." up at every move of the scanout, a flicker); else through ramfb
	[ -x out/qemu/bin/qemu-system-aarch64 ] && export QEMU_GPU=panel || export QEMU_GPU=1
fi
[ -n "$QEMU_GPU" ] && [ "$QEMU_GPU" != 0 ] && [[ " $CMDLINE " != *" gl="* ]] && export CMDLINE="gl=1 $CMDLINE"
exec $E --secs "$SECS" -- "$@"
