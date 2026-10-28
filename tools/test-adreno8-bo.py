#!/usr/bin/env python3
"""Exercise external BO ownership with the production CB0 mapper under ASan/UBSan."""
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"

FENCES = r'''
/* adreno8_bo.c's clock: each reading BO_CHECK_NS or more after the last unless a case stops it */
static uint64_t bo_clock, bo_clock_step = 1ull << 40;
static uint64_t bo_test_clock(void) { return bo_clock += bo_clock_step; }
static bool commands_verified, context_verified, memory_healthy, hfi_healthy, idle_ok;
static bool adreno8_reset_required;
static unsigned closed, idle_calls, eop_waits, eop_delay;
static uint64_t *pending_entry, pending_value;
bool adreno8_gpu_commands_ready(void) { return commands_verified && memory_healthy; }
bool adreno8_gpu_memory_ready(void) { return adreno8_gpu_space.is_on && memory_healthy; }
bool adreno8_gpu_memory_verify(void) {
    return memory_healthy && adreno8_space_check(&adreno8_gpu_space, 0, 0);
}
bool adreno8_gpu_faulted(void) { return !memory_healthy || adreno8_space_faulted(&adreno8_gpu_space); }
bool adreno8_hwsched_ready(void) { return memory_healthy && hfi_healthy; }
bool adreno8_hfi_poll(void) { return hfi_healthy; }
void adreno8_hwsched_memory_failed(void) { closed++; memory_healthy = false; }
bool adreno8_context_wait_idle(const char *why) {
    assert(why != NULL);
    if (!context_verified || !memory_healthy) return false;
    while (eop_delay) {
        if (pending_entry) assert(*pending_entry == pending_value);
        eop_delay--; eop_waits++;
    }
    idle_calls++;
    if (pending_entry) assert(*pending_entry == pending_value);
    return idle_ok;
}
'''

CASES = r'''
static vm_offset_t startup_memory;
static uint64_t startup_pte;
static void fresh(void) {
    reset(&adreno8_gpu_space, false);
    adreno8_gpu_space.context = 0; adreno8_gpu_space.name = "gpu";
    memset(&adreno8_gmu_space, 0, sizeof(adreno8_gmu_space));
    adreno8_gmu_space.context = GMU_CONTEXT;
    memset(&adreno8_secure_space, 0, sizeof(adreno8_secure_space));
    adreno8_secure_space.context = SECURE_CONTEXT;
    bo_state = BO_COLD; bo_checked = 0; bo_clock_step = 1ull << 40;
    commands_verified = context_verified = memory_healthy = hfi_healthy = idle_ok = true;
    adreno8_reset_required = true; /* Secure transfer retains pages through the normal boot. */
    closed = idle_calls = eop_waits = eop_delay = 0; pending_entry = NULL;
    assert(kmem_alloc(kernel_map, &startup_memory, PAGE_SIZE, 0, 0) == KERN_SUCCESS);
    assert(adreno8_space_new_table(&adreno8_gpu_space, 0, 0));
    assert(adreno8_space_map_region(&adreno8_gpu_space, ADRENO8_GPU_IOVA_START,
        startup_memory, PAGE_SIZE, ADRENO8_MEM_READ | ADRENO8_MEM_EXEC));
    startup_pte = *adreno8_space_entry(&adreno8_gpu_space, ADRENO8_GPU_IOVA_START);
    assert(adreno8_space_on(&adreno8_gpu_space, 0, 0));
}
/* Bytes cleaned since event `from`, each event less than a table; whether one held `value` at `entry`. */
static uint64_t cleaned_since(unsigned from, const uint64_t *entry, uint64_t value, bool *seen) {
    uint64_t bytes = 0;
    vm_offset_t at = (vm_offset_t)entry;
    *seen = false;
    for (unsigned i = from; i < clean_count; i++) {
        assert(cleaned[i].size < 4096);
        bytes += cleaned[i].size;
        if (cleaned[i].start <= at && at + 8 <= cleaned[i].start + cleaned[i].snap_len &&
            !memcmp(cleaned[i].snap + (at - cleaned[i].start), &value, 8)) *seen = true;
    }
    return bytes;
}
static void stays_closed(uint64_t address, uint64_t size) {
    unsigned old_writes = writes, old_allocations = allocation_count;
    assert(!adreno8_bo_ready() && closed && adreno8_reset_required);
    assert(!adreno8_bo_map(address, 0x50000000, false));
    assert(!adreno8_bo_commit() && !adreno8_bo_unmap(address, size));
    assert(!adreno8_bo_release(address, size) && !adreno8_bo_reserve(4096));
    sync_ok = idle_ok = hfi_healthy = memory_healthy = true;
    assert(!adreno8_bo_enable());
    assert(writes == old_writes && allocation_count == old_allocations);
}
int main(int argc, char **argv) {
    unsigned mode = argc > 1 ? (unsigned)atoi(argv[1]) : 0;
    fresh();
    unsigned old_writes = writes, old_allocations = allocation_count;
    assert(!adreno8_bo_ready() && !adreno8_bo_map(ADRENO8_GPU_IOVA_START, 0x50000000, false));
    context_verified = false;
    assert(!adreno8_bo_enable() && !closed && !idle_calls);
    assert(writes == old_writes && allocation_count == old_allocations);
    context_verified = true;
    assert(adreno8_bo_enable() && adreno8_bo_ready() && idle_calls == 1);
    if (mode == 24) {
        /* within BO_CHECK_NS of a check that passed the state answers; after it the check runs again */
        bo_clock_step = 0;
        assert(adreno8_bo_ready());
        hfi_healthy = false;
        assert(adreno8_bo_ready() && !closed);
        bo_clock_step = 1ull << 40;
        assert(!adreno8_bo_ready() && closed);
        stays_closed(ADRENO8_GPU_IOVA_START, 4096); release_all(); return 0;
    }
    if (mode >= 1 && mode <= 5) {
        const uint64_t bad_sizes[] = {0, 1, 4095, UINT64_MAX, (1ULL << 32)};
        assert(!adreno8_bo_reserve(bad_sizes[mode - 1]));
        assert(*adreno8_space_entry(&adreno8_gpu_space, ADRENO8_GPU_IOVA_START) == startup_pte);
        stays_closed(ADRENO8_GPU_IOVA_START, 4096); release_all(); return 0;
    }
    if (mode == 6) {
        alloc_fail_after = 0;
        assert(!adreno8_bo_reserve(3u << 20));
        assert(adreno8_gpu_space.is_on && closed);
        stays_closed(ADRENO8_GPU_IOVA_START, 4096); release_all(); return 0;
    }
    uint64_t size = mode == 7 ? (2u << 20) + 4096 : 3 * 4096;
    uint64_t address = adreno8_bo_reserve(size);
    assert(address == ADRENO8_GPU_IOVA_START + PAGE_SIZE);
    uint64_t next = adreno8_bo_reserve(4096);
    assert(next == address + size);
    uint32_t unused;
    assert(adreno8_space_find_range(&adreno8_gpu_space, ADRENO8_GPU_IOVA_START,
        ADRENO8_GPU_IOVA_END, 4096, 4096, &unused) && unused == next + 4096);
    for (uint64_t at = 0; at < size; at += 4096)
        assert(*adreno8_space_entry(&adreno8_gpu_space, address + at) == ADRENO8_PTE_EXTERNAL);
    assert(adreno8_space_check(&adreno8_gpu_space, 0, 0));
    uint64_t *first = adreno8_space_entry(&adreno8_gpu_space, address);
    if (mode >= 8 && mode <= 11) {
        const uint64_t bad_pa[] = {0, 0x50000001, 1ULL << 36, UINT64_MAX};
        assert(!adreno8_bo_map(address, bad_pa[mode - 8], false));
        assert(*first == ADRENO8_PTE_EXTERNAL);
        stays_closed(address, size); release_all(); return 0;
    }
    if (mode == 12 || mode == 13) {
        bool result = mode == 12 ? adreno8_bo_unmap(ADRENO8_GPU_IOVA_START, PAGE_SIZE) :
            adreno8_bo_release(ADRENO8_GPU_IOVA_START, PAGE_SIZE);
        assert(!result && *adreno8_space_entry(&adreno8_gpu_space, ADRENO8_GPU_IOVA_START) == startup_pte);
        stays_closed(address, size); release_all(); return 0;
    }
    /* (mode 23 enters the pages in the other order: the commit's range takes both either way) */
    if (mode == 23) assert(adreno8_bo_map(address + 4096, 0x60000000, true));
    assert(adreno8_bo_map(address, 0x50000000, false));
    if (mode != 23) assert(adreno8_bo_map(address + 4096, 0x60000000, true));
    assert((*first & ~PTE_ADDRESS) == (ADRENO8_PTE_EXTERNAL | adreno8_space_attributes(7)));
    assert((*adreno8_space_entry(&adreno8_gpu_space, address + 4096) & ~PTE_ADDRESS) ==
        (ADRENO8_PTE_EXTERNAL | adreno8_space_attributes(5)));
    uint64_t mapped = *first;
    if (mode == 14) {
        assert(!adreno8_bo_map(address, 0x70000000, false) && *first == mapped);
        stays_closed(address, size); release_all(); return 0;
    }
    if (mode == 15) {
        sync_ok = false;
        assert(!adreno8_bo_commit() && *first == mapped && adreno8_gpu_space.is_on);
        stays_closed(address, size); release_all(); return 0;
    }
    unsigned before_commit = clean_count;
    bool seen;
    assert(adreno8_bo_commit());
    /* the commit cleans the two entries entered, not the tables */
    assert(cleaned_since(before_commit, first, mapped, &seen) == 2 * sizeof(uint64_t) && seen);
    assert(adreno8_space_check(&adreno8_gpu_space, 0, 0));
    if (mode == 16) {
        assert(!adreno8_bo_release(address, size) && *first == mapped);
        stays_closed(address, size); release_all(); return 0;
    }
    if (mode == 17) {
        hfi_healthy = false;
        assert(!adreno8_bo_ready() && *first == mapped);
        stays_closed(address, size); release_all(); return 0;
    }
    if (mode == 18) {
        idle_ok = false;
        assert(!adreno8_bo_unmap(address, size) && *first == mapped);
        stays_closed(address, size); release_all(); return 0;
    }
    if (mode == 19) {
        sync_ok = false;
        assert(!adreno8_bo_unmap(address, size) && *first == ADRENO8_PTE_EXTERNAL);
        assert(adreno8_gpu_space.is_on);
        stays_closed(address, size); release_all(); return 0;
    }
    if (mode == 20) {
        *adreno8_space_entry(&adreno8_gpu_space, address + 8192) |= 4;
        assert(!adreno8_space_check(&adreno8_gpu_space, 0, 0));
        assert(!adreno8_bo_unmap(address, size) && *first == mapped);
        stays_closed(address, size); release_all(); return 0;
    }
    if (mode == 21 || mode == 22) {
        struct adreno8_gpu_space *other = mode == 21 ? &adreno8_gmu_space : &adreno8_secure_space;
        map_one(other);
        uint64_t *entry = adreno8_space_entry(other, 0x60001000);
        uint64_t tagged = *entry | ADRENO8_PTE_EXTERNAL;
        *entry = tagged;
        assert(!adreno8_space_verify(other, false));
        *entry = ADRENO8_PTE_EXTERNAL;
        assert(!adreno8_space_verify(other, false));
        other->context = 0;
        assert(!adreno8_space_verify(other, false));
        *entry = tagged;
        assert(!adreno8_space_verify(other, false));
    }
    pending_entry = first; pending_value = mapped; eop_delay = 7;
    old_writes = writes; unsigned old_clean = clean_count;
    assert(adreno8_bo_unmap(address, size) && eop_waits == 7 && idle_calls == 2);
    assert(*first == ADRENO8_PTE_EXTERNAL && writes > old_writes && clean_count > old_clean);
    /* (mode 7 crosses into a second leaf; its first event is too long for the fixture's snapshot) */
    assert(cleaned_since(old_clean, first, ADRENO8_PTE_EXTERNAL, &seen) == size / 4096 * sizeof(uint64_t) &&
        (seen || mode == 7));
    pending_entry = NULL;
    assert(adreno8_bo_release(address, size) && *first == 0);
    assert(adreno8_bo_reserve(4096) == address);
    if (size == 3 * 4096) {
        /* first fit from the lowest free page: the freed hole fills up, a larger request goes past it */
        assert(adreno8_bo_reserve(4096) == address + 4096);
        assert(adreno8_bo_reserve(8192) == next + 4096);
        assert(adreno8_bo_reserve(4096) == address + 8192);
    }
    assert(*adreno8_space_entry(&adreno8_gpu_space, ADRENO8_GPU_IOVA_START) == startup_pte);
    assert(adreno8_space_entry(&adreno8_gpu_space, next) &&
        *adreno8_space_entry(&adreno8_gpu_space, next) == ADRENO8_PTE_EXTERNAL);
    release_all();
    puts("external BO: shared occupancy, permissions, partial map, EOP, TLB and retention: PASS");
}
'''


def main():
    fixture = runpy.run_path(str(ROOT / "tools/test-adreno8-smmu.py"))
    stub = fixture["stub"].replace("assert(size == 4096);", "assert(size != 0);")
    setup = fixture["tests"].split("int main(void) {")[0]
    with tempfile.TemporaryDirectory(prefix="adreno8-bo-") as directory:
        target = Path(directory)
        (target / "adreno8_private.h").write_text(stub)
        for name in ("adreno8_smmu_private.h", "adreno8_memory_private.h", "adreno8_context_private.h",
                "adreno8_hwsched_private.h", "adreno8_bo_private.h"):
            (target / name).write_text((DEV / name).read_text())
        (target / "adreno8_smmu.c").write_text(fixture["driver"])
        (target / "adreno8_bo.c").write_text((DEV / "adreno8_bo.c").read_text())
        source = ('#include "adreno8_smmu.c"\n' + FENCES + '\n#define mach_absolute_time bo_test_clock\n'
            '#include "adreno8_bo.c"\n#undef mach_absolute_time\n#undef printf\n' + setup + CASES)
        path = target / "check.c"
        path.write_text(source)
        for page_size in (4096, 16384):
            binary = target / f"check-{page_size}"
            compiled = subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}",
                "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(path), "-o", str(binary)],
                capture_output=True, text=True)
            if compiled.returncode:
                raise RuntimeError(compiled.stderr)
            for mode in range(25):
                result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError(f"PAGE_SIZE={page_size}, mode={mode}:\n" + result.stdout + result.stderr)
            print(f"External BO: 25 ownership/lifecycle scenarios, PAGE_SIZE={page_size}: PASS")


if __name__ == "__main__":
    main()
