#!/bin/bash
# Build the gpu3d probe: out/Image, an arm64 Linux Image for crosvm (AVF) and QEMU -kernel.
# Needs clang and ld.lld (Apple's clang works with Homebrew's lld), and llvm-objcopy.
set -euo pipefail
cd "$(dirname "$0")"
CC=${CC:-clang}
LD=${LD:-ld.lld}
OBJCOPY=${OBJCOPY:-$(command -v llvm-objcopy || echo /opt/homebrew/opt/llvm/bin/llvm-objcopy)}
READELF=${READELF:-$(command -v llvm-readelf || echo /opt/homebrew/opt/llvm/bin/llvm-readelf)}
CFLAGS="--target=aarch64-none-elf -O2 -ffreestanding -fno-builtin -nostdlib -fPIE -fvisibility=hidden
	-mgeneral-regs-only -mstrict-align -fno-stack-protector -Wall -Wextra -Werror
	-I../../../xnu/files/bsd"
mkdir -p out
SH=../../shim
SRCS="$SH/start.S main.c bench.c $SH/fdtx.c $SH/uart.c ../../../xnu/files/bsd/dev/redwood_vgpu3d.c"
OBJS=
for f in $SRCS; do
	o=out/$(basename "${f%.*}").o
	$CC $CFLAGS -c "$f" -o "$o"
	OBJS="$OBJS $o"
done
# the CPU half of the compositing comparison, with NEON
$CC ${CFLAGS/-mgeneral-regs-only/} -O3 -c cpublend.c -o out/cpublend.o
OBJS="$OBJS out/cpublend.o"
$LD -pie --no-dynamic-linker -z norelro --build-id=none -T $SH/avfboot.ld $OBJS -o out/probe.elf
# every dynamic relocation must be one start.S applies
bad=$($READELF -r out/probe.elf | grep -E 'R_AARCH64_' | grep -v -c R_AARCH64_RELATIVE || true)
[ "${bad:-0}" = 0 ] || { echo "error: $bad non-RELATIVE dynamic relocations" >&2; exit 1; }
undef=$($READELF -s out/probe.elf | awk '$7 == "UND" && $8 != ""' | wc -l)
[ "$undef" -eq 0 ] || { $READELF -s out/probe.elf | awk '$7 == "UND" && $8 != ""'; echo "error: undefined symbols" >&2; exit 1; }
$OBJCOPY -O binary out/probe.elf out/Image
ls -l out/Image
