/*
 * Virtio for the kernel's own drivers, over either transport:
 *
 *   - virtio-pci 1.x ("modern"), on the PCI host bridge of sys/redwood_pci.h: what crosvm
 *     (the Android Virtualization Framework) offers, and QEMU with -device virtio-*-pci;
 *   - virtio-mmio version 2, QEMU virt's 32 transports at 0x0a000000 (-device virtio-*-device,
 *     -global virtio-mmio.force-legacy=false). That bus address means nothing anywhere else, so
 *     it is only looked at with boot-arg virtio_mmio=1 (or one of the old per-driver boot-args
 *     virtio_blk=1, virtio_input=1, virtio_net=1, virtio_sound=1).
 *
 * The drivers (redwood_virtio_{blk,net,input,sound,console}.c) see one interface: feature
 * negotiation, split virtqueues, the device's configuration space, and interrupts or polling.
 * Implementation: bsd/dev/redwood_virtio.c.
 *
 * A driver, in short (redwood_virtio_blk.c is a complete one):
 *
 *   vio_dev_t *d = vio_find(VIRTIO_ID_BLOCK, 0);          // the first block device, any transport
 *   uint64_t features;
 *   vio_init(d, WANTED_FEATURES, &features);              // reset, ACK, DRIVER, FEATURES_OK
 *   vio_queue_t *q;
 *   vio_queue_setup(d, 0, 0, &q);                         // rings in contiguous kernel memory
 *   vio_intr_enable(d);                                   // optional: wakes vio_intr_wait
 *   vio_driver_ok(d);
 *   ...
 *   struct vio_sg sg[] = { { hdr_pa, 16, false }, { data_pa, n, true }, { status_pa, 1, true } };
 *   uint32_t seq = vio_intr_seq(d);
 *   vio_queue_add(q, sg, 3, cookie);
 *   vio_queue_kick(q);
 *   while ((c = vio_queue_get(q, &len)) == NULL) {
 *           vio_intr_wait(d, seq, 10 * NSEC_PER_MSEC);     // sleeps; polls when it cannot
 *           seq = vio_intr_seq(d);
 *   }
 *
 * Devices whose events come on their own (input, a GPU's cursor and fence completions) can
 * have vio_service_start() call them back from a kernel thread after every interrupt and at
 * least every poll_ns, never in interrupt context.
 *
 * Locking: queues are not locked here. A driver serializes all use of one queue (one mutex
 * per queue, or a single thread). Everything up to vio_driver_ok and the queue operations
 * work before the VM is up when the rings come from vio_queue_setup_mem (the console does that
 * from serial_init); vio_queue_setup, vio_dma_alloc, interrupts and sleeping waits need a
 * running kernel.
 *
 * DMA: guest physical addresses, no IOMMU. VIRTIO_F_ACCESS_PLATFORM is accepted when offered
 * (crosvm offers it for protected VMs), on the assumption that the platform's mapping is the
 * identity. Memory is normal cacheable memory: virtio devices of QEMU and crosvm are coherent.
 */
#ifndef _SYS_REDWOOD_VIRTIO_H_
#define _SYS_REDWOOD_VIRTIO_H_

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <sys/cdefs.h>

__BEGIN_DECLS

/* Device types (virtio 1.2, section 5) */
#define VIRTIO_ID_NET           1
#define VIRTIO_ID_BLOCK         2
#define VIRTIO_ID_CONSOLE       3
#define VIRTIO_ID_RNG           4
#define VIRTIO_ID_GPU           16
#define VIRTIO_ID_INPUT         18
#define VIRTIO_ID_VSOCK         19
#define VIRTIO_ID_SOUND         25

/* Transport feature bits; the device's own are below 24 */
#define VIRTIO_F_INDIRECT_DESC  28
#define VIRTIO_F_EVENT_IDX      29
#define VIRTIO_F_VERSION_1      32      /* always negotiated: legacy devices are not driven */
#define VIRTIO_F_ACCESS_PLATFORM 33     /* accepted when offered */
#define VIRTIO_F_RING_PACKED    34
#define VIRTIO_BIT(b)           (1ull << (b))

/* device_status */
#define VIRTIO_STATUS_ACKNOWLEDGE       0x01
#define VIRTIO_STATUS_DRIVER            0x02
#define VIRTIO_STATUS_DRIVER_OK         0x04
#define VIRTIO_STATUS_FEATURES_OK       0x08
#define VIRTIO_STATUS_NEEDS_RESET       0x40
#define VIRTIO_STATUS_FAILED            0x80

/* interrupt status bits */
#define VIRTIO_ISR_QUEUE        0x1
#define VIRTIO_ISR_CONFIG       0x2

#define VIO_TRANSPORT_PCI       1
#define VIO_TRANSPORT_MMIO      2

typedef struct vio_dev vio_dev_t;
typedef struct vio_queue vio_queue_t;

/* One buffer of a descriptor chain: read by the device (write = false) or written by it. */
struct vio_sg {
	uint64_t        pa;
	uint32_t        len;
	bool            write;
};

/*
 * The nth (from 0) device of this type: PCI devices first, in enumeration order, then
 * virtio-mmio slots if that bus is enabled. A device the firmware still uses (the loader's own
 * disk under UEFI) is skipped. NULL if there is none. The first call enumerates.
 */
vio_dev_t *vio_find(uint32_t type, unsigned nth);
uint32_t vio_type(vio_dev_t *d);
int vio_transport(vio_dev_t *d);
/* "pci 00:02.0" or "mmio slot 31", for messages */
const char *vio_name(vio_dev_t *d);
/* The PCI function, NULL for virtio-mmio. */
const struct redwood_pci_dev *vio_pci(vio_dev_t *d);

/*
 * Reset, ACKNOWLEDGE | DRIVER, accept offered & (want | VERSION_1 | ACCESS_PLATFORM),
 * FEATURES_OK. 0, or ENOTSUP when the device lacks VERSION_1 or refuses FEATURES_OK.
 */
int vio_init(vio_dev_t *d, uint64_t want, uint64_t *negotiated);
uint64_t vio_device_features(vio_dev_t *d);
uint64_t vio_features(vio_dev_t *d);   /* negotiated */
void vio_driver_ok(vio_dev_t *d);
void vio_fail(vio_dev_t *d);
void vio_reset(vio_dev_t *d);
uint8_t vio_status(vio_dev_t *d);
uint16_t vio_num_queues(vio_dev_t *d);         /* PCI: num_queues; mmio: probed, at most 16 */

/* The device-specific configuration space; a read is consistent (config generation). */
uint8_t  vio_cfg_read8(vio_dev_t *d, uint32_t off);
uint16_t vio_cfg_read16(vio_dev_t *d, uint32_t off);
uint32_t vio_cfg_read32(vio_dev_t *d, uint32_t off);
uint64_t vio_cfg_read64(vio_dev_t *d, uint32_t off);
void vio_cfg_read(vio_dev_t *d, uint32_t off, void *buf, uint32_t len);
void vio_cfg_write8(vio_dev_t *d, uint32_t off, uint8_t v);
void vio_cfg_write16(vio_dev_t *d, uint32_t off, uint16_t v);
void vio_cfg_write32(vio_dev_t *d, uint32_t off, uint32_t v);

/*
 * Queue 'index', between vio_init and vio_driver_ok. size 0 = the device's maximum, at most
 * 256; otherwise the largest power of two <= min(size, maximum). vio_queue_setup allocates the
 * rings (vio_dma_alloc); vio_queue_setup_mem takes vio_queue_mem_size(size) bytes of zeroed,
 * 4 KB-aligned memory at physical address pa (static memory for early users).
 * 0, ENOENT (no such queue) or ENOMEM.
 */
int vio_queue_setup(vio_dev_t *d, uint16_t index, uint16_t size, vio_queue_t **out);
size_t vio_queue_mem_size(uint16_t size);
int vio_queue_setup_mem(vio_dev_t *d, uint16_t index, uint16_t size, void *mem, uint64_t pa, vio_queue_t **out);
uint16_t vio_queue_size(vio_queue_t *q);
uint16_t vio_queue_num_free(vio_queue_t *q);
vio_dev_t *vio_queue_dev(vio_queue_t *q);

/*
 * Put a chain of n buffers (the device-readable ones first) on the queue under a non-NULL
 * cookie. The device sees it after vio_queue_kick. 0, or ENOSPC with fewer than n free
 * descriptors.
 */
int vio_queue_add(vio_queue_t *q, const struct vio_sg *sg, unsigned n, void *cookie);
/* Publish what was added and notify the device, unless it asked not to be. */
void vio_queue_kick(vio_queue_t *q);
/* The cookie of the next chain the device is done with (and the bytes it wrote), or NULL. */
void *vio_queue_get(vio_queue_t *q, uint32_t *len);
/* Ask the device not to interrupt for this queue (a hint: VIRTQ_AVAIL_F_NO_INTERRUPT). */
void vio_queue_intr(vio_queue_t *q, bool enable);

/*
 * Interrupts: the device's INTx line (PCI) or its SPI (virtio-mmio) through
 * gic_v3_spi_attach(). The handler reads and acknowledges the interrupt status, adds it to the
 * device's pending bits, bumps vio_intr_seq and wakes vio_intr_wait. ENXIO when there is no
 * line, ENOTSUP with boot-arg virtio_poll=1: the driver then polls (vio_intr_wait still works,
 * it just sleeps out its timeout).
 */
int vio_intr_enable(vio_dev_t *d);
bool vio_intr_active(vio_dev_t *d);
uint32_t vio_intr_seq(vio_dev_t *d);
uint8_t vio_intr_isr(vio_dev_t *d);    /* take and clear the pending bits */
/*
 * Until vio_intr_seq(d) != seq or timeout_ns has passed; true if an interrupt came. Where it
 * cannot block (interrupts off, preemption disabled, before threads) it spins.
 */
bool vio_intr_wait(vio_dev_t *d, uint32_t seq, uint64_t timeout_ns);

/* A kernel thread calling cb(arg, isr) after each interrupt and at least every poll_ns (isr 0). */
typedef void (*vio_service_fn)(void *arg, uint8_t isr);
int vio_service_start(vio_dev_t *d, vio_service_fn cb, void *arg, uint64_t poll_ns);

/*
 * A shared memory region of the device (virtio-pci capability type 8, e.g. virtio-gpu's
 * host-visible memory, shmid 1): its CPU physical address and length. ENOENT when the device
 * has none with that id (and always on virtio-mmio, which is not looked at for them).
 */
int vio_shm_region(vio_dev_t *d, uint8_t id, uint64_t *pa, uint64_t *len);

/* Physically contiguous, zeroed, page-aligned wired memory; NULL on failure. Never freed. */
void *vio_dma_alloc(size_t size, uint64_t *pa);

__END_DECLS

#endif /* _SYS_REDWOOD_VIRTIO_H_ */
