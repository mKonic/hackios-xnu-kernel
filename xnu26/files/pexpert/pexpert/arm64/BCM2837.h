/*
 * Copyright (c) 2016 Apple Inc. All rights reserved.
 */

#ifndef _PEXPERT_ARM_BCM2837_H
#define _PEXPERT_ARM_BCM2837_H

#define NO_MONITOR 1
#define NO_ECORE 1

#define BCM2837
#define BCM2837_BRINGUP
#define ARM_ARCH_TIMER
#define GENERIC_GIC_V3_IRQ                 1 /* interrupts via GICv3 Group 1, see osfmk/arm64/gic_v3_irq.c */
#define GENERIC_PSCI_SMP                  1 /* more cores are started through PSCI, see osfmk/arm64/psci_smp.c */

/*
 * The cores this kernel really runs on (Cortex-A55/A78, QEMU cortex-a76) are
 * ARMv8.2: they set PSTATE.PAN on every exception entry, so the kernel must
 * know to lift it around user memory accesses.
 */
#define __ARM_PAN_AVAILABLE__     1

/*
 * iOS 26 has only arm64e programs. The kernel stays arm64 and leaves the pointer
 * authentication keys disabled (SCTLR_EL1.EnIA/EnIB/EnDA/EnDB clear), so that the PAC and
 * AUT instructions of those programs leave pointers as they are, and the shared cache's
 * authenticated pointers are rebased as plain ones (vm_shared_region_slide_page_v3/v5).
 * The CPU subtype is reported as arm64e only so that ml_grade_binary() accepts them (boot-arg
 * arm64e=0: arm64, for iOS 16.5, whose dyld then picks its arm64 slice and shared cache).
 */
#define REDWOOD_ARM64E_USERLAND   1

#define __ARM_ARCH__              8
#define __ARM_VMSA__              8
#define __ARM_VFP__               4
#define __ARM_COHERENT_CACHE__    1
#define __ARM_DEBUG__             7
#define __ARM64_PMAP_SUBPAGE_L1__ 1

#ifndef ASSEMBLER

/*
 * Apple's core-type field of MPIDR_EL1 (apple_arm64_regs.h): pexpert/arm64/apple_arm64_cpu.h,
 * which every arm64 build includes, names it. Nothing on this board calls arm64_core_type(): on
 * these cores bits 16-18 are affinity level 2.
 */
#define MPIDR_CORETYPE_SHIFT  (16)
#define MPIDR_CORETYPE_WIDTH  (3)
#define MPIDR_CORETYPE_MASK   ((1ULL << MPIDR_CORETYPE_WIDTH) - 1)
#define MPIDR_CORETYPE_ACC_E  (0ULL)
#define MPIDR_CORETYPE_ACC_P  (1ULL)
#define MPIDR_CORETYPE_ACC_M  (4ULL)

#define PI3_UART
#define PL011_UART                      /* as found on QEMU virt */

#define PI3_BREAK                               asm volatile("brk #0");

#define BCM2837_GPFSEL0_V               (pi3_gpio_base_vaddr + 0x0)
#define BCM2837_GPSET0_V                (pi3_gpio_base_vaddr + 0x1C)
#define BCM2837_GPCLR0_V                (pi3_gpio_base_vaddr + 0x28)
#define BCM2837_GPPUD_V                 (pi3_gpio_base_vaddr + 0x94)
#define BCM2837_GPPUDCLK0_V             (pi3_gpio_base_vaddr + 0x98)

#define BCM2837_FSEL_INPUT              0x0
#define BCM2837_FSEL_OUTPUT             0x1
#define BCM2837_FSEL_ALT0               0x4
#define BCM2837_FSEL_ALT1               0x5
#define BCM2837_FSEL_ALT2               0x6
#define BCM2837_FSEL_ALT3               0x7
#define BCM2837_FSEL_ALT4               0x3
#define BCM2837_FSEL_ALT5               0x2

#define BCM2837_FSEL_NFUNCS             54
#define BCM2837_FSEL_REG(func)          (BCM2837_GPFSEL0_V + (4 * ((func) / 10)))
#define BCM2837_FSEL_OFFS(func)         (((func) % 10) * 3)
#define BCM2837_FSEL_MASK(func)         (0x7 << BCM2837_FSEL_OFFS(func))

#define BCM2837_AUX_ENABLES_V           (pi3_aux_base_vaddr + 0x4)
#define BCM2837_AUX_MU_IO_REG_V         (pi3_aux_base_vaddr + 0x40)
#define BCM2837_AUX_MU_IER_REG_V        (pi3_aux_base_vaddr + 0x44)
#define BCM2837_AUX_MU_IIR_REG_V        (pi3_aux_base_vaddr + 0x48)
#define BCM2837_AUX_MU_LCR_REG_V        (pi3_aux_base_vaddr + 0x4C)
#define BCM2837_AUX_MU_MCR_REG_V        (pi3_aux_base_vaddr + 0x50)
#define BCM2837_AUX_MU_LSR_REG_V        (pi3_aux_base_vaddr + 0x54)
#define BCM2837_AUX_MU_MSR_REG_V        (pi3_aux_base_vaddr + 0x58)
#define BCM2837_AUX_MU_SCRATCH_V        (pi3_aux_base_vaddr + 0x5C)
#define BCM2837_AUX_MU_CNTL_REG_V       (pi3_aux_base_vaddr + 0x60)
#define BCM2837_AUX_MU_STAT_REG_V       (pi3_aux_base_vaddr + 0x64)
#define BCM2837_AUX_MU_BAUD_REG_V       (pi3_aux_base_vaddr + 0x68)
#define BCM2837_PUT32(addr, value) do { *((volatile uint32_t *) addr) = value; } while(0)
#define BCM2837_GET32(addr) *((volatile uint32_t *) addr)

/*
 * No fixed PLATFORM_PANIC_LOG_PADDR: the Raspberry Pi address (0x3c0fc000) is
 * not RAM on other boards. The panic log region comes from the device tree
 * instead (/pram and /chosen/embedded-panic-log-size), like on Apple devices.
 */
#endif /* ! ASSEMBLER */

#endif /* ! _PEXPERT_ARM_BCM2837_H */
