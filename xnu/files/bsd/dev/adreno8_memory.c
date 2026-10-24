/* GPU/GMU shared allocations retained until a full device reset. */
#include "adreno8_memory_private.h"
#include "adreno8_hwsched_private.h"

#define printf adreno8_say
#define MEMORY_GRANULE 4096u
#define MEMORY_COUNT 64u
#define MEMORY_BLOCK_LIMIT (64u * 1024u * 1024u)
#define MEMORY_TOTAL_LIMIT (128u * 1024u * 1024u)
#define GPU_IOVA_START 0x10000000u
#define GPU_IOVA_END 0xe0000000u
#define GPU_MATCH 0u

struct adreno8_memory_entry {
	struct adreno8_memory buffer;
	struct adreno8_hfi_mem_request request;
	bool from_hfi, complete;
};

static struct adreno8_memory_entry adreno8_memory_entries[MEMORY_COUNT];
static uint32_t adreno8_memory_count;
static uint64_t adreno8_memory_bytes;
static bool adreno8_memory_failed;

bool
adreno8_memory_retained(void)
{
	return adreno8_memory_count != 0 || adreno8_gpu_space.table_count != 0 || adreno8_gpu_space.is_on;
}

static struct adreno8_memory *
adreno8_memory_alloc(const char *name, uint32_t size, uint32_t alignment,
    uint32_t gpu_prot, uint32_t gmu_prot, bool gmu_cacheable)
{
	struct adreno8_memory_entry *entry;
	struct adreno8_memory *buffer;
	uint64_t allocated = ((uint64_t)size + PAGE_MASK) & ~(uint64_t)PAGE_MASK;
	uint32_t gpu_address = 0, gmu_address = 0;

	if (adreno8_memory_failed || adreno8_memory_count == MEMORY_COUNT || name == NULL ||
	    size == 0 || (size & (MEMORY_GRANULE - 1)) || size > MEMORY_BLOCK_LIMIT ||
	    alignment < MEMORY_GRANULE || (alignment & (alignment - 1)) ||
	    (gpu_prot && (!(gpu_prot & ADRENO8_MEM_READ) || (gpu_prot & ~15u))) ||
	    (gmu_prot && (!(gmu_prot & ADRENO8_MEM_READ) || (gmu_prot & ~15u))) ||
	    allocated > MEMORY_TOTAL_LIMIT - adreno8_memory_bytes) {
		return NULL;
	}
	if (gpu_prot && !adreno8_space_find_range(&adreno8_gpu_space, GPU_IOVA_START,
	    GPU_IOVA_END, size, alignment, &gpu_address)) {
		return NULL;
	}
	if (gmu_prot && (adreno8_gmu_space.table_count == 0 ||
	    !adreno8_space_find_range(&adreno8_gmu_space, gmu_cacheable ? 0x4000 : 0x60000000,
	    gmu_cacheable ? 0x1000000 : 0x80000000, size, alignment, &gmu_address))) {
		return NULL;
	}
	if (gpu_prot && adreno8_gpu_space.table_count == 0 &&
	    adreno8_space_new_table(&adreno8_gpu_space, 0, 0) == 0) {
		return NULL;
	}
	entry = &adreno8_memory_entries[adreno8_memory_count];
	buffer = &entry->buffer;
	if (kmem_alloc(kernel_map, &buffer->memory, (vm_size_t)allocated,
	    KMA_KOBJECT | KMA_ZERO | KMA_DATA, VM_KERN_MEMORY_IOKIT) != KERN_SUCCESS) {
		buffer->memory = 0;
		return NULL;
	}
	/* Record ownership before any descriptor can expose this allocation to DMA. */
	adreno8_memory_count++;
	adreno8_memory_bytes += allocated;
	buffer->name = name;
	buffer->size = size;
	buffer->gpu_address = gpu_address;
	buffer->gmu_address = gmu_address;
	adreno8_to_memory(buffer->memory, (vm_size_t)allocated);
	if ((gpu_prot && !adreno8_space_map_region(&adreno8_gpu_space, gpu_address,
	    buffer->memory, size, gpu_prot)) ||
	    (gmu_prot && !adreno8_space_map_region(&adreno8_gmu_space, gmu_address,
	    buffer->memory, size, gmu_prot))) {
		adreno8_memory_failed = true;
		printf("adreno8: shared memory mapping failed; retain allocations until cold reset\n");
		return NULL;
	}
	entry->complete = true;
	printf("adreno8: shared buffer %s: size %#x, GPU %#x, GMU %#x\n",
	    name, size, gpu_address, gmu_address);
	return buffer;
}

struct adreno8_memory *
adreno8_gpu_alloc(const char *name, uint32_t size, uint32_t alignment, uint32_t prot)
{
	uint64_t rounded = ((uint64_t)size + MEMORY_GRANULE - 1) & ~(uint64_t)(MEMORY_GRANULE - 1);

	if (size == 0 || rounded > MEMORY_BLOCK_LIMIT || prot == 0) {
		return NULL;
	}
	return adreno8_memory_alloc(name, (uint32_t)rounded, alignment, prot, 0, false);
}

bool
adreno8_gpu_memory_ready(void)
{
	return adreno8_gpu_space.is_on && !adreno8_memory_failed;
}

bool
adreno8_gpu_memory_enable(void)
{
	if (adreno8_memory_failed || !adreno8_cx_ready()) {
		return false;
	}
	if (adreno8_gpu_space.is_on) {
		return true;
	}
	if (!adreno8_space_on(&adreno8_gpu_space, 0, GPU_MATCH)) {
		adreno8_memory_failed = true;
		return false;
	}
	return true;
}

bool
adreno8_gpu_faulted(void)
{
	bool fault = adreno8_memory_failed;
	if (adreno8_gpu_space.is_on) fault |= adreno8_space_faulted(&adreno8_gpu_space);
	if (adreno8_gmu_space.is_on) fault |= adreno8_space_faulted(&adreno8_gmu_space);
	return fault;
}

static bool
adreno8_memory_range(const struct adreno8_memory *buffer, uint32_t offset, uint32_t size)
{
	for (unsigned i = 0; i < adreno8_memory_count; i++) {
		if (buffer == &adreno8_memory_entries[i].buffer && adreno8_memory_entries[i].complete) {
			return size != 0 && offset <= buffer->size && size <= buffer->size - offset;
		}
	}
	return false;
}

bool
adreno8_memory_publish(const struct adreno8_memory *buffer, uint32_t offset, uint32_t size)
{
	if (!adreno8_memory_range(buffer, offset, size)) return false;
	adreno8_to_memory(buffer->memory + offset, size);
	return true;
}

static void
adreno8_memory_invalidate(vm_offset_t start, uint32_t size)
{
	adreno8_settle();
	for (vm_offset_t line = start & ~(vm_offset_t)63; line < start + size; line += 64) {
		__asm__ volatile ("dc ivac, %0" : : "r" (line) : "memory");
	}
	adreno8_settle();
}

bool
adreno8_memory_acquire(const struct adreno8_memory *buffer, uint32_t offset, uint32_t size)
{
	if (!adreno8_memory_range(buffer, offset, size)) return false;
	adreno8_memory_invalidate(buffer->memory + offset, size);
	return true;
}

bool
adreno8_hwsched_alloc(const struct adreno8_hfi_mem_request *request, struct adreno8_hfi_mem_result *result)
{
	uint32_t gpu_prot = 0, gmu_prot = 0, va_shift, size_shift;
	uint64_t rounded;
	struct adreno8_memory *buffer = NULL;

	if (result == NULL) return false;
	memset(result, 0, sizeof(*result));
	if (request == NULL || adreno8_memory_failed || request->size == 0 || request->kind >= 27 ||
	    (request->flags & ~0x3ffu) || (request->align & ~0xffffu)) {
		return false;
	}
	/* These kinds require secure or peripheral resources that this allocator does not own. */
	if (request->kind == 5 || request->kind == 11 || request->kind == 12 || request->kind == 13 ||
	    request->kind == 19 || request->kind == 20 || request->kind == 23) return false;
	for (unsigned i = 0; i < adreno8_memory_count; i++) {
		struct adreno8_memory_entry *entry = &adreno8_memory_entries[i];
		if (entry->from_hfi && entry->request.kind == request->kind && entry->request.handle == request->handle) {
			if (!entry->complete || entry->request.flags != request->flags ||
			    entry->request.size != request->size || entry->request.align != request->align) return false;
			buffer = &entry->buffer;
			goto success;
		}
	}
	va_shift = request->align & 0xff;
	size_shift = request->align >> 8;
	if (va_shift > 31 || size_shift > 31) return false;
	if (va_shift < 12) va_shift = 12;
	if (size_shift < 12) size_shift = 12;
	rounded = ((uint64_t)request->size + (1ULL << size_shift) - 1) & ~((1ULL << size_shift) - 1);
	if (rounded > MEMORY_BLOCK_LIMIT) return false;
	if (request->flags & ADRENO8_HFI_MEM_GFX) {
		gpu_prot = ADRENO8_MEM_READ;
		if (request->flags & ADRENO8_HFI_MEM_GFX_WRITE) gpu_prot |= ADRENO8_MEM_WRITE;
		if (request->flags & ADRENO8_HFI_MEM_GFX_PRIV) gpu_prot |= ADRENO8_MEM_PRIV;
	}
	if (!(request->flags & ADRENO8_HFI_MEM_GFX)) {
		/* The vendor's GMU-only reserve path always grants privileged read/write access. */
		gmu_prot = ADRENO8_MEM_READ | ADRENO8_MEM_WRITE | ADRENO8_MEM_PRIV | ADRENO8_MEM_EXEC;
	} else if (request->flags & ADRENO8_HFI_MEM_GMU) {
		/* Vendor get_attrs leaves GMU execute permission enabled; CACHEABLE selects its VA range. */
		gmu_prot = ADRENO8_MEM_READ | ADRENO8_MEM_EXEC;
		if (request->flags & ADRENO8_HFI_MEM_GMU_WRITE) gmu_prot |= ADRENO8_MEM_WRITE;
		if (request->flags & ADRENO8_HFI_MEM_GMU_PRIV) gmu_prot |= ADRENO8_MEM_PRIV;
	}
	buffer = adreno8_memory_alloc("hwsched", (uint32_t)rounded, 1u << va_shift,
	    gpu_prot, gmu_prot, (request->flags & ADRENO8_HFI_MEM_CACHEABLE) != 0);
	if (buffer == NULL) return false;
	adreno8_memory_entries[adreno8_memory_count - 1].from_hfi = true;
	adreno8_memory_entries[adreno8_memory_count - 1].request = *request;
success:
	result->gpu_address = buffer->gpu_address;
	result->gmu_address = buffer->gmu_address;
	result->size = buffer->size;
	return true;
}
