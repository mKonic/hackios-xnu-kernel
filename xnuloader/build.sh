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
# "fastboot getvar build-id" says what the image claims to be: BUILD_ID names the source commit
# the build was made from, a full lower-case 40-hex value passed in by the builder as an input
# fixed outside the source, and the build manifest pairs that value with the artifact hash
# recorded after the link. The artifact hash stays out of the image: a file that contains its
# own hash changes that hash by being written. Deriving the value from git here is deliberately
# not done: on a modified tree it would name a commit the built sources do not match. The reply
# packer fits 63 characters including the four-character code (59 for the value), so a 40-hex
# commit sits well inside the limit. An unset BUILD_ID keeps its honest answer "unknown" and is
# warned about here: a test build must set it.
bid_flags=()
if [ -z "${BUILD_ID+x}" ]; then
	echo "build.sh: BUILD_ID unset: getvar build-id will report unknown (a test build must set it)" >&2
elif [ -z "$BUILD_ID" ]; then
	echo "build.sh: BUILD_ID is set but empty: set it to one full lower-case 40-hex source commit, or leave it unset to report unknown" >&2
	exit 1
elif [[ ! "$BUILD_ID" =~ ^[0-9a-f]{40}$ ]]; then
	echo "build.sh: BUILD_ID must be one full lower-case 40-hex source commit (got ${#BUILD_ID} characters)" >&2
	exit 1
else
	# the escaped quotes stay inside the array element: the compiler must receive a C string,
	# a bare hex token would define the macro as a number
	bid_flags=("-DLOADER_BUILD_ID=\"${BUILD_ID}\"")
fi
${CLANG:-clang} "${bid_flags[@]}" --target=aarch64-unknown-windows -ffreestanding -fno-builtin -fno-stack-protector -fshort-wchar \
  -mgeneral-regs-only -O1 -Wall -Wextra -Wno-unused-parameter ${EXTRA_CFLAGS:-} -c loader.c -o loader.obj
# /base: UEFI loads an image at its preferred address when that is free. The PE default (0x140000000)
# is in the middle of the phone's largest block of RAM, which XNU needs in one piece.
${LLD_LINK:-lld-link} /subsystem:efi_application /entry:efi_main /nodefaultlib /base:0xA1000000 /out:$out loader.obj
if [ $out = loader.efi ]; then cp -f loader.efi loader-generic.efi; fi
ls -la $out
