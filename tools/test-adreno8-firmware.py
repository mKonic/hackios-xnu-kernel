#!/usr/bin/env python3
"""Exercise production GMU parsing, allocation and loading with guarded host memory."""
from pathlib import Path
import argparse
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("firmware", nargs="?", type=Path, default=ROOT.parent / "hackios-xnu-userland/firmware-gen8/gen80000_gmu.bin")
args = parser.parse_args()
driver = (ROOT / "xnu/files/bsd/dev/adreno8_gmu.c").read_text()
header = (ROOT / "xnu/files/bsd/dev/adreno8_private.h").read_text()
constants = "\n".join(line for line in header.splitlines() if line.startswith(("#define GMU", "#define GPU_GBIF_", "#define RSCC")))


def function(name, result):
    start = driver.index(name + "(")
    end = driver.index("\n}\n", start) + 2
    return result + "\n" + driver[start:end] + "\n"


source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "adreno8_wire_private.h"
''' + constants + r'''
#define PAGE_MASK (PAGE_SIZE - 1u)
#define KMA_KOBJECT 0
#define KMA_ZERO 0
#define KMA_DATA 0
#define VM_KERN_MEMORY_IOKIT 0
#define KERN_SUCCESS 0
#define kernel_map 0
typedef uintptr_t vm_offset_t;
typedef size_t vm_size_t;
struct adreno8_gpu_block { const char *name; uint32_t address, size; vm_offset_t memory; };
struct adreno8_block { const char *name; uint32_t phys, size; vm_offset_t base; };
struct adreno8_gpu_block adreno8_gmu_demands[GMU_BLOCKS], adreno8_gmu_log, adreno8_gmu_hfi;
struct adreno8_block adreno8_gmu, adreno8_gpu, adreno8_rscc;
struct { bool is_on; } adreno8_gmu_space;
static unsigned adreno8_gmu_demand_count, adreno8_step, writes, allocations, failures;
static bool adreno8_fw_loaded, adreno8_gmu_runs, adreno8_rsc_asleep_at_first, adreno8_rsc_woken_by_us;
static bool fail_allocation, corrupt_readback, emulate_start = true, scm_ok = true;
static bool adreno8_scm_gpu_init(void) { return scm_ok; }
static const uint8_t *adreno8_gmu_firmware;
static uint32_t adreno8_gmu_firmware_size;
static uint32_t registers[0x68000 / 4], gpu_registers[0x10000 / 4], rsc_registers[0x1000 / 4];
static int kmem_alloc(int map, vm_offset_t *memory, vm_size_t size, int flags, int tag) {
    if (fail_allocation) return 1;
    *memory = (uintptr_t)calloc(1, size); allocations++; return *memory ? 0 : 1;
}
static uint64_t kvtophys(vm_offset_t memory) { return memory; }
static bool adreno8_cx_ready(void) { return true; }
static bool adreno8_read_firmware(void) { return adreno8_fw_validate(adreno8_gmu_firmware, adreno8_gmu_firmware_size); }
static void adreno8_announce(const char *message) {}
static void adreno8_settle(void) {}
static void adreno8_to_memory(vm_offset_t start, vm_size_t size) {}
static uint32_t adreno8_rd(vm_offset_t address) {
    uint32_t value = *(uint32_t *)address;
    return corrupt_readback && address == adreno8_gmu.base + GMU_ITCM ? value ^ 1 : value;
}
static void adreno8_wr(vm_offset_t address, uint32_t value) {
    *(uint32_t *)address = value; writes++;
    if (emulate_start && address == adreno8_gmu.base + GMU_CM3_SYSRESET && value == 0)
        registers[GMU_FW_INIT_RESULT / 4] = GMU_FW_STARTED;
}
static void adreno8_put(struct adreno8_block *block, const char *name, uint32_t offset, uint32_t value) {
    adreno8_wr(block->base + offset, value);
}
static uint32_t adreno8_get(struct adreno8_block *block, const char *name, uint32_t offset) {
    return adreno8_rd(block->base + offset);
}
static bool adreno8_wait_for(struct adreno8_block *block, uint32_t offset, uint32_t mask, uint32_t value, uint32_t ms) {
    return (adreno8_rd(block->base + offset) & mask) == value;
}
static void adreno8_gmu_post_mortem(void) { failures++; }
''' + "\n".join(function(name, "bool" if name != "adreno8_seen_at" else "vm_offset_t") for name in (
    "adreno8_block_new", "adreno8_covered", "adreno8_seen_at", "adreno8_step_firmware", "adreno8_step_gmu_start"
)) + r'''
static void release_memory(void) {
    for (unsigned i = 0; i < adreno8_gmu_demand_count; i++) free((void *)adreno8_gmu_demands[i].memory);
    memset(adreno8_gmu_demands, 0, sizeof(adreno8_gmu_demands)); adreno8_gmu_demand_count = 0;
    free((void *)adreno8_gmu_log.memory); free((void *)adreno8_gmu_hfi.memory);
    memset(&adreno8_gmu_log, 0, sizeof(adreno8_gmu_log)); memset(&adreno8_gmu_hfi, 0, sizeof(adreno8_gmu_hfi));
}
static void set_image(const void *data, uint32_t size) {
    adreno8_gmu_firmware = data; adreno8_gmu_firmware_size = size;
    memset(registers, 0, sizeof(registers)); writes = 0; corrupt_readback = false;
    adreno8_gmu_space.is_on = true; adreno8_fw_loaded = false; adreno8_gmu_runs = false;
}
int main(int argc, char **argv) {
    struct adreno8_fw_block block;
    uint32_t offset = 0;
    uint32_t valid[] = {0, 8, 0, 0, 0x20001000, 0x101};
    uint32_t crossing[] = {0x3ffc, 8, 0, 0, 0, 0};
    uint32_t overflow[] = {0xfffffffc, 8, 0, 0, 0, 0};
    uint32_t unbacked[] = {0x4000, 4, 0, 0, 0};
    uint32_t beyond[] = {0x4000, 0, 1, 4, 0x4000, 8, 0, 0, 0, 0};
    uint32_t overlap[] = {0, 8, 0, 0, 0, 0, 4, 4, 0, 0, 0};
    uint32_t late_metadata[] = {0, 4, 0, 0, 0, 0, 0, 1, 4};
    uint32_t partial_header[] = {0, 4, 0};
    uint32_t truncated[] = {0, 8, 0, 0, 0};
    uint32_t misaligned[] = {1, 4, 0, 0, 0};
    uint32_t zero_reservation[] = {0x4000, 0, 1, 0};
    adreno8_gmu.base = (uintptr_t)registers; adreno8_gpu.base = (uintptr_t)gpu_registers; adreno8_rscc.base = (uintptr_t)rsc_registers;
    assert(adreno8_fw_validate((void *)valid, sizeof(valid)));
    assert(!adreno8_fw_validate((void *)valid, 0));
#define BAD(array) assert(!adreno8_fw_validate((void *)array, sizeof(array)))
    BAD(crossing); BAD(overflow); BAD(unbacked); BAD(beyond); BAD(overlap); BAD(late_metadata);
    BAD(partial_header); BAD(truncated); BAD(misaligned); BAD(zero_reservation);
    assert(!adreno8_fw_next((void *)truncated, sizeof(truncated), &offset, &block) && offset == 0);
    assert(adreno8_fw_region(0x10007ffc, 4) == ADRENO8_FW_DTCM);
    assert(adreno8_fw_region(0x10007ffc, 8) == ADRENO8_FW_INVALID);
    assert(adreno8_fw_region(0xc0000000, 4096) == ADRENO8_FW_MEMORY);
    assert(!adreno8_range_contains(0x4000, 0x1000, 0xfffffff0, 0x5000));
    assert(adreno8_block_new(&adreno8_gmu_demands[0], "test", 0x4000, 0x1001));
    adreno8_gmu_demand_count = 1;
    assert(!adreno8_block_new(&adreno8_gmu_demands[1], "page overlap", 0x5000, 0x1000));
    assert(!adreno8_block_new(&adreno8_gmu_demands[1], "unaligned", 0x6004, 4));
    assert(!adreno8_block_new(&adreno8_gmu_demands[1], "too large", 0x60000000, GMU_BLOCK_CAP + 1));
    assert(adreno8_covered(0x4000, 0x1001)); assert(!adreno8_covered(0x4000, 0x1004));
    fail_allocation = true;
    assert(!adreno8_block_new(&adreno8_gmu_demands[1], "allocation failure", 0x6000, 0x1000));
    assert(!adreno8_gmu_demands[1].memory && adreno8_gmu_demand_count == 1); fail_allocation = false;
    release_memory();
    set_image(valid, sizeof(valid)); assert(adreno8_step_firmware() && adreno8_fw_loaded);
    assert(registers[GMU_ITCM / 4] == 0x20001000 && registers[GMU_ITCM / 4 + 1] == 0x101);
    set_image(valid, sizeof(valid)); corrupt_readback = true;
    assert(!adreno8_step_firmware() && !adreno8_fw_loaded && registers[GMU_CM3_SYSRESET / 4] == 1);
    set_image(crossing, sizeof(crossing)); assert(!adreno8_step_firmware() && writes == 0);
    set_image(valid, sizeof(valid)); registers[GMU_FW_INIT_RESULT / 4] = GMU_FW_STARTED;
    assert(!adreno8_step_firmware() && writes == 0);
    FILE *file = fopen(argv[1], "rb"); assert(file);
    assert(fseek(file, 0, SEEK_END) == 0); long size = ftell(file); assert(size > 0 && size <= UINT32_MAX);
    rewind(file); uint8_t *image = malloc(size); assert(fread(image, 1, size, file) == (size_t)size); fclose(file);
    assert(adreno8_fw_validate(image, size)); offset = 0; unsigned external = 0, segments = 0;
    while (offset < size) {
        assert(adreno8_fw_next(image, size, &offset, &block));
        if (block.size) { segments++; continue; }
        if ((block.type == 1 || block.type == 7) && adreno8_fw_region(block.address, block.value) == ADRENO8_FW_MEMORY) {
            assert(adreno8_block_new(&adreno8_gmu_demands[adreno8_gmu_demand_count], "firmware", block.address, block.value));
            adreno8_gmu_demand_count++; external++;
        }
    }
    assert(external == 6 && segments == 4);
    assert(adreno8_block_new(&adreno8_gmu_log, "log", 0x60002000, 0x4000));
    assert(adreno8_block_new(&adreno8_gmu_hfi, "hfi", 0x60006000, 0x4000));
    set_image(image, size); assert(adreno8_step_firmware() && adreno8_fw_loaded);
    unsigned before = writes; scm_ok = false;
    assert(!adreno8_step_gmu_start() && !adreno8_gmu_runs && writes == before);
    scm_ok = true;
    assert(adreno8_step_gmu_start() && adreno8_gmu_runs);
    assert(registers[GMU_GENERAL_10 / 4] == 0x08000000 && registers[GMU_RPMH_POWER_STATE / 4] == 0xf);
    assert(registers[GMU_AHB_FENCE_RANGE_0 / 4] == (1u << 31 | 0x32u << 18 | 0x8a0));
    assert(gpu_registers[GPU_GBIF_HALT / 4] == 8);
    assert(registers[GMU_MRC_GBIF_QOS / 4] == 0x33);
    assert(registers[GMU_AO_INTERRUPT_MASK / 4] == UINT32_MAX && registers[GMU_GMU2HOST_MASK / 4] == UINT32_MAX);
    adreno8_gmu_runs = false; registers[GMU_CM3_SYSRESET / 4] = 1; emulate_start = false;
    assert(!adreno8_step_gmu_start() && adreno8_gmu_runs && failures == 1);
    adreno8_gmu_runs = false; registers[GMU_CM3_SYSRESET / 4] = 1; adreno8_rsc_asleep_at_first = true;
    assert(!adreno8_step_gmu_start() && !adreno8_gmu_runs && adreno8_rsc_woken_by_us);
    registers[GMU_RSCC_CONTROL_ACK / 4] = 2; rsc_registers[RSCC_SEQ_BUSY / 4] = 1;
    assert(!adreno8_step_gmu_start() && !adreno8_gmu_runs);
    registers[GMU_RSCC_CONTROL_REQ / 4] = 0; rsc_registers[RSCC_SEQ_BUSY / 4] = 0; emulate_start = true;
    assert(adreno8_step_gmu_start() && !adreno8_rsc_woken_by_us && registers[GMU_RSCC_CONTROL_REQ / 4] == 0);
    release_memory(); free(image);
    puts("Firmware boundaries, allocation failures, readback failures and startup conditions: PASS");
}
'''
with tempfile.TemporaryDirectory(prefix="adreno8-firmware-") as directory:
    src = Path(directory) / "check.c"
    src.write_text(source)
    for page_size in (4096, 16384):
        binary = Path(directory) / f"check-{page_size}"
        subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}u", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(ROOT / "xnu/files/bsd/dev"), str(src), "-o", str(binary)], check=True)
        result = subprocess.run([str(binary), str(args.firmware.resolve())], capture_output=True, text=True)
        if result.returncode:
            print(result.stdout, result.stderr)
            result.check_returncode()
        print(f"XNU page size {page_size}: {result.stdout.splitlines()[-1]}")
