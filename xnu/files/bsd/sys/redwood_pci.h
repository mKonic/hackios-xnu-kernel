/*
 * A generic PCI host bridge: the FDT's "pci-host-ecam-generic" (QEMU virt) or
 * "pci-host-cam-generic" (crosvm, the Android Virtualization Framework), described to the
 * kernel by the loader as the device tree node /pci0 (see bsd/dev/redwood_pci.c for its
 * properties). Only the first bus is enumerated: both machines put every device on it.
 * BARs that the firmware or the VMM left programmed are kept; the others are assigned from
 * the bridge's memory windows. INTx is mapped to a GIC interrupt through the node's
 * interrupt map; MSI/MSI-X are not used.
 *
 * Plain C, no allocation and no locks in enumeration and config access, so the console
 * (redwood_virtio_console.c) can use it from serial_init(). C++ callers (iokit) include this
 * inside extern "C" (it does that itself).
 */
#ifndef _SYS_REDWOOD_PCI_H_
#define _SYS_REDWOOD_PCI_H_

#include <stdint.h>
#include <stdbool.h>
#include <sys/cdefs.h>

__BEGIN_DECLS

#define REDWOOD_PCI_MAX_DEVS    32

#define REDWOOD_PCI_BAR_MEM64   0x01    /* 64-bit memory BAR (takes this slot and the next) */
#define REDWOOD_PCI_BAR_PREFETCH 0x02
#define REDWOOD_PCI_BAR_IO      0x04    /* an I/O BAR: never enabled */

struct redwood_pci_dev {
	uint8_t         bus, slot, func;
	uint8_t         int_pin;        /* 1..4 = INTA..INTD, 0 = none */
	uint16_t        vendor, device;
	uint16_t        subsys_vendor, subsys;
	uint32_t        class_rev;      /* class code << 8 | revision */
	uint32_t        intid;          /* GIC INTID of its INTx line, 0 = none */
	bool            intx_edge;      /* the map says edge-triggered (normally level) */
	bool            firmware;       /* still in use by the firmware: leave it alone */
	uint64_t        bar_pa[6];      /* CPU physical address, 0 = absent or unassigned */
	uint64_t        bar_size[6];
	uint8_t         bar_flags[6];
	uintptr_t       cfg;            /* kernel VA of the function's configuration space */
	uint32_t        cfg_size;       /* 256 (CAM) or 4096 (ECAM) */
};

/* Find /pci0, enumerate, assign BARs. Idempotent; 0, or ENXIO without a PCI bridge. */
int redwood_pci_init(void);
/* Print the device table (once; not from serial_init, printf is not up there yet). */
void redwood_pci_dump(void);
unsigned redwood_pci_count(void);
struct redwood_pci_dev *redwood_pci_device(unsigned index);

uint8_t  redwood_pci_cfg_read8(const struct redwood_pci_dev *d, uint32_t off);
uint16_t redwood_pci_cfg_read16(const struct redwood_pci_dev *d, uint32_t off);
uint32_t redwood_pci_cfg_read32(const struct redwood_pci_dev *d, uint32_t off);
void redwood_pci_cfg_write8(const struct redwood_pci_dev *d, uint32_t off, uint8_t v);
void redwood_pci_cfg_write16(const struct redwood_pci_dev *d, uint32_t off, uint16_t v);
void redwood_pci_cfg_write32(const struct redwood_pci_dev *d, uint32_t off, uint32_t v);
/* The next capability with this id after offset 'after' (0: from the first), 0 if none. */
uint8_t redwood_pci_find_cap(const struct redwood_pci_dev *d, uint8_t cap_id, uint8_t after);
/* Map [off, off + size) of a memory BAR as device memory; 0 on failure. */
uintptr_t redwood_pci_map_bar(const struct redwood_pci_dev *d, unsigned bar, uint64_t off, uint64_t size);
/* Command register: memory decoding and bus mastering on, INTx on or off. */
void redwood_pci_enable(const struct redwood_pci_dev *d, bool intx);

__END_DECLS

#endif /* _SYS_REDWOOD_PCI_H_ */
