#!/usr/bin/env python3
"""Exercise shared allocations through the production memory and SMMU code."""
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / "tools/test-adreno8-smmu.py"))
stub = fixture["stub"].replace("assert(size == 4096);", "assert(size != 0);")
driver = (ROOT / "xnu/files/bsd/dev/adreno8_memory.c").read_text()
driver, replaced = re.subn(
    r'static void\nadreno8_memory_invalidate\(.*?\n}',
    'static void adreno8_memory_invalidate(vm_offset_t start, uint32_t size) { invalidations++; }',
    driver, count=1, flags=re.S,
)
assert replaced == 1
setup = fixture["tests"].split("int main(void) {")[0]
tests = r'''
static void reset_memory(void) {
    reset(&adreno8_gmu_space, false);
    memset(&adreno8_gpu_space, 0, sizeof(adreno8_gpu_space));
    adreno8_gpu_space.name = "gpu";
    memset(adreno8_memory_entries, 0, sizeof(adreno8_memory_entries));
    adreno8_memory_count = 0;
    adreno8_memory_bytes = 0;
    adreno8_memory_failed = false;
    invalidations = 0;
    map_one(&adreno8_gmu_space);
    assert(adreno8_space_on(&adreno8_gmu_space, 5, 1));
}
int main(void) {
    struct adreno8_hfi_mem_result result, again;
    struct adreno8_hfi_mem_request request = {
        .flags = ADRENO8_HFI_MEM_GFX | ADRENO8_HFI_MEM_GFX_PRIV | ADRENO8_HFI_MEM_GFX_WRITE |
                 ADRENO8_HFI_MEM_GMU | ADRENO8_HFI_MEM_GMU_PRIV | ADRENO8_HFI_MEM_GMU_WRITE,
        .kind = 1, .handle = 42, .size = 8193, .align = (12 << 8) | 16,
    };
    reset_memory();
    assert(!adreno8_memory_retained() && !adreno8_gpu_memory_ready());
    struct adreno8_memory *sqe = adreno8_gpu_alloc("sqe", 111472, 4096,
        ADRENO8_MEM_READ | ADRENO8_MEM_EXEC);
    assert(sqe && sqe->size == 0x1c000 && sqe->gmu_address == 0);
    assert(adreno8_memory_retained() && !adreno8_gpu_memory_ready());
    for (unsigned i = 0; i < sqe->size; i++) assert(((uint8_t *)sqe->memory)[i] == 0);
    assert((*adreno8_space_entry(&adreno8_gpu_space, sqe->gpu_address) & ~PTE_ADDRESS) == 0xec3);
    memset((void *)sqe->memory, 0xa5, 111472);
    assert(adreno8_memory_publish(sqe, 0, 111472));
    assert(!adreno8_memory_publish(sqe, sqe->size - 1, 2));
    assert(!adreno8_memory_publish(NULL, 0, 1));
    assert(adreno8_gpu_memory_enable() && adreno8_gpu_memory_ready());
    unsigned before = writes;
    assert(adreno8_gpu_memory_enable() && writes == before);
    assert(!adreno8_gpu_faulted());
    assert(adreno8_hwsched_alloc(&request, &result));
    assert(result.size == 0x3000 && result.gmu_address == 0x60010000);
    assert((result.gpu_address & 0xffff) == 0 && result.gpu_address >= sqe->gpu_address + sqe->size);
    struct adreno8_memory *shared = &adreno8_memory_entries[1].buffer;
    for (unsigned offset = 0; offset < result.size; offset += 4096) {
        assert(walk(adreno8_gpu_space.tables[0].phys, result.gpu_address + offset) ==
               walk(adreno8_gmu_space.tables[0].phys, result.gmu_address + offset));
        assert(walk(adreno8_gpu_space.tables[0].phys, result.gpu_address + offset) ==
               kvtophys(shared->memory + offset));
    }
    assert((*adreno8_space_entry(&adreno8_gpu_space, result.gpu_address) & ~PTE_ADDRESS) ==
           (0xe03 | (3ULL << 53)));
    assert((*adreno8_space_entry(&adreno8_gmu_space, result.gmu_address) & ~PTE_ADDRESS) == 0xe03);
    unsigned allocations_before = allocation_count;
    before = writes;
    assert(adreno8_hwsched_alloc(&request, &again));
    assert(again.gpu_address == result.gpu_address && again.gmu_address == result.gmu_address);
    assert(allocation_count == allocations_before && writes == before);
    request.size++;
    assert(!adreno8_hwsched_alloc(&request, &again));
    assert(again.gpu_address == 0 && again.gmu_address == 0 && again.size == 0);
    request.size--;
    assert(adreno8_memory_acquire(shared, 0, shared->size) && invalidations == 1);
    assert(!adreno8_memory_acquire(shared, UINT32_MAX, 4096) && invalidations == 1);
    regs[(0x20000 + SMMU_CB_FSR) / 4] = 8;
    assert(adreno8_gpu_faulted());

    reset_memory();
    request = (struct adreno8_hfi_mem_request){.flags = ADRENO8_HFI_MEM_GMU |
        ADRENO8_HFI_MEM_GMU_PRIV | ADRENO8_HFI_MEM_CACHEABLE, .size = 17};
    assert(adreno8_hwsched_alloc(&request, &result));
    assert(result.gpu_address == 0 && result.gmu_address == 0x4000 && result.size == 4096);
    assert((*adreno8_space_entry(&adreno8_gmu_space, result.gmu_address) & ~PTE_ADDRESS) == 0xe03);
    request = (struct adreno8_hfi_mem_request){.size = 1, .handle = 1};
    assert(adreno8_hwsched_alloc(&request, &again));
    assert(again.gpu_address == 0 && again.gmu_address != 0);
    assert((*adreno8_space_entry(&adreno8_gmu_space, again.gmu_address) & ~PTE_ADDRESS) == 0xe03);
    const uint32_t rejected_kinds[] = {5, 11, 12, 13, 19, 20, 23, 27};
    for (unsigned i = 0; i < sizeof(rejected_kinds) / sizeof(rejected_kinds[0]); i++) {
        request.kind = rejected_kinds[i];
        assert(!adreno8_hwsched_alloc(&request, &again));
    }
    request.kind = 0;
    request.handle = 1;
    request.flags |= ADRENO8_HFI_MEM_SECURE;
    assert(!adreno8_hwsched_alloc(&request, &again));
    request.flags &= ~ADRENO8_HFI_MEM_SECURE;
    request.align = 32;
    assert(!adreno8_hwsched_alloc(&request, &again));
    request.align = 31 << 8;
    assert(!adreno8_hwsched_alloc(&request, &again));
    request.align = 12;
    request.size = UINT32_MAX;
    assert(!adreno8_hwsched_alloc(&request, &again));
    assert(adreno8_memory_count == 2);

    reset_memory();
    request = (struct adreno8_hfi_mem_request){.flags = ADRENO8_HFI_MEM_HOST, .size = 1};
    for (unsigned i = 0; i < MEMORY_COUNT; i++) {
        request.handle = i;
        assert(adreno8_hwsched_alloc(&request, &result));
        assert(result.gpu_address == 0 && result.gmu_address != 0);
    }
    request.handle++;
    assert(!adreno8_hwsched_alloc(&request, &result));
    reset_memory();
    adreno8_memory_bytes = MEMORY_TOTAL_LIMIT - PAGE_SIZE;
    assert(adreno8_hwsched_alloc(&request, &result));
    request.handle++;
    assert(!adreno8_hwsched_alloc(&request, &result));
    assert(adreno8_memory_bytes == MEMORY_TOTAL_LIMIT);

    reset_memory();
    request = (struct adreno8_hfi_mem_request){.flags = ADRENO8_HFI_MEM_GMU |
        ADRENO8_HFI_MEM_GMU_PRIV | ADRENO8_HFI_MEM_GMU_WRITE, .size = 4096};
    alloc_fail_after = 0;
    assert(!adreno8_hwsched_alloc(&request, &result));
    assert(adreno8_memory_count == 0 && !adreno8_memory_retained());
    alloc_fail_after = -1;
    sync_ok = false;
    assert(!adreno8_hwsched_alloc(&request, &result));
    assert(adreno8_memory_retained() && adreno8_gmu_space.is_on && adreno8_memory_failed);
    assert(adreno8_memory_entries[0].buffer.memory && !adreno8_memory_entries[0].complete);
    allocations_before = allocation_count;
    sync_ok = true;
    assert(!adreno8_hwsched_alloc(&request, &result));
    assert(allocation_count == allocations_before && adreno8_gpu_faulted());

    reset_memory();
    request = (struct adreno8_hfi_mem_request){.flags = ADRENO8_HFI_MEM_GMU, .size = 4096};
    next_pa = 1ULL << 36;
    assert(!adreno8_hwsched_alloc(&request, &result));
    assert(adreno8_memory_count == 1 && adreno8_memory_retained());
    assert(adreno8_memory_entries[0].buffer.memory != 0);
    release_all();
    puts("Shared GPU/GMU permissions, replay, limits and failure retention: PASS");
}
'''
with tempfile.TemporaryDirectory(prefix="adreno8-memory-") as directory:
    target = Path(directory)
    (target / "adreno8_private.h").write_text(stub)
    for name in ("adreno8_memory_private.h", "adreno8_hwsched_private.h"):
        (target / name).write_text((ROOT / "xnu/files/bsd/dev" / name).read_text())
    (target / "adreno8_smmu.c").write_text(fixture["driver"])
    (target / "adreno8_memory.c").write_text(driver)
    (target / "check.c").write_text('static unsigned invalidations;\n#include "adreno8_smmu.c"\n'
        '#include "adreno8_memory.c"\n' + setup + tests)
    for page_size in (4096, 16384):
        binary = target / f"check-{page_size}"
        subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        str(target / "check.c"), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
        print(f"XNU PAGE_SIZE={page_size}: PASS")
