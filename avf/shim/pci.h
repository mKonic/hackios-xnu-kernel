#pragma once
#include <stdint.h>

/*
 * The PCI host bridge from the FDT (crosvm: pci-host-cam-generic at 0x2e000000), as the ADT node
 * /pci0 that the kernel's PCI host driver reads (bsd/dev/redwood_pci.c). From agent B's
 * xnuloader/loader.c (pci_from_fdt); see there for the node's format.
 */
#define PCI_MAX_RANGES  4
#define PCI_MAX_INTX    64

struct pci_range_le {
	uint32_t space, prefetch;
	uint64_t pci, cpu, size;
};

struct pci_intx_le {
	uint32_t hi, pin, intid, edge;
};

struct pci_host {
	int present;
	const char *compatible;
	uint64_t ecam[2];
	uint32_t bus_range[2];
	struct pci_range_le ranges[PCI_MAX_RANGES];
	uint32_t nranges;
	struct pci_intx_le intx[PCI_MAX_INTX];
	uint32_t nintx;
	uint32_t mask[2];
	uint32_t probe_only;
	uint32_t firmware_devfn[4];
	uint32_t nfirmware;
};

extern struct pci_host PCI;
int pci_from_fdt(const uint8_t *fdt);
