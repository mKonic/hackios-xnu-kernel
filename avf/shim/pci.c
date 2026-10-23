/*
 * PCI host bridge from the FDT, for the ADT's /pci0. Copied from xnuloader/loader.c
 * (pci_from_fdt, by the virtio-pci track) with the UEFI parts left out.
 */
#include <stddef.h>
#include "pci.h"

static size_t
str_len(const char *s)
{
	size_t n = 0;
	while (s[n])
		n++;
	return n;
}

static int
str_eq(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return *a == *b;
}


struct pci_host PCI;

static uint32_t
fdt32(const uint8_t *p)
{
	return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static uint64_t
pci_fdt_cells(const uint8_t *p, uint32_t cells)
{
	uint64_t v = 0;
	for (uint32_t i = 0; i < cells; i++) {
		v = v << 32 | fdt32(p + 4 * i);
	}
	return v;
}

/* does a "compatible" list (len bytes of NUL-separated strings) contain want? */
static int
compat_has(const char *list, uint32_t len, const char *want)
{
	for (uint32_t at = 0; at < len;) {
		if (str_eq(list + at, want)) {
			return 1;
		}
		at += (uint32_t)str_len(list + at) + 1;
	}
	return 0;
}

#define FDT_MAX_CONTROLLERS 8

/*
 * The FDT, twice: first every interrupt controller (phandle -> #address-cells, #interrupt-cells),
 * then the host bridge and /chosen. Only what the bridge needs is understood.
 */
int
pci_from_fdt(const uint8_t *fdt)
{
	struct { uint32_t phandle, addr_cells, intr_cells; } ctl[FDT_MAX_CONTROLLERS];
	uint32_t nctl = 0;

	if (fdt32(fdt) != 0xd00dfeed) {
		return 0;
	}
	const uint8_t *st = fdt + fdt32(fdt + 8);
	const char *strs = (const char *)fdt + fdt32(fdt + 12);
	uint32_t st_size = fdt32(fdt + 36);

	for (int pass = 0; pass < 2; pass++) {
		/* the node being read, depth by depth: only the innermost matters */
		uint32_t depth = 0, phandle = 0, addr_cells = 2, intr_cells = 0, is_ctl = 0, is_pci = 0, is_chosen = 0;
		const uint8_t *reg = 0, *ranges = 0, *imap = 0, *imask = 0, *busr = 0;
		uint32_t reg_len = 0, ranges_len = 0, imap_len = 0, imask_len = 0;
		const char *compat = 0;

		for (uint32_t at = 0; at + 4 <= st_size;) {
			uint32_t tok = fdt32(st + at);
			at += 4;
			if (tok == 1) {                 /* BEGIN_NODE name */
				const char *name = (const char *)st + at;
				at += ((uint32_t)str_len(name) + 1 + 3) & ~3u;
				depth++;
				phandle = 0, addr_cells = 2, intr_cells = 0, is_ctl = 0, is_pci = 0;
				is_chosen = depth == 2 && (str_eq(name, "chosen"));
				reg = ranges = imap = imask = busr = 0;
				compat = 0;
			} else if (tok == 3) {          /* PROP len nameoff value */
				uint32_t len = fdt32(st + at), nameoff = fdt32(st + at + 4);
				const uint8_t *v = st + at + 8;
				const char *pn = strs + nameoff;
				at += 8 + ((len + 3) & ~3u);
				if (str_eq(pn, "phandle") && len == 4) {
					phandle = fdt32(v);
				} else if (str_eq(pn, "#address-cells") && len == 4) {
					addr_cells = fdt32(v);
				} else if (str_eq(pn, "#interrupt-cells") && len == 4) {
					intr_cells = fdt32(v);
				} else if (str_eq(pn, "interrupt-controller")) {
					is_ctl = 1;
				} else if (str_eq(pn, "compatible")) {
					if (compat_has((const char *)v, len, "pci-host-ecam-generic")) {
						is_pci = 1, compat = "pci-host-ecam-generic";
					} else if (compat_has((const char *)v, len, "pci-host-cam-generic")) {
						is_pci = 1, compat = "pci-host-cam-generic";
					}
				} else if (str_eq(pn, "reg")) {
					reg = v, reg_len = len;
				} else if (str_eq(pn, "ranges")) {
					ranges = v, ranges_len = len;
				} else if (str_eq(pn, "interrupt-map")) {
					imap = v, imap_len = len;
				} else if (str_eq(pn, "interrupt-map-mask")) {
					imask = v, imask_len = len;
				} else if (str_eq(pn, "bus-range") && len == 8) {
					busr = v;
				} else if (pass == 1 && is_chosen && str_eq(pn, "linux,pci-probe-only") && len == 4) {
					PCI.probe_only = fdt32(v);
				}
			} else if (tok == 2) {          /* END_NODE: the node is complete */
				if (pass == 0 && is_ctl && phandle && nctl < FDT_MAX_CONTROLLERS) {
					ctl[nctl].phandle = phandle;
					ctl[nctl].addr_cells = addr_cells;
					ctl[nctl].intr_cells = intr_cells ? intr_cells : 3;
					nctl++;
				}
				if (pass == 1 && is_pci && !PCI.present && reg && reg_len >= 16) {
					/* the bridge sits under the root: 2 address and 2 size cells */
					PCI.present = 1;
					PCI.compatible = compat;
					PCI.ecam[0] = pci_fdt_cells(reg, 2);
					PCI.ecam[1] = pci_fdt_cells(reg + 8, 2);
					PCI.bus_range[0] = busr ? fdt32(busr) : 0;
					PCI.bus_range[1] = busr ? fdt32(busr + 4) : 0;
					/* ranges: child phys.hi phys.mid phys.lo, parent addr (2), size (2) */
					for (uint32_t o = 0; ranges && o + 28 <= ranges_len && PCI.nranges < PCI_MAX_RANGES; o += 28) {
						uint32_t hi = fdt32(ranges + o);
						struct pci_range_le *r = &PCI.ranges[PCI.nranges++];
						r->space = (hi >> 24) & 3;
						r->prefetch = (hi >> 30) & 1;
						r->pci = pci_fdt_cells(ranges + o + 4, 2);
						r->cpu = pci_fdt_cells(ranges + o + 12, 2);
						r->size = pci_fdt_cells(ranges + o + 20, 2);
					}
					/* interrupt-map: phys.hi mid lo, pin, phandle, parent unit, parent specifier */
					for (uint32_t o = 0; imap && o + 20 <= imap_len && PCI.nintx < PCI_MAX_INTX;) {
						uint32_t ph = fdt32(imap + o + 16), pa = 2, pi = 3;
						for (uint32_t c = 0; c < nctl; c++) {
							if (ctl[c].phandle == ph) {
								pa = ctl[c].addr_cells, pi = ctl[c].intr_cells;
							}
						}
						uint32_t n = 5 + pa + pi;
						if (o + 4 * n > imap_len) {
							break;
						}
						const uint8_t *spec = imap + o + 4 * (5 + pa);
						if (pi >= 2) {
							struct pci_intx_le *e = &PCI.intx[PCI.nintx++];
							uint32_t type = fdt32(spec), num = fdt32(spec + 4);
							e->hi = fdt32(imap + o);
							e->pin = fdt32(imap + o + 12);
							e->intid = type == 1 ? 16 + num : 32 + num;
							e->edge = pi >= 3 ? (fdt32(spec + 8) & 3) != 0 : 0;
						}
						o += 4 * n;
					}
					PCI.mask[0] = imask && imask_len >= 16 ? fdt32(imask) : 0xffff00;
					PCI.mask[1] = imask && imask_len >= 16 ? fdt32(imask + 12) : 7;
				}
				depth--;
				is_chosen = 0;
			} else if (tok == 9) {          /* END */
				break;
			} else if (tok != 4) {          /* anything but NOP: give up */
				break;
			}
		}
	}
	return PCI.present;
}

