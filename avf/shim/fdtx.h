#pragma once
#include <stdint.h>

/*
 * Read-only flattened device tree walker for the AVF target (crosvm and
 * QEMU virt hand us their FDT in x0). Nodes are identified by the offset of
 * their FDT_BEGIN_NODE token inside the structure block; -1 = none.
 * All loads are naturally aligned: the MMU is off (Device memory).
 */
struct fdt {
	uintptr_t base, strct, strs;
	uint32_t totalsize, struct_size;
};

int fdt_open(struct fdt *f, uintptr_t base);
/* "/", "/chosen", "/cpus"; a component without '@' matches "name@unit" */
int fdt_path(const struct fdt *f, const char *path);
/* first node (after offset `after`, -1 = from the start) with this compatible string */
int fdt_find_compatible(const struct fdt *f, int after, const char *compat);
int fdt_first_child(const struct fdt *f, int node);
int fdt_next_sibling(const struct fdt *f, int node);
const char *fdt_name(const struct fdt *f, int node);
/* property value (big endian, 4-byte aligned) or 0; *len set if non-null */
uintptr_t fdt_prop(const struct fdt *f, int node, const char *name, uint32_t *len);
int fdt_is_compatible(const struct fdt *f, int node, const char *compat);
/* big-endian cell readers */
uint32_t fdt_be32(uintptr_t a);
uint64_t fdt_cells(uintptr_t a, int ncells);
