#!/bin/bash
# Build the emulator (emu.cpp): out/emu/emu, with its toolbar icons rasterised into out/emu/icons.
# Needs: g++ (C++23), SDL3, GLES 3, glib/gio, zlib, rsvg-convert.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
OUT=$ROOT/out/emu
mkdir -p "$OUT/icons"
for svg in "$HERE"/icons/*.svg; do
	png=$OUT/icons/$(basename "${svg%.svg}").png
	[ "$png" -nt "$svg" ] || sed 's/currentColor/#ffffff/' "$svg" | rsvg-convert -w 96 -h 96 -o "$png"
done
g++ -std=c++23 -O2 -Wall -Wextra -Werror -DHACKIOS_ROOT="\"$ROOT\"" -DEMU_ICONS="\"$OUT/icons\"" \
	-o "$OUT/emu" "$HERE/emu.cpp" "$HERE/qemu_link.cpp" "$HERE/png.cpp" \
	$(pkg-config --cflags --libs sdl3 glesv2 gio-unix-2.0 zlib)
echo "emu: $OUT/emu"
