#!/usr/bin/env python3
"""Drive the one-shot adreno8_secure_xfer entry against scripted SCM outcomes."""
from pathlib import Path
import runpy
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adreno8_logcheck as logcheck
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / "tools/test-adreno8-smmu.py"))
stub = fixture["stub"].replace("assert(size == 4096);", "assert(size != 0);")
stub += "\nuint32_t adreno8_gpu8_secure_mode;\n"
poison_hook = """static vm_offset_t poison_va; /* when set, kvtophys fails for that page */
static uint64_t kvtophys(vm_offset_t va) {
    if (poison_va && va >= poison_va && va < poison_va + PAGE_SIZE) return 0;"""
stub = stub.replace("static uint64_t kvtophys(vm_offset_t va) {", poison_hook, 1)
assert "poison_va" in stub
driver = (ROOT / "xnu/files/bsd/dev/adreno8_secure.c").read_text()
setup = fixture["tests"].split("int main(void) {")[0]
fakes = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uintptr_t vm_offset_t;
typedef size_t vm_size_t;
int32_t adreno8_scm_call(uint64_t function, uint64_t arginfo, uint64_t argument0,
    uint64_t argument1, uint64_t argument2, uint64_t argument3, uint64_t *answer);
void adreno8_sleep_ms(uint32_t ms);
struct wire_region { uint64_t address, size; };
struct wire_perm { uint32_t vmid, perm; uint64_t context; uint32_t context_size, unused; };
static struct scm_call { uint64_t function, arginfo, args[4]; } calls[80];
static struct wire_region snap[80][32];
static struct wire_perm perm_snap[80][2];
static uint64_t ext_snap[80][4];
static unsigned call_count, clean_at_call[80], clean_lo[80];
static int32_t outcomes[80][2];
static uint64_t outcome_answers[80];
static unsigned outcome_count, outcome_next;
static unsigned sleeps, sleeps_total;
static void script(int32_t status, uint64_t answer) {
    assert(outcome_count < 80);
    outcomes[outcome_count][0] = status;
    outcome_answers[outcome_count] = answer;
    outcome_count++;
}
static struct wire_region *staged(uint64_t page_phys) {
    return (struct wire_region *)physical_table(page_phys);
}
static struct wire_perm *staged_perms(uint64_t page_phys) {
    return (struct wire_perm *)((uintptr_t)physical_table(page_phys) + 576);
}
static uint64_t *staged_extended(uint64_t page_phys) {
    return (uint64_t *)((uintptr_t)physical_table(page_phys) + 640);
}
static uint32_t *staged_sources(uint64_t page_phys) {
    return (uint32_t *)((uintptr_t)physical_table(page_phys) + 512);
}

@@SNAPHELP@@
int32_t adreno8_scm_call(uint64_t function, uint64_t arginfo, uint64_t argument0,
    uint64_t argument1, uint64_t argument2, uint64_t argument3, uint64_t *answer)
{
    assert(call_count < 80);
    assert(function == 0x42000c16ull && arginfo == 0x1117ull);
    calls[call_count] = (struct scm_call){function, arginfo, {argument0, argument1, argument2, argument3}};
    clean_lo[call_count] = call_count ? clean_at_call[call_count - 1] : 0;
    clean_at_call[call_count] = clean_count;
    for (unsigned i = 0; i < argument1 / 16; i++) {
        struct wire_region *live = (struct wire_region *)(uintptr_t)physical_table(argument0);
        snap[call_count][i] = live[i];
    }
    /* One argument page serves every batch, so the permission and extended
     * records must be captured at call time, not read back later. */
    unsigned perm_count = (unsigned)(staged_extended(argument0)[2] / 24);
    assert(perm_count <= 2);
    for (unsigned i = 0; i < perm_count; i++) {
        perm_snap[call_count][i] = staged_perms(argument0)[i];
    }
    for (unsigned i = 0; i < 4; i++) {
        ext_snap[call_count][i] = staged_extended(argument0)[i];
    }


@@SNAP@@
    unsigned pick = outcome_next < outcome_count ? outcome_next : outcome_count - 1;
    *answer = outcome_answers[pick];
    int32_t status = outcomes[pick][0];
    if (outcome_next < outcome_count) outcome_next++;
    call_count++;
    return status;
}
void adreno8_sleep_ms(uint32_t ms) { sleeps++; sleeps_total += ms; }
/* Every byte MP_ASSIGN reads from the argument page: 32 region slots, the
 * source word at 512, the permission records at 576 and the four extended
 * argument words at 640 (ASSIGN_USED). */
#define ARG_CLEAN_BYTES (640u + 4u * sizeof(uint64_t))
/* Cleans before run_xfer are setup noise (page-table creation, a freed and
 * re-taken argument page could even reuse the address); batch checks start
 * at this baseline instead of the window start. */
static unsigned xfer_clean_base;
static int arg_clean_event(unsigned call) {
    vm_offset_t arg_va = (vm_offset_t)(uintptr_t)physical_table(calls[call].args[0]);
    unsigned lo = clean_lo[call] > xfer_clean_base ? clean_lo[call] : xfer_clean_base;
    int ev = -1;
    for (unsigned i = lo; i < clean_at_call[call]; i++) {
        if (cleaned[i].start == arg_va && cleaned[i].size >= ARG_CLEAN_BYTES &&
            cleaned[i].snap_len >= ARG_CLEAN_BYTES) {
            ev = (int)i; /* last covering clean inside this call's window */
        }
    }
    return ev;
}
static void check_envelope(unsigned call, unsigned regions) {
    struct scm_call *c = &calls[call];
    assert(c->args[0] == kvtophys((vm_offset_t)(uintptr_t)physical_table(c->args[0])));
    assert(c->args[1] == regions * 16);
    assert(c->args[2] == c->args[0] + 512 && c->args[3] == c->args[0] + 640);
    assert(*staged_sources(c->args[0]) == 3);
    assert(ext_snap[call][0] == 4 && ext_snap[call][1] == c->args[0] + 576);
    int ev = arg_clean_event(call);
    assert(ev >= 0); /* a clean over all 672 wire bytes ran since the previous SCM */
    /* The bytes at clean time must equal what the SMC entry reads. Later
     * batches restage the same page, so compare against the call-time
     * snapshots; a clean staged early leaves stale snapshot bytes here. */
    const unsigned char *sn = cleaned[ev].snap;
    assert(sizeof(struct wire_region) == 16 && sizeof(struct wire_perm) == 24);
    for (unsigned i = 0; i < regions; i++) {
        assert(memcmp(sn + i * 16, &snap[call][i], sizeof(snap[call][0])) == 0);
    }
    uint32_t sources_three = 3;
    assert(memcmp(sn + 512, &sources_three, 4) == 0);
    assert(ext_snap[call][2] % 24 == 0 && ext_snap[call][2] <= 48);
    assert(memcmp(sn + 576, perm_snap[call], (size_t)ext_snap[call][2]) == 0);
    assert(memcmp(sn + 640, ext_snap[call], 4 * sizeof(uint64_t)) == 0);
}
'''
fakes = fakes.replace("@@SNAPHELP@@", logcheck.SNAP_HELPERS_C).replace(
    "@@SNAP@@", logcheck.snap_c("call_count"))
assert "@@SNAP" not in fakes
tests = r'''
static struct adreno8_memory make_buffer(unsigned pages) {
    struct adreno8_memory buffer;
    vm_offset_t memory;
    assert(kmem_alloc(0, &memory, (vm_size_t)pages * PAGE_SIZE, 0, 0) == KERN_SUCCESS);
    memset(&buffer, 0, sizeof(buffer));
    buffer.name = "test";
    buffer.memory = memory;
    buffer.size = pages * PAGE_SIZE;
    return buffer;
}
/* The secure space mirrors the production mapping shape: root, level 1, level 2. */
static struct adreno8_gpu_space *secure_space(unsigned tables) {
    for (unsigned i = 0; i < tables; i++) {
        assert(adreno8_space_new_table(&adreno8_secure_space, i == 0 ? 0 : (i == 1 ? 1 : 2), 0));
    }
    return &adreno8_secure_space;
}
static bool run_xfer(struct adreno8_gpu_space *space, struct adreno8_memory *buffer) {
    xfer_clean_base = clean_count; /* batch clean checks start at this event index */
    return adreno8_secure_preflight() && adreno8_secure_xfer(space, buffer);
}
static void check_regions(unsigned call, struct adreno8_memory *buffer, unsigned pages,
    unsigned first_page)
{
    struct scm_call *c = &calls[call];
    struct wire_region *regions = snap[call];
    assert(c->args[1] % 16 == 0);
    unsigned seen = c->args[1] / 16;
    /* The fixture gives every kernel page a non-neighbor physical address. */
    assert(seen == pages);
    for (unsigned i = 0; i < seen; i++) {
        assert(regions[i].address == kvtophys(buffer->memory + (first_page + i) * PAGE_SIZE));
        assert(regions[i].size == PAGE_SIZE);
    }
}
static void check_data_call(unsigned call, struct adreno8_memory *buffer, unsigned regions,
    unsigned first_page)
{
    check_envelope(call, regions);
    check_regions(call, buffer, regions, first_page);
    assert(perm_snap[call][0].vmid == 10 && perm_snap[call][0].perm == 6 &&
           perm_snap[call][0].context == 0);
    assert(ext_snap[call][2] == 24);
}
static void check_table_call(unsigned call, struct adreno8_gpu_space *space, unsigned count)
{
    check_envelope(call, count);
    assert(perm_snap[call][0].vmid == 3 && perm_snap[call][0].perm == 6);
    assert(perm_snap[call][1].vmid == 10 && perm_snap[call][1].perm == 4);
    assert(ext_snap[call][2] == 48);
    int ev = arg_clean_event(call);
    for (unsigned i = 0; i < count; i++) {
        assert(snap[call][i].address == space->tables[i].phys);
        assert(snap[call][i].size == PAGE_SIZE);
        /* Each table page is cleaned over its native page before the batch
         * stages, so the clean event sits inside this call's window and
         * predates the argument-page clean. */
        vm_offset_t tva = (vm_offset_t)(uintptr_t)physical_table(snap[call][i].address);
        int hit = 0;
        unsigned lo = clean_lo[call] > xfer_clean_base ? clean_lo[call] : xfer_clean_base;
        for (unsigned j = lo; j < (unsigned)ev; j++) {
            if (cleaned[j].start == tva && cleaned[j].size >= PAGE_SIZE) {
                hit = 1;
            }
        }
        assert(hit);
    }
}
int main(int argc, char **argv) {
    struct adreno8_memory buffer;
    struct adreno8_gpu_space *space;

    /* The production preflight reads the SMMU layout; seed the same register
     * picture the boot probe sees so the handover paths can run. The GMU
     * route seed is the stage-9 write for slot 1: the strict preflight
     * requires it to still read back as configured. */
    regs[SMMU_IDR0 / 4] = 0x4c017e18;
    regs[SMMU_IDR1 / 4] = 0x40000015;
    regs[SMMU_IDR2 / 4] = 0x5511;
    regs[SMMU_SCR0 / 4] = 0x2e0406;
    adreno8_gmu_space.is_on = true;
    regs[SMMU_SMR(1) / 4] = SMR_VALID | SMR_MASK << 16 | 5;
    regs[SMMU_S2CR(1) / 4] = SMMU_S2CR_SHCFG_NSH | GMU_CONTEXT;
    assert(adreno8_step_smmu_read());

    if (argc > 1 && strcmp(argv[1], "preflight") == 0) {
        /* An occupied candidate context bank stops the handover before staging,
         * SCM calls, the one-shot state and the uncertain-ownership flag; the
         * same entry succeeds after the register picture is clean again. */
        script(0, 0);
        regs[(0x23000 + SMMU_CB_SCTLR) / 4] = 1u;
        buffer = make_buffer(1);
        space = secure_space(1);
        assert(!run_xfer(space, &buffer));
        assert(call_count == 0 && !adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_NOT_STARTED);
        regs[(0x23000 + SMMU_CB_SCTLR) / 4] = 0u;
        assert(run_xfer(space, &buffer));
        assert(call_count == 2 && adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        puts("secure preflight rejection leaves the one-shot entry unconsumed: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "argfail") == 0) {
        /* The argument page allocation is retryable and pre-SCM, so the abort
         * closes the path without marking any page lost. */
        buffer = make_buffer(1);
        space = secure_space(1);
        alloc_fail_after = 0;
        assert(!run_xfer(space, &buffer));
        assert(call_count == 0 && !adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        alloc_fail_after = -1;
        assert(!run_xfer(space, &buffer));
        assert(call_count == 0 && space->secure_tables == 0);
        puts("argument page failure ends the handover before any MP_ASSIGN: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "answer") == 0) {
        /* The table batch landed, the data batch came back with a nonzero
         * answer: ownership is unclear, pages retained, cursor not committed. */
        script(0, 0);
        script(0, 5);
        buffer = make_buffer(2);
        space = secure_space(3);
        assert(!run_xfer(space, &buffer));
        assert(call_count == 2 && adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(space->secure_tables == 0);
        assert(!run_xfer(space, &buffer) && call_count == 2);
        puts("MP_ASSIGN nonzero answer retained through the handover: PASS");
        return 0;
    }
    if (argc > 1 && strncmp(argv[1], "waitq", 5) == 0) {
        /* A positive status leaves the request unfinished and ends the
         * experiment: no EBUSY-style retry, the handover stays terminal and
         * one-shot. Status 2 and 3 both take this path. */
        int32_t pending = argv[1][5] == '3' ? 3 : 2;
        script(pending, 0);
        buffer = make_buffer(1);
        space = secure_space(1);
        assert(!run_xfer(space, &buffer));
        assert(call_count == 1 && sleeps == 0 && adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(!run_xfer(space, &buffer) && call_count == 1);
        printf("MP_ASSIGN WAITQ status %d retained without continuation: PASS\n", (int)pending);
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "failsecond") == 0) {
        /* The table batch lands and the first data batch fails: the failed
         * batch was fully staged and printed, so its wire snapshot must match
         * its log lines, and no later batch may appear at all. */
        script(0, 0);
        script(5, 5);
        buffer = make_buffer(3);
        space = secure_space(3);
        assert(!run_xfer(space, &buffer));
        assert(call_count == 2 && adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        puts("second batch failure keeps the failed batch fully logged: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "regions1025") == 0) {
        /* Past the earlier 1024-entry logging budget: the driver keeps no
         * region cache, so all 1025 regions plus the table page are staged,
         * printed from the staged batch, and handed over in full. */
        script(0, 0);
        buffer = make_buffer(1025);
        space = secure_space(1);
        assert(run_xfer(space, &buffer));
        assert(call_count == 1 + (1025 + 31) / 32 && !adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        puts("1025 regions hand over fully with no region cache: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "poison") == 0) {
        /* An unprovable physical page in the planning walk stops the handover
         * before any MP_ASSIGN, so no page needs the lost flag. */
        buffer = make_buffer(2);
        space = secure_space(1);
        poison_va = buffer.memory;
        assert(!run_xfer(space, &buffer));
        assert(call_count == 0 && !adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(space->secure_tables == 0);
        puts("physical check failure ends the handover before any MP_ASSIGN: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "batchfail") == 0) {
        /* The second data batch fails after the table call and the first data
         * batch landed: ownership is unclear, the cursor never commits. */
        script(0, 0);
        script(0, 0);
        script(-5, 0);
        buffer = make_buffer(40);
        space = secure_space(3);
        assert(!run_xfer(space, &buffer));
        assert(call_count == 3 && adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(space->secure_tables == 0);
        puts("second MP_ASSIGN data batch failure retained: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "ebusy") == 0) {
        /* -EBUSY retries at the driver's own 30 ms spacing; the second attempt of
         * the table batch lands and the handover completes. */
        script(-12, 0);
        script(0, 0);
        script(0, 0);
        buffer = make_buffer(3);
        space = secure_space(3);
        assert(run_xfer(space, &buffer));
        assert(call_count == 3 && sleeps == 1 && sleeps_total == 30);
        assert(!adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(space->secure_tables == 3);
        puts("EBUSY retry inside one handover completes: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "exhaust") == 0) {
        /* EBUSY retries exhausted on the first batch: pages retained, every
         * later handover attempt refuses without a new SCM call. */
        for (unsigned i = 0; i < 20; i++) script(-12, 0);
        buffer = make_buffer(1);
        space = secure_space(1);
        assert(!run_xfer(space, &buffer));
        assert(call_count == 20 && sleeps == 20 && sleeps_total == 600);
        assert(adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(!run_xfer(space, &buffer) && call_count == 20);
        puts("EBUSY retry exhaustion ends the secure path for the boot: PASS");
        return 0;
    }

    if (argc > 1 && strcmp(argv[1], "guards") == 0) {
        /* Rejected inputs change no state and issue no SCM call; the same
         * entry still succeeds afterwards, so a guard never consumes the
         * one-shot. */
        struct adreno8_memory zero = { .name = "test" };
        struct adreno8_gpu_space nospace = { .name = "secure", .context = SECURE_CONTEXT };
        struct adreno8_memory empty;
        script(0, 0);
        script(0, 0);
        buffer = make_buffer(1);
        space = secure_space(1);
        assert(!adreno8_secure_xfer(space, &zero));
        empty = buffer;
        empty.size = 0;
        assert(!adreno8_secure_xfer(space, &empty));
        assert(!adreno8_secure_xfer(&nospace, &buffer));
        space->secure_tables = space->table_count + 1;
        assert(!adreno8_secure_xfer(space, &buffer));
        space->secure_tables = space->table_count;
        space->table_count = sizeof(space->tables) / sizeof(space->tables[0]) + 1;
        assert(!adreno8_secure_xfer(space, &buffer));
        space->table_count = 1;
        space->is_on = true;
        assert(!adreno8_secure_xfer(space, &buffer));
        space->is_on = false;
        space->secure_tables = 0;
        assert(call_count == 0 && !adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_NOT_STARTED);
        assert(adreno8_secure_preflight() && adreno8_secure_xfer(space, &buffer));
        assert(call_count == 2 && adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        puts("input guards reject without consuming the one-shot: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "contig") == 0) {
        /* Physically adjacent pages merge into one region while the separate
         * table allocations stay three single-page regions: the table plan and
         * the data plan differ in one handover. */
        script(0, 0);
        script(0, 0);
        pa_flat = true;
        buffer = make_buffer(70);
        space = secure_space(3);
        assert(run_xfer(space, &buffer));
        assert(call_count == 2 && !adreno8_secure_lost());
        check_table_call(0, space, 3);
        check_envelope(1, 1);
        assert(snap[1][0].address == kvtophys(buffer.memory) &&
               snap[1][0].size == 70 * PAGE_SIZE);
        assert(perm_snap[1][0].vmid == 10 && perm_snap[1][0].perm == 6);
        assert(ext_snap[1][2] == 24);
        assert(space->secure_tables == 3);
        puts("adjacent pages merge into one region, table pages stay separate: PASS");
        return 0;
    }
    if (argc > 1 && (strncmp(argv[1], "regions", 7) == 0 || strncmp(argv[1], "mixed", 5) == 0)) {
        /* Region counts around the 32-entry batch boundary: regions31/32 fit
         * one data call, regions33 needs 32 + 1. The mixed variants merge two
         * adjacent pages per region so multi-page entries cross the boundary
         * too. */
        bool mixed = argv[1][0] == 'm';
        unsigned regions = (unsigned)atoi(argv[1] + (mixed ? 5 : 7));
        assert(regions == 31 || regions == 32 || regions == 33);
        unsigned pages = mixed ? 2 * regions : regions, span = mixed ? 2 : 1;
        unsigned data_calls = (regions + ASSIGN_SECTIONS - 1) / ASSIGN_SECTIONS;
        for (unsigned i = 0; i < data_calls + 1; i++) script(0, 0);
        pa_flat = true;
        gap_every = span;
        buffer = make_buffer(pages);
        space = secure_space(3);
        assert(run_xfer(space, &buffer));
        assert(call_count == 1 + data_calls && !adreno8_secure_lost());
        check_table_call(0, space, 3);
        unsigned first_page = 0, left = regions;
        for (unsigned c = 1; c <= data_calls; c++) {
            unsigned entries = left < ASSIGN_SECTIONS ? left : ASSIGN_SECTIONS;
            check_envelope(c, entries);
            for (unsigned i = 0; i < entries; i++) {
                assert(snap[c][i].address ==
                       kvtophys(buffer.memory + (first_page + i * span) * PAGE_SIZE));
                assert(snap[c][i].size == span * PAGE_SIZE);
            }
            first_page += entries * span;
            left -= entries;
        }
        assert(first_page == pages);
        assert(space->secure_tables == 3);
        printf("%u %s regions split into %u data calls: PASS\n",
            regions, mixed ? "two-page" : "single-page", data_calls);
        return 0;
    }
    if (argc > 1 && (strcmp(argv[1], "unaligned") == 0 || strcmp(argv[1], "unaligned-table") == 0)) {
        /* A 2 KiB physical shift breaks granule alignment. The data variant
         * fails in the planning walk, the table variant in the pre-SCM table
         * check while the data plan passes: both end with zero SCM calls and
         * no page lost. */
        bool on_table = argv[1][8] == 't';
        pa_flat = true;
        space = secure_space(1);
        buffer = make_buffer(2);
        shift_va = on_table ? space->tables[0].table : buffer.memory;
        shift_delta = 2048;
        assert(!run_xfer(space, &buffer));
        assert(call_count == 0 && !adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        assert(space->secure_tables == 0);
        printf("unaligned %s page ends the handover before any MP_ASSIGN: PASS\n",
            on_table ? "table" : "data");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "va-unaligned") == 0) {
#if PAGE_SIZE > SMMU_GRANULE
        /* Shifting the buffer start 4 KiB inside a 16 KiB page keeps every
         * physical page aligned and contiguous under the fixture map, so
         * only the native-alignment rule in the page helper rejects it. */
        pa_flat = true;
        space = secure_space(1);
        buffer = make_buffer(2);
        buffer.memory += SMMU_GRANULE;
        assert(!run_xfer(space, &buffer));
        assert(call_count == 0 && !adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        puts("native-unaligned buffer ends the handover before any MP_ASSIGN: PASS");
#else
        puts("native page equals the granule here, no unaligned start exists: PASS");
#endif
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "table-align") == 0) {
        /* A 4 KiB shift keeps the table page granule-aligned but leaves it
         * misaligned to the native page size on 16 KiB kernels, and the
         * read-back no longer matches the address the mapper recorded: the
         * handover rejects it on every page size. */
        pa_flat = true;
        script(0, 0);
        script(0, 0);
        space = secure_space(1);
        buffer = make_buffer(1);
        shift_va = space->tables[0].table;
        shift_delta = SMMU_GRANULE;
        assert(!run_xfer(space, &buffer));
        assert(call_count == 0 && !adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        puts("native alignment and read-back gate table pages: PASS");
        return 0;
    }
    if (argc > 1 && strcmp(argv[1], "parange-table") == 0) {
        /* A table page ending exactly at the 36-bit limit is inside the
         * translatable range at 4 KiB granularity and ends past the limit when
         * the native page is 16 KiB: the check runs before the first MP_ASSIGN
         * either way. */
        script(0, 0);
        script(0, 0);
        buffer = make_buffer(1);
        next_pa = SMMU_PHYS_LIMIT - SMMU_GRANULE;
        space = secure_space(1);
        /* Only the table page sits at the boundary; the argument page must
         * stay inside the range or the argument page check ends the handover
         * first. */
        next_pa = 0x80000000 + 0x8000000;
        if (PAGE_SIZE > 4096) {
            assert(!run_xfer(space, &buffer));
            assert(call_count == 0 && adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        } else {
            assert(run_xfer(space, &buffer));
            assert(call_count == 2 && space->secure_tables == 1);
        }
        puts("table page physical range checked against the SMMU limit: PASS");
        return 0;
    }

    /* Success path, one handover: table pages first with {HLOS read/write,
     * CP_PIXEL read}, then 70 non-adjacent data pages in 32 + 32 + 6 region
     * entries with CP_PIXEL read/write, the cursor commits, the state closes,
     * and a replayed request adds no SCM call. */
    script(0, 0);
    script(0, 0);
    script(0, 0);
    script(0, 0);
    buffer = make_buffer(70);
    space = secure_space(3);
    assert(run_xfer(space, &buffer));
    assert(call_count == 4 && !adreno8_secure_lost());
    check_table_call(0, space, 3);
    check_data_call(1, &buffer, 32, 0);
    check_data_call(2, &buffer, 32, 32);
    check_data_call(3, &buffer, 6, 64);
    assert(space->secure_tables == 3);
    assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
    assert(!run_xfer(space, &buffer));
    assert(call_count == 4);
    release_all();
    puts("one-shot handover: envelopes, batching, cursor commit and terminal state: PASS");
}
'''
if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="adreno8-secure-") as directory:
        target = Path(directory)
        (target / "adreno8_private.h").write_text(stub)
        for name in ("adreno8_smmu_private.h", "adreno8_memory_private.h", "adreno8_secure_private.h"):
            (target / name).write_text((ROOT / "xnu/files/bsd/dev" / name).read_text())
        (target / "adreno8_smmu.c").write_text(fixture["driver"])
        (target / "adreno8_secure.c").write_text(driver)
        (target / "check.c").write_text('#include "adreno8_smmu.c"\n' + fakes +
            '#include "adreno8_secure.c"\n' + setup + tests)
        for page_size in (4096, 16384):
            binary = target / f"check-{page_size}"
            subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}", "-fsanitize=address,undefined",
                            "-fno-omit-frame-pointer", str(target / "check.c"), "-o", str(binary)], check=True)
            for mode in ((), ("argfail",), ("preflight",), ("answer",), ("waitq",),
                         ("poison",), ("batchfail",), ("ebusy",), ("exhaust",),
                         ("guards",), ("contig",), ("regions31",), ("regions32",),
                         ("regions33",), ("mixed31",), ("mixed32",), ("mixed33",),
                         ("unaligned",), ("unaligned-table",), ("table-align",),
                         ("va-unaligned",), ("waitq3",), ("regions1025",),
                         ("failsecond",), ("parange-table",)):
                result = subprocess.run([str(binary), *mode], check=True,
                                        capture_output=True, text=True)
                if mode == ():
                    # The plan line prints from the real PAGE_SIZE and merge rule:
                    # single-page regions, ceil(regions / 32) data calls plus one
                    # table call over the three pages.
                    want = ("70 pages merge into 70 data regions plus 3 table pages, "
                            "4 logical MP_ASSIGN batches")
                    assert want in result.stdout, (want, result.stdout)
                    assert ("secure handover complete: 70 pages owned by CP_PIXEL "
                            "after 4 top-level MP_ASSIGN SCM calls, A6 interrupted "
                            "continuation SMCs not counted") in result.stdout
                    assert "no MEM_ALLOC reply" in result.stdout
                if mode == ("ebusy",):
                    # The log tells logical batches apart from real SCM attempts:
                    # the EBUSY retry makes three attempts for two batches.
                    assert "2 logical MP_ASSIGN batches" in result.stdout
                    assert ("secure handover complete: 3 pages owned by CP_PIXEL "
                            "after 3 top-level MP_ASSIGN SCM calls, A6 interrupted "
                            "continuation SMCs not counted") in result.stdout
                if mode in ((), ("contig",), ("regions31",), ("regions32",),
                            ("regions33",), ("mixed31",), ("mixed32",),
                            ("mixed33",), ("regions1025",), ("ebusy",)):
                    # Region lines come from the staged batch: their count,
                    # per-batch numbering, and total length must match the
                    # plan line the same run printed.
                    plan = re.search(r"(\d+) pages merge into (\d+) data regions "
                                     r"plus (\d+) table pages", result.stdout)
                    assert plan, result.stdout
                    data_regions, table_regions = int(plan.group(2)), int(plan.group(3))
                    lines = re.findall(r"MP_ASSIGN plan: batch (\d+) region (\d+) addr ([0-9a-fx]+) len (\d+)",
                                       result.stdout)
                    assert len(lines) == data_regions + table_regions, (mode, len(lines))
                    seen: dict[int, list[int]] = {}
                    for batch, index, _addr, length in lines:
                        seen.setdefault(int(batch), []).append(int(index))
                        assert int(length) % page_size == 0
                    for indices in seen.values():
                        assert indices == list(range(len(indices)))
                    # Table regions carry one native page each, so the total
                    # length covers data pages plus table pages.
                    assert sum(int(length) for _b, _i, _a, length in lines) \
                        == (int(plan.group(1)) + table_regions) * page_size
                    headers = re.findall(r"MP_ASSIGN plan: batch \d+ (?:table|data), permissions",
                                         result.stdout)
                    assert len(headers) == (table_regions + 31) // 32 \
                        + (data_regions + 31) // 32
                    # Scenario side, independent of the log: how many table
                    # pages and merged data regions this mode staged. Batch
                    # roles in the stream must equal this declared sequence.
                    pages, merged = {
                        (): (70, 70), ("ebusy",): (3, 3), ("regions1025",): (1025, 1025),
                        ("contig",): (70, 1), ("regions31",): (31, 31),
                        ("regions32",): (32, 32), ("regions33",): (33, 33),
                        ("mixed31",): (62, 31), ("mixed32",): (64, 32),
                        ("mixed33",): (66, 33),
                    }[mode]
                    tables = 1 if mode == ("regions1025",) else 3
                    assert (data_regions, table_regions) == (merged, tables)
                    roles = (["table"] * ((tables + 31) // 32)
                             + ["data"] * ((merged + 31) // 32))
                    # Same-stream snapshots: every printed region, permission,
                    # and argument page must equal what the SCM entry read.
                    logcheck.check(result.stdout, page_size, roles)
                abort_modes = {("argfail",), ("poison",), ("unaligned",),
                               ("unaligned-table",), ("table-align",)}
                if page_size == 16384:
                    # At 4 KiB there is no start between the native page and
                    # the granule, so only the 16 KiB build rejects a page.
                    abort_modes.add(("va-unaligned",))
                if mode in (("failsecond",), ("answer",)):
                    # The parser asserts the whole stream: batch numbers,
                    # per-batch ordering, wire comparison for the landed table
                    # batch and the failed data batch, and that no event of
                    # any kind (plan, region, before, returned) follows the
                    # failed batch under any prefix. Roles come from the
                    # scenario: one table batch, then one data batch.
                    batches, calls, failed = logcheck.check(result.stdout, page_size,
                                                            ["table", "data"])
                    assert failed == 1 and sorted(batches) == [0, 1], (failed, sorted(batches))
                    assert len(calls) == 2 and batches[1]["failed"]
                if mode in (("waitq",), ("waitq3",)):
                    # The WAITQ status ends the handover on the table batch;
                    # the full stream is one failed batch and one SCM group.
                    batches, calls, failed = logcheck.check(result.stdout, page_size,
                                                            ["table"])
                    assert failed == 0 and sorted(batches) == [0], (failed, sorted(batches))
                    assert len(calls) == 1
                if mode in abort_modes or (mode == ("parange-table",) and page_size == 16384):
                    assert "ends before any MP_ASSIGN; no page transferred" in result.stdout
                    assert "secure handover attempted" not in result.stdout
                    assert "MP_ASSIGN batch" not in result.stdout
                    assert "MP_ASSIGN plan" not in result.stdout
                    assert "scmsnap call" not in result.stdout
                if mode in (("contig",), ("regions31",), ("regions32",), ("regions33",),
                            ("mixed31",), ("mixed32",), ("mixed33",)):
                    # The plan line prints merged region counts, so the batch
                    # split is auditable from the serial log.
                    pages, regions = {
                        ("contig",): (70, 1), ("regions31",): (31, 31),
                        ("regions32",): (32, 32), ("regions33",): (33, 33),
                        ("mixed31",): (62, 31), ("mixed32",): (64, 32),
                        ("mixed33",): (66, 33),
                    }[mode]
                    want = (f"{pages} pages merge into {regions} data regions plus "
                            f"3 table pages, {(regions + 31) // 32 + 1} "
                            "logical MP_ASSIGN batches")
                    assert want in result.stdout, (want, result.stdout)
                if mode == ("guards",):
                    assert "input rejected" in result.stdout
                    assert "secure handover complete: 1 pages owned by CP_PIXEL" in result.stdout
            print(f"XNU PAGE_SIZE={page_size}: PASS")
        # PAGE_SIZE coming from an extern variable, the shape the upstream
        # osfmk/mach/arm/vm_param.h of xnu-8796.121.2 gives it (PAGE_SHIFT_CONST
        # is declared there, not defined as a constant).  The same production
        # file, layout and asserts must compile without a constant PAGE_SIZE.
        (target / "runtime-page.h").write_text(
            "extern unsigned long vm_page_size;\n"
            "unsigned long vm_page_size = 4096;\n")
        subprocess.run(["cc", "-std=c11", "-DPAGE_SIZE=vm_page_size",
                        "-include", str(target / "runtime-page.h"), "-c",
                        str(target / "check.c"), "-o", str(target / "check-rt.o")],
                       check=True)
        print("PAGE_SIZE from an extern variable: layout and asserts compile: PASS")
