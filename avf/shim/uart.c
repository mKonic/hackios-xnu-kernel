/*
 * Polled UART console for the AVF target. The MMU is off, so every access
 * is Device memory: keep them naturally aligned and of the register's width.
 *
 *   UART_8250   crosvm ttyS0 (MMIO 0x3f8, byte registers, stride 1). AVF
 *               wires it to the VM's captured console.
 *   UART_PL011  QEMU virt (0x09000000), used for development.
 */
#include "uart.h"

static uintptr_t uart_base;
static int uart_kind;

#define U8250_THR	0
#define U8250_IER	1
#define U8250_FCR	2
#define U8250_LCR	3
#define U8250_MCR	4
#define U8250_LSR	5
#define U8250_LSR_THRE	0x20

#define PL011_DR	0x00
#define PL011_FR	0x18
#define PL011_FR_TXFF	0x20

static inline uint8_t rd8(uintptr_t a) { return *(volatile uint8_t *)a; }
static inline void wr8(uintptr_t a, uint8_t v) { *(volatile uint8_t *)a = v; }
static inline uint32_t rd32(uintptr_t a) { return *(volatile uint32_t *)a; }
static inline void wr32(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

void uart_init(int kind, uintptr_t base)
{
	uart_kind = kind;
	uart_base = base;
	if (kind == UART_8250) {
		wr8(base + U8250_IER, 0);		/* polled */
		wr8(base + U8250_LCR, 0x03);		/* 8N1, DLAB off; baud is whatever firmware left */
		wr8(base + U8250_FCR, 0x07);		/* FIFO on, both cleared */
		wr8(base + U8250_MCR, 0x03);		/* DTR, RTS */
	}
	/* PL011: QEMU's is usable without setup */
}

static void uart_putc_raw(char c)
{
	if (!uart_base)
		return;
	if (uart_kind == UART_8250) {
		for (int i = 0; i < 100000 && !(rd8(uart_base + U8250_LSR) & U8250_LSR_THRE); i++)
			;
		wr8(uart_base + U8250_THR, (uint8_t)c);
	} else {
		for (int i = 0; i < 100000 && (rd32(uart_base + PL011_FR) & PL011_FR_TXFF); i++)
			;
		wr32(uart_base + PL011_DR, (uint8_t)c);
	}
}

void uart_putc(char c)
{
	if (c == '\n')
		uart_putc_raw('\r');
	uart_putc_raw(c);
}

void uart_puts(const char *s)
{
	while (*s)
		uart_putc(*s++);
}

void uart_hex(uint64_t v)
{
	static const char hex[] = "0123456789abcdef";
	int started = 0;
	uart_puts("0x");
	for (int i = 60; i >= 0; i -= 4) {
		int d = (v >> i) & 0xf;
		if (d || started || i == 0) {
			uart_putc(hex[d]);
			started = 1;
		}
	}
}

void uart_dec(uint64_t v)
{
	char buf[21];
	int i = 20;
	buf[i] = 0;
	do {
		buf[--i] = '0' + v % 10;
		v /= 10;
	} while (v && i > 0);
	uart_puts(&buf[i]);
}

void uart_kv(const char *label, uint64_t v)
{
	uart_puts(label);
	uart_hex(v);
}
