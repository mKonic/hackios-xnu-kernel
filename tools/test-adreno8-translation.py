#!/usr/bin/env python3
"""Verify object mappings, secure handover checks and register-only faults."""
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / "tools/test-adreno8-smmu.py"))
setup = fixture["tests"].split("int main(void) {")[0]
stub = fixture["stub"].replace(
    "return a.pa + page * 0x20000 + off;",
    "return a.pa + page * 0x20000 + off + "
    "((shift_va && va >= shift_va && va < shift_va + PAGE_SIZE) ? shift_delta : 0);")
# macOS arm64 protects memory in 16 KiB pages, including the 4 KiB XNU case.
stub = stub.replace("aligned_alloc(PAGE_SIZE, size)",
                    "aligned_alloc(16384, (size + 16383) & ~(size_t)16383)")
stub = stub.replace("static uint32_t reject_offset = ~0u, reject_value;",
                    "static uint32_t reject_offset = ~0u, reject_value, fsr_after_enable, sctlr_after_route_xor;")
stub = stub.replace("    regs[offset / 4] = value;",
    "    regs[offset / 4] = value;\n"
    "    if (offset == 0x23000 + SMMU_CB_SCTLR && (value & SCTLR_M))\n"
    "        regs[(0x23000 + SMMU_CB_FSR) / 4] |= fsr_after_enable;\n"
    "    if (offset == SMMU_S2CR(SECURE_MATCH) && value == 0x303)\n"
    "        regs[(0x23000 + SMMU_CB_SCTLR) / 4] ^= sctlr_after_route_xor;")
stub = stub.replace("static uint64_t next_pa = 0x80000000;",
    "static uint64_t next_pa = 0x80000000;\nstatic vm_offset_t ordinary_data;\nstatic bool forbid_mmio;")
stub = stub.replace("            if (!pa_flat) {", "            if (a.va == ordinary_data)\n"
    "                return a.pa + delta / 4096 * 0x3000 + delta % 4096 +\n"
    "                    ((shift_va && va >= shift_va && va < shift_va + 4096) ? shift_delta : 0);\n"
    "            if (!pa_flat) {")
for signature in ("static uint32_t adreno8_rd(vm_offset_t address) {",
                  "static void adreno8_put(struct adreno8_block *b, const char *name, uint32_t offset, uint32_t value) {"):
    assert signature in stub
    stub = stub.replace(signature, signature + "\n    assert(!forbid_mmio);", 1)
tests = r'''
#include <sys/mman.h>
static const uint32_t mapping_size = 0x138000;
static const uint32_t mapping_iova = 0xf01f0000;
static const uint32_t mapping_prot = ADRENO8_MEM_READ | ADRENO8_MEM_WRITE | ADRENO8_MEM_PRIV;
static vm_offset_t mapping_memory;
static void prepare(void) {
    struct adreno8_gpu_space ignored;
    reset(&ignored, false);
    memset(&adreno8_secure_space, 0, sizeof(adreno8_secure_space));
    adreno8_secure_space.name = "secure";
    adreno8_secure_space.context = SECURE_CONTEXT;
    assert(adreno8_step_smmu_read());
    assert(kmem_alloc(0, &mapping_memory, mapping_size, 0, 0) == KERN_SUCCESS);
    assert(adreno8_space_new_table(&adreno8_secure_space, 0, 0));
    assert(adreno8_space_map_region(&adreno8_secure_space, mapping_iova, mapping_memory,
        mapping_size, mapping_prot));
    regs[(0x23000 + SMMU_CB_SCTLR) / 4] = 0x96cc51a;
}
static bool check_mapping(void) {
    return adreno8_translation_walk(&adreno8_secure_space, mapping_iova,
        mapping_memory, mapping_size, mapping_prot);
}
static bool check_ordinary(void) {
    return adreno8_translation_walk(&adreno8_gpu_space, 0x101f0000,
        mapping_memory, mapping_size, 5);
}
static void check_ordinary_mapping(void) {
    prepare(); ordinary_data=mapping_memory;
    memset(&adreno8_gpu_space, 0, sizeof(adreno8_gpu_space));
    adreno8_gpu_space.name="gpu";
    assert(adreno8_space_new_table(&adreno8_gpu_space, 0, 0));
    assert(adreno8_space_map_region(&adreno8_gpu_space, 0x101f0000, mapping_memory, mapping_size, 5));
    adreno8_gpu_space.is_on=true;
    assert(mprotect((void *)mapping_memory, mapping_size, PROT_NONE)==0);
    forbid_mmio=true;
    assert(check_ordinary());
    /* Independent RO+EXEC reference: NC, outer-shareable, AF, nG, AP=RO/unprivileged. */
    for (uint32_t at=0;at<mapping_size;at+=4096) {
        uint64_t pa=kvtophys(mapping_memory+at);
        uint64_t *pte=adreno8_space_entry(&adreno8_gpu_space, 0x101f0000+at);
        assert(*pte==(pa|0xec3ULL));
        if (at) assert(pa!=kvtophys(mapping_memory)+at);
    }
    const uint32_t offsets[]={0,4096,0x10000,mapping_size-4096};
    const uint64_t bits[]={1,4,0x20,0x40,0x80,0x100,0x400,0x800,1ULL<<53,1ULL<<54,1ULL<<55,0x1000};
    for (unsigned i=0;i<sizeof(offsets)/sizeof(*offsets);i++) {
        uint64_t *pte=adreno8_space_entry(&adreno8_gpu_space,0x101f0000+offsets[i]), saved=*pte;
        for (unsigned j=0;j<sizeof(bits)/sizeof(*bits);j++) {
            *pte=saved^bits[j]; assert(!check_ordinary());
        }
        *pte=saved;
    }
    for (unsigned i=0;i<adreno8_gpu_space.table_count;i++) {
        struct adreno8_space_table *t=&adreno8_gpu_space.tables[i];
        uint64_t saved=t->phys; t->phys^=0x1000;
        assert(!check_ordinary()); t->phys=saved;
        shift_va=t->table; shift_delta=0x1000;
        assert(!check_ordinary()); shift_va=shift_delta=0;
        if (t->level==2) continue;
        uint64_t *entries=(uint64_t *)t->table;
        for (unsigned j=0;j<512;j++) if (entries[j]) {
            saved=entries[j]; entries[j]=saved|1ULL<<63;
            assert(!check_ordinary()); entries[j]=saved;
        }
    }
    shift_va=mapping_memory+4096; shift_delta=0x1000;
    assert(!check_ordinary()); shift_va=shift_delta=0;
    adreno8_gpu_space.is_on=false; assert(!check_ordinary());
    adreno8_gpu_space.is_on=true; adreno8_gpu_space.context=1; assert(!check_ordinary());
    adreno8_gpu_space.context=0;
    struct adreno8_gpu_space other=adreno8_gpu_space;
    assert(!adreno8_translation_walk(&other,0x101f0000,mapping_memory,mapping_size,5));
    assert(!adreno8_translation_walk(NULL,0x101f0000,mapping_memory,mapping_size,5));
    assert(check_ordinary());
    forbid_mmio=false;
    assert(mprotect((void *)mapping_memory,mapping_size,PROT_READ|PROT_WRITE)==0);
    ordinary_data=0;
    puts("ordinary mapping: global CB0/on guard, independent 4K PAs/full 0xec3, tables and inaccessible data: PASS");
}
int main(void) {
    check_ordinary_mapping();
    prepare();
    assert(ADRENO8_SECURE_IOVA_START == 0xf0000000u && ADRENO8_SECURE_IOVA_END == 0xfffff000u);
    uint32_t end_page = 0;
    assert(adreno8_space_find_range(&adreno8_secure_space, 0xffffe000u, ADRENO8_SECURE_IOVA_END,
        4096, 4096, &end_page) && end_page == 0xffffe000u);
    assert(!adreno8_space_find_range(&adreno8_secure_space, 0xffffe000u, ADRENO8_SECURE_IOVA_END,
        8192, 4096, &end_page));
    assert(!adreno8_space_find_range(&adreno8_secure_space, ADRENO8_SECURE_IOVA_START,
        ADRENO8_SECURE_IOVA_END, 0x10000000u, 4096, &end_page));
    assert(adreno8_space_table(&adreno8_secure_space, 1, 3) != NULL);
    assert(((uint64_t *)adreno8_secure_space.tables[0].table)[0] == 0);
    assert(adreno8_space_table(&adreno8_secure_space, 2, 0x780) != NULL);
    assert(adreno8_space_table(&adreno8_secure_space, 2, 0x781) != NULL);
    assert(check_mapping());
    /* Each mutation preserves the first leaf and damages a later part of the
     * mapping, except the explicit first-leaf case. */
    const uint32_t offsets[] = {0, SMMU_GRANULE, 0x10000, mapping_size - SMMU_GRANULE};
    for (unsigned i = 0; i < sizeof(offsets) / sizeof(offsets[0]); i++) {
        uint64_t *pte = adreno8_space_entry(&adreno8_secure_space, mapping_iova + offsets[i]);
        uint64_t saved = *pte;
        const uint64_t mutations[] = {0, saved ^ 0x1000, saved ^ 1, saved ^ (1ULL << 7),
            saved ^ (1ULL << 6), saved ^ (1ULL << 53), saved ^ (1ULL << 10)};
        for (unsigned j = 0; j < sizeof(mutations) / sizeof(mutations[0]); j++) {
            *pte = mutations[j];
            assert(!check_mapping());
        }
        *pte = saved;
    }
    for (unsigned i = 0; i < adreno8_secure_space.table_count; i++) {
        struct adreno8_space_table *t = &adreno8_secure_space.tables[i];
        if (t->level == 2) continue;
        uint64_t *descriptor = (uint64_t *)t->table;
        for (unsigned j = 0; j < 512; j++) {
            uint64_t saved = descriptor[j];
            if (saved == 0) continue;
            descriptor[j] = saved ^ 1;
            assert(!check_mapping());
            descriptor[j] = saved ^ 0x1000;
            assert(!check_mapping());
            descriptor[j] = saved | (1ULL << 63);
            assert(!check_mapping());
            descriptor[j] = saved;
        }
    }
    uint64_t root_saved = adreno8_secure_space.tables[0].phys;
    adreno8_secure_space.tables[0].phys ^= 0x1000;
    assert(!check_mapping());
    adreno8_secure_space.tables[0].phys = root_saved;
    /* Physical discontinuity late in the buffer must be checked at its own
     * offset, rather than derived from the first physical page. */
    shift_va = mapping_memory + mapping_size - PAGE_SIZE;
    shift_delta = 0x100000;
    assert(!check_mapping());
    shift_va = shift_delta = 0;
    assert(check_mapping());
    assert(!adreno8_translation_walk(&adreno8_secure_space, mapping_iova,
        mapping_memory, mapping_size - 1, mapping_prot));
    assert(!adreno8_translation_walk(&adreno8_secure_space, mapping_iova,
        mapping_memory, mapping_size, ADRENO8_MEM_READ));
    puts("complete secure mapping: parent descriptors, every leaf, physical pages and permissions: PASS");

    const struct write_record rejected[] = {
        {SMMU_S2CR(SECURE_MATCH), 0x20300},
        {SMMU_SMR(SECURE_MATCH), SMR_VALID | SECURE_STREAM},
        {0x23000 + SMMU_CB_SCTLR, 0},
        {0x1000 + SMMU_CBAR(SECURE_CONTEXT), STAGE_1},
        {0x1000 + SMMU_CBA2R(SECURE_CONTEXT), 1},
        {0x23000 + SMMU_CB_TCR2, TCR2_39_BITS},
        {0x23000 + SMMU_CB_TCR, TCR_39_BITS},
        {0x23000 + SMMU_CB_TTBR0, 0},
        {0x23000 + SMMU_CB_TTBR0 + 4, 0},
        {0x23000 + SMMU_CB_MAIR0, MAIR0_NORMAL},
        {0x23000 + SMMU_CB_FSR, FSR_ALL},
        {0x23000 + SMMU_CB_SCTLR, SCTLR_ON},
    };
    for (unsigned i = 0; i < sizeof(rejected) / sizeof(rejected[0]); i++) {
        prepare();
        assert(check_mapping());
        reject_offset = rejected[i].offset;
        reject_value = rejected[i].value;
        if (reject_offset == 0x23000 + SMMU_CB_TTBR0) {
            reject_value = (uint32_t)adreno8_secure_space.tables[0].phys;
        } else if (reject_offset == 0x23000 + SMMU_CB_TTBR0 + 4) {
            reject_value = (uint32_t)(adreno8_secure_space.tables[0].phys >> 32) | SECURE_CONTEXT << 16;
        } else if (reject_offset == 0x23000 + SMMU_CB_FSR) {
            regs[reject_offset / 4] = 2;
        }
        assert(!adreno8_secure_translation_3a(&adreno8_secure_space));
        assert(write_log[write_log_count - 1].offset == reject_offset);
        assert(write_log[write_log_count - 1].value == reject_value);
        if (i == 0) assert((regs[SMMU_SMR(SECURE_MATCH) / 4] & SMR_VALID) == 0);
        else assert((regs[SMMU_S2CR(SECURE_MATCH) / 4] & 0x30000) == 0x20000);
        assert(adreno8_secure_space.is_on == (i == sizeof(rejected) / sizeof(rejected[0]) - 1));
    }
    prepare();
    sync_ok = false;
    assert(!adreno8_secure_translation_3a(&adreno8_secure_space));
    assert(!adreno8_secure_space.is_on);
    assert(regs[(0x23000 + SMMU_CB_SCTLR) / 4] == 0);
    puts("secure context writes: each failed readback stops, TLB timeout stops before enable: PASS");

    for (unsigned stall = 0; stall <= 1; stall++) {
        prepare();
        adreno8_gpu_stall = stall;
        assert(adreno8_secure_translation_3a(&adreno8_secure_space));
        assert(regs[(0x23000 + SMMU_CB_SCTLR) / 4] == (stall ? 0xa7 : 0x27));
        assert(adreno8_secure_translation_3b(&adreno8_secure_space));
        assert(regs[SMMU_S2CR(SECURE_MATCH) / 4] == 0x303);
    }
    for (unsigned unexpected = 0; unexpected <= 1; unexpected++) {
        prepare();
        adreno8_gpu_stall = true;
        if (unexpected) sctlr_enable_bits = 1u << 6;
        else sctlr_raz_mask |= SCTLR_CFCFG;
        assert(!adreno8_secure_translation_3a(&adreno8_secure_space));
        assert(adreno8_secure_space.is_on && regs[SMMU_S2CR(SECURE_MATCH) / 4] == 0x20300);
        assert(write_log[write_log_count - 1].offset == 0x23000 + SMMU_CB_SCTLR);
    }
    const uint32_t sctlr_damage[] = {SCTLR_CFCFG, 1u << 6};
    for (unsigned i = 0; i < sizeof(sctlr_damage) / sizeof(sctlr_damage[0]); i++) {
        for (unsigned after_route = 0; after_route <= 1; after_route++) {
            prepare();
            adreno8_gpu_stall = true;
            assert(adreno8_secure_translation_3a(&adreno8_secure_space));
            if (after_route) sctlr_after_route_xor = sctlr_damage[i];
            else regs[(0x23000 + SMMU_CB_SCTLR) / 4] ^= sctlr_damage[i];
            unsigned route_writes = write_log_count;
            assert(!adreno8_secure_translation_3b(&adreno8_secure_space));
            assert(adreno8_secure_space.is_on && regs[SMMU_S2CR(SECURE_MATCH) / 4] == 0x20300);
            if (!after_route) {
                for (unsigned j = route_writes; j < write_log_count; j++)
                    assert(write_log[j].offset != SMMU_S2CR(SECURE_MATCH) || write_log[j].value != 0x303);
            }
            sctlr_after_route_xor = 0;
        }
    }
    puts("secure stall: 3a/3b exact control value, missing CFCFG and extra CFIE stop and retain: PASS");

    /* SMMUv2 FSR.Format [10:9] is read-only. Format 0b10 selects AArch64;
     * it remains set after W1C and must not prevent either validation step. */
    prepare();
    regs[(0x23000 + SMMU_CB_FSR) / 4] = 0x400;
    assert(adreno8_secure_translation_3a(&adreno8_secure_space));
    assert(regs[(0x23000 + SMMU_CB_FSR) / 4] == 0x400);
    assert(!adreno8_space_faulted(&adreno8_secure_space));
    for (unsigned stall = 0; stall <= 1; stall++) {
        for (unsigned bit = 0; bit < 32; bit++) {
            uint32_t fault = 1u << bit;
            if (!(fault & FSR_ALL)) continue;
            prepare();
            adreno8_gpu_stall = stall;
            regs[(0x23000 + SMMU_CB_FSR) / 4] = 0x400 | fault;
            reject_offset = 0x23000 + SMMU_CB_FSR;
            reject_value = FSR_ALL;
            assert(!adreno8_secure_translation_3a(&adreno8_secure_space));
            assert(!adreno8_secure_space.is_on);
            assert(write_log[write_log_count - 1].offset == reject_offset);
            assert(adreno8_space_faulted(&adreno8_secure_space));
            prepare();
            adreno8_gpu_stall = stall;
            regs[(0x23000 + SMMU_CB_FSR) / 4] = 0x400;
            fsr_after_enable = fault;
            assert(!adreno8_secure_translation_3a(&adreno8_secure_space));
            assert(adreno8_secure_space.is_on);
            assert(adreno8_space_faulted(&adreno8_secure_space));
            fsr_after_enable = 0;
        }
    }
    puts("secure FSR: AArch64 format accepted; every fault bit stops before/after enable: PASS");

    prepare();
    assert(check_mapping());
    assert(adreno8_secure_translation_3a(&adreno8_secure_space));
    assert(adreno8_secure_space.is_on);
    assert(regs[SMMU_S2CR(SECURE_MATCH) / 4] == 0x20300);
    unsigned writes_before = writes;
    assert(!adreno8_secure_translation_3a(&adreno8_secure_space) && writes == writes_before);
    /* Any data/table dereference in fault handling now faults in the host
     * process. Only the metadata and simulated registers remain accessible. */
    assert(mprotect((void *)mapping_memory, mapping_size, PROT_NONE) == 0);
    for (unsigned i = 0; i < adreno8_secure_space.table_count; i++)
        assert(mprotect((void *)adreno8_secure_space.tables[i].table, PAGE_SIZE, PROT_NONE) == 0);
    assert(!check_mapping());
    assert(!adreno8_space_faulted(&adreno8_secure_space));
    regs[(0x23000 + SMMU_CB_FSR) / 4] = 2;
    regs[(0x23000 + SMMU_CB_FAR_LO) / 4] = mapping_iova;
    assert(adreno8_space_faulted(&adreno8_secure_space));
    assert(mprotect((void *)mapping_memory, mapping_size, PROT_READ | PROT_WRITE) == 0);
    for (unsigned i = 0; i < adreno8_secure_space.table_count; i++)
        assert(mprotect((void *)adreno8_secure_space.tables[i].table, PAGE_SIZE, PROT_READ | PROT_WRITE) == 0);
    release_all();
    puts("secure faults: enabled state retained, fault registers reported with inaccessible pages: PASS");
}
'''

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="adreno8-translation-") as directory:
        target = Path(directory)
        (target / "adreno8_private.h").write_text(stub)
        (target / "adreno8_memory_private.h").write_text(
            (ROOT / "xnu/files/bsd/dev/adreno8_memory_private.h").read_text())
        (target / "adreno8_smmu_private.h").write_text(
            (ROOT / "xnu/files/bsd/dev/adreno8_smmu_private.h").read_text())
        (target / "adreno8_smmu.c").write_text(fixture["driver"])
        (target / "check.c").write_text('#include "adreno8_smmu.c"\n' + setup + tests)
        for page_size in (4096, 16384):
            binary = target / f"check-{page_size}"
            subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}",
                            "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                            str(target / "check.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            if result.returncode:
                print(result.stdout)
                print(result.stderr)
                result.check_returncode()
            print("\n".join(line for line in result.stdout.splitlines() if "PASS" in line))
            print(f"XNU PAGE_SIZE={page_size}: PASS")

        walker = (ROOT / "xnu/files/bsd/dev/adreno8_translation.c").read_text()
        smmu = fixture["driver"][:-len(walker)]
        variants = {
            "omit_stall_in_3a": smmu + walker.replace(
                "value = adreno8_smmu_sctlr(SECURE_CONTEXT);", "value = SCTLR_ON;", 1),
            "ignore_pre_route_sctlr": smmu + walker.replace(
                'adreno8_get(&adreno8_smmu, "secure sctlr before translate", '
                'cb + SMMU_CB_SCTLR) != sctlr', 'false', 1),
            "ignore_post_route_sctlr": smmu + walker.replace(
                'adreno8_get(&adreno8_smmu, "secure sctlr after translate", '
                'cb + SMMU_CB_SCTLR) != sctlr', 'false', 1),
            "reject_format_as_fault": (smmu + walker).replace(
                "if ((read & FSR_ALL) != 0) {", "if (read != 0) {", 1),
            "ignore_pre_enable_fault": (smmu + walker).replace(
                "if ((read & FSR_ALL) != 0) {", "if (false) {", 1),
            "first_leaf_only": smmu + walker.replace("at < size;", "at < SMMU_GRANULE;"),
            "ignore_table_descriptor": smmu + walker.replace(
                "descriptor == (table->phys | PTE_TABLE)", "true"),
            "ignore_leaf_permissions": smmu + walker.replace(
                "pte != (phys | attributes)", "(pte & PTE_ADDRESS) != phys"),
            "ignore_leaf_physical_address": smmu + walker.replace(
                "pte != (phys | attributes)", "(pte & ~PTE_ADDRESS) != attributes"),
            "ordinary_wrong_space": smmu + walker.replace("space == &adreno8_gpu_space && ", ""),
            "ordinary_wrong_context": smmu + walker.replace("space->context == 0 && ", ""),
            "ordinary_off": smmu + walker.replace("space->context == 0 && space->is_on", "space->context == 0"),
            "secure_on": smmu + walker.replace("space->context == SECURE_CONTEXT && !space->is_on", "space->context == SECURE_CONTEXT"),
            "ignore_table_physical_address": smmu + walker.replace("kvtophys(table->table) == table->phys", "true"),
            "omit_enabled_state": (smmu + walker).replace("space->is_on = true;", ""),
            "read_secure_leaf_on_fault": (smmu + walker).replace(
                "if (space == &adreno8_secure_space) {", "if (false) {"),
        }
        for name, mutated in variants.items():
            assert mutated != fixture["driver"], name
            (target / "adreno8_smmu.c").write_text(mutated)
            for page_size in (4096, 16384):
                binary = target / f"mutant-{name}-{page_size}"
                subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}",
                                "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                                str(target / "check.c"), "-o", str(binary)], check=True)
                result = subprocess.run([str(binary)], capture_output=True, text=True)
                assert result.returncode != 0, f"mutation survived: {name}, PAGE_SIZE={page_size}"
        print(f"Object mapping/fault mutations rejected: {len(variants)} x 2 page sizes")
