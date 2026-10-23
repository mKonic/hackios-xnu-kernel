#pragma once
#include <stdint.h>

#define UART_NONE	0
#define UART_8250	1	/* ns16550a, byte registers */
#define UART_PL011	2	/* arm,pl011 */

void uart_init(int kind, uintptr_t base);
void uart_putc(char c);
void uart_puts(const char *s);
void uart_hex(uint64_t v);
void uart_dec(uint64_t v);
/* label followed by v in hex, no newline */
void uart_kv(const char *label, uint64_t v);
