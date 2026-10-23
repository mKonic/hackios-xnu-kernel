/*
 * The virtio transport layer of sys/redwood_virtio.h: virtio-pci 1.x and virtio-mmio v2
 * behind one interface, split virtqueues, interrupts through the GIC or polling.
 *
 * virtio-pci (virtio 1.2, section 4.1): the function's vendor capabilities (id 9) name the
 * BAR regions of the common configuration, notifications, the interrupt status byte and the
 * device's own configuration. Only these "modern" registers are used; a transitional device
 * (PCI id below 0x1040) is driven through them too, never through its legacy I/O BAR.
 * MSI-X is left off, so the device signals over INTx and the interrupt status byte says why;
 * reading that byte also lowers the line.
 *
 * virtio-mmio (section 4.2), version 2 only, QEMU virt's 32 slots of 0x200 bytes at
 * 0x0a000000, slot n raising SPI 16 + n (INTID 48 + n). The bus is looked at only with
 * boot-arg virtio_mmio=1 or one of virtio_blk=1, virtio_input=1, virtio_net=1, virtio_sound=1.
 *
 * Split virtqueues (section 2.7): descriptor table, driver ("available") ring and device
 * ("used") ring in one physically contiguous allocation, followed by this driver's cookie
 * per descriptor. Free descriptors are chained through their next fields. No indirect
 * descriptors, no event index: notifications are suppressed with the rings' flags only.
 *
 * Interrupts (vio_intr_enable): one gic_v3_spi_attach() per GIC line, whose handler serves
 * every device on that line (QEMU virt spreads all of PCI over four INTx lines). For each it
 * reads and acknowledges the interrupt status, collects the bits, counts and wakes waiters.
 * Level-triggered: the line drops once the status is read (PCI) or acknowledged (mmio).
 */
#include <kern/clock.h>
#include <kern/kalloc.h>
#include <kern/locks.h>
#include <kern/thread.h>
#include <kern/sched_prim.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <mach/vm_param.h>
#include <machine/atomic.h>
#include <machine/machine_routines.h>
#include <pexpert/pexpert.h>
#include <sys/errno.h>
#include <sys/param.h>
#include <sys/systm.h>
#include <sys/redwood_pci.h>
#include <sys/redwood_virtio.h>
#include <vm/vm_kern.h>

extern vm_offset_t ml_io_map(vm_offset_t phys_addr, vm_size_t size);
extern uint64_t kvtophys(vm_offset_t va);
extern int get_preemption_level(void);
extern kern_return_t gic_v3_spi_attach(uint32_t intid, bool edge, void (*handler)(void *context, uint32_t intid),
    void *context);

#define dmb_ish()       __builtin_arm_dmb(0xb)
#define dmb_ishst()     __builtin_arm_dmb(0xa)
#define dmb_ishld()     __builtin_arm_dmb(0x9)
#define dsb_sy()        __builtin_arm_dsb(0xf)

/* ---- virtio-pci ---- */
#define PCI_VENDOR_VIRTIO       0x1af4
#define PCI_CAP_VENDOR          0x09
#define PCI_CAP_MSIX            0x11
#define VIRTIO_PCI_CAP_COMMON   1
#define VIRTIO_PCI_CAP_NOTIFY   2
#define VIRTIO_PCI_CAP_ISR      3
#define VIRTIO_PCI_CAP_DEVICE   4
#define VIRTIO_PCI_CAP_SHM      8

#define COMMON_DFSELECT         0x00
#define COMMON_DF               0x04
#define COMMON_GFSELECT         0x08
#define COMMON_GF               0x0c
#define COMMON_MSIX             0x10
#define COMMON_NUMQ             0x12
#define COMMON_STATUS           0x14
#define COMMON_CFGGENERATION    0x15
#define COMMON_Q_SELECT         0x16
#define COMMON_Q_SIZE           0x18
#define COMMON_Q_MSIX           0x1a
#define COMMON_Q_ENABLE         0x1c
#define COMMON_Q_NOFF           0x1e
#define COMMON_Q_DESCLO         0x20
#define COMMON_Q_DESCHI         0x24
#define COMMON_Q_AVAILLO        0x28
#define COMMON_Q_AVAILHI        0x2c
#define COMMON_Q_USEDLO         0x30
#define COMMON_Q_USEDHI         0x34
#define VIRTIO_MSI_NO_VECTOR    0xffff

/* ---- virtio-mmio ---- */
#define MMIO_BUS                0x0a000000UL    /* QEMU virt */
#define MMIO_SLOTS              32
#define MMIO_SLOT_SIZE          0x200
#define MMIO_INTID_BASE         48              /* SPI 16 */

#define MMIO_MAGIC              0x000           /* "virt" */
#define MMIO_VERSION            0x004
#define MMIO_DEVICE_ID          0x008
#define MMIO_DEVICE_FEATURES    0x010
#define MMIO_DEVICE_FEATURES_SEL 0x014
#define MMIO_DRIVER_FEATURES    0x020
#define MMIO_DRIVER_FEATURES_SEL 0x024
#define MMIO_QUEUE_SEL          0x030
#define MMIO_QUEUE_NUM_MAX      0x034
#define MMIO_QUEUE_NUM          0x038
#define MMIO_QUEUE_READY        0x044
#define MMIO_QUEUE_NOTIFY       0x050
#define MMIO_INTERRUPT_STATUS   0x060
#define MMIO_INTERRUPT_ACK      0x064
#define MMIO_STATUS             0x070
#define MMIO_QUEUE_DESC_LOW     0x080
#define MMIO_QUEUE_DESC_HIGH    0x084
#define MMIO_QUEUE_DRIVER_LOW   0x090
#define MMIO_QUEUE_DRIVER_HIGH  0x094
#define MMIO_QUEUE_DEVICE_LOW   0x0a0
#define MMIO_QUEUE_DEVICE_HIGH  0x0a4
#define MMIO_CONFIG_GENERATION  0x0fc
#define MMIO_CONFIG             0x100

/* ---- split virtqueue ---- */
#define VIRTQ_DESC_F_NEXT       1
#define VIRTQ_DESC_F_WRITE      2
#define VIRTQ_AVAIL_F_NO_INTERRUPT 1
#define VIRTQ_USED_F_NO_NOTIFY  1
#define VIO_QUEUE_MAX           256

struct vq_desc {
	uint64_t addr;
	uint32_t len;
	uint16_t flags;
	uint16_t next;
};

struct vq_used_elem {
	uint32_t id;
	uint32_t len;
};

#define VIO_MAX_DEVS            (REDWOOD_PCI_MAX_DEVS + MMIO_SLOTS)
#define VIO_MAX_QUEUES          8
#define VIO_MAX_LINES           16

struct vio_queue {
	vio_dev_t                       *dev;
	uint16_t                        index, size;
	volatile struct vq_desc         *desc;
	volatile uint16_t               *avail;         /* flags, idx, ring[size] */
	volatile uint16_t               *used;          /* flags, idx, then used elements */
	void                            **cookie;
	uint16_t                        free_head, num_free;
	uint16_t                        avail_idx, last_used;
	uintptr_t                       notify;
	bool                            ready;
};

struct vio_dev {
	int                             transport;
	uint32_t                        type;
	char                            name[24];
	struct redwood_pci_dev          *pci;
	uintptr_t                       common, notify, isr, devcfg, mmio;
	uint32_t                        notify_mult;
	uint32_t                        intid;
	bool                            edge;
	uint64_t                        features;
	uint16_t                        nqueues;
	struct vio_queue                queue[VIO_MAX_QUEUES];
	bool                            intr_on;
	uint32_t _Atomic                seq;
	uint8_t _Atomic                 isr_pending;
	struct vio_dev                  *next_on_line;
};

static struct vio_dev vio_devs[VIO_MAX_DEVS];
static unsigned vio_ndevs;
static bool vio_probed;

struct vio_line {
	uint32_t        intid;
	struct vio_dev  *devs;          /* every device with interrupts on, on this GIC line */
};
static struct vio_line vio_lines[VIO_MAX_LINES];

static LCK_GRP_DECLARE(vio_lck_grp, "redwood-virtio");
static LCK_MTX_DECLARE(vio_intr_lock, &vio_lck_grp);

/* ---- register access ---- */

#define R8(a)           (*(volatile uint8_t *)(a))
#define R16(a)          (*(volatile uint16_t *)(a))
#define R32(a)          (*(volatile uint32_t *)(a))
#define W8(a, v)        (*(volatile uint8_t *)(a) = (v))
#define W16(a, v)       (*(volatile uint16_t *)(a) = (v))
#define W32(a, v)       (*(volatile uint32_t *)(a) = (v))

static inline uint32_t
mmio_rd(vio_dev_t *d, uint32_t reg)
{
	return R32(d->mmio + reg);
}

static inline void
mmio_wr(vio_dev_t *d, uint32_t reg, uint32_t v)
{
	W32(d->mmio + reg, v);
}

/* ---- discovery ---- */

static bool
mmio_bus_enabled(void)
{
	static const char *const args[] = { "virtio_blk", "virtio_input", "virtio_net", "virtio_sound" };
	uint32_t on = 0;

	if (PE_parse_boot_argn("virtio_mmio", &on, sizeof(on))) {
		return on != 0;
	}
	for (unsigned i = 0; i < sizeof(args) / sizeof(args[0]); i++) {
		on = 0;
		if (PE_parse_boot_argn(args[i], &on, sizeof(on)) && on) {
			return true;
		}
	}
	return false;
}

static void
probe_pci(struct redwood_pci_dev *p)
{
	struct vio_dev *d;
	uint32_t type;
	uintptr_t region[5] = { 0 };
	uint32_t mult = 0;

	if (p->vendor != PCI_VENDOR_VIRTIO || p->device < 0x1000 || p->device > 0x107f) {
		return;
	}
	type = p->device >= 0x1040 ? p->device - 0x1040u : p->subsys;
	for (uint8_t cap = redwood_pci_find_cap(p, PCI_CAP_VENDOR, 0); cap != 0;
	    cap = redwood_pci_find_cap(p, PCI_CAP_VENDOR, cap)) {
		uint8_t cfg_type = redwood_pci_cfg_read8(p, cap + 3);
		uint8_t bar = redwood_pci_cfg_read8(p, cap + 4);
		uint32_t off = redwood_pci_cfg_read32(p, cap + 8), len = redwood_pci_cfg_read32(p, cap + 12);

		if (cfg_type < VIRTIO_PCI_CAP_COMMON || cfg_type > VIRTIO_PCI_CAP_DEVICE || region[cfg_type] || bar >= 6 ||
		    len == 0) {
			continue;       /* the first of each type is the one to use */
		}
		region[cfg_type] = redwood_pci_map_bar(p, bar, off, len);
		if (cfg_type == VIRTIO_PCI_CAP_NOTIFY) {
			mult = redwood_pci_cfg_read32(p, cap + 16);
		}
	}
	if (!region[VIRTIO_PCI_CAP_COMMON] || !region[VIRTIO_PCI_CAP_NOTIFY] || !region[VIRTIO_PCI_CAP_ISR] ||
	    vio_ndevs == VIO_MAX_DEVS) {
		return;         /* legacy only, or BARs that could not be placed */
	}
	d = &vio_devs[vio_ndevs++];
	d->transport = VIO_TRANSPORT_PCI;
	d->type = type;
	d->pci = p;
	d->common = region[VIRTIO_PCI_CAP_COMMON];
	d->notify = region[VIRTIO_PCI_CAP_NOTIFY];
	d->isr = region[VIRTIO_PCI_CAP_ISR];
	d->devcfg = region[VIRTIO_PCI_CAP_DEVICE];
	d->notify_mult = mult;
	d->intid = p->intid;
	d->edge = p->intx_edge;
	snprintf(d->name, sizeof(d->name), "pci %02x:%02x.%x", p->bus, p->slot, p->func);
}

static void
probe_mmio(void)
{
	vm_offset_t bus = ml_io_map(MMIO_BUS, MMIO_SLOTS * MMIO_SLOT_SIZE);

	if (bus == 0) {
		return;
	}
	for (unsigned slot = 0; slot < MMIO_SLOTS && vio_ndevs < VIO_MAX_DEVS; slot++) {
		uintptr_t regs = bus + slot * MMIO_SLOT_SIZE;
		uint32_t type;

		if (R32(regs + MMIO_MAGIC) != 0x74726976 || (type = R32(regs + MMIO_DEVICE_ID)) == 0) {
			continue;
		}
		if (R32(regs + MMIO_VERSION) != 2) {
			printf("virtio: mmio slot %u is a legacy transport; start QEMU with "
			    "-global virtio-mmio.force-legacy=false\n", slot);
			continue;
		}
		struct vio_dev *d = &vio_devs[vio_ndevs++];
		d->transport = VIO_TRANSPORT_MMIO;
		d->type = type;
		d->mmio = regs;
		d->devcfg = regs + MMIO_CONFIG;
		d->intid = MMIO_INTID_BASE + slot;
		snprintf(d->name, sizeof(d->name), "mmio slot %u", slot);
	}
}

static void
probe(void)
{
	if (vio_probed) {
		return;
	}
	vio_probed = true;
	if (redwood_pci_init() == 0) {
		for (unsigned i = 0; i < redwood_pci_count(); i++) {
			probe_pci(redwood_pci_device(i));
		}
	}
	if (mmio_bus_enabled()) {
		probe_mmio();
	}
}

vio_dev_t *
vio_find(uint32_t type, unsigned nth)
{
	probe();
	for (unsigned i = 0; i < vio_ndevs; i++) {
		struct vio_dev *d = &vio_devs[i];
		if (d->type != type || (d->pci && d->pci->firmware)) {
			continue;
		}
		if (nth-- == 0) {
			return d;
		}
	}
	return NULL;
}

uint32_t
vio_type(vio_dev_t *d)
{
	return d->type;
}

int
vio_transport(vio_dev_t *d)
{
	return d->transport;
}

const char *
vio_name(vio_dev_t *d)
{
	return d->name;
}

const struct redwood_pci_dev *
vio_pci(vio_dev_t *d)
{
	return d->pci;
}

/* ---- status and features ---- */

uint8_t
vio_status(vio_dev_t *d)
{
	return d->transport == VIO_TRANSPORT_PCI ? R8(d->common + COMMON_STATUS) : (uint8_t)mmio_rd(d, MMIO_STATUS);
}

static void
set_status(vio_dev_t *d, uint8_t s)
{
	if (d->transport == VIO_TRANSPORT_PCI) {
		W8(d->common + COMMON_STATUS, s);
	} else {
		mmio_wr(d, MMIO_STATUS, s);
	}
}

void
vio_reset(vio_dev_t *d)
{
	set_status(d, 0);
	/* the reset is complete when the status reads 0 again */
	for (int i = 0; i < 100000 && vio_status(d) != 0; i++) {
		delay(1);
	}
	for (unsigned q = 0; q < VIO_MAX_QUEUES; q++) {
		d->queue[q].ready = false;
	}
}

void
vio_fail(vio_dev_t *d)
{
	set_status(d, vio_status(d) | VIRTIO_STATUS_FAILED);
}

void
vio_driver_ok(vio_dev_t *d)
{
	dsb_sy();
	set_status(d, vio_status(d) | VIRTIO_STATUS_DRIVER_OK);
}

uint64_t
vio_device_features(vio_dev_t *d)
{
	uint64_t f = 0;

	for (uint32_t half = 0; half < 2; half++) {
		uint32_t v;
		if (d->transport == VIO_TRANSPORT_PCI) {
			W32(d->common + COMMON_DFSELECT, half);
			v = R32(d->common + COMMON_DF);
		} else {
			mmio_wr(d, MMIO_DEVICE_FEATURES_SEL, half);
			v = mmio_rd(d, MMIO_DEVICE_FEATURES);
		}
		f |= (uint64_t)v << (32 * half);
	}
	return f;
}

uint64_t
vio_features(vio_dev_t *d)
{
	return d->features;
}

static void
set_driver_features(vio_dev_t *d, uint64_t f)
{
	for (uint32_t half = 0; half < 2; half++) {
		if (d->transport == VIO_TRANSPORT_PCI) {
			W32(d->common + COMMON_GFSELECT, half);
			W32(d->common + COMMON_GF, (uint32_t)(f >> (32 * half)));
		} else {
			mmio_wr(d, MMIO_DRIVER_FEATURES_SEL, half);
			mmio_wr(d, MMIO_DRIVER_FEATURES, (uint32_t)(f >> (32 * half)));
		}
	}
}

int
vio_init(vio_dev_t *d, uint64_t want, uint64_t *negotiated)
{
	uint64_t offered;

	if (d->pci) {
		uint8_t msix = redwood_pci_find_cap(d->pci, PCI_CAP_MSIX, 0);
		if (msix) {     /* INTx only: MSI-X off */
			redwood_pci_cfg_write16(d->pci, msix + 2,
			    redwood_pci_cfg_read16(d->pci, msix + 2) & (uint16_t)~0x8000);
		}
		redwood_pci_enable(d->pci, d->intr_on);
	}
	vio_reset(d);
	set_status(d, VIRTIO_STATUS_ACKNOWLEDGE);
	set_status(d, VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER);
	offered = vio_device_features(d);
	if (!(offered & VIRTIO_BIT(VIRTIO_F_VERSION_1))) {
		vio_fail(d);
		return ENOTSUP;
	}
	d->features = offered & (want | VIRTIO_BIT(VIRTIO_F_VERSION_1) | VIRTIO_BIT(VIRTIO_F_ACCESS_PLATFORM));
	d->features &= ~(VIRTIO_BIT(VIRTIO_F_RING_PACKED) | VIRTIO_BIT(VIRTIO_F_EVENT_IDX) |
	    VIRTIO_BIT(VIRTIO_F_INDIRECT_DESC));
	set_driver_features(d, d->features);
	set_status(d, VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER | VIRTIO_STATUS_FEATURES_OK);
	if (!(vio_status(d) & VIRTIO_STATUS_FEATURES_OK)) {
		vio_fail(d);
		return ENOTSUP;
	}
	if (d->transport == VIO_TRANSPORT_PCI) {
		d->nqueues = R16(d->common + COMMON_NUMQ);
		W16(d->common + COMMON_MSIX, VIRTIO_MSI_NO_VECTOR);
	} else {
		d->nqueues = 0;
		for (uint32_t q = 0; q < 16; q++) {
			mmio_wr(d, MMIO_QUEUE_SEL, q);
			if (mmio_rd(d, MMIO_QUEUE_NUM_MAX) == 0) {
				break;
			}
			d->nqueues = (uint16_t)(q + 1);
		}
	}
	if (negotiated) {
		*negotiated = d->features;
	}
	return 0;
}

uint16_t
vio_num_queues(vio_dev_t *d)
{
	return d->nqueues;
}

/* ---- device configuration ---- */

static uint8_t
cfg_generation(vio_dev_t *d)
{
	return d->transport == VIO_TRANSPORT_PCI ? R8(d->common + COMMON_CFGGENERATION) :
	       (uint8_t)mmio_rd(d, MMIO_CONFIG_GENERATION);
}

void
vio_cfg_read(vio_dev_t *d, uint32_t off, void *buf, uint32_t len)
{
	uint8_t gen;

	if (d->devcfg == 0) {
		memset(buf, 0xff, len);
		return;
	}
	do {
		gen = cfg_generation(d);
		for (uint32_t i = 0; i < len;) {
			/* natural accesses: a device may only accept those */
			if (len - i >= 4 && ((off + i) & 3) == 0) {
				uint32_t v = R32(d->devcfg + off + i);
				memcpy((uint8_t *)buf + i, &v, 4);
				i += 4;
			} else if (len - i >= 2 && ((off + i) & 1) == 0) {
				uint16_t v = R16(d->devcfg + off + i);
				memcpy((uint8_t *)buf + i, &v, 2);
				i += 2;
			} else {
				((uint8_t *)buf)[i] = R8(d->devcfg + off + i);
				i++;
			}
		}
	} while (gen != cfg_generation(d));
}

uint8_t
vio_cfg_read8(vio_dev_t *d, uint32_t off)
{
	uint8_t v;
	vio_cfg_read(d, off, &v, sizeof(v));
	return v;
}

uint16_t
vio_cfg_read16(vio_dev_t *d, uint32_t off)
{
	uint16_t v;
	vio_cfg_read(d, off, &v, sizeof(v));
	return v;
}

uint32_t
vio_cfg_read32(vio_dev_t *d, uint32_t off)
{
	uint32_t v;
	vio_cfg_read(d, off, &v, sizeof(v));
	return v;
}

uint64_t
vio_cfg_read64(vio_dev_t *d, uint32_t off)
{
	uint64_t v;
	vio_cfg_read(d, off, &v, sizeof(v));
	return v;
}

void
vio_cfg_write8(vio_dev_t *d, uint32_t off, uint8_t v)
{
	if (d->devcfg) {
		W8(d->devcfg + off, v);
	}
}

void
vio_cfg_write16(vio_dev_t *d, uint32_t off, uint16_t v)
{
	if (d->devcfg) {
		W16(d->devcfg + off, v);
	}
}

void
vio_cfg_write32(vio_dev_t *d, uint32_t off, uint32_t v)
{
	if (d->devcfg) {
		W32(d->devcfg + off, v);
	}
}

/* ---- queues ---- */

static uint16_t
fit_size(uint16_t want, uint32_t max)
{
	uint32_t n = want ? MIN(want, max) : max;

	n = MIN(n, VIO_QUEUE_MAX);
	while (n & (n - 1)) {
		n &= n - 1;     /* the largest power of two below */
	}
	return (uint16_t)n;
}

#define AVAIL_OFF(n)    ((size_t)(n) * sizeof(struct vq_desc))
#define USED_OFF(n)     ((AVAIL_OFF(n) + 6 + 2 * (size_t)(n) + 3) & ~(size_t)3)
#define COOKIE_OFF(n)   ((USED_OFF(n) + 6 + 8 * (size_t)(n) + 7) & ~(size_t)7)

size_t
vio_queue_mem_size(uint16_t size)
{
	return round_page(COOKIE_OFF(size) + sizeof(void *) * (size_t)size);
}

static uint32_t
queue_max(vio_dev_t *d, uint16_t index)
{
	if (d->transport == VIO_TRANSPORT_PCI) {
		W16(d->common + COMMON_Q_SELECT, index);
		return R16(d->common + COMMON_Q_SIZE);
	}
	mmio_wr(d, MMIO_QUEUE_SEL, index);
	return mmio_rd(d, MMIO_QUEUE_NUM_MAX);
}

static int
queue_start(vio_dev_t *d, uint16_t index, uint16_t size, void *mem, uint64_t pa, vio_queue_t **out)
{
	struct vio_queue *q = &d->queue[index];
	uint64_t desc = pa, avail = pa + AVAIL_OFF(size), used = pa + USED_OFF(size);

	q->dev = d;
	q->index = index;
	q->size = size;
	q->desc = (volatile struct vq_desc *)mem;
	q->avail = (volatile uint16_t *)((uintptr_t)mem + AVAIL_OFF(size));
	q->used = (volatile uint16_t *)((uintptr_t)mem + USED_OFF(size));
	q->cookie = (void **)((uintptr_t)mem + COOKIE_OFF(size));
	for (uint16_t i = 0; i < size; i++) {
		q->desc[i].next = (uint16_t)(i + 1);
		q->cookie[i] = NULL;
	}
	q->free_head = 0;
	q->num_free = size;
	q->avail_idx = q->last_used = 0;
	dmb_ish();

	if (d->transport == VIO_TRANSPORT_PCI) {
		uintptr_t c = d->common;
		W16(c + COMMON_Q_SELECT, index);
		W16(c + COMMON_Q_SIZE, size);
		W32(c + COMMON_Q_DESCLO, (uint32_t)desc);
		W32(c + COMMON_Q_DESCHI, (uint32_t)(desc >> 32));
		W32(c + COMMON_Q_AVAILLO, (uint32_t)avail);
		W32(c + COMMON_Q_AVAILHI, (uint32_t)(avail >> 32));
		W32(c + COMMON_Q_USEDLO, (uint32_t)used);
		W32(c + COMMON_Q_USEDHI, (uint32_t)(used >> 32));
		W16(c + COMMON_Q_MSIX, VIRTIO_MSI_NO_VECTOR);
		q->notify = d->notify + (uintptr_t)R16(c + COMMON_Q_NOFF) * d->notify_mult;
		dsb_sy();
		W16(c + COMMON_Q_ENABLE, 1);
	} else {
		mmio_wr(d, MMIO_QUEUE_SEL, index);
		if (mmio_rd(d, MMIO_QUEUE_READY)) {
			return EBUSY;
		}
		mmio_wr(d, MMIO_QUEUE_NUM, size);
		mmio_wr(d, MMIO_QUEUE_DESC_LOW, (uint32_t)desc);
		mmio_wr(d, MMIO_QUEUE_DESC_HIGH, (uint32_t)(desc >> 32));
		mmio_wr(d, MMIO_QUEUE_DRIVER_LOW, (uint32_t)avail);
		mmio_wr(d, MMIO_QUEUE_DRIVER_HIGH, (uint32_t)(avail >> 32));
		mmio_wr(d, MMIO_QUEUE_DEVICE_LOW, (uint32_t)used);
		mmio_wr(d, MMIO_QUEUE_DEVICE_HIGH, (uint32_t)(used >> 32));
		q->notify = d->mmio + MMIO_QUEUE_NOTIFY;
		dsb_sy();
		mmio_wr(d, MMIO_QUEUE_READY, 1);
	}
	q->ready = true;
	*out = q;
	return 0;
}

int
vio_queue_setup_mem(vio_dev_t *d, uint16_t index, uint16_t size, void *mem, uint64_t pa, vio_queue_t **out)
{
	uint32_t max;

	if (index >= VIO_MAX_QUEUES || (max = queue_max(d, index)) == 0) {
		return ENOENT;
	}
	size = fit_size(size, max);
	if (size == 0 || (pa & 0xfff) || mem == NULL) {
		return EINVAL;
	}
	return queue_start(d, index, size, mem, pa, out);
}

int
vio_queue_setup(vio_dev_t *d, uint16_t index, uint16_t size, vio_queue_t **out)
{
	uint32_t max;
	uint64_t pa;
	void *mem;

	if (index >= VIO_MAX_QUEUES || (max = queue_max(d, index)) == 0) {
		return ENOENT;
	}
	size = fit_size(size, max);
	if (size == 0) {
		return ENOENT;
	}
	if ((mem = vio_dma_alloc(vio_queue_mem_size(size), &pa)) == NULL) {
		return ENOMEM;
	}
	return queue_start(d, index, size, mem, pa, out);
}

uint16_t
vio_queue_size(vio_queue_t *q)
{
	return q->size;
}

uint16_t
vio_queue_num_free(vio_queue_t *q)
{
	return q->num_free;
}

vio_dev_t *
vio_queue_dev(vio_queue_t *q)
{
	return q->dev;
}

int
vio_queue_add(vio_queue_t *q, const struct vio_sg *sg, unsigned n, void *cookie)
{
	uint16_t head, at;

	if (n == 0 || cookie == NULL || !q->ready) {
		return EINVAL;
	}
	if (n > q->num_free) {
		return ENOSPC;
	}
	head = at = q->free_head;
	for (unsigned i = 0; i < n; i++) {
		volatile struct vq_desc *dsc = &q->desc[at];
		dsc->addr = sg[i].pa;
		dsc->len = sg[i].len;
		dsc->flags = (uint16_t)((sg[i].write ? VIRTQ_DESC_F_WRITE : 0) | (i + 1 < n ? VIRTQ_DESC_F_NEXT : 0));
		at = dsc->next;         /* the free list's next is the chain's next */
	}
	q->free_head = at;
	q->num_free -= (uint16_t)n;
	q->cookie[head] = cookie;
	q->avail[2 + (q->avail_idx % q->size)] = head;
	q->avail_idx++;
	return 0;
}

void
vio_queue_kick(vio_queue_t *q)
{
	dmb_ishst();                    /* descriptors and ring entries before the index */
	q->avail[1] = q->avail_idx;
	dmb_ish();                      /* the index before reading the device's flags */
	if (!(q->used[0] & VIRTQ_USED_F_NO_NOTIFY)) {
		dsb_sy();
		if (q->dev->transport == VIO_TRANSPORT_PCI) {
			W16(q->notify, q->index);
		} else {
			W32(q->notify, q->index);
		}
	}
}

void *
vio_queue_get(vio_queue_t *q, uint32_t *len)
{
	volatile struct vq_used_elem *ring = (volatile struct vq_used_elem *)(q->used + 2);
	uint32_t id, written;
	uint16_t at, n = 1;
	void *cookie;

	if (!q->ready || q->last_used == q->used[1]) {
		return NULL;
	}
	dmb_ishld();                    /* the index before the element it covers */
	id = ring[q->last_used % q->size].id;
	written = ring[q->last_used % q->size].len;
	q->last_used++;
	if (id >= q->size || q->cookie[id] == NULL) {
		printf("virtio: %s queue %u: the device returned descriptor %u, which it does not have\n",
		    q->dev->name, q->index, id);
		return NULL;
	}
	for (at = (uint16_t)id; q->desc[at].flags & VIRTQ_DESC_F_NEXT; n++) {
		at = q->desc[at].next;
	}
	q->desc[at].next = q->free_head;
	q->free_head = (uint16_t)id;
	q->num_free += n;
	cookie = q->cookie[id];
	q->cookie[id] = NULL;
	if (len) {
		*len = written;
	}
	return cookie;
}

void
vio_queue_intr(vio_queue_t *q, bool enable)
{
	q->avail[0] = enable ? 0 : VIRTQ_AVAIL_F_NO_INTERRUPT;
	dmb_ish();
}

/* ---- interrupts ---- */

static void
line_interrupt(void *context, __unused uint32_t intid)
{
	struct vio_line *line = context;

	for (struct vio_dev *d = line->devs; d != NULL; d = d->next_on_line) {
		uint8_t isr;

		if (d->transport == VIO_TRANSPORT_PCI) {
			isr = R8(d->isr);       /* reading it clears it and lowers INTx */
		} else {
			isr = (uint8_t)mmio_rd(d, MMIO_INTERRUPT_STATUS);
			if (isr) {
				mmio_wr(d, MMIO_INTERRUPT_ACK, isr);
			}
		}
		if (isr) {
			os_atomic_or(&d->isr_pending, isr, relaxed);
			os_atomic_inc(&d->seq, release);
			thread_wakeup((event_t)&d->seq);
		}
	}
}

int
vio_intr_enable(vio_dev_t *d)
{
	uint32_t poll = 0;
	int error = 0;
	unsigned i;

	if (PE_parse_boot_argn("virtio_poll", &poll, sizeof(poll)) && poll) {
		return ENOTSUP;
	}
	if (d->intid < 32) {
		return ENXIO;
	}
	lck_mtx_lock(&vio_intr_lock);
	if (d->intr_on) {
		goto out;
	}
	for (i = 0; i < VIO_MAX_LINES && vio_lines[i].intid != 0 && vio_lines[i].intid != d->intid; i++) {
	}
	if (i == VIO_MAX_LINES) {
		error = ENOSPC;
		goto out;
	}
	if (vio_lines[i].intid == 0) {
		/* the first device on this line: the GIC calls line_interrupt with the list */
		vio_lines[i].devs = d;
		d->next_on_line = NULL;
		if (gic_v3_spi_attach(d->intid, d->edge, line_interrupt, &vio_lines[i]) != KERN_SUCCESS) {
			vio_lines[i].devs = NULL;
			error = EBUSY;
			goto out;
		}
		vio_lines[i].intid = d->intid;
	} else {
		boolean_t s = ml_set_interrupts_enabled(FALSE);
		d->next_on_line = vio_lines[i].devs;
		vio_lines[i].devs = d;
		ml_set_interrupts_enabled(s);
	}
	d->intr_on = true;
	if (d->pci) {
		redwood_pci_enable(d->pci, true);
	}
out:
	lck_mtx_unlock(&vio_intr_lock);
	return error;
}

bool
vio_intr_active(vio_dev_t *d)
{
	return d->intr_on;
}

uint32_t
vio_intr_seq(vio_dev_t *d)
{
	return os_atomic_load(&d->seq, acquire);
}

uint8_t
vio_intr_isr(vio_dev_t *d)
{
	return os_atomic_xchg(&d->isr_pending, 0, relaxed);
}

static bool
can_block(void)
{
	return ml_get_interrupts_enabled() && !ml_at_interrupt_context() && get_preemption_level() == 0;
}

/* Sleep until the count moves or the deadline passes. */
static bool
sleep_for(vio_dev_t *d, uint32_t seq, uint64_t timeout_ns)
{
	uint64_t deadline;

	nanoseconds_to_deadline(timeout_ns, &deadline);
	assert_wait_deadline((event_t)&d->seq, THREAD_UNINT, deadline);
	if (os_atomic_load(&d->seq, acquire) != seq) {
		clear_wait(current_thread(), THREAD_AWAKENED);
		return true;
	}
	thread_block(THREAD_CONTINUE_NULL);
	return os_atomic_load(&d->seq, acquire) != seq;
}

bool
vio_intr_wait(vio_dev_t *d, uint32_t seq, uint64_t timeout_ns)
{
	if (os_atomic_load(&d->seq, acquire) != seq) {
		return true;
	}
	if (d->intr_on && can_block()) {
		return sleep_for(d, seq, timeout_ns);
	}
	/* no interrupt to wait for: come back soon and let the caller look at its queue */
	delay((int)MIN(timeout_ns / NSEC_PER_USEC, 20));
	return os_atomic_load(&d->seq, acquire) != seq;
}

struct vio_service {
	vio_dev_t       *dev;
	vio_service_fn  cb;
	void            *arg;
	uint64_t        poll_ns;
};

static void
service_thread(void *param, __unused wait_result_t wr)
{
	struct vio_service *s = param;

	for (;;) {
		uint32_t seq = vio_intr_seq(s->dev);
		s->cb(s->arg, vio_intr_isr(s->dev));
		if (os_atomic_load(&s->dev->seq, acquire) == seq) {
			(void)sleep_for(s->dev, seq, s->poll_ns);
		}
	}
}

int
vio_service_start(vio_dev_t *d, vio_service_fn cb, void *arg, uint64_t poll_ns)
{
	struct vio_service *s;
	thread_t thread;

	if ((s = kalloc_data(sizeof(*s), Z_WAITOK | Z_ZERO)) == NULL) {
		return ENOMEM;
	}
	s->dev = d;
	s->cb = cb;
	s->arg = arg;
	s->poll_ns = poll_ns ? poll_ns : 10 * NSEC_PER_MSEC;
	(void)vio_intr_enable(d);
	if (kernel_thread_start(service_thread, s, &thread) != KERN_SUCCESS) {
		return ENOMEM;
	}
	thread_deallocate(thread);
	return 0;
}

/* ---- shared memory regions ---- */

int
vio_shm_region(vio_dev_t *d, uint8_t id, uint64_t *pa, uint64_t *len)
{
	struct redwood_pci_dev *p = d->pci;

	if (p == NULL) {
		return ENOENT;
	}
	for (uint8_t cap = redwood_pci_find_cap(p, PCI_CAP_VENDOR, 0); cap != 0;
	    cap = redwood_pci_find_cap(p, PCI_CAP_VENDOR, cap)) {
		/* struct virtio_pci_cap64: the 32-bit offset and length, then their high halves */
		uint8_t bar = redwood_pci_cfg_read8(p, cap + 4);
		uint64_t off, size;

		if (redwood_pci_cfg_read8(p, cap + 3) != VIRTIO_PCI_CAP_SHM || redwood_pci_cfg_read8(p, cap + 5) != id ||
		    bar >= 6 || p->bar_pa[bar] == 0) {
			continue;
		}
		off = redwood_pci_cfg_read32(p, cap + 8) | (uint64_t)redwood_pci_cfg_read32(p, cap + 16) << 32;
		size = redwood_pci_cfg_read32(p, cap + 12) | (uint64_t)redwood_pci_cfg_read32(p, cap + 20) << 32;
		if (off > p->bar_size[bar] || size > p->bar_size[bar] - off) {
			return EINVAL;
		}
		*pa = p->bar_pa[bar] + off;
		*len = size;
		return 0;
	}
	return ENOENT;
}

/* ---- memory ---- */

void *
vio_dma_alloc(size_t size, uint64_t *pa)
{
	vm_offset_t va = 0;

	if (kmem_alloc_contig(kernel_map, &va, round_page(size), PAGE_MASK, 0, 0,
	    KMA_KOBJECT | KMA_ZERO | KMA_DATA, VM_KERN_MEMORY_IOKIT) != KERN_SUCCESS) {
		return NULL;
	}
	*pa = kvtophys(va);
	return (void *)va;
}
