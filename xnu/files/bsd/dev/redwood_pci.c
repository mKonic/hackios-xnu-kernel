/*
 * A generic PCI host bridge (sys/redwood_pci.h): flat configuration space, either ECAM
 * (QEMU virt's "pci-host-ecam-generic": bus << 20 | device << 15 | function << 12) or CAM
 * (crosvm's "pci-host-cam-generic": bus << 16 | device << 11 | function << 8, 256 bytes of
 * configuration space per function), and one or more memory windows for the BARs.
 *
 * The loader describes it in the device tree node /pci0, every value little-endian and every
 * address absolute (not relative to /arm-io):
 *
 *   compatible       "pci-host-ecam-generic" or "pci-host-cam-generic"
 *   ecam             u64 base, u64 size        configuration space, starting at the first bus
 *   bus-range        u32 first, u32 last       (default 0, 0)
 *   pci-ranges       { u32 space, u32 prefetchable, u64 pci address, u64 cpu address, u64 size }...
 *                    space: 1 I/O, 2 32-bit memory, 3 64-bit memory (FDT "ranges" phys.hi bits 25:24)
 *   intx-map         { u32 phys.hi, u32 pin, u32 GIC INTID, u32 edge }...
 *                    phys.hi = bus << 16 | device << 11 | function << 8, pin 1..4 (FDT "interrupt-map"
 *                    with the GIC's SPI number already turned into an INTID: SPI n = INTID 32 + n)
 *   intx-mask        u32 phys.hi mask, u32 pin mask   (FDT "interrupt-map-mask")
 *   probe-only       u32 1: every BAR is the VMM's and stays as it is (crosvm: linux,pci-probe-only)
 *   firmware-devfn   u32 (bus << 8 | device << 3 | function)...: functions the firmware still uses
 *                    (the loader's own disk under UEFI), which drivers must leave alone
 *
 * Only the first bus is looked at: QEMU virt (without root ports) and crosvm put every device
 * there. Bridges are listed but not followed. A BAR that already holds an address inside a
 * window is kept (the firmware or the VMM placed it); the others are given space above every
 * kept one, from the 32-bit window when they fit there. I/O BARs are never enabled. MSI and
 * MSI-X stay off: interrupts are INTx (redwood_virtio.c), whose GIC INTID comes from the map.
 *
 * Nothing here allocates or locks, so the console can enumerate from serial_init(); the device
 * table is a static array, filled once.
 */
#include <mach/mach_types.h>
#include <mach/vm_param.h>
#include <libkern/libkern.h>
#include <pexpert/pexpert.h>
#include <pexpert/device_tree.h>
#include <sys/errno.h>
#include <sys/systm.h>
#include <sys/redwood_pci.h>

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);

#define PCI_VENDOR_ID           0x00
#define PCI_DEVICE_ID           0x02
#define PCI_COMMAND             0x04
#define  PCI_COMMAND_MEMORY     0x0002
#define  PCI_COMMAND_MASTER     0x0004
#define  PCI_COMMAND_INTX_OFF   0x0400
#define PCI_STATUS              0x06
#define  PCI_STATUS_CAP_LIST    0x0010
#define PCI_CLASS_REVISION      0x08
#define PCI_HEADER_TYPE         0x0e
#define PCI_BAR0                0x10
#define PCI_SUBSYS_VENDOR       0x2c
#define PCI_SUBSYS_ID           0x2e
#define PCI_CAP_PTR             0x34
#define PCI_INTERRUPT_PIN       0x3d

#define SPACE_IO                1
#define SPACE_MEM32             2
#define SPACE_MEM64             3

#define MAX_WINDOWS             4
#define MAX_INTX                64
#define MAX_FIRMWARE            8

struct window {
	uint32_t        space;
	bool            prefetch;
	uint64_t        pci, cpu, size;
	uint64_t        next;           /* allocation pointer, a PCI address */
};

static struct {
	bool            probed, present, dumped;
	uintptr_t       cfg;            /* the first bus */
	uint64_t        cfg_pa;
	uint32_t        shift;          /* 20 ECAM, 16 CAM */
	uint32_t        bus;
	bool            probe_only;
	struct window   win[MAX_WINDOWS];
	unsigned        nwin;
	struct { uint32_t hi, pin, intid, edge; } intx[MAX_INTX];
	unsigned        nintx;
	uint32_t        mask_hi, mask_pin;
	uint32_t        firmware[MAX_FIRMWARE];
	unsigned        nfirmware;
	struct redwood_pci_dev dev[REDWOOD_PCI_MAX_DEVS];
	unsigned        ndev;
	unsigned        skipped;        /* functions beyond REDWOOD_PCI_MAX_DEVS */
} pci;

/* ---- configuration space ---- */

uint8_t
redwood_pci_cfg_read8(const struct redwood_pci_dev *d, uint32_t off)
{
	return off < d->cfg_size ? *(volatile uint8_t *)(d->cfg + off) : 0xff;
}

uint16_t
redwood_pci_cfg_read16(const struct redwood_pci_dev *d, uint32_t off)
{
	return off + 1 < d->cfg_size ? *(volatile uint16_t *)(d->cfg + (off & ~1u)) : 0xffff;
}

uint32_t
redwood_pci_cfg_read32(const struct redwood_pci_dev *d, uint32_t off)
{
	return off + 3 < d->cfg_size ? *(volatile uint32_t *)(d->cfg + (off & ~3u)) : 0xffffffff;
}

void
redwood_pci_cfg_write8(const struct redwood_pci_dev *d, uint32_t off, uint8_t v)
{
	if (off < d->cfg_size) {
		*(volatile uint8_t *)(d->cfg + off) = v;
	}
}

void
redwood_pci_cfg_write16(const struct redwood_pci_dev *d, uint32_t off, uint16_t v)
{
	if (off + 1 < d->cfg_size) {
		*(volatile uint16_t *)(d->cfg + (off & ~1u)) = v;
	}
}

void
redwood_pci_cfg_write32(const struct redwood_pci_dev *d, uint32_t off, uint32_t v)
{
	if (off + 3 < d->cfg_size) {
		*(volatile uint32_t *)(d->cfg + (off & ~3u)) = v;
	}
}

uint8_t
redwood_pci_find_cap(const struct redwood_pci_dev *d, uint8_t cap_id, uint8_t after)
{
	uint8_t at;

	if (!(redwood_pci_cfg_read16(d, PCI_STATUS) & PCI_STATUS_CAP_LIST)) {
		return 0;
	}
	at = after ? redwood_pci_cfg_read8(d, after + 1) : redwood_pci_cfg_read8(d, PCI_CAP_PTR);
	for (int guard = 0; at >= 0x40 && guard < 48; guard++) {
		at &= ~3;
		if (redwood_pci_cfg_read8(d, at) == cap_id) {
			return at;
		}
		at = redwood_pci_cfg_read8(d, at + 1);
	}
	return 0;
}

uintptr_t
redwood_pci_map_bar(const struct redwood_pci_dev *d, unsigned bar, uint64_t off, uint64_t size)
{
	if (bar >= 6 || d->bar_pa[bar] == 0 || (d->bar_flags[bar] & REDWOOD_PCI_BAR_IO) ||
	    off > d->bar_size[bar] || size > d->bar_size[bar] - off || size == 0) {
		return 0;
	}
	return (uintptr_t)ml_io_map((vm_offset_t)(d->bar_pa[bar] + off), (vm_size_t)size);
}

void
redwood_pci_enable(const struct redwood_pci_dev *d, bool intx)
{
	uint16_t cmd = redwood_pci_cfg_read16(d, PCI_COMMAND);

	cmd |= PCI_COMMAND_MEMORY | PCI_COMMAND_MASTER;
	cmd &= ~0x0001;                 /* never I/O space */
	if (intx) {
		cmd &= ~PCI_COMMAND_INTX_OFF;
	} else {
		cmd |= PCI_COMMAND_INTX_OFF;
	}
	redwood_pci_cfg_write16(d, PCI_COMMAND, cmd);
}

unsigned
redwood_pci_count(void)
{
	return pci.ndev;
}

struct redwood_pci_dev *
redwood_pci_device(unsigned index)
{
	return index < pci.ndev ? &pci.dev[index] : NULL;
}

/* ---- the device tree node ---- */

static bool
read_node(void)
{
	DTEntry node;
	void const *prop;
	unsigned int size;

	if (SecureDTLookupEntry(NULL, "/pci0", &node) != kSuccess ||
	    SecureDTGetProperty(node, "ecam", &prop, &size) != kSuccess || size < 16) {
		return false;
	}
	uint64_t const *ecam = prop;
	pci.cfg_pa = ecam[0];
	pci.shift = 20;
	if (SecureDTGetProperty(node, "compatible", &prop, &size) == kSuccess && size > 0 &&
	    strncmp(prop, "pci-host-cam-generic", size) == 0) {
		pci.shift = 16;
	}
	if (SecureDTGetProperty(node, "bus-range", &prop, &size) == kSuccess && size >= 4) {
		pci.bus = ((uint32_t const *)prop)[0];
	}
	if ((ecam[1] >> pci.shift) == 0) {
		return false;           /* not even the first bus */
	}
	if (SecureDTGetProperty(node, "probe-only", &prop, &size) == kSuccess && size >= 4) {
		pci.probe_only = ((uint32_t const *)prop)[0] != 0;
	}
	if (SecureDTGetProperty(node, "pci-ranges", &prop, &size) == kSuccess) {
		struct { uint32_t space, prefetch; uint64_t pci, cpu, size; } const *r = prop;
		for (unsigned i = 0; i < size / sizeof(*r) && pci.nwin < MAX_WINDOWS; i++) {
			if (r[i].space != SPACE_MEM32 && r[i].space != SPACE_MEM64) {
				continue;
			}
			struct window *w = &pci.win[pci.nwin++];
			w->space = r[i].space;
			w->prefetch = r[i].prefetch != 0;
			w->pci = r[i].pci;
			w->cpu = r[i].cpu;
			w->size = r[i].size;
			w->next = w->pci;
		}
	}
	if (SecureDTGetProperty(node, "intx-map", &prop, &size) == kSuccess) {
		uint32_t const *m = prop;
		for (unsigned i = 0; i < size / 16 && pci.nintx < MAX_INTX; i++, pci.nintx++) {
			pci.intx[pci.nintx].hi = m[4 * i];
			pci.intx[pci.nintx].pin = m[4 * i + 1];
			pci.intx[pci.nintx].intid = m[4 * i + 2];
			pci.intx[pci.nintx].edge = m[4 * i + 3];
		}
	}
	pci.mask_hi = 0xffff00;
	pci.mask_pin = 7;
	if (SecureDTGetProperty(node, "intx-mask", &prop, &size) == kSuccess && size >= 8) {
		pci.mask_hi = ((uint32_t const *)prop)[0];
		pci.mask_pin = ((uint32_t const *)prop)[1];
	}
	if (SecureDTGetProperty(node, "firmware-devfn", &prop, &size) == kSuccess) {
		for (unsigned i = 0; i < size / 4 && pci.nfirmware < MAX_FIRMWARE; i++) {
			pci.firmware[pci.nfirmware++] = ((uint32_t const *)prop)[i];
		}
	}
	return true;
}

/* ---- enumeration ---- */

static struct window *
window_of(uint64_t pci_addr, uint64_t size)
{
	for (unsigned i = 0; i < pci.nwin; i++) {
		struct window *w = &pci.win[i];
		if (pci_addr >= w->pci && pci_addr - w->pci < w->size && size <= w->size - (pci_addr - w->pci)) {
			return w;
		}
	}
	return NULL;
}

/* Size the BARs of a type 0 function; keep those already placed, note the rest. */
static void
size_bars(struct redwood_pci_dev *d, uint64_t bus_addr[6])
{
	uint16_t cmd = redwood_pci_cfg_read16(d, PCI_COMMAND);

	/* no decoding while the BARs read back their size */
	redwood_pci_cfg_write16(d, PCI_COMMAND, cmd & ~(PCI_COMMAND_MEMORY | 0x0001));
	for (unsigned b = 0; b < 6; b++) {
		uint32_t off = PCI_BAR0 + 4 * b;
		uint32_t lo = redwood_pci_cfg_read32(d, off), hi = 0, slo, shi = 0xffffffff;
		uint64_t addr, mask;

		bus_addr[b] = 0;
		if (lo & 1) {
			d->bar_flags[b] = REDWOOD_PCI_BAR_IO;
			redwood_pci_cfg_write32(d, off, 0xffffffff);
			slo = redwood_pci_cfg_read32(d, off);
			redwood_pci_cfg_write32(d, off, lo);
			d->bar_size[b] = (slo & ~3u) ? (uint32_t)(~(slo & ~3u) + 1) & 0xffff : 0;
			continue;
		}
		bool is64 = ((lo >> 1) & 3) == 2;
		if (is64 && b == 5) {
			break;
		}
		redwood_pci_cfg_write32(d, off, 0xffffffff);
		slo = redwood_pci_cfg_read32(d, off);
		redwood_pci_cfg_write32(d, off, lo);
		if (is64) {
			hi = redwood_pci_cfg_read32(d, off + 4);
			redwood_pci_cfg_write32(d, off + 4, 0xffffffff);
			shi = redwood_pci_cfg_read32(d, off + 4);
			redwood_pci_cfg_write32(d, off + 4, hi);
		}
		mask = ((uint64_t)shi << 32 | (slo & ~0xfu));
		if (!is64) {
			mask |= 0xffffffff00000000ull;
		}
		if ((slo & ~0xfu) == 0 && (!is64 || shi == 0)) {
			if (is64) {
				b++;
			}
			continue;       /* not implemented */
		}
		d->bar_size[b] = ~mask + 1;
		d->bar_flags[b] = (is64 ? REDWOOD_PCI_BAR_MEM64 : 0) | ((lo & 8) ? REDWOOD_PCI_BAR_PREFETCH : 0);
		addr = ((uint64_t)hi << 32) | (lo & ~0xfu);
		struct window *w = addr ? window_of(addr, d->bar_size[b]) : NULL;
		if (w != NULL || (pci.probe_only && addr != 0)) {
			bus_addr[b] = addr;
			d->bar_pa[b] = w ? w->cpu + (addr - w->pci) : addr;
			if (w && addr + d->bar_size[b] > w->next) {
				w->next = addr + d->bar_size[b];
			}
		}
		if (is64) {
			b++;
		}
	}
	redwood_pci_cfg_write16(d, PCI_COMMAND, cmd);
}

/* Place a BAR nobody placed: the 32-bit window when it fits, else a 64-bit one. */
static void
place_bar(struct redwood_pci_dev *d, unsigned b)
{
	uint64_t size = d->bar_size[b];
	bool is64 = d->bar_flags[b] & REDWOOD_PCI_BAR_MEM64;

	for (int pass = 0; pass < 2; pass++) {
		for (unsigned i = 0; i < pci.nwin; i++) {
			struct window *w = &pci.win[i];
			if ((pass == 0) != (w->space == SPACE_MEM32) || (w->space == SPACE_MEM64 && !is64)) {
				continue;
			}
			uint64_t at = (w->next + size - 1) & ~(size - 1);
			if (at < w->pci || at - w->pci >= w->size || size > w->size - (at - w->pci) ||
			    (!is64 && ((at + size - 1) >> 32))) {
				continue;
			}
			w->next = at + size;
			redwood_pci_cfg_write32(d, PCI_BAR0 + 4 * b, (uint32_t)at);
			if (is64) {
				redwood_pci_cfg_write32(d, PCI_BAR0 + 4 * b + 4, (uint32_t)(at >> 32));
			}
			d->bar_pa[b] = w->cpu + (at - w->pci);
			return;
		}
	}
}

static void
add_function(uint32_t slot, uint32_t fn, uintptr_t cfg)
{
	struct redwood_pci_dev *d;
	uint32_t hi = pci.bus << 16 | slot << 11 | fn << 8;
	uint32_t devfn = pci.bus << 8 | slot << 3 | fn;

	if (pci.ndev == REDWOOD_PCI_MAX_DEVS) {
		pci.skipped++;
		return;
	}
	d = &pci.dev[pci.ndev++];
	d->bus = (uint8_t)pci.bus;
	d->slot = (uint8_t)slot;
	d->func = (uint8_t)fn;
	d->cfg = cfg;
	d->cfg_size = 1u << (pci.shift - 8);
	d->vendor = redwood_pci_cfg_read16(d, PCI_VENDOR_ID);
	d->device = redwood_pci_cfg_read16(d, PCI_DEVICE_ID);
	d->class_rev = redwood_pci_cfg_read32(d, PCI_CLASS_REVISION);
	for (unsigned i = 0; i < pci.nfirmware; i++) {
		d->firmware |= pci.firmware[i] == devfn;
	}
	if ((redwood_pci_cfg_read8(d, PCI_HEADER_TYPE) & 0x7f) != 0) {
		return;                 /* a bridge: listed, not followed */
	}
	d->subsys_vendor = redwood_pci_cfg_read16(d, PCI_SUBSYS_VENDOR);
	d->subsys = redwood_pci_cfg_read16(d, PCI_SUBSYS_ID);
	d->int_pin = redwood_pci_cfg_read8(d, PCI_INTERRUPT_PIN);
	if (d->int_pin >= 1 && d->int_pin <= 4) {
		for (unsigned i = 0; i < pci.nintx; i++) {
			if ((hi & pci.mask_hi) == pci.intx[i].hi && (d->int_pin & pci.mask_pin) == pci.intx[i].pin) {
				d->intid = pci.intx[i].intid;
				d->intx_edge = pci.intx[i].edge != 0;
				break;
			}
		}
	} else {
		d->int_pin = 0;
	}
}

int
redwood_pci_init(void)
{
	uint64_t bus_addr[REDWOOD_PCI_MAX_DEVS][6];

	if (pci.probed) {
		return pci.present ? 0 : ENXIO;
	}
	pci.probed = true;
	if (!read_node()) {
		return ENXIO;
	}
	pci.cfg = (uintptr_t)ml_io_map((vm_offset_t)pci.cfg_pa, (vm_size_t)1 << pci.shift);
	if (pci.cfg == 0) {
		return ENXIO;
	}
	pci.present = true;

	for (uint32_t slot = 0; slot < 32; slot++) {
		for (uint32_t fn = 0; fn < 8; fn++) {
			uintptr_t cfg = pci.cfg + ((uintptr_t)slot << (pci.shift - 5)) + ((uintptr_t)fn << (pci.shift - 8));
			uint16_t vendor = *(volatile uint16_t *)cfg;

			if (vendor == 0xffff || vendor == 0) {
				if (fn == 0) {
					break;
				}
				continue;
			}
			add_function(slot, fn, cfg);
			if (fn == 0 && !(*(volatile uint8_t *)(cfg + PCI_HEADER_TYPE) & 0x80)) {
				break;  /* not a multi-function device */
			}
		}
	}
	/* sizes first and every kept BAR, so new ones go above all of them */
	for (unsigned i = 0; i < pci.ndev; i++) {
		struct redwood_pci_dev *d = &pci.dev[i];
		if ((redwood_pci_cfg_read8(d, PCI_HEADER_TYPE) & 0x7f) == 0) {
			size_bars(d, bus_addr[i]);
		}
	}
	for (unsigned i = 0; i < pci.ndev && !pci.probe_only; i++) {
		struct redwood_pci_dev *d = &pci.dev[i];
		if (d->firmware) {
			continue;
		}
		for (unsigned b = 0; b < 6; b++) {
			if (d->bar_size[b] && d->bar_pa[b] == 0 && !(d->bar_flags[b] & REDWOOD_PCI_BAR_IO)) {
				place_bar(d, b);
			}
			if (d->bar_flags[b] & REDWOOD_PCI_BAR_MEM64) {
				b++;
			}
		}
	}
	return 0;
}

void
redwood_pci_dump(void)
{
	(void)redwood_pci_init();
	if (!pci.present || pci.dumped) {
		return;
	}
	pci.dumped = true;
	printf("pci: %s at %#llx, bus %u, %u windows, %u INTx entries%s: %u functions%s\n",
	    pci.shift == 16 ? "CAM" : "ECAM", pci.cfg_pa, pci.bus, pci.nwin, pci.nintx,
	    pci.probe_only ? ", BARs as the VMM set them" : "", pci.ndev, pci.skipped ? " (more not listed)" : "");
	for (unsigned i = 0; i < pci.ndev; i++) {
		struct redwood_pci_dev *d = &pci.dev[i];
		printf("pci: %02x:%02x.%x %04x:%04x class %06x", d->bus, d->slot, d->func, d->vendor, d->device,
		    d->class_rev >> 8);
		for (unsigned b = 0; b < 6; b++) {
			if (d->bar_size[b] && !(d->bar_flags[b] & REDWOOD_PCI_BAR_IO)) {
				printf(" bar%u %#llx+%#llx", b, d->bar_pa[b], d->bar_size[b]);
			}
		}
		if (d->int_pin) {
			printf(" INT%c -> %u", 'A' + d->int_pin - 1, d->intid);
		}
		printf("%s\n", d->firmware ? " (the firmware's)" : "");
	}
}
