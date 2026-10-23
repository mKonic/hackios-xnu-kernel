#!/bin/bash
# QEMU for HackiOS: the release the distribution ships (11.1.2) with tools/qemu/*.patch, for the aarch64
# machine only, built into out/qemu. qemu/run.sh takes it from there when it is there.
#   0001  rutabaga's scanout moves from one resource to the next without QEMU's "Display output is not
#         active." placeholder in between, and a flush tells the display of the resource it shows only
#         (the direct scanout of kernel patch 1303 flickered without it)
# Needs what the distribution's QEMU needs to build, with rutabaga-ffi (its gfxstream) and meson.
set -e
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
VERSION=11.1.2
D=$ROOT/xnu-deps/build/qemu
OUT=$ROOT/out/qemu
mkdir -p "$D" "$OUT"
cd "$D"
[ -f qemu-$VERSION.tar.xz ] || curl -sfLO https://download.qemu.org/qemu-$VERSION.tar.xz
rm -rf qemu-$VERSION
tar xf qemu-$VERSION.tar.xz
cd qemu-$VERSION
for p in "$ROOT"/tools/qemu/*.patch; do
	patch -p1 -s < "$p"
done
./configure --prefix="$OUT" --target-list=aarch64-softmmu --disable-docs --disable-werror --disable-modules \
	--enable-rutabaga-gfx --enable-dbus-display --enable-opengl --enable-gio --enable-slirp >/dev/null
make -j"${JOBS:-8}" >/dev/null
make install >/dev/null
echo "build.sh: $OUT/bin/qemu-system-aarch64 ($("$OUT/bin/qemu-system-aarch64" --version | head -1))"
