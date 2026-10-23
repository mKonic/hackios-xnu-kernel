ENTRY(_head)
SECTIONS
{
	. = 0;
	.text : { *(.text.head) *(.text*) }
	.rodata : { *(.rodata*) }
	. = ALIGN(16);
	.data : { *(.data*) *(.got*) }
	. = ALIGN(16);
	.rela.dyn : {
		__rela_start = .;
		*(.rela*)
		__rela_end = .;
	}
	. = ALIGN(16);
	.bss (NOLOAD) : {
		__bss_start = .;
		*(.bss*) *(COMMON)
		. = ALIGN(16);
		__bss_end = .;
	}
	. = ALIGN(4096);
	. += 0x10000;
	__stack_top = .;
	_end = .;
	_image_size = _end - _head;
	/DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.dynsym) *(.dynstr) *(.hash) *(.gnu.hash) *(.dynamic) *(.interp) }
}
