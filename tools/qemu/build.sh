#!/usr/bin/env bash
# QEMU for HackiOS: the release the distribution ships (11.1.2) with tools/qemu/*.patch, for the aarch64
# machine only, built into out/qemu. qemu/run.sh takes it from there when it is there.
#   0001  rutabaga keeps one picture per scanout: moving the scanout to another resource of its size only
#         notes it, a flush of the shown resource reads into that picture (no placeholder, no frames
#         dropped by the D-Bus display at every move: kernel patch 1303 moves it every frame)
#   0002  helper_lookup_tb_ptr tries the jump cache with the state's fields, not a copy of the whole state
#         (a store-forwarding stall on every lookup; iOS's indirect calls and returns make lookups common)
#   0003  exit without the libraries' destructors (gfxstream's sync thread crashed in an unloading EGL at
#         every exit, with a core dump of the guest's memory each time)
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
