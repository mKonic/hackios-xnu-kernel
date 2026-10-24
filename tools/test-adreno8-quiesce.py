#!/usr/bin/env python3
"""Run the production stop paths with tracked MMIO, frees and loader return."""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
header = (ROOT / "xnu/files/bsd/dev/adreno8_private.h").read_text()
driver = (ROOT / "xnu/files/bsd/dev/adreno8.c").read_text()
structures = "\n".join(
    re.search(rf"struct {name} \{{.*?\n}};", header, re.S).group()
    for name in ("adreno8_block", "adreno8_gpu_block", "adreno8_space_table", "adreno8_gpu_space")
)
constant_names = (
    "GMU", "GMU_GMU2HOST_MASK", "GMU_AHB_FENCE_CLR", "GMU_CM3_SYSRESET",
    "GMU_RSCC_CONTROL_REQ", "GPUCC_CX_GDSCR", "GMU_MATCH",
)
constants = "\n".join(
    re.search(rf"^#define {name}(?:\(|\s).*", header, re.M).group()
    for name in constant_names
)
functions = "\n".join(
    re.search(rf"(?:static )?void\n{name}\(void\)\n\{{.*?\n}}", driver, re.S).group()
    for name in ("adreno8_gpu8_block_stop", "adreno8_quiesce")
)
# Returning from WFE is intercepted so the test can inspect retained resources.
functions, waits = re.subn(r'__asm__ volatile \("wfe"\);', "mock_wfe();", functions)
assert waits >= 1

stub = r'''
#include <assert.h>
#include <setjmp.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uintptr_t vm_offset_t;
#define PAGE_MASK (PAGE_SIZE - 1)
''' + structures + "\n" + constants + r'''
static struct {
    bool reset_required, gmu_runs, gmu_talks, rsc_woken_by_us, cx_changed;
    bool quiet, fw_loaded;
    uint32_t cx_saved_gdscr, gmu_demand_count;
    struct adreno8_block gmu, gpucc;
    struct adreno8_gpu_space gmu_space;
    struct adreno8_gpu_block gmu_demands[2], gmu_log, gmu_hfi;
    vm_offset_t log_view[4], hfi_view_table, hfi_view_cmd, hfi_view_resp, hfi_view_debug;
} resources;
static unsigned mmio_reads, mmio_writes, frees, space_off_calls, log_chars;
static unsigned loader_reuses, parks;
static bool stop_succeeds;
static bool dynamic_memory;
static bool adreno8_memory_retained(void) { return dynamic_memory; }
static jmp_buf parked;
static int kernel_map;
static int quiet_printf(const char *format, ...) { return 0; }
#define printf quiet_printf
static void mock_wfe(void) { parks++; longjmp(parked, 1); }
static void loader_log_putc(char ch) { assert(ch); log_chars++; }
static void adreno8_wr(vm_offset_t address, uint32_t value) { mmio_writes++; }
static uint32_t adreno8_rd(vm_offset_t address) { mmio_reads++; return 2; }
static void adreno8_put(struct adreno8_block *block, const char *name,
                       uint32_t offset, uint32_t value) { mmio_writes++; }
static void kmem_free(int map, vm_offset_t address, size_t size) {
    assert(address && size && !(size & PAGE_MASK));
    frees++;
}
static void adreno8_space_off(struct adreno8_gpu_space *space, uint32_t match) {
    assert(match == GMU_MATCH);
    space_off_calls++;
    if (stop_succeeds) space->is_on = false;
}
'''
names = (
    "reset_required", "gmu_runs", "gmu_talks", "rsc_woken_by_us", "cx_changed",
    "quiet", "fw_loaded", "cx_saved_gdscr", "gmu_demand_count", "gmu", "gpucc",
    "gmu_space", "gmu_demands", "gmu_log", "gmu_hfi", "log_view",
    "hfi_view_table", "hfi_view_cmd", "hfi_view_resp", "hfi_view_debug",
)
aliases = "\n".join(f"#define adreno8_{name} resources.{name}" for name in names)
tests = r'''
#undef printf
static void reset(bool requested) {
    memset(&resources, 0, sizeof(resources));
    mmio_reads = mmio_writes = frees = space_off_calls = log_chars = 0;
    loader_reuses = parks = 0;
    stop_succeeds = true;
    dynamic_memory = false;
    resources.reset_required = requested;
    resources.gmu_runs = resources.gmu_talks = resources.rsc_woken_by_us = true;
    resources.cx_changed = resources.fw_loaded = true;
    resources.gmu.base = 0x100000;
    resources.gpucc.base = 0x200000;
    resources.cx_saved_gdscr = 0x12345678;
    resources.gmu_space.is_on = true;
    resources.gmu_space.table_count = resources.gmu_demand_count = 2;
    for (unsigned i = 0; i < 2; i++) {
        resources.gmu_space.tables[i].table = (i + 1) * PAGE_SIZE;
        resources.gmu_demands[i].memory = (i + 3) * PAGE_SIZE;
        resources.gmu_demands[i].size = PAGE_SIZE + i;
    }
    resources.gmu_log.memory = 5 * PAGE_SIZE;
    resources.gmu_hfi.memory = 6 * PAGE_SIZE;
    resources.gmu_log.size = resources.gmu_hfi.size = PAGE_SIZE;
    for (unsigned i = 0; i < 4; i++) resources.log_view[i] = (i + 7) * PAGE_SIZE;
    resources.hfi_view_table = 11 * PAGE_SIZE;
    resources.hfi_view_cmd = 12 * PAGE_SIZE;
    resources.hfi_view_resp = 13 * PAGE_SIZE;
    resources.hfi_view_debug = 14 * PAGE_SIZE;
}
static void return_to_loader(void) {
    adreno8_quiesce();
    loader_reuses++;
}
int main(void) {
    unsigned char before[sizeof(resources)];

    reset(true);
    memcpy(before, &resources, sizeof(resources));
    adreno8_gpu8_block_stop();
    adreno8_gpu8_block_stop();
    assert(memcmp(before, &resources, sizeof(resources)) == 0);
    assert(mmio_reads == 0 && mmio_writes == 0 && frees == 0 && space_off_calls == 0);

    /* A panic must stop before the caller can reuse loader-owned kernel memory. */
    if (setjmp(parked) == 0) {
        return_to_loader();
        assert(!"DCVS quiesce returned to its caller");
    }
    assert(parks == 1 && log_chars > 0 && loader_reuses == 0);
    assert(memcmp(before, &resources, sizeof(resources)) == 0);
    assert(mmio_reads == 0 && mmio_writes == 0 && frees == 0 && space_off_calls == 0);

    reset(false); dynamic_memory = true;
    memcpy(before, &resources, sizeof(resources));
    adreno8_gpu8_block_stop();
    if (setjmp(parked) == 0) {
        return_to_loader();
        assert(!"Dynamic GPU mappings were reused");
    }
    assert(memcmp(before, &resources, sizeof(resources)) == 0);
    assert(mmio_reads == 0 && mmio_writes == 0 && frees == 0 && space_off_calls == 0);

    /* The unguarded controls must exercise cleanup, not silently skip both paths. */
    reset(false);
    adreno8_gpu8_block_stop();
    assert(mmio_writes > 0 && space_off_calls == 1 && frees == 6);
    assert(!resources.gmu_space.is_on && resources.gmu_space.table_count == 0);
    assert(!resources.cx_changed && !resources.gmu_runs && !resources.fw_loaded);
    assert(resources.gmu_demand_count == 0);
    assert(!resources.gmu_log.memory && !resources.gmu_hfi.memory);
    assert(!resources.hfi_view_table && !resources.log_view[0]);

    reset(false);
    return_to_loader();
    assert(mmio_writes == 3 && space_off_calls == 1 && frees == 0);
    assert(!resources.gmu_runs && !resources.gmu_space.is_on && loader_reuses == 1);

    reset(false);
    stop_succeeds = false;
    adreno8_gpu8_block_stop();
    assert(space_off_calls == 1 && frees == 0 && resources.gmu_space.is_on);
    assert(resources.cx_changed && resources.gmu_space.table_count == 2);
    assert(resources.gmu_demand_count == 2 && resources.gmu_log.memory != 0);
    if (setjmp(parked) == 0) {
        return_to_loader();
        assert(!"Incomplete SMMU shutdown returned to its caller");
    }
    assert(parks == 1 && loader_reuses == 0 && frees == 0);
    puts("DCVS resource retention, panic return and ordinary cleanup: PASS");
}
'''
with tempfile.TemporaryDirectory(prefix="adreno8-quiesce-") as directory:
    target = Path(directory)
    source = target / "check.c"
    source.write_text(stub + "\n" + aliases + "\n" + functions + "\n" + tests)
    for page_size in (4096, 16384):
        binary = target / f"check-{page_size}"
        subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                        str(source), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
        print(f"XNU PAGE_SIZE={page_size}: PASS")
