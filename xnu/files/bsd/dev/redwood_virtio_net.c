/*
 * A virtio network device as the link behind en0 (see sys/redwood_net.h for what a link is).
 *
 * Any transport of sys/redwood_virtio.h: "-device virtio-net-device" on QEMU virt's virtio-mmio
 * bus (-global virtio-mmio.force-legacy=false, boot-arg virtio_net=1, which turns that bus on;
 * qemu/run-ios.sh does both with QEMU_NET=1), or a virtio-pci network device (QEMU's
 * virtio-net-pci, crosvm's), used unless boot-arg virtio_net=0.
 *
 * Nothing of what the device offers is taken (no checksums or segmentation left to it, no
 * merged receive buffers): a 12-byte header nobody reads, then the frame, one buffer each.
 * No interrupt is used; the interface's thread asks (virtio_poll()).
 */
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <mach/vm_param.h>
#include <net/ethernet.h>
#include <pexpert/pexpert.h>
#include <sys/kpi_mbuf.h>
#include <sys/redwood_net.h>
#include <sys/redwood_virtio.h>
#include <sys/systm.h>

#define VIRTIO_NET_F_MAC        5               /* the device has an address of its own */
#define CFG_MAC                 0x00

#define QUEUE_SIZE              64              /* descriptors, and buffers, per queue */
#define QUEUE_RECEIVE           0
#define QUEUE_TRANSMIT          1
#define BUFFER_SIZE             2048            /* a buffer never crosses a page: one address will do for it */

/* before every frame, either way; nothing of it is used (no checksum or segmentation is left to the device) */
struct virtio_net_header {
	uint8_t flags, gso_type;
	uint16_t header_length, gso_size, checksum_start, checksum_offset, buffers;
};

struct queue {
	vio_queue_t *           vq;
	uint8_t *               buffers;        /* QUEUE_SIZE of BUFFER_SIZE */
	uint64_t                buffers_pa;
	uint16_t                free[QUEUE_SIZE];       /* transmit: the buffers the device does not have */
	unsigned                free_count;
};

static vio_dev_t *net_dev;
static struct queue receive_queue, transmit_queue;

/* cookies are buffer number + 1 (never NULL) */
#define COOKIE(i)               ((void *)(uintptr_t)((i) + 1))
#define BUFFER_OF(c)            ((uint16_t)((uintptr_t)(c) - 1))

static void
give(struct queue *q, uint16_t id, uint32_t len, bool device_writes)
{
	struct vio_sg sg = { q->buffers_pa + (uint64_t)id * BUFFER_SIZE, len, device_writes };

	(void)vio_queue_add(q->vq, &sg, 1, COOKIE(id));
}

static bool
queue_make(struct queue *q, uint16_t index, bool device_writes)
{
	int error;

	if ((error = vio_queue_setup(net_dev, index, QUEUE_SIZE, &q->vq)) != 0) {
		printf("net: virtio queue %u: %d\n", index, error);
		return false;
	}
	if ((q->buffers = vio_dma_alloc(QUEUE_SIZE * BUFFER_SIZE, &q->buffers_pa)) == NULL) {
		return false;
	}
	for (uint16_t i = 0; i < vio_queue_size(q->vq); i++) {
		if (device_writes) {
			give(q, i, BUFFER_SIZE, true);          /* the device gets them all, to fill */
		} else {
			q->free[q->free_count++] = i;
		}
	}
	return true;
}

/* A frame to send. */
static bool
virtio_transmit(mbuf_t packet, size_t length)
{
	struct queue *q = &transmit_queue;
	uint8_t *buffer;
	uint16_t id;
	void *done;

	/* the buffers the device has done with */
	while ((done = vio_queue_get(q->vq, NULL)) != NULL) {
		q->free[q->free_count++] = BUFFER_OF(done);
	}
	if (q->free_count == 0 || length > BUFFER_SIZE - sizeof(struct virtio_net_header)) {
		return false;
	}
	id = q->free[--q->free_count];
	buffer = q->buffers + (size_t)id * BUFFER_SIZE;
	bzero(buffer, sizeof(struct virtio_net_header));
	if (mbuf_copydata(packet, 0, length, buffer + sizeof(struct virtio_net_header)) != 0) {
		q->free[q->free_count++] = id;
		return false;
	}
	give(q, id, (uint32_t)(sizeof(struct virtio_net_header) + length), false);
	vio_queue_kick(q->vq);
	return true;
}

/* Hand up what has arrived; returns how many frames that was. */
static unsigned
virtio_poll(void)
{
	struct queue *q = &receive_queue;
	unsigned frames = 0;
	uint32_t length;
	void *done;

	while ((done = vio_queue_get(q->vq, &length)) != NULL) {
		uint16_t id = BUFFER_OF(done);

		if (length > sizeof(struct virtio_net_header) && length <= BUFFER_SIZE) {
			redwood_net_input(q->buffers + (size_t)id * BUFFER_SIZE + sizeof(struct virtio_net_header),
			    length - sizeof(struct virtio_net_header));
		}
		give(q, id, BUFFER_SIZE, true);         /* the buffer goes back */
		frames++;
	}
	if (frames) {
		vio_queue_kick(q->vq);
	}
	return frames;
}

static bool
virtio_net_start(uint8_t address[ETHER_ADDR_LEN])
{
	uint64_t offered, features;
	int error;

	if ((net_dev = vio_find(VIRTIO_ID_NET, 0)) == NULL) {
		printf("net: no virtio network device (QEMU: -device virtio-net-pci or virtio-net-device)\n");
		return false;
	}
	offered = vio_device_features(net_dev);
	if ((error = vio_init(net_dev, VIRTIO_BIT(VIRTIO_NET_F_MAC), &features)) != 0) {
		printf("net: %s: features refused (the device offers %#llx)\n", vio_name(net_dev), offered);
		return false;
	}
	if (!queue_make(&receive_queue, QUEUE_RECEIVE, true) || !queue_make(&transmit_queue, QUEUE_TRANSMIT, false)) {
		vio_fail(net_dev);
		return false;
	}
	if (features & VIRTIO_BIT(VIRTIO_NET_F_MAC)) {
		vio_cfg_read(net_dev, CFG_MAC, address, ETHER_ADDR_LEN);
	}
	vio_driver_ok(net_dev);
	vio_queue_kick(receive_queue.vq);
	printf("net: virtio network device on %s (it offers features %#llx)\n", vio_name(net_dev), offered);
	return true;
}

static struct redwood_net_link virtio_link = {
	.name = "a virtio network device",
	.address = { 0x02, 'r', 'e', 'd', 'w', 'd' },   /* if the device has none: one "locally administered" */
	.up = true,
	.transmit = virtio_transmit,
	.poll = virtio_poll,
};

void
redwood_virtio_net_init(void)
{
	uint32_t enabled = 1;

	if (PE_parse_boot_argn("virtio_net", &enabled, sizeof(enabled)) && !enabled) {
		return;
	}
	if (vio_find(VIRTIO_ID_NET, 0) == NULL) {
		return;                 /* quietly: most machines have none */
	}
	if (!virtio_net_start(virtio_link.address) || !redwood_net_attach(&virtio_link)) {
		printf("net: no network interface\n");
	}
}
