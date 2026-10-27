#!/bin/bash
# What the xnu-12377 kernel build (xnu26/build-macos.sh) needs besides Xcode, on macOS: the
# open sources of the macOS 26.5 release that the kernel takes from Apple's internal SDK, and
# an iPhoneOS SDK that has them in usr/local. Everything goes to xnu-deps/26/ (not committed);
# what is there already is left alone.
#
#   xnu26/deps-macos.sh            stage 1, and stage 2 once the kernel's headers are exported
#   xnu26/deps-macos.sh --stage1   sources, the SDK
#   xnu26/deps-macos.sh --stage2   libfirehose_kernel.a and libpthread_kext.a, compiled against the
#                                  kernel's headers (xnu26/build-macos.sh exporthdrs installhdrs)
#   xnu26/deps-macos.sh --rebuild-libs   stage 2 from scratch
set -e -o pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/env-macos.sh"
mkdir -p "$XNU26_DEPS/src"
cd "$XNU26_DEPS"

log() { echo "deps26: $*"; }
die() { echo "deps26: $*" >&2; exit 1; }

# fetch NAME TAG: one tag of apple-oss-distributions/NAME, without its history
fetch() {
	[ -d "src/$1/.git" ] && return 0
	log "fetching $1 $2"
	git init -q "src/$1"
	git -C "src/$1" remote add origin "https://github.com/apple-oss-distributions/$1.git"
	git -C "src/$1" fetch -q --depth 1 origin "refs/tags/$2" || die "cannot fetch $2"
	git -C "src/$1" -c advice.detachedHead=false checkout -q FETCH_HEAD
}

# replace FILE with standard input unless it is the same already (make looks at time stamps)
update() {
	cat > "$1.new"
	if cmp -s "$1.new" "$1"; then rm "$1.new"; else mv "$1.new" "$1"; fi
}

stage1() {
	# the versions macOS 26.5 (xnu-12377.121.6) was released with
	fetch AvailabilityVersions AvailabilityVersions-157.2
	fetch libdispatch libdispatch-1542.100.32
	fetch libpthread libpthread-539.100.4

	# The SDK: Xcode's, as links, except for usr/local, which the public SDK does not have.
	local xc
	xc=$(xcrun --sdk iphoneos --show-sdk-path) || die "no iPhoneOS SDK in Xcode"
	if [ ! -d "$XNU26_SDK/usr/include" ]; then
		log "making $XNU26_SDK from $xc"
		mkdir -p "$XNU26_SDK/usr"
		for f in "$xc"/*; do
			case "${f##*/}" in
			usr) ;;
			SDKSettings.*) cp "$f" "$XNU26_SDK/" ;;
			*) ln -sfn "$f" "$XNU26_SDK/${f##*/}" ;;
			esac
		done
		for f in "$xc"/usr/*; do
			[ "${f##*/}" = local ] || ln -sfn "$f" "$XNU26_SDK/usr/${f##*/}"
		done
	fi
	local l=$XNU26_SDK/usr/local
	mkdir -p "$l/include/kernel/os" "$l/lib/kernel" "$l/libexec"
	# availability.pl, with its version list built in (the script otherwise reads it from next to itself)
	if [ ! -x "$l/libexec/availability.pl" ]; then
		src/AvailabilityVersions/availability --preprocess src/AvailabilityVersions/availability "$l/libexec/availability.pl"
		chmod +x "$l/libexec/availability.pl"
	fi
	# Headers of Apple's internal SDK that 12377 includes and its EXTERNAL_HEADERS no longer has:
	# libTrustCache, CoreEntitlements V2, CodeSignature. A Kernel Debug Kit of macOS 26 has them
	# (KDKROOT=...); without one they come from github.com/theo-os/xnu, which carries copies.
	local k=$l/include/kernel d
	if [ ! -f "$k/TrustCache/API.h" ]; then
		if [ -n "$KDKROOT" ]; then
			log "private headers from $KDKROOT"
			local ph=$KDKROOT/System/Library/Frameworks/Kernel.framework/Versions/A/PrivateHeaders/platform
			mkdir -p "$k/CoreEntitlements"
			cp -R "$ph/TrustCache" "$ph/CodeSignature" "$k/"
			cp -R "$ph/CoreEntitlements/V2" "$k/CoreEntitlements/"
			cp "$KDKROOT/System/Library/KernelSupport/CoreEntitlements/V2/Kernel.h" "$k/CoreEntitlements/V2/"
		else
			[ -d src/theo-xnu/.git ] || {
				log "fetching the private headers of theo-os/xnu"
				git init -q src/theo-xnu
				git -C src/theo-xnu remote add origin https://github.com/theo-os/xnu.git
				git -C src/theo-xnu fetch -q --depth 1 origin 54ea37c23af20f9d0354c0d758eb51dc68dbcd81 || die "cannot fetch theo-os/xnu"
				git -C src/theo-xnu -c advice.detachedHead=false checkout -q FETCH_HEAD
			}
			mkdir -p "$k/CoreEntitlements"
			for d in TrustCache CodeSignature; do cp -R "src/theo-xnu/EXTERNAL_HEADERS/$d" "$k/"; done
			cp -R src/theo-xnu/EXTERNAL_HEADERS/CoreEntitlements/V2 "$k/CoreEntitlements/"
		fi
	fi
	# the kernel's half of a private libdispatch header (unifdef exits with 1 when it removed something)
	{ unifdef -DOS_FIREHOSE_SPI=1 -DKERNEL=1 src/libdispatch/os/firehose_buffer_private.h || [ $? -eq 1 ]; } |
		update "$l/include/kernel/os/firehose_buffer_private.h"
	update "$l/include/kernel/os/firehose_types.defs" < src/libdispatch/src/firehose/firehose_types.defs
	log "stage 1 done"
}

# The firehose (os_log) buffer of libdispatch and the pthread kernel extension are open source
# but not part of XNU: Apple links the first from its internal SDK and loads the second as a
# kext. Here both are static libraries in the SDK that makedefs/MakeInc.def links into the kernel.
stage2() {
	local lib=$XNU26_SDK/usr/local/lib/kernel hdrs=$XNU26_SRC/BUILD/obj/EXPORT_HDRS
	local kfw=$XNU26_SRC/BUILD/dst/System/Library/Frameworks/Kernel.framework
	# (xnu-deps/build/pthread/inc: a TargetConditionals.h, which the kernel headers include)
	local kcc=(xcrun --sdk iphoneos clang -arch arm64 -c -Os -mkernel -nostdinc -fno-builtin -fno-common
		-DKERNEL=1 -DKERNEL_PRIVATE=1 -DPRIVATE=1 -I"$XNU26_ROOT/xnu-deps/build/pthread/inc")
	[ -d "$lib" ] || die "stage 1 has not been done"
	[ -d "$hdrs/osfmk" ] && [ -d "$kfw/PrivateHeaders" ] ||
		die "no kernel headers in $XNU26_SRC/BUILD (xnu26/build-macos.sh exporthdrs installhdrs)"
	if [ -n "$REBUILD_LIBS" ]; then rm -f "$lib/libfirehose_kernel.a" "$lib/libpthread_kext.a"; fi
	mkdir -p build
	if [ ! -f "$lib/libfirehose_kernel.a" ]; then
		log "building libfirehose_kernel.a"
		# as libdispatch's libfirehose_kernel.xcconfig: the installed Kernel.framework headers, and
		# the kernel's own for the private ones that those do not have
		"${kcc[@]}" -Wno-packed -DDISPATCH_USE_DTRACE=0 -DOS_ATOMIC_CONFIG_MEMORY_ORDER_DEPENDENCY=1 \
			-DOS_ATOMIC_CONFIG_STARVATION_FREE_ONLY=0 -DOS_FIREHOSE_SPI=1 \
			-Isrc/libdispatch -Isrc/libdispatch/src/firehose -I"$kfw/PrivateHeaders" -I"$kfw/Headers" \
			-I"$XNU26_SRC/libkern/firehose" -I"$XNU26_SDK/usr/local/include/kernel" -I"$XNU26_SRC/EXTERNAL_HEADERS" \
			-idirafter "$hdrs/osfmk" -idirafter "$hdrs/bsd" -idirafter "$hdrs/libkern" \
			-o build/firehose_buffer.o src/libdispatch/src/firehose/firehose_buffer.c
		xcrun libtool -static -o "$lib/libfirehose_kernel.a" build/firehose_buffer.o
	fi
	if [ ! -f "$lib/libpthread_kext.a" ]; then
		log "building libpthread_kext.a"
		# The two renames avoid kernel symbols of the same names: a kext is a link unit of its own.
		for f in kern_init kern_support kern_synch; do
			"${kcc[@]}" -Wno-int-conversion -D__PTHREAD_EXPOSE_INTERNALS__=1 -D__DARWIN_64_BIT_INO_T=1 \
				-DARM64_BOARD_CONFIG_BCM2837 -Dpthread_kern=pthread_kext_kern -Dcurrent_uthread=pthread_kext_current_uthread \
				-Isrc/libpthread -I"$hdrs/osfmk" -I"$hdrs/bsd" -I"$hdrs/libkern" -I"$hdrs/pexpert" -I"$hdrs/iokit" \
				-I"$hdrs/security" -I"$XNU26_SRC/EXTERNAL_HEADERS" -Isrc/libpthread/include -Isrc/libpthread/private \
				-o build/pthread_$f.o src/libpthread/kern/$f.c
		done
		xcrun libtool -static -o "$lib/libpthread_kext.a" build/pthread_kern_init.o build/pthread_kern_support.o \
			build/pthread_kern_synch.o
	fi
	log "stage 2 done"
}

case "$1" in
--stage1) stage1 ;;
--stage2) stage2 ;;
--rebuild-libs) REBUILD_LIBS=1 stage2 ;;
"")
	stage1
	if [ -d "$XNU26_SRC/BUILD/dst/System/Library/Frameworks/Kernel.framework" ]; then stage2
	else log "stage 2 after: xnu26/build-macos.sh exporthdrs installhdrs"; fi ;;
*) sed -n '2,11p' "$HERE/deps-macos.sh"; exit 1 ;;
esac
