#!/usr/bin/env python3
"""Run the production SMMU mapper against allocated memory and simulated registers."""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
header = (ROOT / "xnu/files/bsd/dev/adreno8_private.h").read_text()
driver = (ROOT / "xnu/files/bsd/dev/adreno8_smmu.c").read_text()
structures = "\n".join(
    re.search(rf"struct {name} \{{.*?\n}};", header, re.S).group()
    for name in ("adreno8_block", "adreno8_gpu_block", "adreno8_space_table", "adreno8_gpu_space")
)
constants = "\n".join(
    line for line in header.splitlines()
    if line.startswith(("#define SMMU_", "#define PTE_", "#define SCTLR_", "#define TCR_",
                        "#define TCR2_", "#define MAIR0_", "#define FSR_", "#define SMR_", "#define STAGE_1",
                        "#define GMU_CONTEXT", "#define GMU_MATCH", "#define GMU_STREAM",
                        "#define GMU(n)", "#define GMU_CM3_SYSRESET", "#define GMU_PWR_CLK_STATUS",
                        "#define SECURE_CONTEXT", "#define SECURE_STREAM",
                        "#define SECURE_MATCH", "#define SECURE_MODE_"))
)
# Replace only the privileged-platform MMIO instruction; the mapper and lifecycle run unchanged.
driver, replaced = re.subn(
    r'static void __attribute__\(\(noinline\)\)\nadreno8_smmu_write64\(.*?\n}',
    'static void adreno8_smmu_write64(vm_offset_t address, uint64_t value) { mock_atos(address, value); }',
    driver, count=1, flags=re.S,
)
assert replaced == 1
# Compile the extracted secure walker with the same production SMMU source.
driver += "\n" + (ROOT / "xnu/files/bsd/dev/adreno8_translation.c").read_text()
stall_parser = re.search(r'^\tadreno8_gpu_stall = PE_parse_boot_argn\("adreno8_gpu_stall".*?;$',
    (ROOT / "xnu/files/bsd/dev/adreno8.c").read_text(), re.M).group()
stub = r'''
#include <assert.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uintptr_t vm_offset_t;
typedef size_t vm_size_t;
#define PAGE_MASK (PAGE_SIZE - 1)
#define KMA_KOBJECT 0
#define KMA_ZERO 0
#define KMA_DATA 0
#define VM_KERN_MEMORY_IOKIT 0
#define KERN_SUCCESS 0
static int kernel_map;
static unsigned adreno8_step;
static bool adreno8_gpu_stall;
static bool adreno8_direct_ring;
static bool bootarg_present;
static uint32_t bootarg_value;
static bool PE_parse_boot_argn(const char *name, void *out, int size) {
    assert(strcmp(name, "adreno8_gpu_stall") == 0 && size == sizeof(uint32_t));
    if (bootarg_present) memcpy(out, &bootarg_value, size);
    return bootarg_present;
}
static void parse_stall(void) {
    uint32_t one = 0;
''' + stall_parser + r'''
}
extern uint32_t adreno8_gpu8_secure_mode; /* defined by the memory harness; unused by the SMMU tests */
''' + structures + "\n" + constants + r'''
static struct adreno8_block adreno8_gmu;
static bool adreno8_reset_required;
extern struct adreno8_gpu_space adreno8_gmu_space, adreno8_secure_space;
/* One record per cache-clean call: address, length, and a snapshot of the
 * first bytes (up to the cap). The secure handover tests check the cleanup
 * order per SCM call against these events; cleans larger than the cap record
 * addr/len only, which is all their coverage checks need. Declared ahead of
 * the harness fakes, which reference the event log from inside adreno8_scm_call. */
#define CLEAN_SNAP_CAP 1024
struct clean_event { vm_offset_t start; vm_size_t size; unsigned snap_len; unsigned char snap[CLEAN_SNAP_CAP]; };
static struct clean_event cleaned[4096];
static unsigned clean_count;
/* The driver's printf is adreno8_say; the real one reaches the console, so
 * the stub forwards to stdout and the runner can check message order. */
static int adreno8_say(const char *s, ...) {
    va_list ap;
    int n;
    va_start(ap, s);
    n = vprintf(s, ap);
    va_end(ap);
    return n;
}
static void adreno8_announce(const char *s, ...) {}
static void adreno8_settle(void) {}
static bool adreno8_cx_ready(void) { return true; }
/* The aperture test executes the production helper and checks its SCM wire arguments. */
static bool adreno8_scm_gpu_aperture(void) { return true; }
static uint64_t mach_absolute_time(void) { return 0x1234abcdull; }
static void absolutetime_to_nanoseconds(uint64_t time, uint64_t *ns) { *ns = time; }
#define NSEC_PER_SEC 1000000000ull
#define NSEC_PER_MSEC 1000000ull
static bool adreno8_quiet;
static void adreno8_space_off(struct adreno8_gpu_space *, uint32_t);
static bool adreno8_space_faulted(struct adreno8_gpu_space *);
static uint32_t regs[0x40000 / 4];
static unsigned writes, translations, ats_requests;
struct write_record { uint32_t offset, value; };
static struct write_record write_log[512];
static unsigned write_log_count;
static unsigned reads[0x40000 / 4];
static bool sync_ok = true, atos_ok = true, atos_wait_ok = true;
static uint32_t reject_offset = ~0u, reject_value;
static uint32_t sctlr_raz_mask, sctlr_enable_bits, tcr2_read_xor, s2cr_trans_raz_mask;
static int alloc_fail_after = -1;
struct allocation { uintptr_t va; size_t size; uint64_t pa; };
static struct allocation allocations[2048];
static unsigned allocation_count;
static uint64_t next_pa = 0x80000000;
static int kmem_alloc(int map, vm_offset_t *out, size_t size, int flags, int tag) {
    if (alloc_fail_after == 0) return 1;
    if (alloc_fail_after > 0) alloc_fail_after--;
    void *p = aligned_alloc(PAGE_SIZE, size);
    assert(p);
    memset(p, 0, size);
    allocations[allocation_count++] = (struct allocation){(uintptr_t)p, size, next_pa};
    next_pa += (size / PAGE_SIZE) * 0x20000;
    *out = (uintptr_t)p;
    return KERN_SUCCESS;
}
static void kmem_free(int map, vm_offset_t va, size_t size) {
    for (unsigned i = 0; i < allocation_count; i++) {
        if (allocations[i].va == va) {
            assert(allocations[i].size == size);
            free((void *)va);
            allocations[i].va = 0;
            return;
        }
    }
    assert(false);
}
/* Flat physical layouts for the MP_ASSIGN batch tests: pa_flat makes pages of
 * one allocation physically adjacent, gap_every inserts a 1 MiB gap every
 * gap_every pages, pa_offset shifts the whole flat map, and skew_all splits
 * every page at its 4 KiB subboundary. The *_va knobs target one page: shift_va
 * moves that page's physical start, skew_va splits it at the 4 KiB subboundary.
 * Default (all off) keeps every page on a 128 KiB stride, never adjacent. */
static bool pa_flat, skew_all;
static unsigned gap_every;
static uint64_t pa_offset, skew_delta, shift_delta;
static vm_offset_t skew_va, shift_va;
static uint64_t kvtophys(vm_offset_t va) {
    for (unsigned i = 0; i < allocation_count; i++) {
        struct allocation a = allocations[i];
        if (a.va && va >= a.va && va - a.va < a.size) {
            uint64_t delta = va - a.va;
            uint64_t page = delta / PAGE_SIZE, off = delta % PAGE_SIZE;
            if (!pa_flat) {
                return a.pa + page * 0x20000 + off;
            }
            uint64_t pa = a.pa + pa_offset + page * PAGE_SIZE +
                (gap_every ? page / gap_every * 0x100000 : 0) + off;
            if (((skew_all || (skew_va && va >= skew_va && va < skew_va + PAGE_SIZE)) &&
                 off >= SMMU_GRANULE) ||
                (shift_va && va >= shift_va && va < shift_va + PAGE_SIZE)) {
                pa += skew_delta + shift_delta;
            }
            return pa;
        }
    }
    assert(false);
    return 0;
}
static uint64_t *physical_table(uint64_t pa) {
    for (unsigned i = 0; i < allocation_count; i++) {
        if (allocations[i].va && allocations[i].pa == pa)
            return (uint64_t *)allocations[i].va;
    }
    assert(false);
    return NULL;
}
static uint64_t walk(uint64_t root, uint32_t va) {
    uint64_t entry = physical_table(root)[va >> 30];
    if ((entry & 3) != 3) return 1;
    entry = physical_table(entry & PTE_ADDRESS)[va >> 21 & 511];
    if ((entry & 3) != 3) return 1;
    entry = physical_table(entry & PTE_ADDRESS)[va >> 12 & 511];
    if ((entry & 0xc03) != 0xc03) return 1; /* ATS1PR is a privileged read. */
    return entry & PTE_ADDRESS;
}
static uint32_t adreno8_rd(vm_offset_t address) {
    assert(address < sizeof(regs));
    return regs[address / 4];
}
static uint32_t adreno8_get(struct adreno8_block *b, const char *name, uint32_t offset) {
    reads[offset / 4]++;
    return adreno8_rd(offset);
}
static void adreno8_put(struct adreno8_block *b, const char *name, uint32_t offset, uint32_t value) {
    assert(offset < sizeof(regs));
    assert(write_log_count < sizeof(write_log) / sizeof(write_log[0]));
    write_log[write_log_count++] = (struct write_record){offset, value};
    writes++;
    if (offset == reject_offset && value == reject_value) return;
    if (offset >= 0x20000 && (offset & 0xfff) == SMMU_CB_SCTLR) {
        value &= ~sctlr_raz_mask;
        if (value & SCTLR_M) value |= sctlr_enable_bits;
    }
    if (offset == 0x22000 + SMMU_CB_TCR2) value = (value | 0x60) ^ tcr2_read_xor;
    if (offset >= 0x20000 && (offset & 0xfff) == SMMU_CB_FSR) {
        regs[offset / 4] &= ~value;
        return;
    }
    if (offset == 0x22000 + SMMU_CB_TLBASID) assert(value == GMU_CONTEXT);
    if (offset == SMMU_S2CR(1) && (value & 0x30000) == 0) value &= ~s2cr_trans_raz_mask;
    if (offset == SMMU_S2CR(1) && value == (GMU_CONTEXT | 0x300)) translations++;
    regs[offset / 4] = value;
}
static bool adreno8_wait_for(struct adreno8_block *b, uint32_t offset, uint32_t mask, uint32_t value, uint32_t ms) {
    if (offset == 0x22000 + 0x8f0) return atos_wait_ok;
    assert(offset == 0x22000 + SMMU_CB_TLBSTATUS || offset == 0x20000 + SMMU_CB_TLBSTATUS ||
           offset == 0x23000 + SMMU_CB_TLBSTATUS);
    return sync_ok;
}
static void adreno8_to_memory(vm_offset_t memory, vm_size_t size) {
    assert(size == 4096);
    assert(clean_count < 4096);
    cleaned[clean_count] = (struct clean_event){memory, size, 0, {0}};
    if (size <= CLEAN_SNAP_CAP) {
        memcpy(cleaned[clean_count].snap, (void *)memory, size);
        cleaned[clean_count].snap_len = (unsigned)size;
    }
    clean_count++;
}
static void mock_atos(vm_offset_t address, uint64_t value) {
    assert(address == 0x22800);
    assert((regs[SMMU_S2CR(1) / 4] & 0x30000) == 0x20000);
    assert(regs[0x22804 / 4] == 0);
    uint64_t root = regs[(0x22000 + SMMU_CB_TTBR0) / 4];
    root |= (uint64_t)regs[(0x22000 + SMMU_CB_TTBR0 + 4) / 4] << 32;
    uint64_t par = atos_ok ? walk(root & PTE_ADDRESS, (uint32_t)value) : 1;
    regs[(0x22000 + 0x50) / 4] = par;
    regs[(0x22000 + 0x54) / 4] = par >> 32;
    ats_requests++;
}
'''
tests = r'''
#undef printf
static void release_all(void) {
    for (unsigned i = 0; i < allocation_count; i++) {
        if (allocations[i].va) free((void *)allocations[i].va);
    }
    allocation_count = 0;
    next_pa = 0x80000000;
}
static void reset(struct adreno8_gpu_space *space, bool atos) {
    release_all();
    memset(space, 0, sizeof(*space));
    space->context = GMU_CONTEXT;
    memset(regs, 0, sizeof(regs));
    memset(reads, 0, sizeof(reads));
    regs[SMMU_IDR0 / 4] = 0x4c017e18 & ~(atos ? 1u << 26 : 0);
    regs[SMMU_IDR1 / 4] = 0x40000015;
    regs[SMMU_IDR2 / 4] = 0x5511;
    regs[SMMU_SCR0 / 4] = 0x2e0406;
    sync_ok = atos_ok = atos_wait_ok = true;
    sctlr_raz_mask = 1u << 12;
    sctlr_enable_bits = 0;
    adreno8_gpu_stall = false;
    tcr2_read_xor = s2cr_trans_raz_mask = 0;
    writes = translations = ats_requests = 0;
    write_log_count = 0;
    clean_count = 0;
    reject_offset = ~0u;
    alloc_fail_after = -1;
    pa_flat = skew_all = false;
    gap_every = 0;
    pa_offset = skew_delta = shift_delta = 0;
    skew_va = shift_va = 0;
}
static void map_one(struct adreno8_gpu_space *space) {
    assert(adreno8_space_new_table(space, 0, 0));
    assert(adreno8_space_map(space, 0x60001000, 0x90001000));
}
/* Seed the GMU route exactly as stage 9 writes it: valid slot 1 naming
 * stream 5 with mask 0 and a translate S2CR into context 2. The strict
 * preflight requires this route to still read back as it was configured. */
static void seed_gmu_route(void) {
    adreno8_gmu_space.is_on = true;
    regs[SMMU_SMR(1) / 4] = SMR_VALID | SMR_MASK << 16 | 5;
    regs[SMMU_S2CR(1) / 4] = SMMU_S2CR_SHCFG_NSH | GMU_CONTEXT;
}
int main(void) {
    struct adreno8_gpu_space space;
    const uint32_t args[] = {0, 1, 2, UINT32_MAX};
    for (unsigned present = 0; present <= 1; present++) {
        for (unsigned i = 0; i < sizeof(args) / sizeof(args[0]); i++) {
            bootarg_present = present; bootarg_value = args[i];
            parse_stall();
            assert(adreno8_gpu_stall == (present && args[i] == 1));
        }
    }
    assert(GMU_CONTEXT == 2 && adreno8_gmu_space.context == 2 && SMR_MASK == 0);
    assert(SMMU_CBA2R(1) == 0x804 && SMMU_CBA2R(2) == 0x808);
    reset(&space, false);
    assert(adreno8_step_smmu_read());
    assert(reads[(0x20000 + SMMU_CB_SCTLR) / 4] == 1);
    assert(reads[(0x21000 + SMMU_CB_SCTLR) / 4] == 1);
    assert(reads[(0x22000 + SMMU_CB_SCTLR) / 4] == 1);
    assert(reads[(0x23000 + SMMU_CB_SCTLR) / 4] == 1);
    assert(writes == 0);
    /* G3: the boot probe must carry the same mapped-range bound as the
     * secure preflight. With CB3 starting exactly at the end of the mapped
     * block, CB2 is still observed and every CB3 read is skipped. */
    reset(&space, false);
    adreno8_smmu.size = 0x23000;
    assert(adreno8_step_smmu_read());
    assert(reads[(0x22000 + SMMU_CB_SCTLR) / 4] == 1);
    assert(reads[0x23000 / 4] == 0);
    adreno8_smmu.size = 0x40000;
    /* AArch64 privileged instruction fetch requires AP_UNPRIV clear on writable pages. */
    assert((PTE_PAGE & (3u << 6)) == 0 && (PTE_PAGE & (3ULL << 53)) == 0);
    assert((PTE_PAGE & (3u << 10)) == (3u << 10) && (MAIR0_NORMAL & 255) == 0x44);
    assert(TCR_39_BITS == 0x803519 && TCR2_39_BITS == 0x38011);
    assert(STAGE_1 == 0x1f300);
    assert(SCTLR_ON == 0x27 && SCTLR_CFCFG == 0x80);
    for (unsigned stall = 0; stall <= 1; stall++) {
        const uint32_t contexts[] = {0, GMU_CONTEXT, SECURE_CONTEXT};
        for (unsigned i = 0; i < sizeof(contexts) / sizeof(contexts[0]); i++) {
            reset(&space, false);
            space.context = contexts[i];
            adreno8_gpu_stall = stall;
            map_one(&space);
            uint32_t stream = space.context == 0 ? 0 : space.context == GMU_CONTEXT ? 5 : 2;
            uint32_t match = space.context == 0 ? 0 : space.context == GMU_CONTEXT ? 1 : 2;
            assert(adreno8_space_on(&space, stream, match));
            uint32_t sctlr = regs[((0x20 + space.context) * 0x1000 + SMMU_CB_SCTLR) / 4];
            assert(sctlr == (stall && space.context != GMU_CONTEXT ? 0xa7 : 0x27));
            assert(!(sctlr & (1u << 6)));
            adreno8_space_off(&space, match);
            assert(!space.is_on);
        }
        /* Contexts outside the two GPU banks keep the baseline control value. */
        assert(adreno8_smmu_sctlr(1) == 0x27 && adreno8_smmu_sctlr(20) == 0x27);
    }
    const uint32_t gpu_contexts[] = {0, SECURE_CONTEXT};
    for (unsigned i = 0; i < sizeof(gpu_contexts) / sizeof(gpu_contexts[0]); i++) {
        for (unsigned unexpected = 0; unexpected <= 1; unexpected++) {
            reset(&space, false);
            space.context = gpu_contexts[i];
            adreno8_gpu_stall = true;
            map_one(&space);
            if (unexpected) sctlr_enable_bits = 1u << 6;
            else sctlr_raz_mask |= SCTLR_CFCFG;
            uint32_t match = space.context == 0 ? 0 : 2;
            assert(!adreno8_space_on(&space, space.context == 0 ? 0 : 2, match));
            assert(!space.is_on && space.table_count == 3 && allocation_count == 3);
            assert(regs[SMMU_S2CR(match) / 4] == 0x20300);
        }
    }
    puts("GPU stall: CB0/CB3 only, CFIE clear, exact readback and failed setup retained: PASS");
    for (unsigned bit = 8; bit <= 9; bit++) {
        reset(&space, false);
        map_one(&space);
        s2cr_trans_raz_mask = 1u << bit;
        assert(!adreno8_space_on(&space, 5, 1));
        assert(!space.is_on && translations == 0);
    }
    /* ASIDPNE is optional; every enabled control bit must still read back. */
    const uint32_t required_sctlr[] = {1u, 2u, 4u, 32u};
    for (unsigned i = 0; i < sizeof(required_sctlr) / sizeof(required_sctlr[0]); i++) {
        reset(&space, false);
        map_one(&space);
        sctlr_raz_mask |= required_sctlr[i];
        assert(!adreno8_space_on(&space, 5, 1));
        assert(!space.is_on && translations == 0);
        assert(reads[(0x22000 + SMMU_CB_TTBR0 + 4) / 4] == 1);
        assert(reads[(0x22000 + SMMU_CB_MAIR0) / 4] == 1);
        assert(reads[(0x22000 + SMMU_CB_MAIR1) / 4] == 1);
    }
    const uint32_t tcr2_mismatches[] = {1u, 2u, 4u, 1u << 4, 1u << 7, 1u << 15, 1u << 16, 1u << 17};
    for (unsigned i = 0; i < sizeof(tcr2_mismatches) / sizeof(tcr2_mismatches[0]); i++) {
        reset(&space, false);
        map_one(&space);
        tcr2_read_xor = tcr2_mismatches[i];
        assert(!adreno8_space_on(&space, 5, 1));
        assert(!space.is_on && translations == 0);
        assert(reads[(0x22000 + SMMU_CB_TTBR1) / 4] == 1);
        assert(reads[(0x22000 + SMMU_CB_MAIR1) / 4] == 1);
    }
    /* Ordinary takeover keeps the conditions the step14b runs verified: an
     * active route into the requested context blocks it (reads only); a
     * fault leftover that only carries the context index does not block,
     * and neither does a non-zero context SCTLR. */
    reset(&space, false);
    map_one(&space);
    regs[SMMU_SMR(5) / 4] = 0x80000003;
    regs[SMMU_S2CR(5) / 4] = 2u;
    assert(!adreno8_space_on(&space, 5, 1) && writes == 0);
    reset(&space, false);
    map_one(&space);
    regs[SMMU_SMR(5) / 4] = 0x80000003;
    regs[SMMU_S2CR(5) / 4] = 0x20002u;
    assert(adreno8_space_on(&space, 5, 1));
    assert(space.is_on && translations == 1);
    adreno8_space_off(&space, 1);
    /* The device-read CB2 SCTLR 0x107c430c (enable bit clear, leftover TCR
     * stage fields) is the run001 step 9 reading. Ordinary initialization
     * must succeed with it and write exactly what the SCTLR=0 baseline
     * writes. */
    static struct write_record baseline[512];
    reset(&space, false);
    map_one(&space);
    assert(adreno8_space_on(&space, 5, 1));
    assert(space.is_on && write_log_count > 0);
    unsigned baseline_writes = write_log_count;
    memcpy(baseline, write_log, baseline_writes * sizeof(baseline[0]));
    adreno8_space_off(&space, 1);
    reset(&space, false);
    regs[(0x22000 + SMMU_CB_SCTLR) / 4] = 0x107c430c;
    map_one(&space);
    assert(adreno8_space_on(&space, 5, 1));
    assert(space.is_on && translations == 1);
    assert(write_log_count == baseline_writes);
    assert(memcmp(baseline, write_log, baseline_writes * sizeof(baseline[0])) == 0);
    adreno8_space_off(&space, 1);
    /* The reuse exception still requires a disabled context: the candidate
     * match holding this exact stream with a fault route but SCTLR
     * 0x107c430c rejects, reads only. */
    reset(&space, false);
    map_one(&space);
    regs[SMMU_SMR(1) / 4] = 0x80000005;
    regs[SMMU_S2CR(1) / 4] = 0x20300u;
    regs[(0x22000 + SMMU_CB_SCTLR) / 4] = 0x107c430c;
    assert(!adreno8_space_on(&space, 5, 1) && writes == 0);
    /* Routing to another context does not conflict, even while active. */
    reset(&space, false);
    map_one(&space);
    regs[SMMU_SMR(5) / 4] = 0x80000003;
    regs[SMMU_S2CR(5) / 4] = 0x10004;
    assert(adreno8_space_on(&space, 5, 1));
    adreno8_space_off(&space, 1);
    reset(&space, false);
    map_one(&space);
    assert(adreno8_space_on(&space, 5, 1));
    assert(regs[(0x22000 + SMMU_CB_SCTLR) / 4] == 0x27);
    assert(regs[(0x22000 + SMMU_CB_TCR2) / 4] == 0x38071);
    assert(space.is_on && translations == 1);
    adreno8_space_off(&space, 1);
    assert(!space.is_on);
    reset(&space, false);
    alloc_fail_after = 0;
    assert(!adreno8_space_new_table(&space, 0, 0) && space.table_count == 0);
    alloc_fail_after = -1;
    assert(!adreno8_space_new_table(&space, 2, 0) && allocation_count == 0);
    assert(adreno8_space_new_table(&space, 0, 0));
    alloc_fail_after = 1;
    assert(!adreno8_space_map(&space, 0x3ffff000, 0x90000000) && space.table_count == 2);
    alloc_fail_after = -1;
    assert(adreno8_space_map(&space, 0x3ffff000, 0x90000000));
    assert(!adreno8_space_map(&space, 0x3ffff000, 0x90001000));
    assert(walk(space.tables[0].phys, 0x3ffff000) == 0x90000000);
    assert(!adreno8_space_map(&space, 0x40000001, 0x90000000));
    assert(!adreno8_space_map(&space, 0x40000000, 0x90000001));
    assert(!adreno8_space_map(&space, 0x40000000, 1ULL << 36));
    struct adreno8_gpu_block block = {"test", 0x3fffe000, 3 * PAGE_SIZE + 1, 0};
    assert(kmem_alloc(0, &block.memory, 4 * PAGE_SIZE, 0, 0) == 0);
    unsigned count = space.table_count;
    assert(!adreno8_space_map_block(&space, &block));
    assert(space.table_count == count && walk(space.tables[0].phys, 0x3fffe000) == 1);
    reset(&space, false);
    assert(adreno8_space_new_table(&space, 0, 0));
    assert(kmem_alloc(0, &block.memory, 4 * PAGE_SIZE, 0, 0) == 0);
    assert(adreno8_space_map_block(&space, &block));
    for (unsigned offset = 0; offset < block.size; offset += 4096) {
        assert(walk(space.tables[0].phys, block.address + offset) == kvtophys(block.memory + offset));
    }
    block.address = 0xfffff000;
    block.size = 4097;
    assert(!adreno8_space_map_block(&space, &block));
    assert(adreno8_space_on(&space, 5, 1));
    assert(space.is_on && ats_requests == 0 && translations == 1);
    assert(!adreno8_space_map(&space, 0x70000000, 0x90000000));
    adreno8_space_off(&space, 1);
    assert(!space.is_on && regs[SMMU_SMR(1) / 4] == 0x80000005);
    assert(regs[SMMU_S2CR(1) / 4] == 0x20300);
    assert(adreno8_space_on(&space, 5, 1));
    adreno8_space_off(&space, 1);
    assert(!space.is_on && translations == 2);
    reset(&space, true);
    map_one(&space);
    assert(adreno8_space_on(&space, 5, 1));
    assert(ats_requests == 1 && translations == 1);
    sync_ok = false;
    adreno8_space_off(&space, 1);
    assert(space.is_on);
    sync_ok = true;
    adreno8_space_off(&space, 1);
    assert(!space.is_on);
    reset(&space, true);
    map_one(&space);
    atos_ok = false;
    assert(!adreno8_space_on(&space, 5, 1));
    assert(!space.is_on && ats_requests == 1 && translations == 0);
    reset(&space, true);
    map_one(&space);
    atos_wait_ok = false;
    assert(!adreno8_space_on(&space, 5, 1));
    assert(space.is_on && translations == 0);
    atos_wait_ok = true;
    adreno8_space_off(&space, 1);
    assert(!space.is_on);
    reset(&space, false);
    map_one(&space);
    reject_offset = 0x1808;
    reject_value = 1;
    assert(!adreno8_space_on(&space, 5, 1));
    assert(!space.is_on && translations == 0);
    reset(&space, false);
    map_one(&space);
    sync_ok = false;
    assert(!adreno8_space_on(&space, 5, 1));
    assert(space.is_on && translations == 0);
    reset(&space, false);
    map_one(&space);
    reject_offset = SMMU_S2CR(1);
    reject_value = 0x20300;
    assert(!adreno8_space_on(&space, 5, 1));
    assert(space.is_on && translations == 0);
    reset(&space, false);
    map_one(&space);
    regs[SMMU_SMR(2) / 4] = 0x84000405;
    assert(!adreno8_space_on(&space, 5, 1) && writes == 0);
    reset(&space, false);
    map_one(&space);
    regs[SMMU_SMR(2) / 4] = 0x80000010;
    regs[SMMU_S2CR(2) / 4] = GMU_CONTEXT;
    assert(!adreno8_space_on(&space, 5, 1) && writes == 0);
    reset(&space, false);
    map_one(&space);
    ((uint64_t *)space.tables[0].table)[1] = 0;
    assert(!adreno8_space_on(&space, 5, 1) && writes == 0);
    reset(&space, false);
    map_one(&space);
    regs[SMMU_SCR0 / 4] |= 1;
    assert(!adreno8_space_on(&space, 5, 1) && writes == 0);

    reset(&space, false);
    assert(adreno8_space_new_table(&space, 0, 0));
    vm_offset_t payload;
    assert(kmem_alloc(0, &payload, 2 * PAGE_SIZE, 0, 0) == KERN_SUCCESS);
    assert(adreno8_space_map_region(&space, 0x1ff000, payload, 2 * PAGE_SIZE,
                                   ADRENO8_MEM_READ | ADRENO8_MEM_EXEC));
    assert((*adreno8_space_entry(&space, 0x1ff000) & ~PTE_ADDRESS) == 0xec3);
    assert(walk(space.tables[0].phys, 0x200000) == kvtophys(payload + 4096));
    assert(walk(space.tables[0].phys, 0x1ff000 + PAGE_SIZE) == kvtophys(payload + PAGE_SIZE));
    uint32_t found;
    assert(adreno8_space_find_range(&space, 0x1ff000, 0x400000, 4096, 0x10000, &found));
    assert(found >= 0x1ff000 + 2 * PAGE_SIZE && (found & 0xffff) == 0);
    unsigned tables_before = space.table_count;
    assert(!adreno8_space_map_region(&space, 0x1ff000, payload, 4096, ADRENO8_MEM_READ));
    assert(!adreno8_space_map_region(&space, 0xfffff000, payload, 8192, ADRENO8_MEM_READ));
    assert(!adreno8_space_map_region(&space, 0x800000, payload, 4096, ADRENO8_MEM_WRITE));
    assert(space.table_count == tables_before);
    assert(adreno8_space_on(&space, 5, 1));
    clean_count = 0;
    assert(adreno8_space_map_region(&space, 0x800000, payload, 4096,
                                   ADRENO8_MEM_READ | ADRENO8_MEM_WRITE | ADRENO8_MEM_PRIV));
    assert(cleaned[0].start == adreno8_space_table(&space, 2, 4)->table);
    assert(cleaned[1].start == adreno8_space_table(&space, 1, 0)->table);
    assert((*adreno8_space_entry(&space, 0x800000) & ~PTE_ADDRESS) == (0xe03 | (3ULL << 53)));
    alloc_fail_after = 1;
    assert(!adreno8_space_map_region(&space, 0x3ffff000, payload, 8192, ADRENO8_MEM_READ));
    assert(adreno8_space_entry(&space, 0x3ffff000) != NULL);
    assert(*adreno8_space_entry(&space, 0x3ffff000) == 0);
    assert(adreno8_space_entry(&space, 0x40000000) == NULL);
    alloc_fail_after = -1;
    sync_ok = false;
    assert(!adreno8_space_map_region(&space, 0x801000, payload, 4096, ADRENO8_MEM_READ));
    assert(space.is_on && walk(space.tables[0].phys, 0x801000) == kvtophys(payload));
    sync_ok = true;
    adreno8_space_off(&space, 1);

    /* CB0 and CB2 retain separate stream ownership and ASIDs. */
    reset(&space, false);
    map_one(&space);
    assert(adreno8_space_on(&space, 5, 1));
    struct adreno8_gpu_space gpu = { .name = "gpu", .context = 0 };
    assert(adreno8_space_new_table(&gpu, 0, 0));
    assert(kmem_alloc(0, &payload, PAGE_SIZE, 0, 0) == KERN_SUCCESS);
    assert(adreno8_space_map_region(&gpu, 0x10000000, payload, 4096,
                                   ADRENO8_MEM_READ | ADRENO8_MEM_EXEC));
    assert(adreno8_space_on(&gpu, 0, 0));
    assert(regs[SMMU_S2CR(0) / 4] == 0x300 && regs[SMMU_S2CR(1) / 4] == 0x302);
    assert(regs[(0x20000 + SMMU_CB_TTBR0 + 4) / 4] >> 16 == 0);
    assert(regs[(0x22000 + SMMU_CB_TTBR0 + 4) / 4] >> 16 == 2);
    assert(regs[SMMU_SMR(2) / 4] == 0);

    /* Secure preflight for the candidate route (stream 2, match 2, CB 3):
     * every rejection is reads only, and bounds are checked before any MMIO
     * read. A pass reports register state, nothing was written. */
    unsigned reads_before = 0;
    reset(&space, false);
    seed_gmu_route();
    assert(adreno8_secure_preflight());
    assert(writes == 0);
    assert(reads[(0x23000 + SMMU_CB_SCTLR) / 4] == 1);
    assert(reads[SMMU_SMR(2) / 4] == 1 && reads[SMMU_SMR(23) / 4] == 1);
    assert(reads[SMMU_S2CR(2) / 4] == 0);
    /* The GMU self-route check re-reads slot 1's SMR and S2CR once more on
     * top of the sample pass. */
    assert(reads[SMMU_SMR(1) / 4] == 2 && reads[SMMU_S2CR(1) / 4] == 2);
    /* The device-read CB3 SCTLR 0x96cc51a is nonzero with the enable (M) bit
     * clear; only M rejects. With M set the strict pass still takes the
     * one-pass read-only view (each SMR once, S2CR only behind a valid SMR),
     * printing the invalid candidate slot with its S2CR explicitly unread. */
    reset(&space, false);
    regs[(0x23000 + SMMU_CB_SCTLR) / 4] = 1u;
    assert(!adreno8_secure_preflight() && writes == 0);
    assert(reads[(0x23000 + SMMU_CB_SCTLR) / 4] == 1);
    for (unsigned j = 0; j < 24; j++) {
        assert(reads[SMMU_SMR(j) / 4] == 1 && reads[SMMU_S2CR(j) / 4] == 0);
    }
    /* The same device-read value with the M bit clear satisfies the HLOS-side
     * conditions at the sample time; secure-world occupancy stays invisible. */
    reset(&space, false);
    seed_gmu_route();
    regs[(0x23000 + SMMU_CB_SCTLR) / 4] = 0x96cc51au;
    assert(adreno8_secure_preflight() && writes == 0);
    assert(reads[(0x23000 + SMMU_CB_SCTLR) / 4] == 1);
    for (unsigned j = 0; j < 24; j++) {
        assert(reads[SMMU_SMR(j) / 4] == (j == 1 ? 2 : 1) &&
               reads[SMMU_S2CR(j) / 4] == (j == 1 ? 2 : 0));
    }
    /* A routing conflict while the M bit rejects: the candidate slot taken and
     * a route into CB3 both print, the scan reaches the last match with
     * each register read once and no S2CR read for the other 22 invalid
     * slots, and the M-bit rejection still stands. */
    reset(&space, false);
    regs[(0x23000 + SMMU_CB_SCTLR) / 4] = 1u;
    regs[SMMU_SMR(2) / 4] = 0x80000002;
    regs[SMMU_S2CR(2) / 4] = 3u;
    regs[SMMU_SMR(5) / 4] = 0x80020000;
    regs[SMMU_S2CR(5) / 4] = 0x10003u;
    assert(!adreno8_secure_preflight() && writes == 0);
    for (unsigned j = 0; j < 24; j++) {
        assert(reads[SMMU_SMR(j) / 4] == 1 &&
               reads[SMMU_S2CR(j) / 4] == (j == 2 || j == 5 ? 1 : 0));
    }
    /* The kernel's own GMU takeover route (exact SID5 -> context 2, as
     * stage 9 writes it) neither names context 3 nor covers stream 2, so the
     * HLOS-side conditions hold, yet the route still prints with its raw
     * values; post-analysis can tell a real view from degraded all-invalid
     * reads. It counts in scanned and valid only. */
    reset(&space, false);
    seed_gmu_route();
    regs[(0x23000 + SMMU_CB_SCTLR) / 4] = 0x96cc51au;
    regs[SMMU_SMR(6) / 4] = 0x80000005;
    regs[SMMU_S2CR(6) / 4] = 0x302u;   /* SHCFG_NSH | context 2, the exact value stage 9 writes */
    assert(adreno8_secure_preflight() && writes == 0);
    for (unsigned j = 0; j < 24; j++) {
        assert(reads[SMMU_SMR(j) / 4] == (j == 1 ? 2 : 1) &&
               reads[SMMU_S2CR(j) / 4] == (j == 1 ? 2 : j == 6 ? 1 : 0));
    }
    reset(&space, false);
    regs[SMMU_SMR(2) / 4] = 0x80000002;
    regs[SMMU_S2CR(2) / 4] = 3u;
    assert(!adreno8_secure_preflight() && writes == 0);
    assert(reads[SMMU_S2CR(2) / 4] == 1);
    reset(&space, false);
    regs[SMMU_SMR(5) / 4] = 0x80000002;
    regs[SMMU_S2CR(5) / 4] = 0x20003u;
    assert(!adreno8_secure_preflight() && writes == 0);
    reset(&space, false);
    regs[SMMU_SMR(5) / 4] = 0x80000004;
    regs[SMMU_S2CR(5) / 4] = 3u;
    assert(!adreno8_secure_preflight() && writes == 0);
    /* Fault and bypass leftovers that route to context 3 from a slot whose
     * SMR does not cover stream 2 still reject under strict rules; the
     * ordinary path would let both through. */
    const uint32_t cb3_route_leftovers[] = {0x20003u, 0x10003u};
    for (unsigned i = 0; i < 2; i++) {
        reset(&space, false);
        regs[SMMU_SMR(5) / 4] = 0x80000004;
        regs[SMMU_S2CR(5) / 4] = cb3_route_leftovers[i];
        assert(!adreno8_secure_preflight() && writes == 0);
    }
    reset(&space, false);
    seed_gmu_route();
    regs[SMMU_SMR(5) / 4] = 0x80000004;
    regs[SMMU_S2CR(5) / 4] = 0x20004u;
    /* An exact entry that does not match stream 2 and does not route to
     * context 3 is fine; the pass needs both halves. */
    assert(adreno8_secure_preflight() && writes == 0);
    /* Stream 2 active on the candidate slot but routed to context 4: the
     * candidate slot is taken, so the preflight rejects. */
    reset(&space, false);
    regs[SMMU_SMR(2) / 4] = 0x80000002;
    regs[SMMU_S2CR(2) / 4] = 0x10004u;
    assert(!adreno8_secure_preflight() && writes == 0);
    assert(reads[SMMU_S2CR(2) / 4] == 1);
    /* A range match whose mask covers stream 2 rejects even when it routes
     * to another context. */
    reset(&space, false);
    regs[SMMU_SMR(5) / 4] = 0x80020000;
    regs[SMMU_S2CR(5) / 4] = 0x10004u;
    assert(!adreno8_secure_preflight() && writes == 0);
    assert(reads[SMMU_SMR(5) / 4] == 1 && reads[SMMU_S2CR(5) / 4] == 1);
    /* An exact match on stream 2 sitting on a non-candidate slot rejects
     * while routing to context 4: the stream is taken either way. */
    reset(&space, false);
    regs[SMMU_SMR(5) / 4] = 0x80000002;
    regs[SMMU_S2CR(5) / 4] = 0x10004u;
    assert(!adreno8_secure_preflight() && writes == 0);
    assert(reads[SMMU_S2CR(5) / 4] == 1);
    /* The GMU space's own route check: each variant below satisfies the
     * candidate-side conditions yet rejects the experiment with zero writes
     * because the GMU's own slot 1 route is absent, moved, or retyped. */
    reset(&space, false);
    adreno8_gmu_space.is_on = false;
    assert(!adreno8_secure_preflight() && writes == 0);
    reset(&space, false);
    adreno8_gmu_space.is_on = true;
    assert(!adreno8_secure_preflight() && writes == 0);
    reset(&space, false);
    seed_gmu_route();
    regs[SMMU_SMR(1) / 4] = 0x84000405;    /* valid slot, mask and stream moved */
    assert(!adreno8_secure_preflight() && writes == 0);
    reset(&space, false);
    seed_gmu_route();
    regs[SMMU_S2CR(1) / 4] = 0x20302u;     /* fault type into context 2 */
    assert(!adreno8_secure_preflight() && writes == 0);
    reset(&space, false);
    seed_gmu_route();
    regs[SMMU_S2CR(1) / 4] = 0x304u;       /* translate into context 4 */
    assert(!adreno8_secure_preflight() && writes == 0);
    adreno8_gmu_space.is_on = false;
    /* Candidate slot beyond the SMR count: rejected with no MMIO read at all. */
    reset(&space, false);
    regs[SMMU_IDR0 / 4] = 0x4c017e02;
    assert(adreno8_step_smmu_read());
    for (unsigned i = 0; i < sizeof(reads) / sizeof(reads[0]); i++) reads_before += reads[i];
    assert(!adreno8_secure_preflight() && writes == 0);
    unsigned reads_after = 0;
    for (unsigned i = 0; i < sizeof(reads) / sizeof(reads[0]); i++) reads_after += reads[i];
    assert(reads_after == reads_before);
    /* One more stream into an enabled context (LPAC SID 1 into the GPU context). */
    reset(&space, false);
    assert(adreno8_step_smmu_read());
    space.name = "route";
    assert(!adreno8_space_route(&space, 1, 3) && writes == 0);
    space.is_on = true;
    assert(!adreno8_space_route(&space, 1, 24) && writes == 0);
    assert(!adreno8_space_route(&space, 0x8000, 3) && writes == 0);
    regs[SMMU_SMR(3) / 4] = 0x80000007;     /* the slot itself is taken */
    assert(!adreno8_space_route(&space, 1, 3) && writes == 0);
    regs[SMMU_SMR(3) / 4] = 0;
    regs[SMMU_SMR(0) / 4] = 0x80010000;     /* stream 0 with mask 1 already covers stream 1 */
    assert(!adreno8_space_route(&space, 1, 3) && writes == 0);
    regs[SMMU_SMR(0) / 4] = 0x80000000;     /* stream 0 alone */
    assert(adreno8_space_route(&space, 1, 3) && write_log_count == 3);
    assert(write_log[0].offset == SMMU_S2CR(3) && write_log[0].value == (SMMU_S2CR_FAULT | SMMU_S2CR_SHCFG_NSH));
    assert(write_log[1].offset == SMMU_SMR(3) && write_log[1].value == 0x80000001);
    assert(write_log[2].offset == SMMU_S2CR(3) && write_log[2].value == (SMMU_S2CR_SHCFG_NSH | space.context));
    assert(adreno8_space_route_check(&space, 1, 3) && !adreno8_space_route_check(&space, 2, 3));
    space.is_on = false;
    assert(!adreno8_space_route_check(&space, 1, 3));
    /* A context fault seen after the switch to translate puts the slot back to fault. */
    reset(&space, false);
    assert(adreno8_step_smmu_read());
    space.name = "route";
    space.is_on = true;
    regs[(0x20000 + space.context * 0x1000 + SMMU_CB_FSR) / 4] = 2;
    assert(!adreno8_space_route(&space, 1, 3));
    assert((regs[SMMU_S2CR(3) / 4] & 0x30000) == SMMU_S2CR_FAULT && regs[SMMU_SMR(3) / 4] == 0x80000001);
    /* An SMR that does not read back stops before the translate write. */
    reset(&space, false);
    assert(adreno8_step_smmu_read());
    space.name = "route";
    space.is_on = true;
    reject_offset = SMMU_SMR(3);
    reject_value = 0x80000001;
    assert(!adreno8_space_route(&space, 1, 3) && write_log_count == 2);
    assert((regs[SMMU_S2CR(3) / 4] & 0x30000) == SMMU_S2CR_FAULT);
    release_all();
    puts("SMMU mapping, register writes, ATOS capability and failure cleanup: PASS");
}
'''
if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="adreno8-smmu-") as directory:
        target = Path(directory)
        (target / "adreno8_private.h").write_text(stub)
        (target / "adreno8_memory_private.h").write_text((ROOT / "xnu/files/bsd/dev/adreno8_memory_private.h").read_text())
        (target / "adreno8_smmu_private.h").write_text((ROOT / "xnu/files/bsd/dev/adreno8_smmu_private.h").read_text())
        (target / "adreno8_smmu.c").write_text(driver)
        (target / "check.c").write_text('#include "adreno8_smmu.c"\n' + tests)
        for page_size in (4096, 16384):
            binary = target / f"check-{page_size}"
            subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}", "-fsanitize=address,undefined",
                            "-fno-omit-frame-pointer", str(target / "check.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            lines = result.stdout.splitlines()
            out = "\n".join(lines)
            summaries = [l for l in lines if "preflight routing sample at t=" in l]
            rejects = [i for i, l in enumerate(lines) if "has the M bit set; the context bank fails the preflight" in l]
            stands = [i for i, l in enumerate(lines) if "the M-bit rejection stands" in l]
            inval = [i for i, l in enumerate(lines) if "invalid; s2cr not read" in l]
            entries = [i for i, l in enumerate(lines)
                       if "preflight routing sample: match" in l and "invalid" not in l]
            # Nineteen strict preflight runs: each prints exactly one summary.
            # Verdicts: two M-bit rejections, eight routing conflicts, five
            # GMU-route rejections, four satisfied. Two M-set scenarios (the
            # M-set leftover, the M-set conflict) each keep the rejection line
            # first, at most one sample line per probed slot, and close with
            # their own summary.
            assert len(summaries) == 19, len(summaries)
            assert len(rejects) == 2 and len(stands) == 2, (len(rejects), len(stands))
            assert out.count("a routing conflict rejects the candidate") == 8
            assert out.count("HLOS-side conditions are satisfied at the sample time") == 9
            assert out.count("context 3 satisfied the HLOS-side conditions") == 4
            for r, s in zip(rejects, stands):
                assert r < s and not [t for t in rejects if r < t < s]
                assert len([i for i in inval + entries if r < i < s]) <= 24
            assert out.count("match 2 smr 0 invalid; s2cr not read") == 16
            assert len(inval) == 16 and len(entries) == 19
            assert "match 2 smr 0x80000002 s2cr 0x3" in out
            assert "match 5 smr 0x80020000 s2cr 0x10003" in out
            assert "match 6 smr 0x80000005 s2cr 0x302" in out
            assert "match 1 smr 0x84000405 s2cr 0x302" in out
            assert "scanned 24, valid 0, 0 on" in lines[stands[0]], lines[stands[0]]
            assert "scanned 24, valid 2, 2 on" in lines[stands[1]], lines[stands[1]]
            # The seeded GMU route prints as its exact stage-9 value on every
            # pass, and the GMU-route negatives reject after their sample.
            assert out.count("match 1 smr 0x80000005 s2cr 0x302") == 4
            assert out.count("the GMU space is not enabled") == 1
            assert out.count("GMU route missing:") == 1
            assert out.count("GMU route moved:") == 1
            assert out.count("GMU route type changed:") == 1
            assert out.count("GMU route context changed:") == 1
            own_gmu = [l for l in summaries if "scanned 24, valid 1, 0 on" in l]
            assert len(own_gmu) == 5, own_gmu
            assert sum("are satisfied at the sample time" in l for l in own_gmu) == 5
            assert out.count("scanned 24, valid 2, 0 on") == 2
            for l in lines:
                if "preflight" in l:
                    print(l)
            print(f"XNU PAGE_SIZE={page_size}: PASS")

        control = (ROOT / "xnu/files/bsd/dev/adreno8_smmu_private.h").read_text()
        variants = {
            "stall_includes_gmu": control.replace(
                "context == 0 || context == SECURE_CONTEXT",
                "context == 0 || context == SECURE_CONTEXT || context == GMU_CONTEXT", 1),
            "stall_enables_interrupt": control.replace(
                "? SCTLR_CFCFG : 0", "? (SCTLR_CFCFG | 1u << 6) : 0", 1),
            "stall_disabled": control.replace(
                "adreno8_gpu_stall &&", "false &&", 1),
        }
        for name, mutated in variants.items():
            assert mutated != control, name
            (target / "adreno8_smmu_private.h").write_text(mutated)
            for page_size in (4096, 16384):
                binary = target / f"mutant-{name}-{page_size}"
                subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}",
                                "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                                str(target / "check.c"), "-o", str(binary)], check=True)
                result = subprocess.run([str(binary)], capture_output=True, text=True)
                assert result.returncode != 0, f"mutation survived: {name}, PAGE_SIZE={page_size}"
        print(f"GPU stall control mutations rejected: {len(variants)} for both page sizes")
