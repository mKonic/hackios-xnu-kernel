/* Generic FDT walker, see fdtx.h. */
#include "fdtx.h"

#define FDT_MAGIC	0xd00dfeed
#define FDT_BEGIN_NODE	1
#define FDT_END_NODE	2
#define FDT_PROP	3
#define FDT_NOP		4
#define FDT_END		9

uint32_t fdt_be32(uintptr_t a)
{
	return __builtin_bswap32(*(volatile uint32_t *)a);
}

uint64_t fdt_cells(uintptr_t a, int ncells)
{
	uint64_t v = 0;
	for (int i = 0; i < ncells; i++)
		v = v << 32 | fdt_be32(a + 4 * i);
	return v;
}

static uintptr_t align4(uintptr_t a)
{
	return (a + 3) & ~3UL;
}

static uint8_t rd8(uintptr_t a)
{
	return *(volatile uint8_t *)a;
}

static uintptr_t skip_str(uintptr_t p)
{
	while (rd8(p))
		p++;
	return p + 1;
}

static int streq_mem(uintptr_t a, const char *b)
{
	while (rd8(a) && rd8(a) == (uint8_t)*b)
		a++, b++;
	return rd8(a) == (uint8_t)*b;
}

int fdt_open(struct fdt *f, uintptr_t base)
{
	if (!base || (base & 7) || fdt_be32(base) != FDT_MAGIC)
		return 0;
	f->base = base;
	f->totalsize = fdt_be32(base + 4);
	f->strct = base + fdt_be32(base + 8);
	f->strs = base + fdt_be32(base + 12);
	f->struct_size = fdt_be32(base + 36);	/* v17 size_dt_struct */
	return 1;
}

/* Offset of the token after node `node`'s properties and children, i.e. its END_NODE + 4. */
static int skip_node(const struct fdt *f, int node)
{
	uintptr_t p = f->strct + node;
	int depth = 0;

	for (;;) {
		uint32_t tok = fdt_be32(p);
		p += 4;
		switch (tok) {
		case FDT_BEGIN_NODE:
			depth++;
			p = align4(skip_str(p));
			break;
		case FDT_END_NODE:
			if (--depth == 0)
				return (int)(p - f->strct);
			break;
		case FDT_PROP:
			p = align4(p + 8 + fdt_be32(p));
			break;
		case FDT_NOP:
			break;
		default:
			return -1;
		}
	}
}

/* First token offset after the node's name (where its properties start). */
static uintptr_t node_body(const struct fdt *f, int node)
{
	return align4(skip_str(f->strct + node + 4));
}

const char *fdt_name(const struct fdt *f, int node)
{
	return (const char *)(f->strct + node + 4);
}

uintptr_t fdt_prop(const struct fdt *f, int node, const char *name, uint32_t *len)
{
	if (node < 0)
		return 0;
	uintptr_t p = node_body(f, node);
	for (;;) {
		uint32_t tok = fdt_be32(p);
		if (tok == FDT_NOP) {
			p += 4;
			continue;
		}
		if (tok != FDT_PROP)
			return 0;
		uint32_t l = fdt_be32(p + 4), nameoff = fdt_be32(p + 8);
		if (streq_mem(f->strs + nameoff, name)) {
			if (len)
				*len = l;
			return p + 12;
		}
		p = align4(p + 12 + l);
	}
}

int fdt_first_child(const struct fdt *f, int node)
{
	if (node < 0)
		return -1;
	uintptr_t p = node_body(f, node);
	for (;;) {
		uint32_t tok = fdt_be32(p);
		if (tok == FDT_PROP)
			p = align4(p + 12 + fdt_be32(p + 4));
		else if (tok == FDT_NOP)
			p += 4;
		else if (tok == FDT_BEGIN_NODE)
			return (int)(p - f->strct);
		else
			return -1;
	}
}

int fdt_next_sibling(const struct fdt *f, int node)
{
	int end = skip_node(f, node);
	if (end < 0)
		return -1;
	uintptr_t p = f->strct + end;
	while (fdt_be32(p) == FDT_NOP)
		p += 4;
	return fdt_be32(p) == FDT_BEGIN_NODE ? (int)(p - f->strct) : -1;
}

/* Does node name "foo@1000" match path component [c, c+n)? */
static int name_match(uintptr_t name, const char *c, int n)
{
	int i;
	for (i = 0; i < n; i++)
		if (rd8(name + i) != (uint8_t)c[i])
			return 0;
	return rd8(name + i) == 0 || rd8(name + i) == '@';
}

int fdt_path(const struct fdt *f, const char *path)
{
	int node = 0;	/* root is the first node */
	if (fdt_be32(f->strct) != FDT_BEGIN_NODE)
		return -1;
	while (*path == '/')
		path++;
	while (*path) {
		int n = 0;
		while (path[n] && path[n] != '/')
			n++;
		int c;
		for (c = fdt_first_child(f, node); c >= 0; c = fdt_next_sibling(f, c))
			if (name_match(f->strct + c + 4, path, n))
				break;
		if (c < 0)
			return -1;
		node = c;
		path += n;
		while (*path == '/')
			path++;
	}
	return node;
}

int fdt_is_compatible(const struct fdt *f, int node, const char *compat)
{
	uint32_t len;
	uintptr_t v = fdt_prop(f, node, "compatible", &len);
	if (!v)
		return 0;
	uintptr_t end = v + len;
	while (v < end) {
		if (streq_mem(v, compat))
			return 1;
		v = skip_str(v);
	}
	return 0;
}

int fdt_find_compatible(const struct fdt *f, int after, const char *compat)
{
	/* Linear scan of every BEGIN_NODE token. */
	uintptr_t p = f->strct;
	int skip = after >= 0;

	for (;;) {
		uint32_t tok = fdt_be32(p);
		switch (tok) {
		case FDT_BEGIN_NODE: {
			int off = (int)(p - f->strct);
			if (skip) {
				if (off == after)
					skip = 0;
			} else if (fdt_is_compatible(f, off, compat)) {
				return off;
			}
			p = align4(skip_str(p + 4));
			break;
		}
		case FDT_END_NODE:
		case FDT_NOP:
			p += 4;
			break;
		case FDT_PROP:
			p = align4(p + 12 + fdt_be32(p + 4));
			break;
		default:
			return -1;
		}
	}
}
