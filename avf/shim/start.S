/*
 * avfboot entry: an arm64 Linux Image (Documentation/arch/arm64/booting.rst),
 * so crosvm (AVF) and QEMU's -kernel load and enter it like a Linux kernel:
 * EL1, MMU and D-cache off, x0 = physical address of the FDT.
 *
 * The image is linked at 0 as a PIE and relocates itself (R_AARCH64_RELATIVE)
 * before any C code runs: crosvm loads it at the start of RAM (0x80000000),
 * QEMU virt at 0x40000000.
 */
	.section .text.head, "ax"
	.globl _head
_head:
	b	_start			/* code0 */
	.long	0			/* code1 */
	.quad	0			/* text_offset: load at the 2MB-aligned base */
	.quad	_image_size		/* image_size, including .bss and the stack */
	.quad	0xa			/* flags: little endian, 4K pages, anywhere in RAM */
	.quad	0			/* res2 */
	.quad	0			/* res3 */
	.quad	0			/* res4 */
	.ascii	"ARM\x64"		/* magic */
	.long	0			/* res5 (no PE header) */

	.text
	.globl _start
_start:
	msr	daifset, #0xf
	mov	x19, x0			/* FDT */
	msr	spsel, #1
	adr	x1, vectors
	msr	vbar_el1, x1
	isb

	/* self-relocation: *(base + r_offset) = base + r_addend for R_AARCH64_RELATIVE */
	adr	x20, _head		/* load address = link-time 0 */
	adrp	x1, __rela_start
	add	x1, x1, :lo12:__rela_start
	adrp	x2, __rela_end
	add	x2, x2, :lo12:__rela_end
1:	cmp	x1, x2
	b.hs	2f
	ldp	x3, x4, [x1], #16	/* r_offset, r_info */
	ldr	x5, [x1], #8		/* r_addend */
	cmp	w4, #1027		/* R_AARCH64_RELATIVE */
	b.ne	1b
	add	x5, x5, x20
	str	x5, [x20, x3]
	b	1b
2:
	/* zero .bss */
	adrp	x1, __bss_start
	add	x1, x1, :lo12:__bss_start
	adrp	x2, __bss_end
	add	x2, x2, :lo12:__bss_end
3:	cmp	x1, x2
	b.hs	4f
	str	xzr, [x1], #8
	b	3b
4:	adrp	x1, __stack_top
	add	x1, x1, :lo12:__stack_top
	mov	sp, x1
	dsb	sy
	ic	iallu
	dsb	sy
	isb
	mov	x0, x19
	bl	avfboot_main
5:	wfe
	b	5b

/*
 * A core started with PSCI CPU_ON (avfboot_main hands the kernel to the biggest core): EL1,
 * MMU and caches off like the boot core. Jumps to hop_entry with x0 = hop_arg, as the boot
 * core would have.
 */
	.globl avfboot_hop
avfboot_hop:
	msr	daifset, #0xf
	adrp	x1, hop_entry
	ldr	x1, [x1, :lo12:hop_entry]
	adrp	x0, hop_arg
	ldr	x0, [x0, :lo12:hop_arg]
	dsb	sy
	ic	iallu
	dsb	sy
	isb
	br	x1

/*
 * Exceptions: every slot saves x0-x30 and calls avfboot_exception(slot, regs),
 * which prints them and powers the VM off.
 */
.macro vent slot
	.balign	0x80
	sub	sp, sp, #(32 * 8)
	stp	x0, x1, [sp, #0]
	mov	x0, #\slot
	b	exc_common
.endm

	.balign	0x800
vectors:
	vent 0
	vent 1
	vent 2
	vent 3
	vent 4
	vent 5
	vent 6
	vent 7
	vent 8
	vent 9
	vent 10
	vent 11
	vent 12
	vent 13
	vent 14
	vent 15

exc_common:
	stp	x2, x3, [sp, #16]
	stp	x4, x5, [sp, #32]
	stp	x6, x7, [sp, #48]
	stp	x8, x9, [sp, #64]
	stp	x10, x11, [sp, #80]
	stp	x12, x13, [sp, #96]
	stp	x14, x15, [sp, #112]
	stp	x16, x17, [sp, #128]
	stp	x18, x19, [sp, #144]
	stp	x20, x21, [sp, #160]
	stp	x22, x23, [sp, #176]
	stp	x24, x25, [sp, #192]
	stp	x26, x27, [sp, #208]
	stp	x28, x29, [sp, #224]
	str	x30, [sp, #240]
	mov	x1, sp
	bl	avfboot_exception
6:	wfe
	b	6b
