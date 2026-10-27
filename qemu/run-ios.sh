#!/bin/bash
# Boot the whole system under QEMU: the big system image preloaded at the loader's resident
# address (it becomes /dev/md0, the root) and the small image in the payload (/dev/md1), the
# same arrangement `xl ramdisk` + `xl run` make on the phone.
# usage: run-ios.sh [seconds] [extra qemu args...]
#
# Counting anything in the log this writes: every console line comes out TWICE, the second copy in a
# block replayed some thousands of lines later (take any launchd line and grep -n it). So `grep -c` gives
# about double, and `sort -u` does not fix it either, because other processes' output interleaves and
# splits one of the two copies. Count distinct events instead -- by timestamp, or by program and pid for
# "Successfully spawned". The phone's logs do not do this; `xl log` fetches the buffer once.
#
# env: ROOT=<small image>, IMG=<system image>, CMDLINE=<extra boot-args>, KERNEL=<kernel to boot>
#      MEMSIZE=<hex>       the kernel's memory window, if not the loader's 6 GB (xnuloader/mkpayload.py --memsize)
#      IDENTITY=v57        the iPhone the device tree names, if not the iPhone X (xnuloader/mkpayload.py --identity)
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
K=${KERNEL:-xnu-src/xnu-8796.121.2/BUILD/obj/DEVELOPMENT_ARM64_BCM2837/mach.development.bcm2837}
[ -f $K ] || { K=prebuilt/mach.development.bcm2837; echo "run-ios.sh: no kernel built here, using $K" >&2; }
IMG=${IMG:-userland/system.apfs}
ROOT=${ROOT:-userland/root.apfs}
[ -f "$IMG" ] && [ -f "$ROOT" ] || { echo "run-ios.sh: $IMG or $ROOT is missing (userland/build.sh makes them)"; exit 1; }
SECS=${1:-60}; shift
RAM_BASE=0x40000000 RD_ADDR=0xFC000000
RAM_GB=${QEMU_RAM_GB:-7}
RAM_SIZE=$((RAM_GB << 30)) FILE_SIZE=$(((RAM_GB > 12 ? RAM_GB : 12) << 30))
INSTANCE=${QEMU_INSTANCE:-$(cat qemu/instance 2>/dev/null || echo 0)}
# an instance takes 2.5-3 GB of host memory; starting one that does not fit gets something killed
AVAILABLE=$(awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo)
if [ "${AVAILABLE:-9999}" -lt 3000 ] && [ -z "$QEMU_FORCE" ]; then
	echo "run-ios.sh: only $AVAILABLE MB of memory available on this machine: stop another QEMU first (QEMU_FORCE=1 to start anyway)"
	exit 1
fi

# Record the input image too: an older image may be selected for the next run.
python3 - "$IMG" qemu/ram.img $((RD_ADDR - RAM_BASE)) $FILE_SIZE <<'PY' || exit 1
import json, os, pathlib, sys
source = pathlib.Path(sys.argv[1]).resolve()
metadata = pathlib.Path(sys.argv[2] + '.source')
st = source.stat()
identity = {'source': str(source), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns}
offset, size = int(sys.argv[3]), int(sys.argv[4])
try:
    cached = json.loads(metadata.read_text())
except (OSError, ValueError):
    cached = None
target = pathlib.Path(sys.argv[2])
if target.is_file() and cached == identity and target.stat().st_size >= offset + st.st_size:
    sys.exit(0)
metadata.unlink(missing_ok=True)
src = os.open(source, os.O_RDONLY)
dst = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
os.ftruncate(dst, size)
total, done = os.fstat(src).st_size, 0
cfr = getattr(os, "copy_file_range", None)        # Linux only; macOS falls to the read/write loop
if cfr:
    while done < total:
        copied = cfr(src, dst, total - done, done, offset + done)
        if copied == 0:
            raise RuntimeError("system image copy made no progress")
        done += copied
else:
    os.lseek(src, 0, os.SEEK_SET); os.lseek(dst, offset, os.SEEK_SET)
    while done < total:
        data = os.read(src, min(1 << 22, total - done))
        if not data:
            raise RuntimeError("system image ended before its recorded size")
        view = memoryview(data)
        while view:
            written = os.write(dst, view)
            if written == 0:
                raise RuntimeError("system image write made no progress")
            done += written
            view = view[written:]
os.close(src)
os.close(dst)
metadata.write_text(json.dumps(identity) + '\n')
PY
# a guest bigger than the file: the file grows (sparse), the image in it stays where it is
python3 -c 'import os, sys; os.path.getsize(sys.argv[1]) >= int(sys.argv[2]) or os.truncate(sys.argv[1], int(sys.argv[2]))' \
	qemu/ram.img $FILE_SIZE || exit 1
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
xnuloader/mkpayload.py -o qemu/payload.bin kernel=$K ramdisk=$ROOT --rdres "$(printf '%x' "$(stat -c %s "$IMG")")" ${MEMSIZE:+--memsize $MEMSIZE} ${IDENTITY:+--identity $IDENTITY} \
	--cmdline "loader_return_after=$SECS epoch=$(date +%s) virtio_input=1 $CMDLINE" >/dev/null || exit 1
# The loader returns up to 1.5 MiB of diagnostics over the emulated serial port.
QEMU_MEM=${RAM_GB}G exec qemu/run.sh $((SECS + 180)) \
	-object memory-backend-file,id=ram,size=$RAM_SIZE,mem-path="$PWD/qemu/ram.img",share=off \
	-machine memory-backend=ram \
	-global virtio-mmio.force-legacy=false -device virtio-tablet-device -device virtio-keyboard-device "${NET[@]}" "${SOUND[@]}" \
	-device ramfb -monitor tcp:127.0.0.1:$((4444 + 10 * INSTANCE)),server,nowait \
	-qmp tcp:127.0.0.1:$((4445 + 10 * INSTANCE)),server,nowait "$@"
