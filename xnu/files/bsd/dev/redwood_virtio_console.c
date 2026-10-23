/*
 * A virtio console as a serial console of the kernel (pexpert/arm/pe_serial.c), for virtual
 * machines with no PL011 or 8250 to print on: crosvm's consoles (the Android Virtualization
 * Framework's hvc0, hvc1, ... are one virtio-console PCI function each), or QEMU's
 * "-device virtio-serial-pci -device virtconsole,nr=0".
 *
 * Port 0 only, without the multiport feature: then port 0 needs no control messages and is
 * connected as soon as the driver is (DRIVER_OK). Set up from serial_init(), before the kernel
 * can allocate or sleep, so the rings and buffers are static and everything is polled.
 *
 * Output: one descriptor per character, posted and the device notified at once (it suppresses
 * notifications itself while it is busy); transmit_ready says whether a descriptor is free
 * after taking back what the device is done with. Input: eight 64-byte buffers the device fills.
 *
 * It is the console only where there is no other: a VMM that also captures a UART (crosvm's
 * 8250 and its hvc0 go to the same place under AVF) would otherwise get every line twice.
 * boot-args: virtio_console=N (N >= 1)  the Nth console device, even next to a UART;
 *            virtio_console=0  none. Without it: the first, when there is no UART.
 */
#include <libkern/libkern.h>
#include <mach/mach_types.h>
#include <mach/vm_param.h>
#include <pexpert/pexpert.h>
#include <sys/param.h>
#include <sys/redwood_virtio.h>

extern uint64_t kvtophys(vm_offset_t va);

#define VCON_RX                 0               /* port 0 receiveq */
#define VCON_TX                 1               /* port 0 transmitq */
#define VCON_QUEUE              64
#define VCON_RX_BUFS            8
#define VCON_RX_BUF             64
#define VCON_RING_MEM           (16 * 1024)     /* vio_queue_mem_size(64) on 16 KB pages */

static uint8_t vcon_rx_ring[VCON_RING_MEM] __attribute__((aligned(16384)));
static uint8_t vcon_tx_ring[VCON_RING_MEM] __attribute__((aligned(16384)));
static uint8_t vcon_tx_bytes[VCON_QUEUE] __attribute__((aligned(64)));
static uint8_t vcon_rx_bytes[VCON_RX_BUFS][VCON_RX_BUF] __attribute__((aligned(64)));

static vio_dev_t *vcon_dev;
static vio_queue_t *vcon_rxq, *vcon_txq;
static uint64_t vcon_busy;                     /* transmit slots the device has */
static unsigned vcon_next;
static uint8_t *vcon_in;                       /* the buffer being read, NULL if none */
static uint32_t vcon_in_len, vcon_in_at;

static void
rx_give(unsigned i)
{
	struct vio_sg sg = { kvtophys((vm_offset_t)vcon_rx_bytes[i]), VCON_RX_BUF, true };
	(void)vio_queue_add(vcon_rxq, &sg, 1, vcon_rx_bytes[i]);
}

static void
tx_reclaim(void)
{
	void *done;

	while ((done = vio_queue_get(vcon_txq, NULL)) != NULL) {
		vcon_busy &= ~(1ull << ((uint8_t *)done - vcon_tx_bytes));
	}
}

/* pe_serial's init: nothing left to do after the probe, also not on a re-init. */
void
redwood_vcon_init(void)
{
}

unsigned int
redwood_vcon_transmit_ready(void)
{
	tx_reclaim();
	return vcon_busy != ~0ull && vio_queue_num_free(vcon_txq) != 0;
}

void
redwood_vcon_transmit_data(uint8_t c)
{
	unsigned slot = vcon_next;

	for (unsigned i = 0; i < VCON_QUEUE && (vcon_busy & (1ull << slot)); i++) {
		slot = (slot + 1) % VCON_QUEUE;
	}
	if (vcon_busy & (1ull << slot)) {
		return;         /* transmit_ready said no; drop rather than wait forever */
	}
	vcon_tx_bytes[slot] = c;
	struct vio_sg sg = { kvtophys((vm_offset_t)&vcon_tx_bytes[slot]), 1, false };
	if (vio_queue_add(vcon_txq, &sg, 1, &vcon_tx_bytes[slot]) == 0) {
		vcon_busy |= 1ull << slot;
		vcon_next = (slot + 1) % VCON_QUEUE;
		vio_queue_kick(vcon_txq);
	}
}

unsigned int
redwood_vcon_receive_ready(void)
{
	uint32_t len;

	if (vcon_in != NULL) {
		return 1;
	}
	if ((vcon_in = vio_queue_get(vcon_rxq, &len)) == NULL) {
		return 0;
	}
	vcon_in_len = MIN(len, (uint32_t)VCON_RX_BUF);
	vcon_in_at = 0;
	if (vcon_in_len == 0) {
		rx_give((unsigned)((vcon_in - &vcon_rx_bytes[0][0]) / VCON_RX_BUF));
		vio_queue_kick(vcon_rxq);
		vcon_in = NULL;
		return 0;
	}
	return 1;
}

uint8_t
redwood_vcon_receive_data(void)
{
	uint8_t c;

	if (!redwood_vcon_receive_ready()) {
		return 0;
	}
	c = vcon_in[vcon_in_at++];
	if (vcon_in_at == vcon_in_len) {
		rx_give((unsigned)((vcon_in - &vcon_rx_bytes[0][0]) / VCON_RX_BUF));
		vio_queue_kick(vcon_rxq);
		vcon_in = NULL;
	}
	return c;
}

/* From serial_init(): true when there is a console to register. other: a UART is registered. */
bool
redwood_vcon_probe(bool other)
{
	uint32_t which = 1;
	bool asked;

	if (vcon_dev != NULL) {
		return true;
	}
	asked = PE_parse_boot_argn("virtio_console", &which, sizeof(which));
	if (which == 0 || (other && !asked)) {
		return false;
	}
	if ((vcon_dev = vio_find(VIRTIO_ID_CONSOLE, which - 1)) == NULL) {
		return false;
	}
	if (vio_queue_mem_size(VCON_QUEUE) > VCON_RING_MEM ||
	    vio_init(vcon_dev, 0, NULL) != 0 ||
	    vio_queue_setup_mem(vcon_dev, VCON_RX, VCON_QUEUE, vcon_rx_ring, kvtophys((vm_offset_t)vcon_rx_ring), &vcon_rxq) != 0 ||
	    vio_queue_setup_mem(vcon_dev, VCON_TX, VCON_QUEUE, vcon_tx_ring, kvtophys((vm_offset_t)vcon_tx_ring), &vcon_txq) != 0) {
		vio_fail(vcon_dev);
		vcon_dev = NULL;
		return false;
	}
	vio_queue_intr(vcon_rxq, false);
	vio_queue_intr(vcon_txq, false);
	for (unsigned i = 0; i < VCON_RX_BUFS; i++) {
		rx_give(i);
	}
	if (vio_queue_size(vcon_txq) < VCON_QUEUE) {
		/* a smaller queue: never more characters in flight than it has descriptors */
		vcon_busy = ~0ull << vio_queue_size(vcon_txq);
	}
	vio_driver_ok(vcon_dev);
	vio_queue_kick(vcon_rxq);
	return true;
}

/* For the log, once the kernel can print: which device the console is. */
const char *
redwood_vcon_name(void)
{
	return vcon_dev ? vio_name(vcon_dev) : NULL;
}
