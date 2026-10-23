#!/usr/bin/env bash
# iOS 16.5 under AVF as a Scion install: puts the boot files into Scion's files/ios165 (owned by
# the app, via run-as), registers that folder as an install ("iOS 16.5 (AVF)", backend AVF,
# security Bring-up), boots it once through the debug build's developer hook and collects its
# console. system.apfs (and root.apfs) must already be in files/ios165 (built by
# hackios-xnu-userland's userland/build.sh; Apple's files, never in a repository).
#
#   FILES=DIR avf/scion/install-ios165.sh [--secs N] [--no-register] [--no-boot]
#
# DIR holds Image (avf/shim/build.sh) and payload.bin:
#   python3 xnuloader/mkpayload.py -o DIR/payload.bin kernel=mach.development.bcm2837 ramdisk=root.apfs
# The layout (avf.properties): the payload's RAM disk (root.apfs) is md1, system.apfs a read-only
# virtio disk the kernel calls md0 (boot-arg md0=rvblk0, patch 1101), the root.
#
# Rules kept: takes /data/local/tmp/pixel.lock (waits for it), never touches files/vm or install
# 1's entry, never force-stops the app while a VM of it runs, restores the hidden-API exemption
# list and releases the lock on exit.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
FILES=${FILES:-$HERE/out}   # Image, payload.bin (see the header); avf.properties is next to this script
SERIAL=57281FDCR008PJ
PKG=io.github.dylanneve1.iosonpixel
DIR=/data/user/0/$PKG/files/ios165
LOGS=/sdcard/Android/data/$PKG/files/logs
SECS=600
REGISTER=1
BOOT=1
while [[ $# -gt 0 ]]; do
	case "$1" in
	--secs) SECS=$2; shift 2 ;;
	--no-register) REGISTER=0; shift ;;
	--no-boot) BOOT=0; shift ;;
	*) echo "unknown option $1" >&2; exit 2 ;;
	esac
done
ADB=(adb -s "$SERIAL")
sh_() { "${ADB[@]}" shell "$@"; }
OUT=${OUT:-avf-ios165-$(date +%Y%m%d-%H%M%S)}
mkdir -p "$OUT"
LOCK=/data/local/tmp/pixel.lock
EXEMPT='Landroid/system/virtualmachine/VirtualMachineCustomImageConfig,Landroid/system/virtualmachine/VirtualMachineConfig$Builder;->setCustomImageConfig('

echo "== waiting for the phone and $LOCK"
"${ADB[@]}" wait-for-device
until sh_ mkdir "$LOCK" 2>/dev/null; do sleep 10; done
prev=$(sh_ settings get global hidden_api_blacklist_exemptions | tr -d '\r')
cleanup() {
	if [[ "$prev" == "null" || -z "$prev" ]]; then
		sh_ settings delete global hidden_api_blacklist_exemptions >/dev/null || true
	else
		sh_ settings put global hidden_api_blacklist_exemptions "'$prev'" || true
	fi
	echo "== exemptions back to: $(sh_ settings get global hidden_api_blacklist_exemptions | tr -d '\r')"
	sh_ rmdir "$LOCK" && echo "== lock released"
}
trap cleanup EXIT

runas() { sh_ "run-as $PKG sh -c '$1'"; }

echo "== files into $DIR"
runas "ls -l files/ios165" | tee "$OUT/before.txt"
runas "test -f files/ios165/system.apfs" || { echo "no system.apfs in $DIR" >&2; exit 1; }
for f in Image payload.bin avf.properties; do
	src=$FILES/$f; [ "$f" = avf.properties ] && src=$HERE/$f
	"${ADB[@]}" push "$src" "/data/local/tmp/ios165-$f" >/dev/null
	sh_ "cat /data/local/tmp/ios165-$f | run-as $PKG sh -c 'cat > files/ios165/$f.new && mv files/ios165/$f.new files/ios165/$f && chmod 600 files/ios165/$f'"
	sh_ rm -f "/data/local/tmp/ios165-$f"
	local_md5=$( (md5 -q "$src" 2>/dev/null || md5sum "$src") | cut -d" " -f1)
	phone_md5=$(runas "md5sum files/ios165/$f" | awk '{print $1}')
	echo "   $f: $phone_md5 $([[ $local_md5 == "$phone_md5" ]] && echo ok || echo "MISMATCH (local $local_md5)")"
	[[ $local_md5 == "$phone_md5" ]]
done
runas "ls -l files/ios165" | tee "$OUT/after.txt"

app_running_vm() {
	sh_ dumpsys activity services "$PKG" 2>/dev/null | grep -q "ServiceRecord" && return 0
	return 1
}

if [[ $REGISTER == 1 ]]; then
	echo "== registering install 2"
	if app_running_vm; then
		echo "   the app has a running service (a VM?): not stopping it; register later with --no-boot" >&2
	else
		# the app keeps its preferences in memory: stop it, or it writes its old list back
		sh_ am force-stop "$PKG"
		"${ADB[@]}" exec-out run-as "$PKG" cat shared_prefs/installs.xml > "$OUT/installs.xml.orig"
		python3 "$HERE/register-install.py" "$OUT/installs.xml.orig" "$OUT/installs.xml" "$DIR"
		if ! cmp -s "$OUT/installs.xml.orig" "$OUT/installs.xml"; then
			"${ADB[@]}" push "$OUT/installs.xml" /data/local/tmp/ios165-installs.xml >/dev/null
			sh_ "cat /data/local/tmp/ios165-installs.xml | run-as $PKG sh -c 'cat > shared_prefs/installs.xml.new && mv shared_prefs/installs.xml.new shared_prefs/installs.xml'"
			sh_ rm -f /data/local/tmp/ios165-installs.xml
			"${ADB[@]}" exec-out run-as "$PKG" cat shared_prefs/installs.xml | cmp - "$OUT/installs.xml" && echo "   written"
		fi
	fi
fi

if [[ $BOOT == 1 ]]; then
	echo "== booting (developer session, ${SECS} s of console)"
	if app_running_vm; then
		echo "   the app is running a VM (install 1?): not stopping it to boot this one" >&2
		exit 1
	fi
	sh_ settings put global hidden_api_blacklist_exemptions "'$EXEMPT'"
	sh_ am force-stop "$PKG"
	sh_ rm -f "$LOGS/dev/avf-console.log"
	"${ADB[@]}" logcat -c || true
	sh_ am start -n "$PKG/.MainActivity" --es backend avf --es vm_dir "$DIR" --ez start true >/dev/null
	end=$(($(date +%s) + SECS))
	while (($(date +%s) < end)); do
		sleep 15
		"${ADB[@]}" pull "$LOGS/dev/avf-console.log" "$OUT/avf-console.log" >/dev/null 2>&1 || true
		printf '   %s: %s lines; last: %s\n' "$(date +%T)" "$(wc -l < "$OUT/avf-console.log" 2>/dev/null || echo 0)" \
			"$(tail -1 "$OUT/avf-console.log" 2>/dev/null | cut -c1-120)"
	done
	"${ADB[@]}" logcat -d > "$OUT/logcat.txt" || true
	echo "== console: $OUT/avf-console.log (the VM keeps running; stop it from the app)"
fi
