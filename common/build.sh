#!/usr/bin/env bash
# Build everything a device needs, in order, skipping what cannot be built on this machine.
#
# usage: common/build.sh <device> [userland/build.sh variables...]
#   common/build.sh qemu-virt
#   common/build.sh redwood GL=1
#
#   1. kernel   xnu/apply.sh, xnu-deps/setup.sh, xnu-build.sh   -- only with theos + iPhoneOS16.5.sdk,
#                                                                  otherwise prebuilt/ is used
#   2. loader   xnuloader/build.sh                              -- only with clang + lld-link; a
#                                                                  profile may set LOADER_CFLAGS
#                                                                  (-DDIRECT_PHONE_PLAT for phones:
#                                                                  that build writes only
#                                                                  xnuloader/loader-phone.efi and
#                                                                  never touches loader.efi,
#                                                                  playbook trap 9)
#   3. images   userland/build.sh (REST=1 and the profile's USERLAND_VARS)
#                                                               -- needs ios165/fetch.sh to have run
#   4. boot     mkxnuimg.py -> out/<device>-boot.img             -- phones: the UEFI image for `fastboot boot`
#
#   SKIP="kernel loader images boot"   leave steps out
. "$(dirname "$(readlink -f "$0")")/lib.sh"
load_device "$1"; shift
skip() { [[ " $SKIP " == *" $1 "* ]]; }
mkdir -p out

# which loader file the profile's flags produce (F04): the phone macro has its own output name,
# so the two variants never overwrite each other and every packaging step names what it packed.
LOADER_CFLAGS=${LOADER_CFLAGS:-}
LOADER_OUT=loader.efi
case " $LOADER_CFLAGS " in *" -DDIRECT_PHONE_PLAT "*) LOADER_OUT=loader-phone.efi ;; esac

if ! skip kernel; then
	if [ -d "${THEOS:-$HOME/theos}/sdks/iPhoneOS16.5.sdk" ] && command -v clang >/dev/null; then
		[ -d xnu-src ] || xnu/apply.sh
		[ -x xnu-deps/host/bin/ld64 ] || xnu-deps/setup.sh
		# Wi-Fi radio driver (phones with a WCN6750): fetched and built once
		[ "$WIFI_ATH11K" = 1 ] && [ ! -f xnu-deps/build/ath11k/out/ath11k.o ] && { tools/mk-ath11k.sh && tools/mk-wlfw.sh; }
		# the export check fails after the kernel is linked, on purpose (xnu/README.md): judge by the file
		touch out/.kernel-start
		./xnu-build.sh -j"$(nproc)" -k > out/kernel-build.log 2>&1 || true
		K=xnu-src/xnu-8796.121.2/BUILD/obj/DEVELOPMENT_ARM64_BCM2837/mach.development.bcm2837
		[ -f $K ] && { [ $K -nt out/.kernel-start ] || ! grep -q "error:" out/kernel-build.log; } &&
			echo "build: kernel $(stat -c %s $K) bytes" || die "kernel build failed, see out/kernel-build.log"
	else
		echo "build: no theos/iPhoneOS16.5.sdk: using prebuilt/mach.development.bcm2837"
	fi
fi

if ! skip loader; then
	if command -v clang >/dev/null && command -v lld-link >/dev/null; then
		EXTRA_CFLAGS="$LOADER_CFLAGS" xnuloader/build.sh || die "loader build failed: nothing gets packaged on top of the stale file"
		echo "build: loader xnuloader/$LOADER_OUT"
	else
		echo "build: no clang/lld-link: keeping the existing xnuloader/$LOADER_OUT (prebuilt only for the default variant)"
	fi
fi

if ! skip images; then
	have_userland
	[ -d ios165/rootfs ] || die "no iOS files yet: run ios165/fetch.sh first (it downloads them from Apple to this machine)"
	env REST=1 $USERLAND_VARS "$@" userland/build.sh || die "image build failed"
fi

if ! skip boot && [ "$KIND" = phone ]; then
	# the UEFI with the loader inside, the way `xl enter` makes it: the loader then brings up its USB link
	# The image carries the variant the profile asked for. A phone that needs the phone macros
	# never falls back to the generic prebuilt -- wrong-platform loader bytes boot-loop silently
	# (trap 9), so here they are refused out loud (F04).
	L=xnuloader/$LOADER_OUT
	if [ ! -f $L ] && [ $LOADER_OUT = loader.efi ] && [ -f prebuilt/loader.efi ]; then
		L=prebuilt/loader.efi
	fi
	[ -f $L ] || die "no xnuloader/$LOADER_OUT: run common/build.sh $DEVICE without SKIP=loader (the generic prebuilt is no substitute for $LOADER_OUT)"
	python3 xnuloader/mkpayload.py -o out/usb-payload.bin loader.efi=$L --usb > /dev/null || die "mkpayload.py failed"
	FD_BASE=$FD_BASE FD_SIZE=$FD_SIZE PAYLOAD_BASE=$PAYLOAD_BASE PAYLOAD_CAP=$PAYLOAD_CAP UEFI_FD=$UEFI_FD DTB=$DTB UEFI_TARGET=$UEFI_TARGET BOOT_HEADER=$BOOT_HEADER \
		python3 mkxnuimg.py -p out/usb-payload.bin -o out/$DEVICE-boot.img || die "mkxnuimg.py failed"
	echo "build: out/$DEVICE-boot.img (loader $(basename $L)) -- boot it with: fastboot boot out/$DEVICE-boot.img (or common/run.sh $DEVICE)"
fi
