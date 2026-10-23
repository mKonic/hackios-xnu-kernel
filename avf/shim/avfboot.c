/*
 * avfboot: boot the HackiOS XNU kernel as the "Linux kernel" of a VM.
 *
 * AVF (Android Virtualization Framework, crosvm on pKVM) boots a custom VM
 * from an arm64 Linux Image, an initrd and kernel parameters. This program
 * is that Image; the initrd is the loader's "XNUP" payload
 * (xnuloader/mkpayload.py: kernel=, optional ramdisk=, cmdline). It does
 * for XNU what xnuloader/loader.c does under UEFI, without UEFI:
 *
 *   - reads the machine from the FDT crosvm (or QEMU) hands over in x0:
 *     RAM, GICv3 regions, timer PPIs, PSCI conduit, CPUs, the UART and RTC
 *   - picks XNU's window: all of RAM less the panic-log page at the top. This
 *     image and the payload sit below the kernel's text (48 MB above the base),
 *     which XNU does not use; a payload too big for that is moved up first
 *   - copies the Mach-O segments (and the RAM disk behind them), writes an
 *     Apple device tree and boot_args like the loader's, and jumps in with the
 *     MMU off, x0 = boot_args
 *
 * Kernel console: the FDT's ns16550a at 0x3f8 (crosvm's ttyS0, which AVF
 * captures as the VM's console) becomes /arm-io/uart0 "ns16550a"; under QEMU
 * the PL011 becomes uart0 "arm,pl011", as with the loader. The FDT's
 * /chosen/bootargs (AVF: the VM's kernel parameters) are appended to the
 * payload's cmdline.
 *
 * There is no return path: the kernel's loader_return slot stays 0.
 */
#include <stdint.h>
#include <stddef.h>
#include "fdtx.h"
#include "uart.h"
#include "pci.h"
#include "identity.h"		/* xnuloader/identity.h: who iOS shall think we are */

#define PAYLOAD_MAGIC		0x50554E58u	/* "XNUP" */
#define KERNEL_VIRT_BASE	0xFFFFFFF004000000ULL
#define KERNEL_LINK_OFFSET	0x03000000ULL
#define BASE_ALIGN		0x02000000ULL	/* 32 MB: L2 block with 16K pages */
#define MAX_MEM_SIZE		0x180000000ULL
#define PAGE_SIZE_MAX		0x4000ULL
#define PANIC_LOG_SIZE		0x80000u
#define NVRAM_BANK_SIZE		0x2000u
#define DT_MAX			0x10000ULL
#define BOOT_LINE_LENGTH	608
#define MAX_CPUS		8

#define PSCI_SYSTEM_OFF		0x84000008ULL
#define PSCI_CPU_OFF		0x84000002ULL
#define PSCI_CPU_ON_64		0xc4000003ULL

#define LC_SEGMENT_64		0x19
#define LC_UNIXTHREAD		0x5
#define MH_MAGIC_64		0xFEEDFACFu

struct payload_header { uint32_t magic, size; };
struct payload_entry { char name[16]; uint32_t size, reserved; };

struct boot_video {
	uint64_t v_baseAddr, v_display, v_rowBytes, v_width, v_height, v_depth;
};

struct boot_args {
	uint16_t revision, version;
	uint64_t virtBase, physBase, memSize, topOfKernelData;
	struct boot_video video;
	uint32_t machineType;
	uint64_t deviceTreeP;
	uint32_t deviceTreeLength;
	char commandLine[BOOT_LINE_LENGTH];
	uint64_t bootFlags, memSizeActual;
};

_Static_assert(__builtin_offsetof(struct boot_args, commandLine) == 108, "boot_args layout");
_Static_assert(sizeof(struct boot_args) == 736, "boot_args layout");

struct mach_header_64 { uint32_t magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved; };
struct load_command { uint32_t cmd, cmdsize; };
struct segment_command_64 {
	uint32_t cmd, cmdsize;
	char segname[16];
	uint64_t vmaddr, vmsize, fileoff, filesize;
	uint32_t maxprot, initprot, nsects, flags;
};

#define rsr(r) ({ uint64_t _v; __asm__ volatile("mrs %0, " r : "=r"(_v)); _v; })

extern char _head[], _end[];

/* ---- small libc: the MMU is off, so everything is Device memory and must be aligned ---- */

void *memset(void *dst, int c, size_t n)
{
	uint8_t *d = dst;
	if (!((uintptr_t)d & 7) && !c) {
		for (; n >= 8; n -= 8, d += 8)
			*(volatile uint64_t *)d = 0;
	}
	while (n--)
		*(volatile uint8_t *)d++ = (uint8_t)c;
	return dst;
}

void *memcpy(void *dst, const void *src, size_t n)
{
	uint8_t *d = dst;
	const uint8_t *s = src;
	if (!(((uintptr_t)d | (uintptr_t)s) & 7)) {
		for (; n >= 8; n -= 8, d += 8, s += 8)
			*(volatile uint64_t *)d = *(volatile const uint64_t *)s;
	}
	while (n--)
		*(volatile uint8_t *)d++ = *(volatile const uint8_t *)s++;
	return dst;
}

static size_t str_len(const char *s)
{
	size_t n = 0;
	while (s[n])
		n++;
	return n;
}

static int str_eq(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return *a == *b;
}

static int starts_with(const char *s, const char *p)
{
	while (*p)
		if (*s++ != *p++)
			return 0;
	return 1;
}

/* is `word` (e.g. "cpus=") the start of one of the space-separated arguments in s? */
static int has_arg(const char *s, const char *word)
{
	for (const char *c = s; *c; c++)
		if ((c == s || c[-1] == ' ') && starts_with(c, word))
			return 1;
	return 0;
}

/* the number after `word` (e.g. "scfb.addr=") in s, decimal or 0x hex; 0 if absent */
static uint64_t arg_num(const char *s, const char *word)
{
	for (const char *c = s; *c; c++) {
		if ((c != s && c[-1] != ' ') || !starts_with(c, word))
			continue;
		const char *p = c + str_len(word);
		uint64_t v = 0;
		if (p[0] == '0' && (p[1] == 'x' || p[1] == 'X')) {
			for (p += 2;; p++) {
				uint64_t d;
				if (*p >= '0' && *p <= '9')
					d = (uint64_t)(*p - '0');
				else if (*p >= 'a' && *p <= 'f')
					d = (uint64_t)(*p - 'a' + 10);
				else if (*p >= 'A' && *p <= 'F')
					d = (uint64_t)(*p - 'A' + 10);
				else
					break;
				v = v * 16 + d;
			}
		} else {
			for (; *p >= '0' && *p <= '9'; p++)
				v = v * 10 + (uint64_t)(*p - '0');
		}
		return v;
	}
	return 0;
}

/*
 * Scion shared framebuffer (SCFB): the AVF app lends the VM a slice of RAM it maps too, and says
 * where with scfb.addr= scfb.size= scfb.w= scfb.h= in the kernel parameters. It becomes the boot
 * framebuffer (boot_args video): XNU's text console and MiniIOMobileFramebuffer draw into buffer 0,
 * MiniIOMobileFramebuffer publishes each frame in the header, and the app copies it to its
 * screen. The layout is the app's: display/SCFB.md in scion-avf.
 */
#define SCFB_MAGIC	0x42464353u	/* "SCFB" */
#define SCFB_FB_OFFSET	0x10000u	/* buffer 0, after the header */
#define FOURCC_XR24	0x34325258u	/* DRM_FORMAT_XRGB8888: bytes B, G, R, X (what iOS draws) */
struct scfb_header {
	uint32_t magic, version, width, height, stride, format, buf_offset[2], nbufs, flags, front, pad0;
	uint64_t frame_seq, input_seq, guest_ns;
	uint32_t dirty[4];
	uint64_t host_seq, host_reading, host_ns;
};
static struct { uint64_t pa, size; uint32_t w, h; } scfb;

/* ---- the machine, from the FDT ---- */

static struct {
	uint64_t ram_lo, ram_hi;
	uint64_t rd_start, rd_end;
	uint64_t gicd, gicd_size, gicr, gicr_size;
	uint32_t vtimer_intid;
	int psci_hvc;
	int ncpus;
	uint64_t mpidr[MAX_CPUS];
	uint32_t capacity[MAX_CPUS];	/* capacity-dmips-mhz, 0 if the FDT does not say */
	uint32_t max_capacity;
	uint64_t self;			/* the core avfboot runs on */
	int uart_kind;
	uint64_t uart_pa, uart_size;
	uint64_t rtc_pa;
	uint64_t fdt_lo, fdt_hi;
	char bootargs[BOOT_LINE_LENGTH];
} m;

static const char *stage = "start";

static uint64_t psci_call(uint64_t fn, uint64_t a1, uint64_t a2, uint64_t a3)
{
	register uint64_t x0 __asm__("x0") = fn;
	register uint64_t x1 __asm__("x1") = a1;
	register uint64_t x2 __asm__("x2") = a2;
	register uint64_t x3 __asm__("x3") = a3;
	if (m.psci_hvc)
		__asm__ volatile("hvc #0" : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3) : :
				 "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11",
				 "x12", "x13", "x14", "x15", "x16", "x17", "memory");
	else
		__asm__ volatile("smc #0" : "+r"(x0), "+r"(x1), "+r"(x2), "+r"(x3) : :
				 "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11",
				 "x12", "x13", "x14", "x15", "x16", "x17", "memory");
	return x0;
}

static void __attribute__((noreturn)) die(const char *why)
{
	uart_puts("avfboot: FATAL: ");
	uart_puts(why);
	uart_puts("\navfboot: powering off\n");
	psci_call(PSCI_SYSTEM_OFF, 0, 0, 0);
	for (;;)
		__asm__ volatile("wfe");
}

void avfboot_exception(uint64_t slot, uint64_t *regs)
{
	uart_kv("\navfboot: exception slot=", slot);
	uart_puts(" during ");
	uart_puts(stage);
	uart_kv("\n  esr=", rsr("esr_el1"));
	uart_kv(" elr=", rsr("elr_el1"));
	uart_kv(" far=", rsr("far_el1"));
	uart_kv(" lr=", regs[30]);
	uart_kv(" load=", (uintptr_t)_head);
	uart_putc('\n');
	die("unexpected exception");
}

static int root_cells(const struct fdt *f, const char *name, int dflt)
{
	uint32_t len;
	uintptr_t v = fdt_prop(f, 0, name, &len);
	return v && len == 4 ? (int)fdt_be32(v) : dflt;
}

static int reg_pair(const struct fdt *f, int node, int index, int ac, int sc, uint64_t *base, uint64_t *size)
{
	uint32_t len;
	uintptr_t v = fdt_prop(f, node, "reg", &len);
	uint32_t stride = 4U * (ac + sc);
	if (!v || len < stride * (index + 1))
		return 0;
	v += stride * index;
	*base = fdt_cells(v, ac);
	*size = fdt_cells(v + 4 * ac, sc);
	return 1;
}

static int mem_streq(uintptr_t a, const char *b)
{
	while (*(volatile char *)a && *(volatile char *)a == *b)
		a++, b++;
	return *(volatile char *)a == *b;
}

static void read_fdt(const struct fdt *f)
{
	int ac = root_cells(f, "#address-cells", 2), sc = root_cells(f, "#size-cells", 1);
	uint32_t len;
	uintptr_t v;
	int n;

	m.fdt_lo = f->base;
	m.fdt_hi = f->base + f->totalsize;

	/* the console first, so everything after can report problems */
	stage = "fdt: uart";
	for (n = fdt_find_compatible(f, -1, "ns16550a"); n >= 0; n = fdt_find_compatible(f, n, "ns16550a")) {
		uint64_t b, s;
		if (reg_pair(f, n, 0, ac, sc, &b, &s) && (!m.uart_pa || b == 0x3f8)) {
			m.uart_kind = UART_8250;	/* crosvm: 0x3f8 is ttyS0, the captured console */
			m.uart_pa = b;
			m.uart_size = s;
		}
	}
	if (!m.uart_pa && (n = fdt_find_compatible(f, -1, "arm,pl011")) >= 0 &&
	    reg_pair(f, n, 0, ac, sc, &m.uart_pa, &m.uart_size))
		m.uart_kind = UART_PL011;
	uart_init(m.uart_kind, m.uart_pa);

	uart_puts("\navfboot: HackiOS XNU boot for crosvm/AVF and QEMU virt\n");
	uart_kv("avfboot: image ", (uintptr_t)_head);
	uart_kv("-", (uintptr_t)_end);
	uart_kv(" fdt ", f->base);
	uart_kv("+", f->totalsize);
	uart_kv(" EL", rsr("CurrentEL") >> 2);
	uart_kv(" MIDR ", rsr("midr_el1"));
	uart_kv(" CNTFRQ ", rsr("cntfrq_el0"));
	uart_putc('\n');

	stage = "fdt: memory";
	for (n = fdt_first_child(f, 0); n >= 0; n = fdt_next_sibling(f, n)) {
		uintptr_t dt = fdt_prop(f, n, "device_type", &len);
		if (dt && mem_streq(dt, "memory") && reg_pair(f, n, 0, ac, sc, &m.ram_lo, &m.ram_hi)) {
			m.ram_hi += m.ram_lo;
			break;
		}
	}
	if (!m.ram_hi)
		die("no memory node in the FDT");

	stage = "fdt: chosen";
	int chosen = fdt_path(f, "/chosen");
	if ((v = fdt_prop(f, chosen, "linux,initrd-start", &len)))
		m.rd_start = fdt_cells(v, len / 4);
	if ((v = fdt_prop(f, chosen, "linux,initrd-end", &len)))
		m.rd_end = fdt_cells(v, len / 4);
	if ((v = fdt_prop(f, chosen, "bootargs", &len))) {
		uint32_t i;
		for (i = 0; i < len && i < BOOT_LINE_LENGTH - 1 && *(volatile char *)(v + i); i++)
			m.bootargs[i] = *(volatile char *)(v + i);
		m.bootargs[i] = 0;
	}

	stage = "fdt: gic";
	if ((n = fdt_find_compatible(f, -1, "arm,gic-v3")) < 0 ||
	    !reg_pair(f, n, 0, ac, sc, &m.gicd, &m.gicd_size) ||
	    !reg_pair(f, n, 1, ac, sc, &m.gicr, &m.gicr_size))
		die("no GICv3 in the FDT");

	stage = "fdt: timer";
	m.vtimer_intid = 27;	/* the architected default, PPI 11 */
	if ((n = fdt_find_compatible(f, -1, "arm,armv8-timer")) >= 0 &&
	    (v = fdt_prop(f, n, "interrupts", &len)) && len >= 36) {
		/* secure phys, phys, virt, hyp; <type num flags> each */
		uint32_t type = fdt_be32(v + 24), num = fdt_be32(v + 28);
		if (type == 1 && num < 16)
			m.vtimer_intid = 16 + num;
	}

	stage = "fdt: psci";
	m.psci_hvc = 1;		/* crosvm, and QEMU virt without EL2/EL3 */
	if ((n = fdt_find_compatible(f, -1, "arm,psci-1.0")) >= 0 ||
	    (n = fdt_find_compatible(f, -1, "arm,psci-0.2")) >= 0 ||
	    (n = fdt_find_compatible(f, -1, "arm,psci")) >= 0) {
		if ((v = fdt_prop(f, n, "method", &len)))
			m.psci_hvc = !mem_streq(v, "smc");
	}

	stage = "fdt: rtc";
	uint64_t rs;
	if ((n = fdt_find_compatible(f, -1, "arm,pl031")) >= 0) {
		reg_pair(f, n, 0, ac, sc, &m.rtc_pa, &rs);
	} else {
		/* crosvm: a PL030 described only as "arm,primecell" + arm,primecell-periphid 0x41030 */
		for (n = fdt_find_compatible(f, -1, "arm,primecell"); n >= 0; n = fdt_find_compatible(f, n, "arm,primecell")) {
			uintptr_t id = fdt_prop(f, n, "arm,primecell-periphid", &len);
			if (id && len == 4 && (fdt_be32(id) & 0xfffff) == 0x41030) {
				reg_pair(f, n, 0, ac, sc, &m.rtc_pa, &rs);
				break;
			}
		}
	}

	stage = "fdt: cpus";
	int cpus = fdt_path(f, "/cpus");
	uintptr_t acv = fdt_prop(f, cpus, "#address-cells", &len);
	int cac = acv && len == 4 ? (int)fdt_be32(acv) : 1;
	uint64_t self = rsr("mpidr_el1") & 0xff00ffffffULL;
	m.self = self;
	m.mpidr[m.ncpus++] = self;	/* the boot core comes first */
	for (n = fdt_first_child(f, cpus); n >= 0 && m.ncpus < MAX_CPUS; n = fdt_next_sibling(f, n)) {
		uintptr_t dt = fdt_prop(f, n, "device_type", &len);
		uintptr_t reg = fdt_prop(f, n, "reg", &len);
		unsigned int clen = 0;
		uintptr_t cap = fdt_prop(f, n, "capacity-dmips-mhz", &clen);
		if (dt && mem_streq(dt, "cpu") && reg && len >= 4U * cac) {
			uint64_t id = fdt_cells(reg, cac) & 0xff00ffffffULL;
			int at = id == self ? 0 : m.ncpus++;
			m.mpidr[at] = id;
			m.capacity[at] = cap && clen == 4 ? fdt_be32(cap) : 0;
			if (m.capacity[at] > m.max_capacity)
				m.max_capacity = m.capacity[at];
		}
	}
	/*
	 * Big cores first. With AVF's host-matched topology crosvm pins each vCPU to the host core
	 * of the same number and gives its capacity: on the Pixel 10 vCPU 0 and 1 are Cortex-A520s,
	 * 2-6 A725s and 7 the X4. avfboot runs on vCPU 0, a little core, and the kernel takes
	 * cpus=N from the front of this list, so without this XNU would boot and do its single-CPU
	 * work on the slowest core and fill up with the little ones first. Sorted by capacity,
	 * largest first (stable), and the kernel starts on the first one (avfboot hops there with
	 * PSCI CPU_ON before it jumps in). Kernel parameter avf_bigcore=0 keeps FDT order.
	 */
	if (m.max_capacity && !(has_arg(m.bootargs, "avf_bigcore=") && arg_num(m.bootargs, "avf_bigcore=") == 0)) {
		for (int i = 1; i < m.ncpus; i++) {
			for (int j = i; j > 0 && m.capacity[j] > m.capacity[j - 1]; j--) {
				uint64_t t = m.mpidr[j];
				uint32_t c = m.capacity[j];
				m.mpidr[j] = m.mpidr[j - 1], m.capacity[j] = m.capacity[j - 1];
				m.mpidr[j - 1] = t, m.capacity[j - 1] = c;
			}
		}
	}

	stage = "fdt: pci";
	if (pci_from_fdt((const uint8_t *)f->base)) {
		uart_puts("avfboot: PCI ");
		uart_puts(PCI.compatible);
		uart_kv(" config ", PCI.ecam[0]);
		uart_kv("+", PCI.ecam[1]);
		uart_kv(" windows ", PCI.nranges);
		uart_kv(" INTx ", PCI.nintx);
		uart_puts(PCI.probe_only ? " probe-only\n" : "\n");
	}

	uart_kv("avfboot: RAM ", m.ram_lo);
	uart_kv("-", m.ram_hi);
	uart_kv(" initrd ", m.rd_start);
	uart_kv("-", m.rd_end);
	uart_kv(" RTC ", m.rtc_pa);
	uart_putc('\n');
	uart_kv("avfboot: GICD ", m.gicd);
	uart_kv(" GICR ", m.gicr);
	uart_kv("+", m.gicr_size);
	uart_kv(" vtimer INTID ", m.vtimer_intid);
	uart_puts(m.psci_hvc ? " PSCI hvc" : " PSCI smc");
	uart_puts(" cpus ");
	uart_dec(m.ncpus);
	uart_kv(" UART ", m.uart_pa);
	uart_puts(m.uart_kind == UART_8250 ? " ns16550a\n" : " pl011\n");
	if (m.max_capacity) {
		uart_puts("avfboot: cpus, kernel's order (MPIDR:capacity):");
		for (int i = 0; i < m.ncpus; i++) {
			uart_kv(" ", m.mpidr[i]);
			uart_kv(":", m.capacity[i]);
		}
		uart_putc('\n');
	}
}

/* ---- Apple device tree writer (as xnuloader/loader.c) ----
 * node := u32 nProperties, u32 nChildren, props[], children[]
 * prop := char name[32], u32 length, value padded to 4 bytes */

struct dt_writer {
	uint8_t *base, *cur;
	uint32_t *counts[16];
	int depth;
};

static void dt_prop(struct dt_writer *w, const char *name, const void *data, uint32_t len)
{
	memset(w->cur, 0, 32);
	memcpy(w->cur, name, str_len(name));
	*(volatile uint32_t *)(w->cur + 32) = len;
	memset(w->cur + 36, 0, (len + 3) & ~3u);
	memcpy(w->cur + 36, data, len);
	w->cur += 36 + ((len + 3) & ~3u);
	w->counts[w->depth - 1][0]++;
}

static void dt_prop_str(struct dt_writer *w, const char *name, const char *value)
{
	dt_prop(w, name, value, (uint32_t)str_len(value) + 1);
}

static void dt_prop_u32(struct dt_writer *w, const char *name, uint32_t value)
{
	dt_prop(w, name, &value, sizeof(value));
}

static void dt_prop_u64(struct dt_writer *w, const char *name, uint64_t value)
{
	dt_prop(w, name, &value, sizeof(value));
}

static void dt_prop_padded(struct dt_writer *w, const char *name, const char *value, uint32_t size)
{
	uint8_t field[64];
	memset(field, 0, sizeof(field));
	memcpy(field, value, str_len(value));
	dt_prop(w, name, field, size);
}

static void dt_identity(struct dt_writer *w, const struct identity_prop *props, uint32_t count, const unsigned char *data)
{
	for (uint32_t i = 0; i < count; i++) {
		/* the screen here is sRGB, not the iPhone X's P3 (see loader.c) */
		if (str_eq(props[i].name, "artwork-display-gamut")) {
			dt_prop(w, props[i].name, "sRGB", 5);
			continue;
		}
		dt_prop(w, props[i].name, data + props[i].offset, props[i].length);
	}
}

static void dt_begin(struct dt_writer *w, const char *name)
{
	if (w->depth > 0)
		w->counts[w->depth - 1][1]++;
	w->counts[w->depth++] = (uint32_t *)w->cur;
	memset(w->cur, 0, 8);
	w->cur += 8;
	dt_prop_str(w, "name", name);
}

static void dt_end(struct dt_writer *w)
{
	w->depth--;
}

/* iBoot's NVRAM copy in CHRP format: a valid header and one empty "common" partition */
static void nvram_chrp_header(uint8_t *h, uint8_t sig, uint32_t bytes, const char *name)
{
	uint16_t blocks = (uint16_t)(bytes / 16), sum;
	memset(h, 0, 16);
	h[0] = sig;
	h[2] = (uint8_t)blocks;
	h[3] = (uint8_t)(blocks >> 8);
	memcpy(h + 4, name, str_len(name));
	sum = h[0];
	for (int i = 2; i < 16; i++)
		sum += h[i];
	while (sum > 0xff)
		sum = (sum & 0xff) + (sum >> 8);
	h[1] = (uint8_t)sum;
}

static uint8_t nvram_image[NVRAM_BANK_SIZE];

static void build_nvram_image(uint8_t *image)
{
	uint32_t lo = 1, hi = 0, generation = 1;
	memset(image, 0, NVRAM_BANK_SIZE);
	nvram_chrp_header(image, 0x5a, 0x20, "nvram");
	nvram_chrp_header(image + 0x20, 0x70, NVRAM_BANK_SIZE - 0x20, "common");
	memcpy(image + 20, &generation, 4);
	for (uint32_t i = 20; i < NVRAM_BANK_SIZE; i++) {
		lo = (lo + image[i]) % 65521;
		hi = (hi + lo) % 65521;
	}
	uint32_t adler = (hi << 16) | lo;
	memcpy(image + 16, &adler, 4);
}

/* ---- the payload ---- */

struct payload_info {
	const uint8_t *kernel, *ramdisk;
	uint32_t kernel_size, ramdisk_size;
	const char *cmdline;
};

static int parse_payload(struct payload_info *pi, uintptr_t base, uint64_t size)
{
	const struct payload_header *ph = (const struct payload_header *)base;
	memset(pi, 0, sizeof(*pi));
	pi->cmdline = "";
	if (ph->magic != PAYLOAD_MAGIC || ph->size + 8 > size)
		return 0;
	const uint8_t *p = (const uint8_t *)(ph + 1), *pend = p + ph->size;
	while (p + sizeof(struct payload_entry) <= pend) {
		const struct payload_entry *e = (const struct payload_entry *)p;
		const uint8_t *data = p + sizeof(*e);
		if (e->name[0] == 0)
			break;
		uart_puts("avfboot: payload entry ");
		uart_puts(e->name);
		uart_kv(" size ", e->size);
		uart_putc('\n');
		if (str_eq(e->name, "kernel")) {
			pi->kernel = data;
			pi->kernel_size = e->size;
		} else if (str_eq(e->name, "ramdisk")) {
			pi->ramdisk = data;
			pi->ramdisk_size = e->size;
		} else if (str_eq(e->name, "cmdline")) {
			pi->cmdline = (const char *)data;
		}
		p += sizeof(*e) + ((e->size + 15) & ~15u);
	}
	return pi->kernel != NULL;
}

static uint64_t align_up(uint64_t v, uint64_t a)
{
	return (v + a - 1) & ~(a - 1);
}

typedef void (*xnu_entry_t)(uint64_t boot_args_pa);
/* start.S: where a core started with PSCI CPU_ON enters, to jump to hop_entry(hop_arg) */
void avfboot_hop(void);
volatile uint64_t hop_entry, hop_arg;

static void cmd_append(char *cl, uint32_t *n, const char *s)
{
	while (*s && *n < BOOT_LINE_LENGTH - 1)
		cl[(*n)++] = *s++;
}

void avfboot_main(uint64_t fdt_pa)
{
	struct fdt f;
	struct payload_info pi;

	if (!fdt_open(&f, fdt_pa))
		die("x0 is not an FDT");	/* no console known yet: silent */
	read_fdt(&f);

	stage = "payload";
	if (!m.rd_start || m.rd_end <= m.rd_start)
		die("no initrd: give the VM the XNUP payload (xnuloader/mkpayload.py) as its initrd");
	if (!parse_payload(&pi, m.rd_start, m.rd_end - m.rd_start))
		die("the initrd is not an XNUP payload with a kernel entry");

	/* --- kernel extent --- */
	const struct mach_header_64 *mh = (const struct mach_header_64 *)pi.kernel;
	if (((uintptr_t)mh & 7) || mh->magic != MH_MAGIC_64)
		die("the kernel entry is not a 64-bit Mach-O");
	uint64_t vm_lo = ~0ULL, vm_hi = 0, entry_va = 0;
	const struct load_command *lc = (const struct load_command *)(mh + 1);
	for (uint32_t i = 0; i < mh->ncmds; i++, lc = (const void *)((const uint8_t *)lc + lc->cmdsize)) {
		if (lc->cmd == LC_SEGMENT_64) {
			const struct segment_command_64 *sg = (const void *)lc;
			if (!sg->vmsize)
				continue;
			if (sg->vmaddr < vm_lo)
				vm_lo = sg->vmaddr;
			if (sg->vmaddr + sg->vmsize > vm_hi)
				vm_hi = sg->vmaddr + sg->vmsize;
		} else if (lc->cmd == LC_UNIXTHREAD) {
			entry_va = ((const uint64_t *)((const uint8_t *)lc + 16))[32];
		}
	}
	if (vm_lo < KERNEL_VIRT_BASE + KERNEL_LINK_OFFSET || !entry_va)
		die("unexpected kernel layout (not a HackiOS kernel?)");

	/*
	 * --- XNU's window: all of RAM, less the panic log at the top ---
	 * The kernel's text goes 48 MB above the window's base (KERNEL_LINK_OFFSET), and XNU never
	 * uses what is below it: this program and the payload (crosvm puts it at +16 MB) live there
	 * for free. Only a payload that reaches into the kernel's destination is moved out of the way
	 * first, to the top of RAM, where XNU gets the pages back once it runs.
	 */
	stage = "layout";
	uint64_t phys_base = align_up(m.ram_lo, BASE_ALIGN);
	uint64_t pram = (m.ram_hi - PANIC_LOG_SIZE) & ~(PAGE_SIZE_MAX - 1);
	uint64_t win_end = pram & ~(BASE_ALIGN - 1);
	scfb.pa = arg_num(m.bootargs, "scfb.addr=");
	scfb.size = arg_num(m.bootargs, "scfb.size=");
	scfb.w = (uint32_t)arg_num(m.bootargs, "scfb.w=");
	scfb.h = (uint32_t)arg_num(m.bootargs, "scfb.h=");
	if (scfb.pa || scfb.size) {
		if (scfb.pa < m.ram_lo || scfb.pa + scfb.size > m.ram_hi || (scfb.pa & 0xfff) || !scfb.w || !scfb.h ||
		    SCFB_FB_OFFSET + (uint64_t)scfb.w * 4 * scfb.h > scfb.size)
			die("scfb.addr/size/w/h do not describe a framebuffer in RAM");
		/* the kernel's memory ends below it */
		if (scfb.pa < win_end)
			win_end = scfb.pa & ~(BASE_ALIGN - 1);
	}
	if (win_end <= phys_base)
		die("not enough RAM");
	uint64_t win_size = win_end - phys_base;
	if (win_size > MAX_MEM_SIZE)
		win_size = MAX_MEM_SIZE;

	uint64_t slide = phys_base - KERNEL_VIRT_BASE;	/* pa = va + slide */
	uint64_t kern_lo = vm_lo + slide, kern_hi = align_up(vm_hi + slide, PAGE_SIZE_MAX);
	uint64_t rd_pa = kern_hi, rd_size = align_up(pi.ramdisk_size, PAGE_SIZE_MAX);
	uint64_t dt_pa = rd_pa + rd_size;
	uint64_t ba_pa = dt_pa + DT_MAX;
	uint64_t top = ba_pa + PAGE_SIZE_MAX;
	if (top + 0x4000000ULL > phys_base + win_size)
		die("not enough RAM for the kernel");
	if ((uintptr_t)_end > kern_lo)
		die("this program overlaps the kernel's destination");
	if (m.rd_start < top && m.rd_end > kern_lo) {
		uint64_t len = m.rd_end - m.rd_start;
		uint64_t to = (phys_base + win_size - len) & ~(PAGE_SIZE_MAX - 1);
		if (to < top)
			die("not enough RAM to move the payload out of the kernel's way");
		uart_kv("avfboot: moving the payload to ", to);
		uart_putc('\n');
		/* upwards, so from the end: the two ranges may overlap */
		for (uint64_t o = (len + 7) & ~7ULL; o > 0; o -= 8)
			*(volatile uint64_t *)(to + o - 8) = *(volatile uint64_t *)(m.rd_start + o - 8);
		m.rd_start = to;
		m.rd_end = to + len;
		if (!parse_payload(&pi, m.rd_start, len))
			die("payload lost in the move");
		mh = (const struct mach_header_64 *)pi.kernel;
	}
	uart_kv("avfboot: window ", phys_base);
	uart_kv("+", win_size);
	uart_kv(" kernel ", kern_lo);
	uart_kv("-", kern_hi);
	uart_kv(" entry ", entry_va + slide);
	uart_kv(" pram ", pram);
	uart_putc('\n');

	/*
	 * From here on the window is written: crosvm's FDT (top of RAM) may be
	 * inside it, and everything needed from it is in `m` already.
	 */
	stage = "copy kernel";
	lc = (const struct load_command *)(mh + 1);
	for (uint32_t i = 0; i < mh->ncmds; i++, lc = (const void *)((const uint8_t *)lc + lc->cmdsize)) {
		const struct segment_command_64 *sg = (const void *)lc;
		if (lc->cmd != LC_SEGMENT_64 || !sg->vmsize)
			continue;
		memset((void *)(sg->vmaddr + slide), 0, sg->vmsize);
		memcpy((void *)(sg->vmaddr + slide), pi.kernel + sg->fileoff, sg->filesize);
	}
	if (rd_size) {
		stage = "copy ramdisk";
		memset((void *)(rd_pa + pi.ramdisk_size), 0, rd_size - pi.ramdisk_size);
		memcpy((void *)rd_pa, pi.ramdisk, pi.ramdisk_size);
		uart_kv("avfboot: ramdisk ", rd_pa);
		uart_kv("+", rd_size);
		uart_putc('\n');
	}

	/* --- boot_args --- */
	stage = "boot_args";
	struct boot_args *ba = (struct boot_args *)ba_pa;
	memset(ba, 0, PAGE_SIZE_MAX);
	ba->revision = 2;
	ba->version = 2;
	ba->virtBase = KERNEL_VIRT_BASE;
	ba->physBase = phys_base;
	ba->memSize = win_size;
	ba->memSizeActual = win_size;
	ba->topOfKernelData = top;
	if (scfb.pa) {
		volatile struct scfb_header *h = (volatile struct scfb_header *)scfb.pa;
		h->magic = SCFB_MAGIC;
		h->version = 1;
		h->width = scfb.w;
		h->height = scfb.h;
		h->stride = scfb.w * 4;
		h->format = FOURCC_XR24;
		h->buf_offset[0] = SCFB_FB_OFFSET;
		h->buf_offset[1] = SCFB_FB_OFFSET;
		h->nbufs = 1;
		h->front = 0;
		h->frame_seq = 1;
		h->flags = 3;	/* ready, and poll: the text console publishes nothing */
		memset((void *)(scfb.pa + SCFB_FB_OFFSET), 0, (uint64_t)scfb.w * 4 * scfb.h);
		ba->video.v_baseAddr = scfb.pa + SCFB_FB_OFFSET;
		ba->video.v_display = 1;
		ba->video.v_rowBytes = scfb.w * 4;
		ba->video.v_width = scfb.w;
		ba->video.v_height = scfb.h;
		ba->video.v_depth = 32;
		uart_kv("avfboot: scfb ", scfb.pa);
		uart_kv("+", scfb.size);
		uart_putc('\n');
	}
	/* otherwise no framebuffer: video stays zero */
	char *cl = ba->commandLine;
	uint32_t n = 0;
	int own_serial = has_arg(pi.cmdline, "serial=") || has_arg(m.bootargs, "serial=");
	cmd_append(cl, &n, own_serial ? "debug=0x8 " : "debug=0x8 serial=3 ");
	if (!has_arg(pi.cmdline, "cpus=") && !has_arg(pi.cmdline, "cpumask=") &&
	    !has_arg(m.bootargs, "cpus=") && !has_arg(m.bootargs, "cpumask="))
		cmd_append(cl, &n, "cpus=1 ");
	if (rd_size && !has_arg(pi.cmdline, "rd=") && !has_arg(m.bootargs, "rd="))
		cmd_append(cl, &n, "rd=md0 ");
	cmd_append(cl, &n, pi.cmdline);
	if (m.bootargs[0]) {
		cmd_append(cl, &n, " ");
		cmd_append(cl, &n, m.bootargs);
	}
	cl[n] = 0;
	int more_cores = has_arg(cl, "cpus=") && !has_arg(cl, "cpus=1");

	/* --- device tree --- */
	stage = "device tree";
	struct dt_writer w = { .base = (uint8_t *)dt_pa, .cur = (uint8_t *)dt_pa, .depth = 0 };
	uint64_t cntfrq = rsr("cntfrq_el0");
	/* io_base: non-zero (XNU takes 0 for "no SoC") and below crosvm's UART at 0x3f8 */
	uint64_t io_base = 0x100, io_size = 0x40000000 - io_base;
	uint64_t ranges[3] = { 0, io_base, io_size };
	uint8_t seed[64];
	for (unsigned i = 0; i < sizeof(seed); i++)
		seed[i] = (uint8_t)(rsr("cntvct_el0") >> (i & 7)) ^ (uint8_t)(i * 37);

	dt_begin(&w, "device-tree");
	dt_identity(&w, identity_root, sizeof(identity_root) / sizeof(identity_root[0]), identity_root_data);
	dt_prop_str(&w, "platform-name", "t8015");
	dt_prop_padded(&w, "serial-number", "AVF00SCIONVM", 32);
	dt_prop_padded(&w, "mlb-serial-number", "AVF00SCIONVMMLB00", 32);
	dt_prop_padded(&w, "model-number", "MQAC2", 32);
	dt_prop_padded(&w, "region-info", "LL/A", 32);
	dt_prop_padded(&w, "regulatory-model-number", "A1865", 32);
	dt_prop_u32(&w, "#address-cells", 2);
	dt_prop_u32(&w, "#size-cells", 2);

	dt_begin(&w, "chosen");
	dt_prop(&w, "random-seed", seed, sizeof(seed));
	dt_prop_u64(&w, "dram-base", phys_base);
	dt_prop_u64(&w, "dram-size", win_size);
	dt_prop_u32(&w, "debug-enabled", 1);
	dt_prop_u32(&w, "display-scale", 3);
	dt_prop_u32(&w, "display-rotation", 0);
	dt_prop_str(&w, "psci-method", m.psci_hvc ? "hvc" : "smc");
	if (m.rtc_pa) {
		uint64_t epoch = *(volatile uint32_t *)m.rtc_pa;	/* PL030/PL031 RTCDR: seconds since 1970 */
		if (epoch > 1700000000ULL)
			dt_prop_u64(&w, "boot-epoch", epoch);
	}
	static const uint8_t wifi_address[6] = { 0x02, 0x53, 0x43, 0x4e, 0x00, 0x01 };
	static const uint8_t bluetooth_address[6] = { 0x02, 0x53, 0x43, 0x4e, 0x00, 0x02 };
	static const uint8_t manifest_hash[48] = { 0x61, 0x76, 0x66 };
	static const uint8_t no_behavior[16];
	dt_prop_u64(&w, "unique-chip-id", 0x0000415646000001ULL);
	dt_prop_u32(&w, "chip-id", 0x8015);
	dt_prop_u32(&w, "board-id", 0x06);
	dt_prop(&w, "mac-address-wifi0", wifi_address, sizeof(wifi_address));
	dt_prop(&w, "mac-address-bluetooth0", bluetooth_address, sizeof(bluetooth_address));
	dt_prop(&w, "boot-manifest-hash", manifest_hash, sizeof(manifest_hash));
	dt_prop(&w, "software-behavior", no_behavior, sizeof(no_behavior));
	dt_prop_str(&w, "firmware-version", "iBoot-8422.100.650");
	dt_prop_u32(&w, "certificate-production-status", 1);
	dt_prop_u32(&w, "certificate-security-mode", 1);
	dt_prop_u32(&w, "effective-production-status-ap", 1);
	dt_prop_u32(&w, "effective-security-mode-ap", 1);
	dt_prop_u32(&w, "secure-boot", 1);
	dt_prop_u32(&w, "production-cert", 1);
	dt_prop_u32(&w, "development-cert", 0);
	dt_prop_u32(&w, "embedded-panic-log-size", PANIC_LOG_SIZE);
	build_nvram_image(nvram_image);
	dt_prop_u32(&w, "nvram-bank-size", NVRAM_BANK_SIZE);
	dt_prop_u32(&w, "nvram-bank-count", 1);
	dt_prop_u32(&w, "nvram-current-bank", 0);
	dt_prop(&w, "nvram-proxy-data", nvram_image, sizeof(nvram_image));
	dt_begin(&w, "memory-map");
	if (rd_size) {
		uint64_t r[2] = { rd_pa, rd_size };
		dt_prop(&w, "RAMDisk", r, sizeof(r));
	}
	dt_end(&w);
	if (more_cores) {
		/*
		 * Every "machine timeout" of the kernel (spinlock and mutex patience, IPI waits:
		 * machine_timeout_init_with_suffix()) times this. Bare-metal values panic under pKVM,
		 * which preempts vCPUs, a lock holder among them, for longer than real cores ever
		 * stall: with 8 host-matched vCPUs the kernel died of "Ticket spinlock ... timeout
		 * after 3001083 ticks" once launchd got busy. The UEFI loader uses 20 on QEMU.
		 * Kernel parameter timeout_scale=N overrides (0: none).
		 */
		uint32_t scale = has_arg(m.bootargs, "timeout_scale=") ? (uint32_t)arg_num(m.bootargs, "timeout_scale=") : 40;
		if (scale > 1) {
			dt_begin(&w, "machine-timeouts");
			dt_prop_u32(&w, "global-scale", scale);
			dt_end(&w);
			uart_kv("avfboot: machine timeouts x", scale);
			uart_puts("\n");
		}
	}
	dt_end(&w);	/* chosen */

	dt_begin(&w, "defaults");
	dt_prop_u32(&w, "kern.vm_compressor", 2);
	dt_end(&w);

	dt_begin(&w, "product");
	dt_identity(&w, identity_product, sizeof(identity_product) / sizeof(identity_product[0]), identity_product_data);
	dt_begin(&w, "haptics");
	dt_identity(&w, identity_haptics, sizeof(identity_haptics) / sizeof(identity_haptics[0]), identity_haptics_data);
	dt_end(&w);
	dt_end(&w);

	dt_begin(&w, "options");
	dt_end(&w);

	uint64_t pram_reg[2] = { pram, PANIC_LOG_SIZE };
	memset((void *)pram, 0, PANIC_LOG_SIZE);
	dt_begin(&w, "pram");
	dt_prop(&w, "reg", pram_reg, sizeof(pram_reg));
	dt_end(&w);

	int ncpus = more_cores ? m.ncpus : 1;
	dt_begin(&w, "cpus");
	for (int i = 0; i < ncpus; i++) {
		char name[5] = { 'c', 'p', 'u', (char)('0' + i), 0 };
		dt_begin(&w, name);
		dt_prop_str(&w, "device_type", "cpu");
		dt_prop_str(&w, "state", i == 0 ? "running" : "waiting");
		dt_prop_u32(&w, "reg", (uint32_t)m.mpidr[i]);
		dt_prop_u32(&w, "cpu-id", (uint32_t)i);
		dt_prop_u64(&w, "timebase-frequency", cntfrq);
		dt_prop_u64(&w, "fixed-frequency", cntfrq);
		dt_prop_u64(&w, "clock-frequency", 2000000000ULL);
		dt_prop_u64(&w, "bus-frequency", 100000000ULL);
		dt_prop_u64(&w, "memory-frequency", 100000000ULL);
		dt_prop_u64(&w, "peripheral-frequency", 24000000ULL);
		/* a little core: the scheduler uses it once the big ones are busy */
		if (m.max_capacity && m.capacity[i] && m.capacity[i] <= m.max_capacity / 2)
			dt_prop_u32(&w, "small-core", 1);
		dt_end(&w);
	}
	dt_end(&w);

	dt_begin(&w, "arm-io");
	dt_prop_str(&w, "device_type", "crosvm");
	dt_prop(&w, "ranges", ranges, sizeof(ranges));
	dt_prop_u32(&w, "#address-cells", 2);
	dt_prop_u32(&w, "#size-cells", 2);
	dt_begin(&w, "gic");
	dt_prop_str(&w, "device_type", "interrupt-controller");
	dt_prop_str(&w, "compatible", "arm,gic-v3");
	uint64_t gic_reg[4] = { m.gicd - io_base, m.gicd_size, m.gicr - io_base, m.gicr_size };
	dt_prop(&w, "reg", gic_reg, sizeof(gic_reg));
	dt_prop_u32(&w, "virtual-timer-intid", m.vtimer_intid);
	dt_end(&w);
	if (m.uart_pa) {
		uint64_t reg2[2] = { m.uart_pa - io_base, m.uart_size ? m.uart_size : 0x1000 };
		dt_begin(&w, "uart0");
		dt_prop_str(&w, "device_type", "uart");
		dt_prop_str(&w, "compatible", m.uart_kind == UART_8250 ? "ns16550a" : "arm,pl011");
		dt_prop(&w, "reg", reg2, sizeof(reg2));
		dt_prop_u32(&w, "boot-console", 1);
		dt_end(&w);
	}
	dt_end(&w);	/* arm-io */

	/* the PCI host bridge, for bsd/dev/redwood_pci.c (format: pci.h, xnuloader/loader.c) */
	if (PCI.present) {
		dt_begin(&w, "pci0");
		dt_prop_str(&w, "compatible", PCI.compatible);
		dt_prop(&w, "ecam", PCI.ecam, sizeof(PCI.ecam));
		dt_prop(&w, "bus-range", PCI.bus_range, sizeof(PCI.bus_range));
		dt_prop(&w, "pci-ranges", PCI.ranges, PCI.nranges * sizeof(PCI.ranges[0]));
		dt_prop(&w, "intx-map", PCI.intx, PCI.nintx * sizeof(PCI.intx[0]));
		dt_prop(&w, "intx-mask", PCI.mask, sizeof(PCI.mask));
		dt_prop_u32(&w, "probe-only", PCI.probe_only);
		dt_end(&w);
	}
	dt_end(&w);	/* device-tree */

	uint32_t dt_len = (uint32_t)(w.cur - w.base);
	if (dt_len > DT_MAX)
		die("device tree overflow");
	ba->deviceTreeP = dt_pa - slide;
	ba->deviceTreeLength = dt_len;

	uart_kv("avfboot: device tree ", dt_pa);
	uart_kv("+", dt_len);
	uart_kv(" boot_args ", ba_pa);
	uart_kv(" top ", top);
	uart_puts("\navfboot: boot-args: ");
	uart_puts(cl);
	uart_kv("\navfboot: jumping to XNU at ", entry_va + slide);
	uart_puts("\n\n");

	stage = "xnu";
	__asm__ volatile("dsb sy; ic iallu; dsb sy; isb" ::: "memory");
	if (m.mpidr[0] != m.self) {
		/* the kernel starts on the big core (see "fdt: cpus"); this one switches itself off */
		hop_entry = entry_va + slide;
		hop_arg = ba_pa;
		__asm__ volatile("dsb sy" ::: "memory");
		uint64_t r = psci_call(PSCI_CPU_ON_64, m.mpidr[0], (uint64_t)(uintptr_t)avfboot_hop, 0);
		if (r != 0)
			die("PSCI CPU_ON of the big core failed (kernel parameter avf_bigcore=0 boots on this one)");
		psci_call(PSCI_CPU_OFF, 0, 0, 0);
		for (;;)
			__asm__ volatile("wfe");
	}
	((xnu_entry_t)(entry_va + slide))(ba_pa);
	die("XNU returned");
}
