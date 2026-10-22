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
 *
 * Boot-arg virtio_net_wifi=1: iOS is told that en0 is a radio, the way the phone's is
 * (redwood_net.c, RedwoodWLAN.cpp, redwood_apple80211.c), and a station of this file's stands
 * behind it (the station boundary, sys/redwood_sta.h). It hears one open network, "Scion" (or
 * virtio_net_ssid=NAME), joins it at the start and whenever iOS asks, and then lets frames
 * pass. So Settings shows Wi-Fi on and that network joined, configd configures the interface
 * by itself (a "Wi-Fi" network service, DHCP), and the status bar has the Wi-Fi bars. Under
 * AVF (Scion's "Network" setting) the cable behind it is Android's VM tethering.
 */
#include <kern/thread.h>
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <mach/vm_param.h>
#include <net/ethernet.h>
#include <pexpert/pexpert.h>
#include <sys/kpi_mbuf.h>
#include <sys/errno.h>
#include <sys/redwood_net.h>
#include <sys/redwood_sta.h>
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

/* ---- virtio_net_wifi=1: a station with one network, always in reach ------------------------------------- */

#define VSTA_FREQUENCY          2437            /* channel 6 */
#define VSTA_RSSI               (-45)

static struct redwood_sta_bss vsta_bss;
static enum redwood_sta_phase vsta_phase = REDWOOD_STA_IDLE;
static int vsta_error;

static void
vsta_link(bool up)
{
	redwood_net_link_state(up);
}

static int
vsta_power(bool on)
{
	if (!on && vsta_phase == REDWOOD_STA_JOINED) {
		vsta_link(false);
	}
	vsta_phase = on ? REDWOOD_STA_IDLE : REDWOOD_STA_OFF;
	return 0;
}

static int
vsta_scan(__unused const struct redwood_sta_scan *what)
{
	return vsta_phase == REDWOOD_STA_OFF ? ENETDOWN : 0;
}

static void
vsta_heard(void (*each)(const struct redwood_sta_bss *bss, void *context), void *context)
{
	each(&vsta_bss, context);
}

static int
vsta_join(const struct redwood_sta_join *what)
{
	bool ours = what->ssid_length == vsta_bss.ssid_length && memcmp(what->ssid, vsta_bss.ssid, what->ssid_length) == 0;

	if (vsta_phase == REDWOOD_STA_OFF) {
		return ENETDOWN;
	}
	if (!ours) {
		vsta_error = ENOENT;            /* (nothing else is in reach) */
		return ENOENT;
	}
	if (what->security != REDWOOD_STA_OPEN) {
		vsta_error = EAUTH;             /* (it is an open network: a key is the wrong one) */
		return EAUTH;
	}
	vsta_error = 0;
	if (vsta_phase != REDWOOD_STA_JOINED) {
		vsta_phase = REDWOOD_STA_JOINED;
		vsta_link(true);
	}
	return 0;
}

static int
vsta_leave(void)
{
	if (vsta_phase == REDWOOD_STA_JOINED) {
		vsta_link(false);
		vsta_phase = REDWOOD_STA_IDLE;
	}
	return 0;
}

static void
vsta_state(struct redwood_sta_state *state)
{
	state->phase = vsta_phase;
	state->bss = vsta_bss;
	state->last_error = vsta_error;
}

static unsigned
vsta_channels(struct redwood_sta_channel *channels, unsigned most)
{
	static const uint16_t frequencies[] = { 2412, 2417, 2422, 2427, 2432, 2437, 2442, 2447, 2452, 2457, 2462, 5180, 5200, 5220, 5240 };
	unsigned count = 0;

	for (unsigned i = 0; i < sizeof(frequencies) / sizeof(frequencies[0]) && count < most; i++) {
		channels[count++] = (struct redwood_sta_channel){ .frequency = frequencies[i] };
	}
	return count;
}

static struct redwood_sta_ops vsta_ops = {
	.power = vsta_power,
	.scan = vsta_scan,
	.heard = vsta_heard,
	.join = vsta_join,
	.leave = vsta_leave,
	.state = vsta_state,
	.channels = vsta_channels,
};

/* The network: what a beacon of an open 802.11g access point on channel 6 would say. */
static void
vsta_start(const uint8_t address[ETHER_ADDR_LEN])
{
	static const uint8_t rates[] = { 0x82, 0x84, 0x8b, 0x96, 0x0c, 0x12, 0x18, 0x24 };     /* 1 2 5.5 11 basic; 6 9 12 18 */
	static const uint8_t more_rates[] = { 0x30, 0x48, 0x60, 0x6c };                       /* 24 36 48 54 */
	static struct redwood_sta_join join;
	char name[33] = "Scion";
	struct redwood_sta_bss *b = &vsta_bss;
	uint8_t *e = b->elements;
	size_t length;

	PE_parse_boot_argn("virtio_net_ssid", name, sizeof(name));
	name[32] = 0;
	length = strlen(name);
	if (length == 0) {
		strlcpy(name, "Scion", sizeof(name));
		length = 5;
	}
	memcpy(b->ssid, name, length);
	b->ssid_length = (uint8_t)length;
	/* the access point's address: ours with another last byte, and "locally administered" */
	memcpy(b->bssid, address, ETHER_ADDR_LEN);
	b->bssid[0] = (uint8_t)((b->bssid[0] | 0x02) & ~0x01);
	b->bssid[5] ^= 0x01;
	b->frequency = VSTA_FREQUENCY;
	b->rssi = VSTA_RSSI;
	b->beacon_interval = 100;
	b->capability = 0x0421;                 /* an access point (ESS), short preamble, short slot time; no privacy */
	*e++ = 0;                               /* SSID */
	*e++ = (uint8_t)length;
	memcpy(e, name, length);
	e += length;
	*e++ = 1;                               /* supported rates */
	*e++ = sizeof(rates);
	memcpy(e, rates, sizeof(rates));
	e += sizeof(rates);
	*e++ = 3;                               /* the channel */
	*e++ = 1;
	*e++ = 6;
	*e++ = 50;                              /* extended rates */
	*e++ = sizeof(more_rates);
	memcpy(e, more_rates, sizeof(more_rates));
	e += sizeof(more_rates);
	b->elements_length = (uint16_t)(e - b->elements);

	memcpy(vsta_ops.address, address, ETHER_ADDR_LEN);
	redwood_sta_attach(&vsta_ops);
	/* joined at the start, and again whenever iOS turns Wi-Fi back on (as the phone joins the network of its file) */
	bzero(&join, sizeof(join));
	memcpy(join.ssid, b->ssid, length);
	join.ssid_length = b->ssid_length;
	join.security = REDWOOD_STA_OPEN;
	if (redwood_sta_ask_join_standing(&join) != 0) {
		printf("net: the virtual station did not take the standing join\n");
	}
	printf("net: en0 is a radio (virtio_net_wifi=1): one open network, \"%s\", joined at the start\n", name);
}

void
redwood_virtio_net_init(void)
{
	uint32_t enabled = 1, wifi = 0;

	if (PE_parse_boot_argn("virtio_net", &enabled, sizeof(enabled)) && !enabled) {
		return;
	}
	if (vio_find(VIRTIO_ID_NET, 0) == NULL) {
		return;                 /* quietly: most machines have none */
	}
	if (PE_parse_boot_argn("virtio_net_wifi", &wifi, sizeof(wifi)) && wifi) {
		virtio_link.wireless = true;
		virtio_link.up = false;         /* until the station has joined its network */
	}
	if (!virtio_net_start(virtio_link.address) || !redwood_net_attach(&virtio_link)) {
		printf("net: no network interface\n");
		return;
	}
	if (virtio_link.wireless) {
		vsta_start(virtio_link.address);
	}
}
