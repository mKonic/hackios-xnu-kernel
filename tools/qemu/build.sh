#!/usr/bin/env bash
# QEMU for HackiOS, built into out/qemu (qemu/run.sh takes it from there when it is there): the branch
# hackios/v11.1.2 of https://github.com/mKonic/qemu, a fork of QEMU -- the 11.1.2 release with
#   hw/display/virtio-gpu-rutabaga: one surface per scanout, shared with the display (no "Display output is
#         not active." flicker, no frames dropped by the D-Bus display; the skin maps the picture)
#   accel/tcg: the next-block lookup without the state copy that stalled on store forwarding, probed in
#         host code at the end of a block, from a jump cache of 64K entries kept across ASID switches;
#         a return-address stack, so that most RETs go to the block they returned to last without a lookup
#   target/arm: native calls on the PSCI conduit (SHA-256, zlib inflate on guest memory; the kernel's patch 1600)
#   system/main: exit without the libraries' destructors (gfxstream crashed in an unloading EGL at exit)
#   contrib/plugins/sampler: a sampling profiler (out/qemu/lib/libsampler.so; qemu/run-ios.sh QEMU_PROFILE=FILE)
#   contrib/plugins/excstat, tbstat, jcsim, rasim: exceptions and system calls by kind, translations, jump cache
#   misses, how often a return-address stack would predict returns
#         (out/qemu/lib; pass one to QEMU with -plugin PATH,out=FILE)
# Needs what the distribution's QEMU needs to build, with rutabaga-ffi (its gfxstream) and meson.
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
REPO=${QEMU_REPO:-https://github.com/mKonic/qemu.git}
BRANCH=${QEMU_BRANCH:-hackios/v11.1.2}
D=$ROOT/xnu-deps/build/qemu/src
OUT=$ROOT/out/qemu
mkdir -p "$(dirname "$D")" "$OUT"
if [ -d "$D/.git" ]; then
	git -C "$D" fetch -q --depth 1 origin "$BRANCH"
	git -C "$D" checkout -q -B "$BRANCH" FETCH_HEAD
else
	git clone -q --depth 1 -b "$BRANCH" "$REPO" "$D"
fi
cd "$D"
[ -f build/build.ninja ] || ./configure --prefix="$OUT" --target-list=aarch64-softmmu --disable-docs --disable-werror \
	--disable-modules --enable-rutabaga-gfx --enable-dbus-display --enable-opengl --enable-gio --enable-slirp >/dev/null
make -j"${JOBS:-8}" >/dev/null
make install >/dev/null
mkdir -p "$OUT/lib" && cp build/contrib/plugins/lib{sampler,excstat,tbstat,jcsim,rasim}.so "$OUT/lib/"
echo "build.sh: $OUT/bin/qemu-system-aarch64 ($("$OUT/bin/qemu-system-aarch64" --version | head -1), $(git rev-parse --short HEAD))"
