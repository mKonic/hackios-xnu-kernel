#!/usr/bin/env bash
# Build the XNU loader as a freestanding AArch64 UEFI application.
# EXTRA_CFLAGS=-DDIRECT_PHONE_PLAT builds the phone image: a framebuffer that
# matches no table entry selects platform_annibale instead of QEMU's ramfb.
#
# The variants are separate files on purpose (review F04, playbook trap 9: feeding the phone
# loader to QEMU hangs the kernel at 616 bytes): a phone build writes only loader-phone.efi and
# never touches loader.efi, so a QEMU run cannot pick up phone macros by accident. The default
# build writes loader.efi -- the name every consumer already uses -- and drops an identical
# loader-generic.efi alias beside it that spells out what the file is.
set -e
cd "$(dirname "$0")"
out=loader.efi
case " ${EXTRA_CFLAGS:-} " in *" -DDIRECT_PHONE_PLAT "*) out=loader-phone.efi ;; esac
${CLANG:-clang} --target=aarch64-unknown-windows -ffreestanding -fno-builtin -fno-stack-protector -fshort-wchar \
  -mgeneral-regs-only -O1 -Wall -Wextra -Wno-unused-parameter ${EXTRA_CFLAGS:-} -c loader.c -o loader.obj
# /base: UEFI loads an image at its preferred address when that is free. The PE default (0x140000000)
# is in the middle of the phone's largest block of RAM, which XNU needs in one piece.
${LLD_LINK:-lld-link} /subsystem:efi_application /entry:efi_main /nodefaultlib /base:0xA1000000 /out:$out loader.obj
if [ $out = loader.efi ]; then cp -f loader.efi loader-generic.efi; fi
ls -la $out
