#!/usr/bin/env bash
# Boot the whole system under QEMU: the big system image preloaded at the loader's resident
# address (it becomes /dev/md0, the root) and the small image in the payload (/dev/md1), the
# same arrangement `xl ramdisk` + `xl run` make on the phone.
# usage: run-ios.sh [seconds] [extra qemu args...]      (seconds 0: no time limit)
#
# Counting anything in the log this writes: every console line comes out TWICE, the second copy in a
# block replayed some thousands of lines later (take any launchd line and grep -n it). So `grep -c` gives
# about double, and `sort -u` does not fix it either, because other processes' output interleaves and
# splits one of the two copies. Count distinct events instead -- by timestamp, or by program and pid for
# "Successfully spawned". The phone's logs do not do this; `xl log` fetches the buffer once.
#
# env: ROOT=<small image>, IMG=<system image>, CMDLINE=<extra boot-args>
#      QEMU_RAM_GB=<n>     the guest's RAM, default 7: memory then ends where the phone's does and the
#                          kernel gets the same 4.2 GB window (the system image included) as on the phone.
#                          The host needs about 2.5 GB per running instance.
#      QEMU_NET=1          with a network: a virtio network device, behind it QEMU's own "user" network
#                          (it hands out an address by DHCP, answers DNS and passes connections on to
#                          this machine's network; no privileges needed). The kernel makes en0 of it
#                          (boot-arg virtio_net=1, added here); see userland/src/net_test.m for the rest.
#      AUDIO=<where>       what plays the sound of iOS: a QEMU audio back end (pipewire, pa, alsa, sdl: each
#                          needs its QEMU module, e.g. the package qemu-audio-pipewire) or FILE.wav to
#                          write it into a file instead. Default: nowhere (the guest plays all the same).
#      QEMU_INSTANCE=<n>   to run several at once: each number has its own monitor and QMP ports
#                          (4444 + 10n, 4445 + 10n); the file qemu/instance sets it for a working copy.
#                          touch.py, screendump.sh and stop.sh follow the same rule.
# The machine has a display (ramfb; without one SpringBoard does not start): the loader sets a mode of the
# size of the phone's screen. qemu/screendump.sh saves a picture of it (through the monitor on port 4444).
# Add "-display gtk" to see it in a window and use it with the mouse: the pointer with the left button
# held is a finger (the keys L, H, Up and Down are the lock, home and volume buttons).
# Touches can also be sent from a script: qemu/touch.py (through the QMP socket on port 4445).
#
# The system image is not read into QEMU: guest RAM is a private mapping of qemu/ram.img, a sparse
# file that holds the image at the right offset (reflinked, so it costs neither disk space nor
# host RAM).
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
K=xnu-src/xnu-8796.121.2/BUILD/obj/DEVELOPMENT_ARM64_BCM2837/mach.development.bcm2837
[ -f $K ] || { K=prebuilt/mach.development.bcm2837; echo "run-ios.sh: no kernel built here, using $K" >&2; }
IMG=${IMG:-userland/system.apfs}
ROOT=${ROOT:-userland/root.apfs}
[ -f "$IMG" ] && [ -f "$ROOT" ] || { echo "run-ios.sh: $IMG or $ROOT is missing (userland/build.sh makes them)"; exit 1; }
SECS=${1:-60}; shift
RAM_BASE=0x40000000 RD_ADDR=0xFC000000
RAM_GB=${QEMU_RAM_GB:-7}
RAM_SIZE=$((RAM_GB << 30)) FILE_SIZE=$((12 << 30))
INSTANCE=${QEMU_INSTANCE:-$(cat qemu/instance 2>/dev/null || echo 0)}
# an instance takes 2.5-3 GB of host memory; starting one that does not fit gets something killed
AVAILABLE=$(awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo)
if [ "${AVAILABLE:-9999}" -lt 3000 ] && [ -z "$QEMU_FORCE" ]; then
	echo "run-ios.sh: only $AVAILABLE MB of memory available on this machine: stop another QEMU first (QEMU_FORCE=1 to start anyway)"
	exit 1
fi

if [ ! -f qemu/ram.img ] || [ "$IMG" -nt qemu/ram.img ]; then
	python3 - "$IMG" qemu/ram.img $((RD_ADDR - RAM_BASE)) $FILE_SIZE <<'PY' || exit 1
import os, sys
src = os.open(sys.argv[1], os.O_RDONLY)
dst = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
offset, size = int(sys.argv[3]), int(sys.argv[4])
os.ftruncate(dst, size)
total, done = os.fstat(src).st_size, 0
while done < total:
    done += os.copy_file_range(src, dst, total - done, done, offset + done)
PY
fi
NET=()
if [ -n "$QEMU_NET" ] && [ "$QEMU_NET" != 0 ]; then
	NET=(-netdev user,id=net0 -device virtio-net-device,netdev=net0)
	CMDLINE="virtio_net=1 $CMDLINE"
fi
# the sound device (QEMU 8.2 or newer has one for this machine): the kernel's sink for /dev/redwood-audio
SOUND=()
if qemu-system-aarch64 -device help 2>/dev/null | grep -q '"virtio-sound-device"'; then
	case "${AUDIO:-none}" in
	*.wav) SOUND=(-audiodev "wav,id=snd,path=$(readlink -f "$AUDIO")") ;;
	*) SOUND=(-audiodev "${AUDIO:-none},id=snd") ;;
	esac
	SOUND+=(-device virtio-sound-device,audiodev=snd,streams=1)   # one stream: output, no microphone of the host
	CMDLINE="virtio_sound=1 $CMDLINE"
fi
# QEMU_VAR=1: /private/var kept between boots (userland/src/var_store.c) on a disk of its own, a sparse
# file (qemu/var.img, or QEMU_VAR_IMG) at the phone's store offsets. QEMU_VAR_SAVE_AFTER=N saves it N
# seconds in: boot once with it, and later boots skip the first-boot work (LaunchServices' registration
# of every application). `rm qemu/var.img` starts from an empty volume again.
VAR=()
# QEMU_VAR=hfs: /private/var is an HFS+ volume of its own (qemu/var-hfs.img, or QEMU_VAR_IMG), written as iOS
# writes, as a phone's data volume is: nothing to save and nothing to load. Made on first use.
if [ "$QEMU_VAR" = hfs ]; then
	VAR_IMG=${QEMU_VAR_IMG:-qemu/var-hfs.img}
	if [ ! -f "$VAR_IMG" ]; then
		MKFS=$(tools/mk-hfsprogs.sh) || { echo "run-ios.sh: no mkfs.hfsplus (tools/mk-hfsprogs.sh)" >&2; exit 1; }
		truncate -s 16G "$VAR_IMG" && "$MKFS" -J -v Data "$VAR_IMG" >/dev/null || exit 1
	fi
	VAR_IMG=$(readlink -f "$VAR_IMG")
	VAR=(-drive "file=$VAR_IMG,format=raw,if=none,id=var" -device virtio-blk-device,drive=var)
	CMDLINE="var_hfs=rvblk0 virtio_blk=1 virtio_blk_rw=1 $CMDLINE"
elif [ -n "$QEMU_VAR" ] && [ "$QEMU_VAR" != 0 ]; then
	VAR_IMG=${QEMU_VAR_IMG:-qemu/var.img}
	[ -f "$VAR_IMG" ] || truncate -s 12G "$VAR_IMG"     # the store sits 8 GiB in; two 2 GiB slots
	VAR_IMG=$(readlink -f "$VAR_IMG")                   # (run.sh starts QEMU from qemu/)
	VAR=(-drive "file=$VAR_IMG,format=raw,if=none,id=var" -device virtio-blk-device,drive=var)
	CMDLINE="var_store=1 virtio_blk=1 virtio_blk_rw=1 ${QEMU_VAR_SAVE_AFTER:+var_save_after=$QEMU_VAR_SAVE_AFTER }$CMDLINE"
fi
# QEMU_GPU=1: beside ramfb a virtio GPU with gfxstream behind it (QEMU's rutabaga device), for /dev/vgpu3d:
# OpenGL ES (and CoreAnimation with boot-arg gl=1) on this machine's GPU. The panel stays ramfb: as the
# scanout, the rutabaga device's frames come back from gfxstream with parts missing once a guest GL
# context is up (gfxstream 0.1.2). ramfb is created first, so it is console 0, the one the skin shows.
# NVIDIA's EGL opens no display while WAYLAND_DISPLAY names the session's compositor (and Mesa's software
# renderer answers instead), so QEMU runs without it: show it with qemu/emu.sh, not -display gtk.
GPU=(-device ramfb)
if [ -n "$QEMU_GPU" ] && [ "$QEMU_GPU" != 0 ]; then
	GPU+=(-device virtio-gpu-rutabaga-device,x-gfxstream-gles=on,hostmem=256M)
	# QEMU_GPU=panel: no ramfb, the GPU's scanout is the panel: a frame CoreAnimation drew on the GPU is
	# shown as the colour buffer it is (patch 1303), no copy in the guest. Needs the QEMU of tools/qemu/build.sh:
	# the distribution's rutabaga puts its "Display output is not active." placeholder up at every move of
	# the scanout between CoreAnimation's two buffers, every frame
	[ "$QEMU_GPU" = panel ] && GPU=(-device virtio-gpu-rutabaga-device,x-gfxstream-gles=on,hostmem=256M,xres=1125,yres=2436)
	# QEMU_GPU_EGL=mesa: Mesa's EGL (llvmpipe) instead, which needs the session's display: for comparing
	if [ "$QEMU_GPU_EGL" = mesa ]; then
		export __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/50_mesa.json
	else
		unset WAYLAND_DISPLAY DISPLAY
	fi
fi
# EMU_QMP=<socket>: shown by the emulator (qemu/emu.sh), which attaches as QEMU's D-Bus display over QMP
EMU=()
[ -n "$EMU_QMP" ] && EMU=(-display dbus,p2p=yes -qmp "unix:$EMU_QMP,server=on,wait=off")
# launchd's own log off the console (kernel patch 1204): two thirds of a boot's console, each character a trapped
# write to the UART here. QEMU_LAUNCHD_LOG=1 keeps it.
[ "$QEMU_LAUNCHD_LOG" = 1 ] || CMDLINE="launchd_console=0 $CMDLINE"
# the profile's four cores (common/devices/qemu-virt.conf); QEMU_SMP=1 for one
export QEMU_SMP=${QEMU_SMP:-4}
[[ " $CMDLINE " == *" cpus="* ]] || CMDLINE="cpus=$QEMU_SMP $CMDLINE"
# the kernel hands back to the loader after SECS and QEMU is stopped 40 s later; 0: neither
LIMIT=; LIMIT_QEMU=0
[ "$SECS" != 0 ] && LIMIT="loader_return_after=$SECS" LIMIT_QEMU=$((SECS + 40))
xnuloader/mkpayload.py -o qemu/payload.bin kernel=$K ramdisk=$ROOT --rdres "$(printf '%x' "$(stat -c %s "$IMG")")" \
	--cmdline "$LIMIT epoch=$(date +%s) virtio_input=1 $CMDLINE" >/dev/null || exit 1
QEMU_MEM=${RAM_GB}G exec qemu/run.sh $LIMIT_QEMU \
	-object memory-backend-file,id=ram,size=$RAM_SIZE,mem-path="$PWD/qemu/ram.img",share=off \
	-machine memory-backend=ram \
	-global virtio-mmio.force-legacy=false -device virtio-tablet-device -device virtio-keyboard-device "${NET[@]}" "${SOUND[@]}" "${VAR[@]}" \
	"${GPU[@]}" -monitor tcp:127.0.0.1:$((4444 + 10 * INSTANCE)),server,nowait \
	-qmp tcp:127.0.0.1:$((4445 + 10 * INSTANCE)),server,nowait "${EMU[@]}" "$@"
