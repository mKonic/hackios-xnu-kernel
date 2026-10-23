/*
 * gpu3d probe: a bare-metal AVF guest that answers "can a VM on the Pixel draw with the
 * Pixel's GPU?" before the XNU virtio-gpu driver exists. It is an arm64 Linux Image (see
 * start.S), so the AVF custom-VM API boots it like a kernel. It:
 *
 *   1. turns the MMU on (identity map: RAM normal cacheable, everything else device);
 *   2. reads the FDT, enumerates the PCI host bridge (crosvm: CAM; QEMU virt: ECAM, whose
 *      unassigned BARs it assigns) and finds the virtio-gpu function;
 *   3. drives it as a virtio-pci 1.x device: features (VIRGL, CONTEXT_INIT), the control
 *      queue, polled;
 *   4. hands that control queue to the portable 3D layer (xnu/files/bsd/dev/redwood_vgpu3d.c,
 *      the same code the kernel will use), which lists the capsets, opens a gfxstream pipe,
 *      and has the host GPU draw and send back a picture (vgpu3d_selftest);
 *   5. prints everything as "GPU3D ..." lines on the console and powers off.
 *
 * Boot parameters (AVF: VirtualMachineCustomImageConfig.addParam):
 *   gpu3d.size=WxH     picture size (default 828x1792, the iPhone 11's)
 *   gpu3d.capset=N     context type for the pipe (default 8, gfxstream-gles; 0 = host default)
 */
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "../../shim/fdtx.h"
#include "../../shim/uart.h"
#include "../../../xnu/files/bsd/sys/redwood_vgpu3d.h"

#define PSCI_SYSTEM_OFF 0x84000008u

extern char _end[];

/* ---- libc bits the compiler may call ---------------------------------------------------- */

void *memcpy(void *d, const void *s, size_t n)
{
	uint8_t *a = d;
	const uint8_t *b = s;
	while (n--)
		*a++ = *b++;
	return d;
}

void *memset(void *d, int c, size_t n)
{
	uint8_t *a = d;
	while (n--)
		*a++ = (uint8_t)c;
	return d;
}

size_t strlen(const char *s)
{
	size_t n = 0;
	while (s[n])
		n++;
	return n;
}

static int startswith(const char *s, const char *p)
{
	while (*p)
		if (*s++ != *p++)
			return 0;
	return 1;
}

static uint32_t parse_u32(const char **s)
{
	uint32_t v = 0;
	while (**s >= '0' && **s <= '9')
		v = v * 10 + (uint32_t)(*(*s)++ - '0');
	return v;
}

/* ---- console, clock, power ------------------------------------------------------------ */

static void say(const char *s) { uart_puts(s); }

static void say_kv(const char *k, uint64_t v)
{
	uart_puts(k);
	uart_dec(v);
	uart_putc('\n');
}

static uint64_t cntfrq;

static uint64_t now_ns(void)
{
	uint64_t c;
	__asm__ volatile("isb; mrs %0, cntvct_el0" : "=r"(c));
	return cntfrq ? c / cntfrq * 1000000000u + c % cntfrq * 1000000000u / cntfrq : 0;
}

static void __attribute__((noreturn)) power_off(void)
{
	register uint64_t x0 __asm__("x0") = PSCI_SYSTEM_OFF;
	__asm__ volatile("hvc #0" : "+r"(x0) :: "memory");
	for (;;)
		__asm__ volatile("wfe");
}

void avfboot_exception(uint64_t slot, uint64_t *regs)
{
	uint64_t esr, far, elr;
	__asm__ volatile("mrs %0, esr_el1; mrs %1, far_el1; mrs %2, elr_el1" : "=r"(esr), "=r"(far), "=r"(elr));
	uart_puts("\nGPU3D exception slot=");
	uart_dec(slot);
	uart_kv(" esr=", esr);
	uart_kv(" far=", far);
	uart_kv(" elr=", elr);
	uart_kv(" x0=", regs[0]);
	uart_puts("\nGPU3D-DONE fail\n");
	power_off();
}

/* ---- memory: bump allocator above the image, identity-mapped MMU ------------------------- */

static uint64_t ram_base, ram_size, heap, heap_end;

static void *dma_alloc(void *arg, size_t size, uint64_t *pa)
{
	(void)arg;
	uint64_t p = (heap + 4095) & ~4095ull;
	if (p + size > heap_end)
		return NULL;
	heap = p + size;
	memset((void *)p, 0, size);
	*pa = p;
	return (void *)p;
}

/* 4K granule, 48-bit VA: one L0 table, L1 tables of 1 GB blocks for the first 2 TB */
static uint64_t l0[512] __attribute__((aligned(4096)));
static uint64_t l1[4][512] __attribute__((aligned(4096)));

#define ATTR_DEVICE     (0ull << 2)     /* MAIR index 0: Device-nGnRE */
#define ATTR_NORMAL     (1ull << 2)     /* MAIR index 1: Normal WB */
#define BLK_AF          (1ull << 10)
#define BLK_SH_INNER    (3ull << 8)
#define BLK_XN          (3ull << 53)

static void mmu_on(void)
{
	for (int t = 0; t < 4; t++) {
		l0[t] = (uint64_t)(uintptr_t)l1[t] | 3;
		for (int i = 0; i < 512; i++) {
			uint64_t pa = ((uint64_t)t * 512 + (uint64_t)i) << 30;
			bool ram = pa + (1ull << 30) > ram_base && pa < ram_base + ram_size;
			l1[t][i] = pa | 1 | BLK_AF | (ram ? ATTR_NORMAL | BLK_SH_INNER : ATTR_DEVICE | BLK_XN);
		}
	}
	uint64_t mmfr0, ips;
	__asm__ volatile("mrs %0, id_aa64mmfr0_el1" : "=r"(mmfr0));
	ips = mmfr0 & 7;
	uint64_t mair = 0x04ull | (0xffull << 8);
	uint64_t tcr = 16 /* T0SZ: 48 bits */ | (1ull << 8) | (1ull << 10) | (3ull << 12) /* WB, inner */ |
	    (1ull << 23) /* EPD1 */ | (ips << 32);
	__asm__ volatile(
	    "dsb sy\n"
	    "msr mair_el1, %0\n"
	    "msr tcr_el1, %1\n"
	    "msr ttbr0_el1, %2\n"
	    "isb\n"
	    "tlbi vmalle1\n"
	    "dsb sy\n"
	    "isb\n"
	    "mrs x9, sctlr_el1\n"
	    "orr x9, x9, #(1 << 0)\n"   /* M */
	    "orr x9, x9, #(1 << 2)\n"   /* C */
	    "orr x9, x9, #(1 << 12)\n"  /* I */
	    "msr sctlr_el1, x9\n"
	    "isb\n"
	    :: "r"(mair), "r"(tcr), "r"((uint64_t)(uintptr_t)l0) : "x9", "memory");
}

/* ---- PCI ------------------------------------------------------------------------------- */

static uintptr_t cfg_base;
static int cfg_ecam;
static uint64_t win32_cpu, win32_pci, win32_size, win64_cpu, win64_pci, win64_size, win32_next, win64_next;

static uintptr_t cfg(int dev, int fn)
{
	return cfg_base + (cfg_ecam ? ((uintptr_t)dev << 15 | (uintptr_t)fn << 12)
	                            : ((uintptr_t)dev << 11 | (uintptr_t)fn << 8));
}
static uint32_t cfg32(uintptr_t c, uint32_t off) { return *(volatile uint32_t *)(c + off); }
static uint16_t cfg16(uintptr_t c, uint32_t off) { return *(volatile uint16_t *)(c + off); }
static uint8_t cfg8(uintptr_t c, uint32_t off) { return *(volatile uint8_t *)(c + off); }
static void wcfg32(uintptr_t c, uint32_t off, uint32_t v) { *(volatile uint32_t *)(c + off) = v; }
static void wcfg16(uintptr_t c, uint32_t off, uint16_t v) { *(volatile uint16_t *)(c + off) = v; }

static int pci_from_fdt(const struct fdt *f)
{
	int node = fdt_find_compatible(f, -1, "pci-host-cam-generic");
	cfg_ecam = 0;
	if (node < 0) {
		node = fdt_find_compatible(f, -1, "pci-host-ecam-generic");
		cfg_ecam = 1;
	}
	if (node < 0)
		return -1;
	uintptr_t reg = fdt_prop(f, node, "reg", NULL);
	cfg_base = (uintptr_t)fdt_cells(reg, 2);
	uint32_t len;
	uintptr_t r = fdt_prop(f, node, "ranges", &len);
	for (uint32_t o = 0; r && o + 28 <= len; o += 28) {
		uint32_t space = fdt_be32(r + o) >> 24 & 3;
		uint64_t pci = fdt_cells(r + o + 4, 2), cpu = fdt_cells(r + o + 12, 2), sz = fdt_cells(r + o + 20, 2);
		if (space == 2 && !win32_size)
			win32_pci = pci, win32_cpu = cpu, win32_size = sz;
		else if (space == 3 && !win64_size)
			win64_pci = pci, win64_cpu = cpu, win64_size = sz;
	}
	win32_next = win32_pci;
	win64_next = win64_pci;
	uart_kv("GPU3D pci cfg=", cfg_base);
	uart_puts(cfg_ecam ? " ecam" : " cam");
	uart_kv(" mem32=", win32_cpu);
	uart_kv("+", win32_size);
	uart_kv(" mem64=", win64_cpu);
	uart_kv("+", win64_size);
	uart_putc('\n');
	return 0;
}

/* CPU address of BAR b, assigning one from the bridge's windows when the VMM left it at 0. */
static uint64_t bar_addr(uintptr_t c, int b)
{
	uint32_t lo = cfg32(c, 0x10 + 4 * (uint32_t)b);
	if (lo & 1)
		return 0;       /* I/O */
	bool is64 = (lo & 6) == 4;
	uint64_t a = lo & ~0xfull;
	if (is64)
		a |= (uint64_t)cfg32(c, 0x14 + 4 * (uint32_t)b) << 32;
	if (a) {
		/* bus address == CPU address on both machines' windows that matter here */
		if (a >= win32_pci && a < win32_pci + win32_size)
			return a - win32_pci + win32_cpu;
		if (a >= win64_pci && a < win64_pci + win64_size)
			return a - win64_pci + win64_cpu;
		return a;
	}
	/* size it */
	wcfg32(c, 0x10 + 4 * (uint32_t)b, 0xffffffff);
	uint64_t mask = cfg32(c, 0x10 + 4 * (uint32_t)b) & ~0xfull;
	if (is64) {
		wcfg32(c, 0x14 + 4 * (uint32_t)b, 0xffffffff);
		mask |= (uint64_t)cfg32(c, 0x14 + 4 * (uint32_t)b) << 32;
	} else {
		mask |= 0xffffffff00000000ull;
	}
	uint64_t size = ~mask + 1;
	if (!size)
		return 0;
	uint64_t *next = is64 && win64_size ? &win64_next : &win32_next;
	uint64_t pci = (*next + size - 1) & ~(size - 1);
	*next = pci + size;
	wcfg32(c, 0x10 + 4 * (uint32_t)b, (uint32_t)pci | (lo & 0xf));
	if (is64)
		wcfg32(c, 0x14 + 4 * (uint32_t)b, (uint32_t)(pci >> 32));
	return next == &win64_next ? pci - win64_pci + win64_cpu : pci - win32_pci + win32_cpu;
}

/* ---- virtio-pci 1.x -------------------------------------------------------------------- */

#define VIRTIO_PCI_CAP_COMMON   1
#define VIRTIO_PCI_CAP_NOTIFY   2
#define VIRTIO_PCI_CAP_ISR      3
#define VIRTIO_PCI_CAP_DEVICE   4
#define VIRTIO_PCI_CAP_SHM      8

struct vdev {
	uintptr_t pci;
	volatile uint8_t *common, *notify, *device;
	uint32_t notify_mult;
	uint64_t shm_pa, shm_len;
	uint64_t features;
};

#define C8(d, o)  (*(volatile uint8_t *)((d)->common + (o)))
#define C16(d, o) (*(volatile uint16_t *)((d)->common + (o)))
#define C32(d, o) (*(volatile uint32_t *)((d)->common + (o)))

struct vring_desc { uint64_t addr; uint32_t len; uint16_t flags, next; };
struct vq {
	uint16_t size, last_used, notify_off;
	struct vring_desc *desc;
	volatile uint16_t *avail;       /* flags, idx, ring[size] */
	volatile uint32_t *used;        /* flags|idx as one word, then {id, len} pairs */
	uint64_t desc_pa;
	/* the command and the reply go through these */
	uint8_t *cmd, *resp;
	uint64_t cmd_pa, resp_pa;
};

static struct vdev gpu;
static struct vq ctrlq, cursorq;   /* crosvm activates virtio-gpu only with both */

static int find_virtio_gpu(void)
{
	for (int dev = 0; dev < 32; dev++) {
		uintptr_t c = cfg(dev, 0);
		uint32_t id = cfg32(c, 0);
		if ((id & 0xffff) == 0xffff)
			continue;
		uart_puts("GPU3D pci ");
		uart_dec((uint64_t)dev);
		uart_kv(" id=", id);
		uart_kv(" class=", cfg32(c, 8) >> 8);
		uart_putc('\n');
		if (id != 0x10501af4)
			continue;
		gpu.pci = c;
		/* BARs first (assignment needs decoding off), then memory + bus master on */
		uint64_t bars[6] = { 0 };
		for (int b = 0; b < 6; b++) {
			uint32_t lo = cfg32(c, 0x10 + 4 * (uint32_t)b);
			bars[b] = bar_addr(c, b);
			if ((lo & 7) == 4)
				b++;
		}
		wcfg16(c, 4, cfg16(c, 4) | 0x6);
		for (uint8_t p = cfg8(c, 0x34) & ~3; p; p = cfg8(c, p + 1) & ~3) {
			if (cfg8(c, p) != 0x09)
				continue;
			uint8_t type = cfg8(c, p + 3), bar = cfg8(c, p + 4), shmid = cfg8(c, p + 5);
			uint64_t off = cfg32(c, p + 8), len = cfg32(c, p + 12);
			uintptr_t va = (uintptr_t)(bars[bar] + off);
			switch (type) {
			case VIRTIO_PCI_CAP_COMMON: gpu.common = (volatile uint8_t *)va; break;
			case VIRTIO_PCI_CAP_NOTIFY:
				gpu.notify = (volatile uint8_t *)va;
				gpu.notify_mult = cfg32(c, p + 16);
				break;
			case VIRTIO_PCI_CAP_DEVICE: gpu.device = (volatile uint8_t *)va; break;
			case VIRTIO_PCI_CAP_SHM:
				/* 64-bit offset/length: high halves at +16/+20 */
				off |= (uint64_t)cfg32(c, p + 16) << 32;
				len |= (uint64_t)cfg32(c, p + 20) << 32;
				if (shmid == 1) {
					gpu.shm_pa = bars[bar] + off;
					gpu.shm_len = len;
				}
				break;
			}
		}
		uart_kv("GPU3D virtio-gpu common=", (uintptr_t)gpu.common);
		uart_kv(" notify=", (uintptr_t)gpu.notify);
		uart_kv(" device=", (uintptr_t)gpu.device);
		uart_kv(" host_visible_shm=", gpu.shm_pa);
		uart_kv("+", gpu.shm_len);
		uart_putc('\n');
		return gpu.common && gpu.notify && gpu.device ? 0 : -1;
	}
	return -1;
}

static int vq_setup(struct vdev *d, struct vq *q, uint16_t index)
{
	C16(d, 0x16) = index;
	uint16_t max = C16(d, 0x18);
	if (!max)
		return -1;
	uint16_t n = max < 16 ? max : 16;
	C16(d, 0x18) = n;
	q->size = n;
	uint64_t pa;
	uint8_t *mem = dma_alloc(NULL, 4096 * 2, &pa);
	if (!mem)
		return -1;
	q->desc = (struct vring_desc *)mem;
	q->desc_pa = pa;
	q->avail = (volatile uint16_t *)(mem + 16 * n);
	q->used = (volatile uint32_t *)(mem + 4096);
	C16(d, 0x1a) = 0xffff;  /* no MSI-X vector: polled */
	*(volatile uint32_t *)(d->common + 0x20) = (uint32_t)pa;
	*(volatile uint32_t *)(d->common + 0x24) = (uint32_t)(pa >> 32);
	*(volatile uint32_t *)(d->common + 0x28) = (uint32_t)(pa + 16 * n);
	*(volatile uint32_t *)(d->common + 0x2c) = (uint32_t)((pa + 16 * n) >> 32);
	*(volatile uint32_t *)(d->common + 0x30) = (uint32_t)(pa + 4096);
	*(volatile uint32_t *)(d->common + 0x34) = (uint32_t)((pa + 4096) >> 32);
	q->notify_off = C16(d, 0x1e);
	C16(d, 0x1c) = 1;
	q->cmd = dma_alloc(NULL, 4096, &q->cmd_pa);
	q->resp = dma_alloc(NULL, 4096, &q->resp_pa);
	return q->cmd && q->resp ? 0 : -1;
}

static uint64_t ctrl_wait_ns, ctrl_count;

/* ops->ctrl: one command, one reply, polled */
static int ctrl_submit(void *arg, const void *cmd, uint32_t cmd_len, void *resp, uint32_t resp_len)
{
	struct vq *q = arg;
	if (cmd_len > 4096 || resp_len > 4096)
		return 7;
	memcpy(q->cmd, cmd, cmd_len);
	memset(q->resp, 0, resp_len);
	q->desc[0] = (struct vring_desc){ q->cmd_pa, cmd_len, 1 /* NEXT */, 1 };
	q->desc[1] = (struct vring_desc){ q->resp_pa, resp_len, 2 /* WRITE */, 0 };
	uint16_t idx = q->avail[1];
	q->avail[2 + idx % q->size] = 0;
	__asm__ volatile("dmb ishst" ::: "memory");
	q->avail[1] = idx + 1;
	__asm__ volatile("dsb sy" ::: "memory");
	*(volatile uint16_t *)(gpu.notify + (uint32_t)q->notify_off * gpu.notify_mult) = 0;
	uint64_t t0 = now_ns();
	for (;;) {
		__asm__ volatile("dmb ish" ::: "memory");
		uint16_t used_idx = (uint16_t)(q->used[0] >> 16);
		if (used_idx != q->last_used)
			break;
		if (now_ns() - t0 > 20000000000ull) {
			uart_puts("GPU3D control queue timeout\n");
			return 60;      /* ETIMEDOUT */
		}
	}
	q->last_used++;
	__asm__ volatile("dmb ish" ::: "memory");
	memcpy(resp, q->resp, resp_len);
	ctrl_wait_ns += now_ns() - t0;
	ctrl_count++;
	return 0;
}

static void log_line(void *arg, const char *line)
{
	(void)arg;
	uart_puts(line);
	uart_putc('\n');
}

static uint64_t ops_now(void *arg)
{
	(void)arg;
	return now_ns();
}

/* ---- main ------------------------------------------------------------------------------ */

static int virtio_gpu_init(uint64_t want)
{
	struct vdev *d = &gpu;
	C8(d, 0x14) = 0;
	while (C8(d, 0x14))
		;
	C8(d, 0x14) = 1;
	C8(d, 0x14) = 1 | 2;
	C32(d, 0x00) = 0;
	uint64_t offered = C32(d, 0x04);
	C32(d, 0x00) = 1;
	offered |= (uint64_t)C32(d, 0x04) << 32;
	uint64_t take = offered & (want | (1ull << 32) | (1ull << 33));
	C32(d, 0x08) = 0;
	C32(d, 0x0c) = (uint32_t)take;
	C32(d, 0x08) = 1;
	C32(d, 0x0c) = (uint32_t)(take >> 32);
	C8(d, 0x14) = 1 | 2 | 8;
	uart_kv("GPU3D features offered=", offered);
	uart_kv(" taken=", take);
	uart_putc('\n');
	if (!(C8(d, 0x14) & 8)) {
		say("GPU3D FEATURES_OK refused\n");
		return -1;
	}
	d->features = take;
	if (vq_setup(d, &ctrlq, 0) || vq_setup(d, &cursorq, 1)) {
		say("GPU3D queue setup failed\n");
		return -1;
	}
	C8(d, 0x14) = 1 | 2 | 8 | 4;
	return 0;
}

static const char *bootargs;

static const char *arg(const char *key)
{
	const char *s = bootargs;
	while (s && *s) {
		while (*s == ' ')
			s++;
		if (startswith(s, key))
			return s + strlen(key);
		while (*s && *s != ' ')
			s++;
	}
	return NULL;
}

static struct vgpu3d g3;

void bench_composite(struct vgpu3d *g, const struct vgpu3d_selftest *t, uint32_t layers);
uint64_t bench_now_ns(void) { return now_ns(); }
void *bench_alloc(size_t size) { uint64_t pa; return dma_alloc(NULL, size, &pa); }

void avfboot_main(uintptr_t fdt_pa)
{
	struct fdt f;
	__asm__ volatile("mrs %0, cntfrq_el0" : "=r"(cntfrq));
	/* FP/SIMD on at EL1 (CPACR_EL1.FPEN): cpublend.c uses NEON */
	__asm__ volatile("mrs x9, cpacr_el1; orr x9, x9, #(3 << 20); msr cpacr_el1, x9; isb" ::: "x9");
	if (!fdt_open(&f, fdt_pa))
		power_off();
	/* console: crosvm's ns16550a at 0x3f8, or QEMU's PL011 */
	int n = fdt_find_compatible(&f, -1, "ns16550a");
	if (n >= 0)
		uart_init(UART_8250, (uintptr_t)fdt_cells(fdt_prop(&f, n, "reg", NULL), 2));
	else if ((n = fdt_find_compatible(&f, -1, "arm,pl011")) >= 0)
		uart_init(UART_PL011, (uintptr_t)fdt_cells(fdt_prop(&f, n, "reg", NULL), 2));
	say("\nGPU3D probe: virtio-gpu 3D (gfxstream) from a bare-metal AVF guest\n");
	bootargs = (const char *)fdt_prop(&f, fdt_path(&f, "/chosen"), "bootargs", NULL);
	if (bootargs) {
		say("GPU3D bootargs: ");
		say(bootargs);
		say("\n");
	}
	int mem = fdt_path(&f, "/memory");
	uintptr_t reg = fdt_prop(&f, mem, "reg", NULL);
	ram_base = fdt_cells(reg, 2);
	ram_size = fdt_cells(reg + 8, 2);
	heap = ((uint64_t)(uintptr_t)_end + 0x1fffff) & ~0x1fffffull;
	heap_end = ram_base + ram_size;
	if (fdt_pa > heap && fdt_pa < heap_end)
		heap_end = fdt_pa & ~0xfffull;
	uart_kv("GPU3D ram=", ram_base);
	uart_kv("+", ram_size);
	uart_kv(" heap=", heap);
	uart_kv("..", heap_end);
	uart_kv(" fdt=", fdt_pa);
	uart_putc('\n');
	mmu_on();
	say("GPU3D mmu on\n");

	uint32_t w = 828, h = 1792, capset = VIRTIO_GPU_CAPSET_GFXSTREAM_GLES;
	const char *a;
	if ((a = arg("gpu3d.size=")) != NULL) {
		w = parse_u32(&a);
		if (*a == 'x')
			a++, h = parse_u32(&a);
	}
	if ((a = arg("gpu3d.capset=")) != NULL)
		capset = parse_u32(&a);

	int ok = 0;
	if (pci_from_fdt(&f) || find_virtio_gpu()) {
		say("GPU3D no virtio-gpu PCI function (is a GpuConfig set on the VM?)\n");
		goto out;
	}
	if (virtio_gpu_init((1ull << VIRTIO_GPU_F_VIRGL) | (1ull << VIRTIO_GPU_F_CONTEXT_INIT) |
	    (1ull << VIRTIO_GPU_F_RESOURCE_BLOB) | (1ull << VIRTIO_GPU_F_EDID)))
		goto out;
	volatile uint32_t *cfgsp = (volatile uint32_t *)gpu.device;
	uint32_t scanouts = cfgsp[2], capsets = cfgsp[3];
	say_kv("GPU3D num_scanouts=", scanouts);
	say_kv("GPU3D num_capsets=", capsets);

	g3.ops = (struct vgpu3d_ops){ &ctrlq, ctrl_submit, dma_alloc, log_line, ops_now };
	{
		struct virtio_gpu_ctrl_hdr c = { .type = VIRTIO_GPU_CMD_GET_DISPLAY_INFO };
		struct virtio_gpu_resp_display_info r;
		if (ctrl_submit(&ctrlq, &c, sizeof(c), &r, sizeof(r)) == 0)
			for (uint32_t i = 0; i < scanouts && i < 16; i++) {
				uart_puts("GPU3D scanout ");
				uart_dec(i);
				uart_puts(": ");
				uart_dec(r.pmodes[i].r.width);
				uart_putc('x');
				uart_dec(r.pmodes[i].r.height);
				uart_puts(r.pmodes[i].enabled ? " enabled\n" : " disabled\n");
			}
	}
	if (vgpu3d_query(&g3, gpu.features, capsets))
		goto out;
	if (capset && !vgpu3d_has_capset(&g3, capset)) {
		say("GPU3D the requested capset is not offered; using the host default\n");
		capset = 0;
	}
	uint64_t t0 = now_ns();
	if (vgpu3d_open(&g3, capset, 1 << 20))
		goto out;
	say_kv("GPU3D open_us=", (now_ns() - t0) / 1000);

	static struct vgpu3d_selftest t;
	uint64_t pa;
	t.width = w;
	t.height = h;
	t.pixels = dma_alloc(NULL, (size_t)w * h * 4, &pa);
	if (!t.pixels) {
		say("GPU3D no memory for the picture\n");
		goto out;
	}
	if (scanouts)
		t.scanout_res = vgpu3d_create_scanout_resource(&g3, w, h);
	t0 = now_ns();
	if (vgpu3d_selftest(&g3, &t) == 0) {
		say_kv("GPU3D selftest_ms=", (now_ns() - t0) / 1000000);
		ok = t.checks_passed == t.checks_total;
		if (!arg("gpu3d.nobench")) {
			bench_composite(&g3, &t, 4);
			bench_composite(&g3, &t, 12);
		}
		if (t.scanout_res && !g3.err) {
			int r = vgpu3d_present(&g3, 0, t.scanout_res, w, h);
			say(r ? "GPU3D present failed\n" : "GPU3D presented on scanout 0\n");
		}
	}
	say_kv("GPU3D ctrl_commands=", ctrl_count);
	say_kv("GPU3D ctrl_avg_us=", ctrl_count ? ctrl_wait_ns / ctrl_count / 1000 : 0);
out:
	say(ok ? "GPU3D-DONE pass\n" : "GPU3D-DONE fail\n");
	/* give a display a moment to show the picture before the VM goes away */
	if (ok && arg("gpu3d.hold=")) {
		const char *s = arg("gpu3d.hold=");
		uint64_t until = now_ns() + (uint64_t)parse_u32(&s) * 1000000000ull;
		while (now_ns() < until)
			;
	}
	power_off();
}
