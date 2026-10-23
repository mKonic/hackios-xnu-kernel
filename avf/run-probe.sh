#!/usr/bin/env bash
# Boot avfboot + an XNUP payload as a custom AVF VM on a phone, through the AVF probe app
# (dylanneve1/scion-avf, probe/), and collect the VM's console.
#
#   avf/run-probe.sh IMAGE PAYLOAD [-s SERIAL] [--mem MIB] [--timeout SEC] [--params "..."] [--out DIR]
#                    [--match-host-cpus]   (one vCPU per host core instead of one; add cpus=N to --params)
#
# IMAGE is avf/shim/out/Image, PAYLOAD an xnuloader/mkpayload.py payload; --params become the VM's
# kernel parameters, which avfboot appends to the kernel's boot-args. The probe app must be installed.
#
# House rules this script keeps: it takes /data/local/tmp/pixel.lock on the phone first (waiting
# while someone else holds it), streams the guest files into the probe's own files/guest (the app
# cannot read files adb pushes), and before releasing the lock it revokes the VM permissions and
# restores the global hidden-API exemption list. It touches no other app.
set -euo pipefail

IMAGE=${1:?usage: run-probe.sh IMAGE PAYLOAD [options]}
PAYLOAD=${2:?usage: run-probe.sh IMAGE PAYLOAD [options]}
shift 2
PKG=io.github.dylanneve1.avfprobe
SERIAL=${ANDROID_SERIAL:-}
MEM=2048
TIMEOUT=90
PARAMS="-v"
OUT=avf-run-$(date +%Y%m%d-%H%M%S)
EXTRA=()
while [[ $# -gt 0 ]]; do
	case "$1" in
	-s) SERIAL=$2; shift 2 ;;
	--mem) MEM=$2; shift 2 ;;
	--timeout) TIMEOUT=$2; shift 2 ;;
	--params) PARAMS=$2; shift 2 ;;
	--out) OUT=$2; shift 2 ;;
	--match-host-cpus) EXTRA+=(--ez match_host_cpus true); shift ;;
	*) echo "unknown option: $1" >&2; exit 2 ;;
	esac
done
ADB=(adb ${SERIAL:+-s "$SERIAL"})
sh_() { "${ADB[@]}" shell "$@"; }
LOCK=/data/local/tmp/pixel.lock
DATA=/sdcard/Android/data/$PKG/files
EXEMPT='Landroid/system/virtualmachine/VirtualMachineCustomImageConfig$Builder;,Landroid/system/virtualmachine/VirtualMachineConfig$Builder;->setCustomImageConfig('
mkdir -p "$OUT"

echo "== waiting for $LOCK"
until sh_ mkdir "$LOCK" 2>/dev/null; do sleep 10; done
# what the exemption list was, before anything can change it (cleanup puts it back)
prev=$(sh_ settings get global hidden_api_blacklist_exemptions | tr -d '\r')
cleanup() {
	if [[ "$prev" == "null" || -z "$prev" ]]; then
		sh_ settings delete global hidden_api_blacklist_exemptions >/dev/null || true
	else
		sh_ settings put global hidden_api_blacklist_exemptions "'$prev'" || true
	fi
	sh_ pm revoke "$PKG" android.permission.MANAGE_VIRTUAL_MACHINE 2>/dev/null || true
	sh_ pm revoke "$PKG" android.permission.USE_CUSTOM_VIRTUAL_MACHINE 2>/dev/null || true
	echo "== exemptions now: $(sh_ settings get global hidden_api_blacklist_exemptions | tr -d '\r'); grants revoked"
	sh_ rmdir "$LOCK" && echo "== lock released"
}
trap cleanup EXIT

sh_ pm list packages | grep -q "package:$PKG\$" || { echo "the probe app ($PKG) is not installed" >&2; exit 1; }
sh_ pm grant "$PKG" android.permission.MANAGE_VIRTUAL_MACHINE
sh_ pm grant "$PKG" android.permission.USE_CUSTOM_VIRTUAL_MACHINE
sh_ settings put global hidden_api_blacklist_exemptions "'$EXEMPT'"

echo "== streaming guest files into the app's files/guest"
sh_ "run-as $PKG sh -c 'mkdir -p files/guest'"
sh_ rm -f "$DATA/guest/Image" "$DATA/guest/initrd.img" "$DATA/report.txt" "$DATA/console.txt" "$DATA/vmlog.txt"
for pair in "$IMAGE:Image" "$PAYLOAD:initrd.img"; do
	src=${pair%%:*} name=${pair##*:}
	"${ADB[@]}" push "$src" "/data/local/tmp/avfboot-$name" >/dev/null
	sh_ "cat /data/local/tmp/avfboot-$name | run-as $PKG sh -c 'cat > files/guest/$name'"
	sh_ rm -f "/data/local/tmp/avfboot-$name"
	echo "   $name: $(sh_ "run-as $PKG sh -c 'wc -c < files/guest/$name'" | tr -d '\r') bytes (local $(wc -c < "$src"))"
done

echo "== running (mem ${MEM} MiB, ${TIMEOUT} s, params: $PARAMS)"
sh_ am force-stop "$PKG"
"${ADB[@]}" logcat -c || true
sh_ am start -W -n "$PKG/.MainActivity" --ez autorun true --el mem_mib "$MEM" --el timeout_s "$TIMEOUT" \
	--es params "'$PARAMS'" ${EXTRA[@]+"${EXTRA[@]}"} >/dev/null
deadline=$(($(date +%s) + TIMEOUT + 60))
until sh_ grep -q '^VERDICT:' "$DATA/report.txt" 2>/dev/null; do
	(($(date +%s) < deadline)) || { echo "no verdict in time" >&2; break; }
	sleep 3
done
for f in report.txt console.txt vmlog.txt; do
	"${ADB[@]}" pull "$DATA/$f" "$OUT/$f" >/dev/null 2>&1 || true
done
"${ADB[@]}" logcat -d > "$OUT/logcat.txt" || true
grep -iE 'virtmgr|virtualizationservice|crosvm|AvfProbe' "$OUT/logcat.txt" > "$OUT/logcat-avf.txt" || true
echo "== results in $OUT"
grep '^VERDICT:' "$OUT/report.txt" 2>/dev/null || true
