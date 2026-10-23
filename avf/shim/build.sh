#!/usr/bin/env bash
# Build avfboot: out/Image, an arm64 Linux Image for crosvm (AVF) and QEMU -kernel.
# Needs clang and ld.lld (any recent LLVM; Apple's clang works with Homebrew's lld).
set -euo pipefail
cd "$(dirname "$0")"
CC=${CC:-clang}
LD=${LD:-ld.lld}
OBJCOPY=${OBJCOPY:-$(command -v llvm-objcopy || echo /opt/homebrew/opt/llvm/bin/llvm-objcopy)}
CFLAGS="--target=aarch64-none-elf -O2 -ffreestanding -fno-builtin -nostdlib -fPIE -fvisibility=hidden
	-mgeneral-regs-only -mstrict-align -fno-stack-protector -Wall -Wextra -I../../xnuloader"
mkdir -p out
for f in start.S avfboot.c fdtx.c uart.c pci.c; do
	$CC $CFLAGS -c $f -o out/${f%.*}.o
done
$LD -pie --no-dynamic-linker -z norelro --build-id=none -T avfboot.ld \
	out/start.o out/avfboot.o out/fdtx.o out/uart.o out/pci.o -o out/avfboot.elf
# every dynamic relocation must be one start.S applies
bad=$($OBJCOPY --version >/dev/null; llvm-readelf -r out/avfboot.elf 2>/dev/null | grep -E 'R_AARCH64_' | grep -v -c R_AARCH64_RELATIVE || true)
[ "${bad:-0}" = 0 ] || { echo "error: $bad non-RELATIVE dynamic relocations" >&2; exit 1; }
$OBJCOPY -O binary out/avfboot.elf out/Image
ls -l out/Image
