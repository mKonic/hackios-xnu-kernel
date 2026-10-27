#!/bin/bash
# Build the xnu-12377 kernel for redwood on macOS with Xcode. Extra make args are passed through
# (exporthdrs: only the headers, which xnu26/deps-macos.sh --stage2 needs).
# The symbol sets for kexts are not made (KEXT_CREATE_SYMBOL_SET): no kext is ever loaded, and
# the exports of the closed parts of the kernel are missing from them.
# env: MACHINE (default BCM2837), KERNEL_CONFIG (default DEVELOPMENT), RC_DARWIN_KERNEL_VERSION (default
#      25.6.0, the Darwin version of iOS 26.6; Apple's build takes it from the SDK's System.kext)
source "$(dirname "${BASH_SOURCE[0]}")/env-macos.sh"
cd "$XNU26_SRC" || exit 1
export RC_DARWIN_KERNEL_VERSION=${RC_DARWIN_KERNEL_VERSION:-25.6.0}
exec ${MAKE:-gmake} XCRUN="$XNU26_ROOT/xnu26/xcrun" SDKROOT="$XNU26_SDK" \
  ARCH_CONFIGS=ARM64 MACHINE_CONFIGS=${MACHINE:-BCM2837} KERNEL_CONFIGS=${KERNEL_CONFIG:-DEVELOPMENT} \
  ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG=arm64 BUILD_WERROR=0 BUILD_LTO=0 DO_CTFMERGE=0 LOGCOLORS=n \
  KEXT_CREATE_SYMBOL_SET=/usr/bin/true "$@"
