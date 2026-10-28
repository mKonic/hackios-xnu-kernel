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
stub = stub.replace("static int adreno8_say", r'''
uint32_t adreno8_gpu8_secure_mode;
static unsigned scm_calls, scm_sleeps, scm_fail_at = 999999u;
static unsigned scm_clean_floor = ~0u; /* clean event count when the first MP_ASSIGN ran */
int32_t adreno8_scm_call(uint64_t function, uint64_t arginfo, uint64_t argument0,
    uint64_t argument1, uint64_t argument2, uint64_t argument3, uint64_t *answer) {
    assert(function == 0x42000c16ull && arginfo == 0x1117ull);
    if (scm_clean_floor == ~0u) scm_clean_floor = clean_count;
    scm_calls++;
    *answer = scm_calls == scm_fail_at ? 5 : 0;
    return scm_calls == scm_fail_at ? -5 : 0;
}
void adreno8_sleep_ms(uint32_t ms) { scm_sleeps++; (void)ms; }
static int adreno8_say''', 1)
# The two middle-break flags move only granule 1 of every page of the
# matching allocation: both ends of each page stay contiguous, only the
# middle of a 16 KiB page points to another physical page. The 0x138000 size
# selects the secure buffer, the PAGE_SIZE size selects table pages. At
# 4 KiB the condition never matches, so the same mode proves the success path.
stub = stub.replace("static vm_offset_t skew_va, shift_va;",
    "static vm_offset_t skew_va, shift_va;\nstatic bool mid_break_data, mid_break_tables;", 1)
stub = stub.replace(
    "            uint64_t page = delta / PAGE_SIZE, off = delta % PAGE_SIZE;\n"
    "            if (!pa_flat) {\n"
    "                return a.pa + page * 0x20000 + off;\n"
    "            }",
    "            uint64_t page = delta / PAGE_SIZE, off = delta % PAGE_SIZE;\n"
    "            bool hit = off >= SMMU_GRANULE && off < 2 * SMMU_GRANULE &&\n"
    "                (mid_break_data ? a.size == 0x138000 :\n"
    "                 mid_break_tables ? a.size == PAGE_SIZE : false);\n"
    "            if (!pa_flat) {\n"
    "                return a.pa + page * 0x20000 + off + (hit ? 0x100000 : 0);\n"
    "            }", 1)
assert "mid_break_data ? a.size == 0x138000" in stub
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
    memset(&adreno8_secure_space, 0, sizeof(adreno8_secure_space));
    adreno8_secure_space.name = "secure";
    adreno8_secure_space.context = SECURE_CONTEXT;
    memset(adreno8_memory_entries, 0, sizeof(adreno8_memory_entries));
    adreno8_memory_count = 0;
    adreno8_memory_bytes = 0;
    adreno8_memory_failed = false;
    invalidations = 0;
    map_one(&adreno8_gmu_space);
    assert(adreno8_space_on(&adreno8_gmu_space, 5, 1));
}
int main(int argc, char **argv) {
    struct adreno8_hfi_mem_result result, again;
    struct adreno8_hfi_mem_request request = {
        .flags = ADRENO8_HFI_MEM_GFX | ADRENO8_HFI_MEM_GFX_PRIV | ADRENO8_HFI_MEM_GFX_WRITE |
                 ADRENO8_HFI_MEM_GMU | ADRENO8_HFI_MEM_GMU_PRIV | ADRENO8_HFI_MEM_GMU_WRITE,
        .kind = 1, .handle = 42, .size = 8193, .align = (12 << 8) | 16,
    };
    reset_memory();
    assert(ADRENO8_SECURE_IOVA_START == 0xf0000000u && ADRENO8_SECURE_IOVA_END == 0xfffff000u);
    if (argc > 1 && strcmp(argv[1], "xfer-iova-align") == 0) {
        request = (struct adreno8_hfi_mem_request){.flags = 0x1007, .kind = 5, .handle = 21,
            .size = 4096, .align = (12 << 8) | 31};
        adreno8_gpu8_secure_mode = SECURE_MODE_HANDOVER;
        assert(!adreno8_hwsched_alloc(&request, &result));
        assert(scm_calls == 0 && adreno8_memory_count == 0 && adreno8_memory_retained());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(result.gpu_address == 0 && result.size == 0);
        puts("secure IOVA: 2 GiB alignment cannot wrap into the 32-bit trusted subset: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "gated") == 0) {
        /* The production kind-5 request stops after the read-only preflight:
         * no MP_ASSIGN, no retained range, no table work; the non-secure
         * allocation path keeps working right afterwards. */
        request = (struct adreno8_hfi_mem_request){.flags = 0x1007, .kind = 5, .handle = 11,
            .size = 0x138000, .align = (12 << 8) | 16};
        adreno8_gpu8_secure_mode = SECURE_MODE_PREFLIGHT;
        assert(!adreno8_hwsched_alloc(&request, &result));
        assert(scm_calls == 0 && !adreno8_memory_failed && !adreno8_memory_retained());
        assert(adreno8_memory_count == 0 && adreno8_secure_space.table_count == 0);
        request.kind = 4;
        request.flags = 0x007;
        assert(adreno8_hwsched_alloc(&request, &again) && !again.secure);
        puts("secure handover gated: no MP_ASSIGN, non-secure path intact: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "translation") == 0) {
        request = (struct adreno8_hfi_mem_request){.flags = 0x1007, .kind = 5, .handle = 21,
            .size = 0x138000, .align = (12 << 8) | 16};
        adreno8_gpu8_secure_mode = SECURE_MODE_TRANSLATION_A;
        assert(!adreno8_hwsched_alloc(&request, &result));
        assert(result.gpu_address == 0 && result.size == 0);
        assert(adreno8_secure_space.is_on && !adreno8_memory_failed);
        assert(regs[SMMU_S2CR(SECURE_MATCH) / 4] == 0x20300);
        assert(!adreno8_gpu_faulted());
        regs[(0x23000 + SMMU_CB_FSR) / 4] = 2;
        assert(adreno8_gpu_faulted());
        assert(adreno8_memory_entries[0].complete);
        assert(adreno8_memory_entries[0].buffer.gpu_address == 0xf0000000u);
        assert(((uint64_t *)adreno8_secure_space.tables[0].table)[0] == 0);
        assert(adreno8_space_table(&adreno8_secure_space, 1, 3) != NULL);
        puts("secure translation 3a: enabled context, blocked route, no reply, secure fault included: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "xfer") == 0) {
        /* Experiment mode through the production call chain: the kind-5 request
         * hands the kernel's own buffer over once and ends there. The result
         * stays zeroed so no MEM_ALLOC reply can be built, CB0 gains no table,
         * CB3 gains no write, and the secure space ends fully assigned. Every
         * later request is refused without a new allocation or SCM call. */
        request = (struct adreno8_hfi_mem_request){.flags = 0x1007, .kind = 5, .handle = 21,
            .size = 0x138000, .align = (12 << 8) | 16};
        adreno8_gpu8_secure_mode = SECURE_MODE_HANDOVER;
        unsigned writes_before = writes;
        assert(!adreno8_hwsched_alloc(&request, &result));
        assert(result.gpu_address == 0 && result.gmu_address == 0 && result.size == 0 && !result.secure);
        /* The fixture never hands out neighbor physical pages, so the pages
         * stay single-page regions: ceil(pages / 32) data calls plus one table
         * call over the root, level-1 and level-2 pages of the mapping. */
        unsigned pages = (request.size + PAGE_SIZE - 1) / PAGE_SIZE;
        assert(scm_calls == (pages + ASSIGN_SECTIONS - 1) / ASSIGN_SECTIONS + 1);
        assert(!adreno8_secure_lost() && !adreno8_memory_failed && adreno8_memory_retained());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(writes == writes_before);
        assert(adreno8_gpu_space.table_count == 0);
        assert(adreno8_secure_space.table_count == 3 && !adreno8_secure_space.is_on);
        assert(adreno8_secure_space.secure_tables == adreno8_secure_space.table_count);
        struct adreno8_memory *secure = &adreno8_memory_entries[0].buffer;
        assert(adreno8_memory_entries[0].secure && adreno8_memory_entries[0].complete && secure->memory != 0);
        assert(secure->gpu_address == 0xf0000000u &&
            (uint64_t)secure->gpu_address + secure->size <= 0xfffff000u);
        struct adreno8_space_table *parent = adreno8_space_table(&adreno8_secure_space, 1, 3);
        assert(parent && ((uint64_t *)adreno8_secure_space.tables[0].table)[3] == (parent->phys | PTE_TABLE));
        assert(((uint64_t *)adreno8_secure_space.tables[0].table)[0] == 0);
        /* TrustZone reads the buffer from DRAM: a clean over the whole request
         * range must land before the first MP_ASSIGN of this handover. */
        int data_clean = 0;
        for (unsigned i = 0; i < scm_clean_floor; i++) {
            if (cleaned[i].start == secure->memory && cleaned[i].size >= request.size) {
                data_clean = 1;
            }
        }
        assert(data_clean);
        for (unsigned offset = 0; offset < request.size; offset += 4096) {
            assert(walk(adreno8_secure_space.tables[0].phys, secure->gpu_address + offset) ==
                   kvtophys(secure->memory + offset));
        }
        assert(!adreno8_memory_publish(secure, 0, 4096) && !adreno8_memory_acquire(secure, 0, 4096));
        unsigned calls_before = scm_calls, allocations_after = allocation_count;
        assert(!adreno8_hwsched_alloc(&request, &again));
        request.handle = 22;
        assert(!adreno8_hwsched_alloc(&request, &again));
        request.kind = 4;
        request.flags = 0x007;
        assert(!adreno8_hwsched_alloc(&request, &again));
        assert(adreno8_gpu_alloc("after", 4096, 4096, ADRENO8_MEM_READ) == NULL);
        assert(scm_calls == calls_before && allocation_count == allocations_after);
        puts("secure handover experiment: one terminal handover, no reply, no CB3 write, path frozen: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "xfer-fail") == 0) {
        /* The second data batch fails after the table call and the first data
         * batch landed: ownership is unclear, every page stays retained, the
         * entry never completes, and later requests add no SCM call. */
        request = (struct adreno8_hfi_mem_request){.flags = 0x1007, .kind = 5, .handle = 23,
            .size = 0x138000, .align = (12 << 8) | 16};
        adreno8_gpu8_secure_mode = SECURE_MODE_HANDOVER;
        scm_fail_at = 3;
        assert(!adreno8_hwsched_alloc(&request, &result));
        assert(scm_calls == 3 && adreno8_secure_lost() && adreno8_memory_failed);
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(adreno8_memory_retained() && !adreno8_memory_entries[0].complete);
        unsigned calls_before = scm_calls;
        assert(!adreno8_hwsched_alloc(&request, &again));
        request.kind = 4;
        request.flags = 0x007;
        assert(!adreno8_hwsched_alloc(&request, &again));
        assert(scm_calls == calls_before);
        puts("secure handover failure retained and frozen: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "xfer-open-fail") == 0) {
        /* The MP_ASSIGN argument page cannot be allocated: the experiment ends
         * before any MP_ASSIGN, no page is transferred, nothing is marked
         * lost, and the boot keeps the terminal refusal. */
        request = (struct adreno8_hfi_mem_request){.flags = 0x1007, .kind = 5, .handle = 25,
            .size = 0x138000, .align = (12 << 8) | 16};
        adreno8_gpu8_secure_mode = SECURE_MODE_HANDOVER;
        /* kmem calls: secure root, buffer, level-1, level-2, then the argument
         * page is the fifth and fails. */
        alloc_fail_after = 4;
        assert(!adreno8_hwsched_alloc(&request, &result));
        assert(scm_calls == 0 && !adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(adreno8_memory_failed && adreno8_memory_retained());
        assert(!adreno8_memory_entries[0].complete);
        alloc_fail_after = -1;
        unsigned allocations_after = allocation_count;
        assert(!adreno8_hwsched_alloc(&request, &again));
        request.kind = 4;
        request.flags = 0x007;
        assert(!adreno8_hwsched_alloc(&request, &again));
        assert(allocation_count == allocations_after && scm_calls == 0);
        puts("secure handover ends before any MP_ASSIGN when the argument page is missing: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "xfer-gmu-changed") == 0) {
        /* The GMU route drifts after stage 9 wrote it: the strict preflight
         * rejects the kind-5 request before any table page, before any SCM
         * call, and before any retention, and the one-shot state stays
         * unconsumed; the non-secure path keeps working. */
        request = (struct adreno8_hfi_mem_request){.flags = 0x1007, .kind = 5, .handle = 31,
            .size = 0x138000, .align = (12 << 8) | 16};
        adreno8_gpu8_secure_mode = SECURE_MODE_HANDOVER;
        regs[SMMU_S2CR(1) / 4] = 0x304u;   /* translate into context 4 */
        assert(!adreno8_hwsched_alloc(&request, &result));
        assert(scm_calls == 0 && !adreno8_memory_failed && !adreno8_memory_retained());
        assert(adreno8_memory_count == 0 && adreno8_secure_space.table_count == 0);
        assert(adreno8_secure_xfer_state() == SECURE_XFER_NOT_STARTED);
        request.kind = 4;
        request.flags = 0x007;
        assert(adreno8_hwsched_alloc(&request, &again) && !again.secure);
        puts("secure handover rejects a changed GMU route before any table work: PASS");
        return 0;
    }
    if (argc > 1 && (strcmp(argv[1], "xfer-middle-data") == 0 ||
                     strcmp(argv[1], "xfer-middle-table") == 0)) {
        /* Only granule 1 of every 16 KiB page moves to another physical page
         * while both ends stay contiguous: a check of first and last word
         * alone would pass, the granule walk rejects. The data variant
         * breaks the buffer (allocation 1), the table variant the root table
         * page (allocation 0). With 4 KiB pages no middle granule exists and
         * the same mode completes the handover. */
        bool table_case = argv[1][12] == 't';
        request = (struct adreno8_hfi_mem_request){.flags = 0x1007, .kind = 5, .handle = 33,
            .size = 0x138000, .align = (12 << 8) | 16};
        adreno8_gpu8_secure_mode = SECURE_MODE_HANDOVER;
        if (table_case) {
            mid_break_tables = true;
        } else {
            mid_break_data = true;
        }
        assert(!adreno8_hwsched_alloc(&request, &result));
        if (PAGE_SIZE == 4096) {
            unsigned pages = 0x138000 / 4096;
            assert(scm_calls == (pages + ASSIGN_SECTIONS - 1) / ASSIGN_SECTIONS + 1);
            assert(!adreno8_secure_lost() && !adreno8_memory_failed && adreno8_memory_retained());
            assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
            assert(adreno8_memory_entries[0].complete);
            puts(table_case ? "middle-break table page cannot exist at 4 KiB: handover completes: PASS"
                 : "middle-break data page cannot exist at 4 KiB: handover completes: PASS");
            return 0;
        }
        assert(scm_calls == 0 && !adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(adreno8_memory_failed && adreno8_memory_retained());
        assert(!adreno8_memory_entries[0].complete);
        unsigned calls_before = scm_calls;
        assert(!adreno8_hwsched_alloc(&request, &again));
        request.kind = 4;
        request.flags = 0x007;
        assert(!adreno8_hwsched_alloc(&request, &again));
        assert(scm_calls == calls_before);
        puts(table_case ? "middle granule of a table page rejected before any MP_ASSIGN: PASS"
             : "middle granule of a data page rejected before any MP_ASSIGN: PASS");
        return 0;
    }
    if (argc > 1 && (strcmp(argv[1], "xfer-subpage") == 0 || strcmp(argv[1], "xfer-skew") == 0)) {
        /* A 16 KiB kernel page can sit on four separate 4 KiB SMMU leaves: the
         * handover refuses pages whose whole native span is not proven. The
         * subpage variant misaligns every physical start by 4 KiB, the skew
         * variant splits every page at its 4 KiB subboundary. With 4 KiB
         * pages the defect cannot exist and the flat layout merges the buffer
         * into one data region, so the whole chain completes in two calls. */
        bool skew = argv[1][8] == 's';
        request = (struct adreno8_hfi_mem_request){.flags = 0x1007, .kind = 5, .handle = 27,
            .size = 0x138000, .align = (12 << 8) | 16};
        adreno8_gpu8_secure_mode = SECURE_MODE_HANDOVER;
        pa_flat = true;
        if (skew) {
            skew_all = true;
            skew_delta = 4096;
        } else {
            pa_offset = 4096;
        }
        assert(!adreno8_hwsched_alloc(&request, &result));
        assert(!adreno8_secure_lost() && adreno8_memory_retained());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        if (PAGE_SIZE == 4096) {
            assert(scm_calls == 2 && !adreno8_memory_failed);
            assert(adreno8_memory_entries[0].secure && adreno8_memory_entries[0].complete);
            assert(adreno8_secure_space.secure_tables == adreno8_secure_space.table_count);
        } else {
            assert(scm_calls == 0 && adreno8_memory_failed && adreno8_memory_count == 1);
            assert(!adreno8_memory_entries[0].complete);
            assert(adreno8_secure_space.secure_tables == 0);
        }
        printf("native page span %s the handover through the hwsched chain: PASS\n",
            PAGE_SIZE == 4096 ? "completes" : "blocks");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "xfer-parange") == 0) {
        /* Allocations start one granule below the 36-bit translation limit:
         * the buffer page lands past it and the mapping refuses the region
         * before any MP_ASSIGN goes out. */
        request = (struct adreno8_hfi_mem_request){.flags = 0x1007, .kind = 5, .handle = 29,
            .size = 4096, .align = (12 << 8) | 16};
        adreno8_gpu8_secure_mode = SECURE_MODE_HANDOVER;
        next_pa = SMMU_PHYS_LIMIT - SMMU_GRANULE;
        assert(!adreno8_hwsched_alloc(&request, &result));
        assert(scm_calls == 0 && !adreno8_secure_lost());
        assert(adreno8_memory_failed && adreno8_memory_retained());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(!adreno8_memory_entries[0].complete);
        puts("pages past the translation limit end before any MP_ASSIGN: PASS");
        return 0;
    }
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
    request = (struct adreno8_hfi_mem_request){.flags = 0x1007, .kind = 5, .handle = 9,
        .size = 0x138000, .align = (12 << 8) | 16};
    /* Mode 0 keeps the secure path closed: the preflight reads, then nothing
     * stages, answers or allocates; MP_ASSIGN count stays zero. */
    assert(!adreno8_hwsched_alloc(&request, &result));
    assert(scm_calls == 0 && adreno8_memory_count == 0 && !adreno8_memory_failed);
    assert(adreno8_gpu_space.table_count == 0 && adreno8_secure_space.table_count == 0);
    request.handle = 10;
    request.flags = 0x007;
    assert(!adreno8_hwsched_alloc(&request, &again));
    request.flags = 0x100f;
    assert(!adreno8_hwsched_alloc(&request, &again));
    request.kind = 4;
    request.flags = 0x1007;
    assert(!adreno8_hwsched_alloc(&request, &again));
    request.flags = 0x007;
    assert(adreno8_hwsched_alloc(&request, &again));
    assert(!again.secure);

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
    for name in ("adreno8_smmu_private.h", "adreno8_memory_private.h", "adreno8_hwsched_private.h", "adreno8_secure_private.h"):
        (target / name).write_text((ROOT / "xnu/files/bsd/dev" / name).read_text())
    (target / "adreno8_smmu.c").write_text(fixture["driver"])
    (target / "adreno8_memory.c").write_text(driver)
    (target / "adreno8_secure.c").write_text((ROOT / "xnu/files/bsd/dev/adreno8_secure.c").read_text())
    (target / "check.c").write_text('static unsigned invalidations;\n#include "adreno8_smmu.c"\n'
        '#include "adreno8_secure.c"\n#undef printf\n#include "adreno8_memory.c"\n' + setup + tests)
    for page_size in (4096, 16384):
        binary = target / f"check-{page_size}"
        subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        str(target / "check.c"), "-o", str(binary)], check=True)
        for mode in ((), ("gated",), ("translation",), ("xfer",), ("xfer-fail",), ("xfer-open-fail",),
                     ("xfer-gmu-changed",), ("xfer-middle-data",), ("xfer-middle-table",),
                     ("xfer-subpage",), ("xfer-skew",), ("xfer-parange",), ("xfer-iova-align",)):
            result = subprocess.run([str(binary), *mode], check=True, capture_output=True, text=True)
            if mode == ("xfer",):
                # The plan line prints from the real PAGE_SIZE and merge rule:
                # single-page regions, one table call, ceil(pages / 32) data calls.
                pages = 0x138000 // page_size
                want = (f"{pages} pages merge into {pages} data regions plus 3 table pages, "
                        f"{(pages + 31) // 32 + 1} logical MP_ASSIGN batches")
                assert want in result.stdout, (want, result.stdout)
                assert "secure handover complete" in result.stdout
                assert "no MEM_ALLOC reply is built" in result.stdout
            if mode == ("xfer-open-fail",) or mode == ("xfer-parange",):
                assert "ends before any MP_ASSIGN; no page transferred" in result.stdout
                assert "MP_ASSIGN batch" not in result.stdout
            if mode == ("xfer-gmu-changed",):
                assert "GMU route context changed" in result.stdout
                assert "secure handover attempted" not in result.stdout
            if mode in (("xfer-middle-data",), ("xfer-middle-table",)):
                key = "secure table page" if mode == ("xfer-middle-table",) else "secure page"
                if page_size == 4096:
                    assert "secure handover complete" in result.stdout
                else:
                    assert key in result.stdout and "unusable for MP_ASSIGN" in result.stdout
                    assert "ends before any MP_ASSIGN; no page transferred" in result.stdout
                    assert "secure handover attempted" not in result.stdout
            if mode in (("xfer-subpage",), ("xfer-skew",)):
                if page_size == 4096:
                    want = ("312 pages merge into 1 data regions plus 3 table pages, "
                            "2 logical MP_ASSIGN batches")
                    assert want in result.stdout, (want, result.stdout)
                    assert "secure handover complete" in result.stdout
                else:
                    assert "unusable for MP_ASSIGN" in result.stdout
                    assert "ends before any MP_ASSIGN; no page transferred" in result.stdout
                    assert "secure handover attempted" not in result.stdout
        print(f"XNU PAGE_SIZE={page_size}: PASS")
