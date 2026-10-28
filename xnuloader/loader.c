/*
 * XNU loader: a UEFI application that plays the role of iBoot for an
 * open-source arm64 XNU kernel on non-Apple hardware.
 *
 *   - finds the "XNUP" payload in RAM (placed there by the boot shim on the
 *     phone, by `-device loader` under QEMU) and the Mach-O kernel inside it
 *   - picks a block of free RAM to be XNU's [physBase, physBase + memSize)
 *   - copies the Mach-O segments there, builds an Apple-format device tree
 *     and a boot_args structure behind the kernel
 *   - exits boot services, turns the MMU off and jumps to the kernel entry
 *
 * Line convention (AGENTS.md 2): this file predates the line limit and is one UEFI entry
 * point whose state threads through the boot services as file-scope statics. It stays one
 * translation unit, because separate translation units would turn that shared state into
 * an external interface, a larger change than the line limit asks for. The includes below
 * are the whole of loader.c's own code, in the order it was written, and each fragment
 * stays under the limit on its own. The order is load-bearing: later fragments call
 * functions and read statics declared by earlier ones. build.sh still compiles loader.c
 * alone and the artifact stays byte-identical to the build from before the split. The
 * host tests that read this file's text reassemble it through their read_tu() helper, so
 * the marked LC-* blocks keep their exact text.
 */
#include "efi.h"
#include "identity.h"         /* who iOS shall think we are: from the iPhone's own device tree (mkidentity.py) */
#include "identity_v57.h"     /* ... or the iPhone 17 (iPhone18,3) for iOS 26: mkidentity.py --prefix v57_ */

#include "loader_01_base_log_dt.inc"
#include "loader_02_platform_cpu_ufs_survey.inc"
#include "loader_03_ufs_block_watch.inc"
#include "loader_04_fault_payload_nvram.inc"
#include "loader_05_layout_run_kernel.inc"
#include "loader_06_usb_entry.inc"
