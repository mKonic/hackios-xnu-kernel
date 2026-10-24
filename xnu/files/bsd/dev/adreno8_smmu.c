/*
 * adreno8_smmu.c -- the SMMU the GMU speaks through: its layout, the GMU's address space and its translation context: step 8 and the machinery steps 9 to 16 live in
 * The ladder, the boot-args and what is not settled: see adreno8.c; the shared world: adreno8_private.h.
 */
#include "adreno8_memory_private.h"

#define printf adreno8_say

/* ---- step 8: the SMMU in front of the GPU ---------------------------------------------------------------- */

/*
 * The device tree assigns SID 5 with mask 0 to the GMU on kgsl-smmu@3da0000.
 * The vendor driver reserves contexts 0/1 for GPU/LPAC and allocates other clients
 * from context 2. Register-bank offsets follow the SMMUv2 ID registers.
 */

static struct adreno8_block adreno8_smmu = { "smmu", 0x03da0000, 0x40000 };

static uint32_t adreno8_smmu_page, adreno8_smmu_pages, adreno8_smmu_contexts, adreno8_smmu_matches;

/* The layout of the register space from the ID registers: printed; only the context's room is required. */

static bool
adreno8_smmu_layout(void)
{
	uint32_t idr0 = adreno8_get(&adreno8_smmu, "idr0", SMMU_IDR0), idr1 = adreno8_get(&adreno8_smmu, "idr1", SMMU_IDR1);

	adreno8_smmu_page = idr1 >> 31 ? 0x10000 : 0x1000;
	adreno8_smmu_pages = 2u << (idr1 >> 28 & 7);
	adreno8_smmu_contexts = idr1 & 0xff;
	adreno8_smmu_matches = idr0 & 0xff;
	printf("adreno8: %u: SMMU: %u stream match registers, %u contexts, %u pages of %#x before the contexts' own\n", adreno8_step,
	    adreno8_smmu_matches, adreno8_smmu_contexts, adreno8_smmu_pages, adreno8_smmu_page);
	if (2 * adreno8_smmu_pages * adreno8_smmu_page != adreno8_smmu.size) {
		printf("adreno8: %u: that makes %#x of registers, the device tree says %#x: believed anyway (redwood's formula, an open question here)\n",
		    adreno8_step, 2 * adreno8_smmu_pages * adreno8_smmu_page, adreno8_smmu.size);
	}
	if (adreno8_smmu_contexts <= GMU_CONTEXT ||
	    (adreno8_smmu_pages + GMU_CONTEXT + 1) * adreno8_smmu_page > adreno8_smmu.size) {
		printf("adreno8: %u: GMU context %u is outside the SMMU register layout\n", adreno8_step, GMU_CONTEXT);
		return false;
	}
	return true;
}

bool
adreno8_step_smmu_read(void)
{
	if (!adreno8_cx_ready()) {
		return false;
	}
	adreno8_announce("read the ID registers of the GPU's SMMU (0x3da0000): global registers, which the hypervisor answers for");
	if (!adreno8_smmu_layout()) {
		return false;
	}
	adreno8_get(&adreno8_smmu, "idr2", SMMU_IDR2);
	adreno8_get(&adreno8_smmu, "idr7", SMMU_IDR7);
	adreno8_get(&adreno8_smmu, "scr0", SMMU_SCR0);
	adreno8_get(&adreno8_smmu, "sgfsr", SMMU_SGFSR);

	adreno8_announce("read the SMMU's stream table and the kinds of its contexts, as the boot loader and hypervisor left them");
	for (uint32_t i = 0; i < adreno8_smmu_matches && i < 16; i++) {
		adreno8_get(&adreno8_smmu, "smr", SMMU_SMR(i));
		adreno8_get(&adreno8_smmu, "s2cr", SMMU_S2CR(i));
	}
	for (uint32_t i = 0; i < adreno8_smmu_contexts && i < 16; i++) {
		adreno8_get(&adreno8_smmu, "cbar", adreno8_smmu_page + SMMU_CBAR(i));
	}

	adreno8_announce("read GPU context 0, LPAC context 1 and GMU context %u", GMU_CONTEXT);
	for (uint32_t cb = 0; cb <= GMU_CONTEXT; cb++) {
		uint32_t base = (adreno8_smmu_pages + cb) * adreno8_smmu_page;

		adreno8_get(&adreno8_smmu, "cb sctlr", base + SMMU_CB_SCTLR);
		adreno8_get(&adreno8_smmu, "cb ttbr0, low", base + SMMU_CB_TTBR0);
		adreno8_get(&adreno8_smmu, "cb ttbr0, high", base + SMMU_CB_TTBR0 + 4);
		adreno8_get(&adreno8_smmu, "cb tcr", base + SMMU_CB_TCR);
		adreno8_get(&adreno8_smmu, "cb fsr", base + SMMU_CB_FSR);
	}
	return true;
}

/* The SMMU uses 4 KiB granules independently of the XNU allocation page size. */
#define SMMU_GRANULE            4096u
#define SMMU_GRANULE_MASK       (SMMU_GRANULE - 1)
#define SMMU_PHYS_LIMIT         (1ULL << 36) /* TCR2.PASIZE = 1; IDR2 on SM8750 reports 36 bits. */
#define SMMU_CB_MAIR1            0x3c
#define SMMU_CB_PAR              0x50
#define SMMU_CB_ATS1PR           0x800
#define SMMU_CB_ATSR             0x8f0
#define SMMU_S2CR_FAULT          (2u << 16)
#define SMMU_S2CR_SHCFG_NSH      (3u << 8)
#define SMMU_IDR0_ATOSNS         (1u << 26)
#define SMMU_TCR2_TBI            (1u << 5 | 1u << 6)
#define SMMU_PTE_XN              (3ULL << 53)
#define SMMU_PTE_REQUIRED        (3ULL | 1ULL << 10 | 1ULL << 11)
#define SMMU_PTE_VARIABLE        (3ULL << 6 | 3ULL << 8 | SMMU_PTE_XN)

struct adreno8_gpu_space adreno8_gmu_space = { "gmu", GMU_CONTEXT };
struct adreno8_gpu_space adreno8_gpu_space = { "gpu", 0 };

static bool adreno8_space_sync(struct adreno8_gpu_space *space);

static struct adreno8_space_table *
adreno8_space_table(struct adreno8_gpu_space *space, unsigned level, uint32_t index)
{
	for (unsigned i = 0; i < space->table_count; i++) {
		if (space->tables[i].level == level && space->tables[i].index == index) {
			return &space->tables[i];
		}
	}
	return NULL;
}

static vm_offset_t
adreno8_space_add_table(struct adreno8_gpu_space *space, unsigned level, uint32_t index)
{
	struct adreno8_space_table *above = NULL, *t;

	if (level > 2 || space->table_count >= sizeof(space->tables) / sizeof(space->tables[0]) ||
	    PAGE_SIZE < SMMU_GRANULE || adreno8_space_table(space, level, index) != NULL) {
		return 0;
	}
	if (level == 0) {
		if (index != 0 || space->table_count != 0) {
			return 0;
		}
	} else {
		above = adreno8_space_table(space, level - 1, index >> 9);
		if (above == NULL || ((uint64_t *)above->table)[index & 0x1ff] != 0) {
			return 0;
		}
	}
	t = &space->tables[space->table_count];
	if (kmem_alloc(kernel_map, &t->table, PAGE_SIZE, KMA_KOBJECT | KMA_ZERO | KMA_DATA,
	    VM_KERN_MEMORY_IOKIT) != KERN_SUCCESS) {
		printf("adreno8: %u: page-table allocation failed\n", adreno8_step);
		t->table = 0;
		return 0;
	}
	t->phys = kvtophys(t->table);
	if (t->phys == 0 || t->phys >= SMMU_PHYS_LIMIT || (t->phys & SMMU_GRANULE_MASK) != 0) {
		kmem_free(kernel_map, t->table, PAGE_SIZE);
		t->table = 0;
		return 0;
	}
	t->level = level;
	t->index = index;
	if (above != NULL) {
		/* A live walker must see the initialized child before its parent becomes valid. */
		if (space->is_on) {
			adreno8_to_memory(t->table, SMMU_GRANULE);
		}
		((uint64_t *)above->table)[index & 0x1ff] = t->phys | PTE_TABLE;
		if (space->is_on) {
			adreno8_to_memory(above->table, SMMU_GRANULE);
		}
	}
	space->table_count++;
	return t->table;
}

vm_offset_t
adreno8_space_new_table(struct adreno8_gpu_space *space, unsigned level, uint32_t index)
{
	return space->is_on ? 0 : adreno8_space_add_table(space, level, index);
}

static uint64_t *
adreno8_space_entry(struct adreno8_gpu_space *space, uint32_t address)
{
	struct adreno8_space_table *leaf = adreno8_space_table(space, 2, address >> 21);
	return leaf == NULL ? NULL : &((uint64_t *)leaf->table)[address >> 12 & 0x1ff];
}

bool
adreno8_space_find_range(struct adreno8_gpu_space *space, uint32_t start, uint32_t end,
    uint32_t size, uint32_t alignment, uint32_t *address)
{
	uint64_t candidate;

	if (address == NULL || size == 0 || (size & SMMU_GRANULE_MASK) ||
	    alignment < SMMU_GRANULE || (alignment & (alignment - 1)) || start >= end) {
		return false;
	}
	candidate = ((uint64_t)start + alignment - 1) & ~(uint64_t)(alignment - 1);
	while (candidate + size <= end) {
		uint32_t at;
		for (at = 0; at < size; at += SMMU_GRANULE) {
			uint64_t *entry = adreno8_space_entry(space, (uint32_t)candidate + at);
			if (entry != NULL && *entry != 0) {
				break;
			}
		}
		if (at == size) {
			*address = (uint32_t)candidate;
			return true;
		}
		candidate = (candidate + at + SMMU_GRANULE + alignment - 1) & ~(uint64_t)(alignment - 1);
	}
	return false;
}

bool
adreno8_space_map_region(struct adreno8_gpu_space *space, uint32_t address,
    vm_offset_t memory, uint32_t size, uint32_t prot)
{
	uint64_t attributes = SMMU_PTE_REQUIRED | 2ULL << 8;

	if (!(prot & ADRENO8_MEM_READ) || (prot & ~15u) || memory == 0 || size == 0 ||
	    ((address | memory | size) & SMMU_GRANULE_MASK) ||
	    (uint64_t)address + size > (1ULL << 32) || memory + size < memory ||
	    space->table_count == 0 || space->tables[0].level != 0) {
		return false;
	}
	if (!(prot & ADRENO8_MEM_WRITE)) attributes |= 1ULL << 7;
	if (!(prot & ADRENO8_MEM_PRIV)) attributes |= 1ULL << 6;
	if (!(prot & ADRENO8_MEM_EXEC)) attributes |= SMMU_PTE_XN;
	/* Preflight every PA and existing PTE before publishing any data mapping. */
	for (uint64_t at = 0; at < size; at += SMMU_GRANULE) {
		uint64_t phys = kvtophys(memory + at);
		uint64_t *entry = adreno8_space_entry(space, address + at);
		if (phys == 0 || phys >= SMMU_PHYS_LIMIT || (phys & SMMU_GRANULE_MASK) ||
		    (entry != NULL && *entry != 0)) {
			return false;
		}
	}
	for (uint64_t at = 0; at < size; at += SMMU_GRANULE) {
		uint32_t va = address + at;
		if ((adreno8_space_table(space, 1, va >> 30) == NULL &&
		    adreno8_space_add_table(space, 1, va >> 30) == 0) ||
		    (adreno8_space_table(space, 2, va >> 21) == NULL &&
		    adreno8_space_add_table(space, 2, va >> 21) == 0)) {
			return false;
		}
	}
	for (uint64_t at = 0; at < size; at += SMMU_GRANULE) {
		*adreno8_space_entry(space, address + at) = kvtophys(memory + at) | attributes;
	}
	/* Only invalid-to-valid additions are supported while a context is running.
	 * The caller must retain backing memory on failure, including a TLB timeout. */
	if (space->is_on) {
		for (unsigned i = 0; i < space->table_count; i++) {
			adreno8_to_memory(space->tables[i].table, SMMU_GRANULE);
		}
		return adreno8_space_sync(space);
	}
	return true;
}

bool
adreno8_space_map(struct adreno8_gpu_space *space, uint32_t address, uint64_t phys)
{
	struct adreno8_space_table *leaf;
	uint32_t i0 = address >> 30, i01 = address >> 21;
	uint64_t *entry;

	if (space->is_on || ((address | phys) & SMMU_GRANULE_MASK) != 0 || phys == 0 || phys >= SMMU_PHYS_LIMIT) {
		return false;
	}
	if (adreno8_space_table(space, 1, i0) == NULL && adreno8_space_new_table(space, 1, i0) == 0) {
		return false;
	}
	leaf = adreno8_space_table(space, 2, i01);
	if (leaf == NULL) {
		if (adreno8_space_new_table(space, 2, i01) == 0) {
			return false;
		}
		leaf = adreno8_space_table(space, 2, i01);
	}
	entry = &((uint64_t *)leaf->table)[address >> 12 & 0x1ff];
	if (*entry != 0) {
		printf("adreno8: %u: duplicate IOVA %#x\n", adreno8_step, address);
		return false;
	}
	*entry = phys | PTE_PAGE;
	return true;
}

bool
adreno8_space_map_block(struct adreno8_gpu_space *space, const struct adreno8_gpu_block *b)
{
	uint64_t length = ((uint64_t)b->size + SMMU_GRANULE_MASK) & ~(uint64_t)SMMU_GRANULE_MASK;

	if (space->is_on || b->memory == 0 || b->size == 0 ||
	    ((b->address | b->memory) & SMMU_GRANULE_MASK) != 0 || (uint64_t)b->address + length > (1ULL << 32)) {
		return false;
	}
	/* Reject overlap before installing any part of this block. */
	for (uint64_t at = 0; at < length; at += SMMU_GRANULE) {
		uint32_t address = b->address + at;
		struct adreno8_space_table *leaf = adreno8_space_table(space, 2, address >> 21);

		if (leaf != NULL && ((uint64_t *)leaf->table)[address >> 12 & 0x1ff] != 0) {
			printf("adreno8: %u: block %s overlaps IOVA %#x\n", adreno8_step, b->name, address);
			return false;
		}
	}
	for (uint64_t at = 0; at < length; at += SMMU_GRANULE) {
		if (!adreno8_space_map(space, b->address + at, kvtophys(b->memory + at))) {
			return false;
		}
	}
	printf("adreno8: %u: %s IOVA %#x: %u mapped 4 KiB pages; XNU page size %#x\n", adreno8_step,
	    b->name, b->address, (unsigned)(length / SMMU_GRANULE), (unsigned)PAGE_SIZE);
	return true;
}

static bool
adreno8_space_sync(struct adreno8_gpu_space *space)
{
	uint32_t cb = (adreno8_smmu_pages + space->context) * adreno8_smmu_page;

	/* Every leaf has nG set, so this ASID invalidation covers the whole space. */
	adreno8_settle();
	adreno8_put(&adreno8_smmu, "cb tlb asid", cb + SMMU_CB_TLBASID, space->context);
	adreno8_put(&adreno8_smmu, "cb tlb sync", cb + SMMU_CB_TLBSYNC, 1);
	if (!adreno8_wait_for(&adreno8_smmu, cb + SMMU_CB_TLBSTATUS, 1, 0, 100)) {
		printf("adreno8: %u: context %u TLB sync timed out\n", adreno8_step, space->context);
		return false;
	}
	adreno8_settle();
	return true;
}

/* Use a 64-bit register write for AArch64 ATS1PR, as in arm-smmu. */
static void __attribute__((noinline))
adreno8_smmu_write64(vm_offset_t address, uint64_t value)
{
	__asm__ volatile ("str %0, [%1]" : : "r" (value), "r" (address) : "memory");
}

static bool
adreno8_space_verify(struct adreno8_gpu_space *space, bool atos)
{
	uint32_t cb = (adreno8_smmu_pages + space->context) * adreno8_smmu_page;
	unsigned pages = 0;

	for (unsigned i = 0; i < space->table_count; i++) {
		struct adreno8_space_table *t = &space->tables[i];
		if (t->level != 2) {
			continue;
		}
		struct adreno8_space_table *parent = adreno8_space_table(space, 1, t->index >> 9);
		if (parent == NULL || ((uint64_t *)parent->table)[t->index & 0x1ff] != (t->phys | PTE_TABLE) ||
		    ((uint64_t *)space->tables[0].table)[t->index >> 9] != (parent->phys | PTE_TABLE)) {
			return false;
		}
		for (unsigned entry = 0; entry < 512; entry++) {
			uint64_t pte = ((uint64_t *)t->table)[entry];
			uint32_t address = (t->index << 21) | (entry << 12);
			uint64_t par;

			if (pte == 0) {
				continue;
			}
			uint64_t attributes = pte & ~PTE_ADDRESS;
			if ((attributes & ~SMMU_PTE_VARIABLE) != SMMU_PTE_REQUIRED ||
			    (attributes & (3ULL << 8)) < (2ULL << 8) ||
			    (pte & PTE_ADDRESS) == 0 || (pte & PTE_ADDRESS) >= SMMU_PHYS_LIMIT) {
				return false;
			}
			pages++;
			if (!atos) {
				continue;
			}
			adreno8_smmu_write64(adreno8_smmu.base + cb + SMMU_CB_ATS1PR, address);
			adreno8_settle();
			if (!adreno8_wait_for(&adreno8_smmu, cb + SMMU_CB_ATSR, 1, 0, 1)) {
				printf("adreno8: %u: ATOS timed out for IOVA %#x\n", adreno8_step, address);
				return false;
			}
			par = adreno8_rd(adreno8_smmu.base + cb + SMMU_CB_PAR);
			par |= (uint64_t)adreno8_rd(adreno8_smmu.base + cb + SMMU_CB_PAR + 4) << 32;
			if ((par & 1) != 0 || (par & PTE_ADDRESS) != (pte & PTE_ADDRESS)) {
				printf("adreno8: %u: ATOS IOVA %#x: PAR %#llx, expected PA %#llx\n", adreno8_step,
				    address, par, pte & PTE_ADDRESS);
				return false;
			}
		}
	}
	printf("adreno8: %u: %u page mappings verified by %s\n", adreno8_step, pages, atos ? "ATOS" : "software table walk");
	return pages != 0;
}

static bool
adreno8_space_available(struct adreno8_gpu_space *space, uint32_t stream, uint32_t match)
{
	if (space->context >= adreno8_smmu_contexts || match >= adreno8_smmu_matches || stream > 0x7fff ||
	    (adreno8_smmu_pages + space->context + 1) * adreno8_smmu_page > adreno8_smmu.size) {
		return false;
	}
	for (unsigned i = 0; i < adreno8_smmu_matches; i++) {
		uint32_t smr = adreno8_get(&adreno8_smmu, "smr ownership", SMMU_SMR(i));
		uint32_t s2cr, mask;

		if (!(smr & SMR_VALID)) {
			continue;
		}
		s2cr = adreno8_get(&adreno8_smmu, "s2cr ownership", SMMU_S2CR(i));
		/* Reuse a blocked stream only with the same match and a disabled context. */
		if (i == match && smr == (SMR_VALID | SMR_MASK << 16 | stream) &&
		    (s2cr & 0x30000) == SMMU_S2CR_FAULT &&
		    adreno8_get(&adreno8_smmu, "cb ownership", (adreno8_smmu_pages + space->context) * adreno8_smmu_page + SMMU_CB_SCTLR) == 0) {
			continue;
		}
		mask = (smr >> 16 & 0x7fff) | SMR_MASK;
		if (i == match || ((smr ^ stream) & 0x7fff & ~mask) == 0 ||
		    ((s2cr & 0x30000) == 0 && (s2cr & 0xff) == space->context)) {
			printf("adreno8: %u: stream match %u conflicts with requested context %u\n", adreno8_step, i, space->context);
			return false;
		}
	}
	return true;
}

bool
adreno8_space_on(struct adreno8_gpu_space *space, uint32_t stream, uint32_t match)
{
	uint64_t root;
	uint32_t cb, idr0, idr1, idr2, smr = SMR_VALID | SMR_MASK << 16 | stream;
	uint32_t s2cr = SMMU_S2CR_SHCFG_NSH | space->context;
	bool atos, registers_ok;

	adreno8_announce("configure %s SMMU context %u and verify its page mappings", space->name, space->context);
	if (space->is_on || space->table_count == 0 || space->tables[0].level != 0 || !adreno8_smmu_layout() ||
	    !adreno8_space_available(space, stream, match)) {
		return false;
	}
	idr0 = adreno8_get(&adreno8_smmu, "idr0 capabilities", SMMU_IDR0);
	idr1 = adreno8_get(&adreno8_smmu, "idr1 capabilities", SMMU_IDR1);
	idr2 = adreno8_get(&adreno8_smmu, "idr2 capabilities", SMMU_IDR2);
	if (!(idr0 & 1u << 30) || !(idr0 & 1u << 27) || !(idr2 & 1u << 12) ||
	    (idr2 & 15) < 1 || (idr2 >> 4 & 15) < 1 || (idr2 >> 8 & 15) < 2 ||
	    space->context < (idr1 >> 16 & 0xff) || (adreno8_get(&adreno8_smmu, "scr0", SMMU_SCR0) & 1)) {
		printf("adreno8: %u: SMMU does not support the requested active 39-bit stage-1 context\n", adreno8_step);
		return false;
	}
	atos = !(idr0 & SMMU_IDR0_ATOSNS);
	if (!atos) {
		printf("adreno8: %u: IDR0.ATOSNS=1; hardware translation operations unavailable\n", adreno8_step);
	}
	if (!adreno8_space_verify(space, false)) {
		return false;
	}
	for (unsigned i = 0; i < space->table_count; i++) {
		adreno8_to_memory(space->tables[i].table, SMMU_GRANULE);
	}
	root = space->tables[0].phys | (uint64_t)space->context << 48;
	cb = (adreno8_smmu_pages + space->context) * adreno8_smmu_page;

	/* is_on also retains ownership after a failed write until cleanup is confirmed. */
	space->is_on = true;
	adreno8_put(&adreno8_smmu, "s2cr fault", SMMU_S2CR(match), SMMU_S2CR_FAULT | SMMU_S2CR_SHCFG_NSH);
	adreno8_put(&adreno8_smmu, "smr", SMMU_SMR(match), smr);
	if ((adreno8_get(&adreno8_smmu, "s2cr fault", SMMU_S2CR(match)) & 0x30000) != SMMU_S2CR_FAULT ||
	    adreno8_get(&adreno8_smmu, "smr", SMMU_SMR(match)) != smr) {
		goto fail;
	}
	adreno8_put(&adreno8_smmu, "cb sctlr", cb + SMMU_CB_SCTLR, 0);
	if (adreno8_get(&adreno8_smmu, "cb sctlr disabled", cb + SMMU_CB_SCTLR) != 0) {
		goto fail;
	}
	adreno8_put(&adreno8_smmu, "cb a2r", adreno8_smmu_page + SMMU_CBA2R(space->context), 1);
	adreno8_put(&adreno8_smmu, "cb bar", adreno8_smmu_page + SMMU_CBAR(space->context), STAGE_1);
	adreno8_put(&adreno8_smmu, "cb tcr2", cb + SMMU_CB_TCR2, TCR2_39_BITS);
	adreno8_put(&adreno8_smmu, "cb tcr", cb + SMMU_CB_TCR, TCR_39_BITS);
	adreno8_put(&adreno8_smmu, "cb ttbr0, low", cb + SMMU_CB_TTBR0, (uint32_t)root);
	adreno8_put(&adreno8_smmu, "cb ttbr0, high", cb + SMMU_CB_TTBR0 + 4, (uint32_t)(root >> 32));
	adreno8_put(&adreno8_smmu, "cb ttbr1, low", cb + SMMU_CB_TTBR1, 0);
	adreno8_put(&adreno8_smmu, "cb ttbr1, high", cb + SMMU_CB_TTBR1 + 4, 0);
	adreno8_put(&adreno8_smmu, "cb mair0", cb + SMMU_CB_MAIR0, MAIR0_NORMAL);
	adreno8_put(&adreno8_smmu, "cb mair1", cb + SMMU_CB_MAIR1, 0);
	adreno8_put(&adreno8_smmu, "cb fsr", cb + SMMU_CB_FSR, FSR_ALL);
	if (!adreno8_space_sync(space)) {
		goto fail;
	}
	adreno8_put(&adreno8_smmu, "cb sctlr", cb + SMMU_CB_SCTLR, SCTLR_ON);
	adreno8_settle();
	/* TBI0/1 can read as one on MMU-500; all IOVAs here are untagged 32-bit values. */
	registers_ok = adreno8_get(&adreno8_smmu, "cb sctlr", cb + SMMU_CB_SCTLR) == SCTLR_ON;
	registers_ok &= adreno8_get(&adreno8_smmu, "cb a2r", adreno8_smmu_page + SMMU_CBA2R(space->context)) == 1;
	registers_ok &= adreno8_get(&adreno8_smmu, "cb bar", adreno8_smmu_page + SMMU_CBAR(space->context)) == STAGE_1;
	registers_ok &= (adreno8_get(&adreno8_smmu, "cb tcr2", cb + SMMU_CB_TCR2) & ~SMMU_TCR2_TBI) == TCR2_39_BITS;
	registers_ok &= adreno8_get(&adreno8_smmu, "cb tcr", cb + SMMU_CB_TCR) == TCR_39_BITS;
	registers_ok &= adreno8_get(&adreno8_smmu, "cb ttbr0, low", cb + SMMU_CB_TTBR0) == (uint32_t)root;
	registers_ok &= adreno8_get(&adreno8_smmu, "cb ttbr0, high", cb + SMMU_CB_TTBR0 + 4) == (uint32_t)(root >> 32);
	registers_ok &= adreno8_get(&adreno8_smmu, "cb ttbr1, low", cb + SMMU_CB_TTBR1) == 0;
	registers_ok &= adreno8_get(&adreno8_smmu, "cb ttbr1, high", cb + SMMU_CB_TTBR1 + 4) == 0;
	registers_ok &= adreno8_get(&adreno8_smmu, "cb mair0", cb + SMMU_CB_MAIR0) == MAIR0_NORMAL;
	registers_ok &= adreno8_get(&adreno8_smmu, "cb mair1", cb + SMMU_CB_MAIR1) == 0;
	if (!registers_ok) {
		printf("adreno8: %u: context %u register verification failed\n", adreno8_step, space->context);
		goto fail;
	}
	if (atos && !adreno8_space_verify(space, true)) {
		goto fail;
	}
	adreno8_put(&adreno8_smmu, "s2cr translate", SMMU_S2CR(match), s2cr);
	adreno8_settle();
	if ((adreno8_get(&adreno8_smmu, "s2cr", SMMU_S2CR(match)) & 0x30303ff) != s2cr ||
	    adreno8_space_faulted(space)) {
		goto fail;
	}
	printf("adreno8: %u: %s translation enabled: stream %u, context %u, root PA %#llx\n", adreno8_step,
	    space->name, stream, space->context, space->tables[0].phys);
	return true;
fail:
	adreno8_space_off(space, match);
	return false;
}

void
adreno8_space_off(struct adreno8_gpu_space *space, uint32_t match)
{
	uint32_t cb = (adreno8_smmu_pages + space->context) * adreno8_smmu_page;
	bool stopped;

	if (!space->is_on) {
		return;
	}
	/* Keep the match valid: unmatched streams may bypass the SMMU. */
	adreno8_put(&adreno8_smmu, "s2cr fault", SMMU_S2CR(match), SMMU_S2CR_FAULT | SMMU_S2CR_SHCFG_NSH);
	adreno8_put(&adreno8_smmu, "cb sctlr", cb + SMMU_CB_SCTLR, 0);
	adreno8_settle();
	stopped = (adreno8_get(&adreno8_smmu, "s2cr fault", SMMU_S2CR(match)) & 0x30000) == SMMU_S2CR_FAULT &&
	    adreno8_get(&adreno8_smmu, "cb sctlr", cb + SMMU_CB_SCTLR) == 0;
	if (!(adreno8_rd(adreno8_smmu.base + SMMU_IDR0) & SMMU_IDR0_ATOSNS) &&
	    !adreno8_wait_for(&adreno8_smmu, cb + SMMU_CB_ATSR, 1, 0, 1)) {
		stopped = false;
	}
	if (!adreno8_space_sync(space) || !stopped) {
		printf("adreno8: %u: context %u cleanup incomplete; retain all mapped memory\n", adreno8_step, space->context);
		return;
	}
	space->is_on = false;
}

bool
adreno8_space_faulted(struct adreno8_gpu_space *space)
{
	uint32_t cb = (adreno8_smmu_pages + space->context) * adreno8_smmu_page, fsr;

	fsr = adreno8_rd(adreno8_smmu.base + cb + SMMU_CB_FSR) & FSR_ALL;
	if (fsr == 0) {
		return false;
	}
	printf("adreno8: %u: %s SMMU fault %#x at %#x:%#x, syndrome %#x\n", adreno8_step, space->name, fsr,
	    adreno8_rd(adreno8_smmu.base + cb + SMMU_CB_FAR_HI), adreno8_rd(adreno8_smmu.base + cb + SMMU_CB_FAR_LO),
	    adreno8_rd(adreno8_smmu.base + cb + SMMU_CB_FSYNR0));
	uint32_t address = adreno8_rd(adreno8_smmu.base + cb + SMMU_CB_FAR_LO);
	struct adreno8_space_table *leaf = adreno8_space_table(space, 2, address >> 21);
	printf("adreno8: %u: fault context SCTLR %#x, leaf PTE %#llx, global FSR %#x\n", adreno8_step,
	    adreno8_rd(adreno8_smmu.base + cb + SMMU_CB_SCTLR),
	    leaf != NULL ? ((uint64_t *)leaf->table)[address >> 12 & 0x1ff] : 0,
	    adreno8_rd(adreno8_smmu.base + SMMU_SGFSR));
	return true;
}
