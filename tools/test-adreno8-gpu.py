#!/usr/bin/env python3
"""Compare gen8.0.0 tables with pinned vendor data and exercise production GPU startup."""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
PIN = "d50b305f7da9e14715a25120a4ac7b1a4b8b97c3"
HASHES = {
    "adreno_gen8.c": "3379f615fd4f350cf22ef60920891df3b7f479044947f3a714e26d22c8e2b56e",
    "adreno-gpulist.h": "7faff3fd1afc101dd1db984a9ca632ce884e8406fa856d4f60632e281077f3f6",
    "gen8_reg.h": "e85cb422ed221896433afcd6d931fd129f4c8e2edb730d15dfc1fc71c9aa51e2",
    "adreno_pm4types.h": "f66d80cd679aa0e0dd032f230bf960a964dfda282944bcb32c6643d1ebe5d731",
}
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--vendor-dir", required=True, type=Path, help=f"graphics-kernel sources at {PIN}")
parser.add_argument("--firmware-dir", type=Path, default=ROOT.parent / "hackios-xnu-userland/firmware-gen8")
args = parser.parse_args()
vendor = {}
for name, expected in HASHES.items():
    data = (args.vendor_dir / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected, f"unexpected vendor input: {name}"
    vendor[name] = data.decode()

registers = dict(re.findall(r"^#define\s+(GEN8_\w+)\s+(0x[0-9a-fA-F]+|[0-9]+)\s*(?:/\*.*)?$", vendor["gen8_reg.h"], re.M))
reference = "#define BIT(n) (1u << (n))\n"
reference += "enum { PIPE_NONE, PIPE_BR, PIPE_BV, PIPE_LPAC, PIPE_AQE0, PIPE_AQE1, PIPE_DDE_BR, PIPE_DDE_BV };\n"
gen = vendor["adreno_gen8.c"]
reference += gen[gen.index("/* CP Interrupt bits */"):gen.index("/* IFPC & Preemption static")]
tables = [
    ("adreno-gpulist.h", "gen8_0_0_nonctxt_regs", "a8_noncontext", "struct a8_reg_value"),
    ("adreno-gpulist.h", "gen8_0_0_protected_regs", "a8_protected", "struct a8_protect_span"),
    ("adreno-gpulist.h", "gen8_ao_hwcg_regs", "a8_ao_hwcg", "struct a8_reg_pair"),
    ("adreno_gen8.c", "gen8_0_0_bicubic_regs", "a8_bicubic", "struct a8_reg_pair"),
    ("adreno_gen8.c", "gen8_pwrup_reglist", "a8_powerup", "uint32_t"),
    ("adreno_gen8.c", "gen8_ifpc_pwrup_reglist", "a8_powerup_ifpc", "uint32_t"),
    ("adreno_gen8.c", "gen8_0_0_pwrup_extlist", "a8_powerup_ext", "struct a8_reg_pipes"),
]
table_checks = ""
for file, old, new, ctype in tables:
    body = re.search(r"\b" + old + r"\[\]\s*=\s*\{(.*?)\n\};", vendor[file], re.S)[1]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"\s*\{\s*0\s*\},?\s*$", "", body)
    # Resolve against the independently supplied vendor header, never the production constants.
    body = re.sub(r"\bGEN8_[A-Z0-9_]+", lambda m: registers[m[0]], body)
    reference += f"static const {ctype} ref_{new}[] = {{{body}\n}};\n"
    table_checks += f"assert(sizeof({new}) == sizeof(ref_{new})); assert(!memcmp({new},ref_{new},sizeof({new})));\n"

pm4 = vendor["adreno_pm4types.h"]
reference += re.search(r"^#define CP_TYPE7_PKT[^\n]+", pm4, re.M)[0] + "\n"
for name in ["pm4_calc_odd_parity_bit", "cp_type7_packet"]:
    reference += re.search(r"static inline uint " + name + r"\([^}]+\}", pm4)[0] + "\n"

shared = (DEV / "adreno8_private.h").read_text()
constants = "\n".join(line for line in shared.splitlines() if line.startswith("#define GMU"))
source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uintptr_t vm_offset_t;
typedef unsigned int uint;
struct adreno8_block { const char *name; uint32_t phys, size; vm_offset_t base; };
struct adreno8_gpu_space;
struct adreno8_block adreno8_gpu = { "gpu", 0, 0x40000, 0x10000000 };
struct adreno8_block adreno8_gmu = { "gmu", 0, 0x68000, 0x20000000 };
bool adreno8_reset_required;
int adreno8_say(const char *fmt, ...) { (void)fmt; return 0; }
uint32_t adreno8_rd(vm_offset_t);
void adreno8_wr(vm_offset_t, uint32_t);
void adreno8_settle(void) {}
void adreno8_sleep_us(uint32_t us) { (void)us; }
const uint8_t *redwood_image_file(const char *, const char *, uint64_t *, int *);
#define _DEV_ADRENO8_PRIVATE_H
''' + constants + '\n#include "adreno8_gpu.c"\n#undef printf\n' + reference + r'''
static uint32_t gpu_regs[8][0x10000], gmu_regs[0x1a000], current_pipe;
static unsigned gx_reads, gx_writes, raw_calls, allocation_count, publish_count;
static bool smmu_ok = true, hfi_ok = true, oob_ok = true, clock_ok = true;
static bool raw_ok = true, idle_ok = true, memory_on, smmu_after_raw, corrupt_scratch, acquire_ok = true;
static struct adreno8_memory allocations[4];
static uint8_t *device_memory[4], *files[2];
static uint64_t file_sizes[2];
static uint32_t allocation_prot[4];

uint32_t adreno8_rd(vm_offset_t address)
{
    if (address >= adreno8_gmu.base && address < adreno8_gmu.base + adreno8_gmu.size)
        return gmu_regs[(address - adreno8_gmu.base) / 4];
    assert(address >= adreno8_gpu.base && address < adreno8_gpu.base + adreno8_gpu.size);
    gx_reads++;
    uint32_t reg = (address - adreno8_gpu.base) / 4;
    if (reg == GEN8_CP_APERTURE_CNTL_HOST) return current_pipe << 12;
    if (reg == GEN8_RBBM_CGC_P2S_STATUS) return clock_ok;
    return gpu_regs[current_pipe][reg];
}

void adreno8_wr(vm_offset_t address, uint32_t value)
{
    if (address >= adreno8_gmu.base && address < adreno8_gmu.base + adreno8_gmu.size) {
        uint32_t reg = (address - adreno8_gmu.base) / 4;
        gmu_regs[reg] = value;
        if (reg == GMU_HOST2GMU_SET / 4 && value == 1u << 30 && oob_ok) {
            gmu_regs[GMU_GMU2HOST_INFO / 4] |= 1u << 31;
            gmu_regs[GMU_PWR_CLK_STATUS / 4] = 0;
        }
        if (reg == GMU_GMU2HOST_CLR / 4) gmu_regs[GMU_GMU2HOST_INFO / 4] &= ~value;
        return;
    }
    assert(address >= adreno8_gpu.base && address < adreno8_gpu.base + adreno8_gpu.size);
    gx_writes++;
    uint32_t reg = (address - adreno8_gpu.base) / 4;
    if (reg == GEN8_CP_APERTURE_CNTL_HOST) { current_pipe = value >> 12; assert(current_pipe < 8); }
    else gpu_regs[current_pipe][reg] = value;
}

const uint8_t *redwood_image_file(const char *dir, const char *name, uint64_t *size, int *disk)
{
    assert(!strcmp(dir, "fw"));
    unsigned i = !strcmp(name, "gen80000_aqe.fw");
    if (!i) assert(!strcmp(name, "gen80000_sqe.fw"));
    *size = file_sizes[i]; *disk = 0; return files[i];
}

struct adreno8_memory *adreno8_gpu_alloc(const char *name, uint32_t size, uint32_t align, uint32_t prot)
{
    assert(!memory_on && allocation_count < 4 && align == 4096);
    unsigned i = allocation_count++;
    struct adreno8_memory *m = &allocations[i];
    m->name = name; m->size = (size + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1);
    m->memory = (vm_offset_t)aligned_alloc(PAGE_SIZE, m->size);
    assert(m->memory); memset((void *)m->memory, 0, m->size);
    m->gpu_address = 0xc0000000 + i * 0x200000;
    device_memory[i] = calloc(1, m->size); assert(device_memory[i]);
    allocation_prot[i] = prot;
    return m;
}

bool adreno8_memory_publish(const struct adreno8_memory *m, uint32_t offset, uint32_t size)
{
    assert(offset <= m->size && size <= m->size - offset);
    memcpy(device_memory[m - allocations] + offset, (void *)(m->memory + offset), size);
    publish_count++; return true;
}
bool adreno8_memory_acquire(const struct adreno8_memory *m, uint32_t offset, uint32_t size)
{
    assert(offset <= m->size && size <= m->size - offset);
    if (!acquire_ok) return false;
    memcpy((void *)(m->memory + offset), device_memory[m - allocations] + offset, size);
    return true;
}
bool adreno8_gpu_memory_ready(void) { return memory_on; }
bool adreno8_gpu_faulted(void) { return !smmu_ok; }
bool adreno8_hwsched_ready(void) { return hfi_ok; }
bool adreno8_hwsched_raw(const uint32_t *pm4_words, uint32_t words, const char *name)
{
    (void)name;
    assert(oob_requested && oob_acked && memory_on && adreno8_reset_required);
    raw_calls++;
    if (!raw_ok) return false;
    if (raw_calls == 1) {
        assert(words == 10 && pm4_words[0] == cp_type7_packet(0x17, 1));
        assert(pm4_words[2] == cp_type7_packet(0x48, 7));
        assert(pm4_words[7] == powerup->gpu_address && pm4_words[8] == 0);
        uint32_t *p = (uint32_t *)device_memory[2];
        assert(p[0] == 0 && p[1] == 0 && p[2] == 0);
        unsigned dynamic = 0;
        for (unsigned pipe = 1; pipe <= 7; pipe++)
            for (unsigned i = 0; i < A8_COUNT(ref_a8_powerup_ext); i++)
                dynamic += !!(ref_a8_powerup_ext[i].pipes & (1u << pipe));
        assert(p[3] == (A8_COUNT(ref_a8_powerup_ifpc) | A8_COUNT(ref_a8_powerup) << 8 | dynamic << 16));
        unsigned at = 4;
        for (unsigned i = 0; i < A8_COUNT(ref_a8_powerup_ifpc); i++, at += 2) assert(p[at] == ref_a8_powerup_ifpc[i]);
        for (unsigned i = 0; i < A8_COUNT(ref_a8_powerup); i++, at += 2) assert(p[at] == ref_a8_powerup[i]);
        for (unsigned pipe = 1; pipe <= 7; pipe++)
            for (unsigned i = 0; i < A8_COUNT(ref_a8_powerup_ext); i++) {
                if (!(ref_a8_powerup_ext[i].pipes & (1u << pipe))) continue;
                assert(p[at] == pipe << 12 && p[at + 1] == ref_a8_powerup_ext[i].reg);
                assert(p[at + 2] == gpu_regs[pipe][p[at + 1]]); at += 3;
            }
        assert(gpu_regs[0][GEN8_CP_SQE_INSTR_BASE_LO] == sqe->gpu_address);
        assert(gpu_regs[0][GEN8_CP_AQE_INSTR_BASE_LO_0] == aqe->gpu_address);
    } else if (raw_calls == 2) {
        assert(words == 2 && pm4_words[0] == cp_type7_packet(0x66, 1) && pm4_words[1] == 0);
    } else {
        assert(raw_calls == 3 && words == 6);
        assert(pm4_words[0] == cp_type7_packet(0x3d, 3));
        assert(pm4_words[1] == scratch->gpu_address && pm4_words[2] == 0);
        assert(pm4_words[4] == cp_type7_packet(0x12, 0) && pm4_words[5] == cp_type7_packet(0x26, 0));
        ((uint32_t *)device_memory[3])[0] = pm4_words[3] ^ corrupt_scratch;
    }
    if (smmu_after_raw) smmu_ok = false;
    if (!idle_ok) gmu_regs[GMU(GEN8_GMUAO_GPU_CX_BUSY_STATUS) / 4] = 1u << 23;
    return true;
}

static void reset(void)
{
    for (unsigned i = 0; i < allocation_count; i++) { free((void *)allocations[i].memory); free(device_memory[i]); }
    memset(allocations, 0, sizeof(allocations)); memset(gpu_regs, 0, sizeof(gpu_regs)); memset(gmu_regs, 0, sizeof(gmu_regs));
    gpu_state = GPU_COLD; sqe = aqe = powerup = scratch = NULL; oob_requested = oob_acked = false;
    gx_reads = gx_writes = raw_calls = allocation_count = publish_count = current_pipe = 0;
    smmu_ok = hfi_ok = oob_ok = clock_ok = raw_ok = idle_ok = acquire_ok = true;
    smmu_after_raw = corrupt_scratch = memory_on = adreno8_reset_required = false;
    gmu_regs[GMU_PWR_CLK_STATUS / 4] = 3;
}

static void prepare(void)
{
    assert(adreno8_gpu_prepare());
    assert(!gx_reads && !gx_writes && allocation_count == 4);
    for (unsigned i = 0; i < 2; i++) {
        assert(allocation_prot[i] == (ADRENO8_MEM_READ | ADRENO8_MEM_EXEC));
        assert(!memcmp(device_memory[i], files[i] + 4, file_sizes[i] - 4));
    }
    assert(allocation_prot[2] == 11 && allocation_prot[3] == 11);
    memory_on = true;
}

int main(int argc, char **argv)
{
    assert(argc == 3);
    for (unsigned i = 0; i < 2; i++) {
        FILE *f = fopen(argv[i + 1], "rb"); assert(f);
        assert(!fseek(f, 0, SEEK_END)); file_sizes[i] = ftell(f); rewind(f);
        files[i] = malloc(file_sizes[i]); assert(files[i]);
        assert(fread(files[i], 1, file_sizes[i], f) == file_sizes[i]); fclose(f);
    }
''' + table_checks + r'''
    assert(A8_CP_SW_MASK == CP_SW_FAULT_STATUS_MASK_PIPE);
    assert(A8_CP_HW_MASK == CP_HW_FAULT_STATUS_MASK_PIPE);
    assert(A8_CP_GLOBAL_MASK == CP_INTERRUPT_STATUS_MASK_GLOBAL);
    for (unsigned opcode = 0; opcode < 128; opcode++)
        for (unsigned words = 0; words < 1024; words++) assert(adreno8_pm4_type7(opcode,words) == cp_type7_packet(opcode,words));
    uint32_t words[10]; memset(words, 0xa5, sizeof(words));
    assert(!adreno8_pm4_cp_init(words,9,0x1000) && words[0] == 0xa5a5a5a5);
    assert(!adreno8_pm4_cp_init(words,10,0) && !adreno8_pm4_cp_init(words,10,0x1001));
    assert(!adreno8_pm4_cp_init(words,10,1ULL << 32));
    assert(adreno8_pm4_cp_init(words,10,0xc0001000) == 10);
    const uint32_t init[] = {0x70970001,0x08000000,0x70c80007,0x14b,3,0x20000000,2,0xc0001000,0,0x80000000};
    assert(!memcmp(words,init,sizeof(init)));
    assert(!adreno8_pm4_secure_exit(words,1));
    assert(!adreno8_pm4_scratch(words,5,0x1000,1));
    assert(!adreno8_pm4_scratch(words,6,0x1001,1));
    assert(!adreno8_pm4_scratch(words,6,1ULL << 32,1));

    reset(); assert(!adreno8_gpu_cp_init() && !gx_reads && !gx_writes);
    reset(); uint64_t saved_size = file_sizes[0]; file_sizes[0] = 4;
    assert(!adreno8_gpu_prepare() && !allocation_count); file_sizes[0] = saved_size;
    reset(); prepare(); assert(adreno8_gpu_cp_init() && adreno8_gpu_cp_ready());
    assert(adreno8_gpu_secure_exit() && adreno8_gpu_scratch_test());
    assert(gpu_state == GPU_TESTED && oob_acked && adreno8_reset_required && raw_calls == 3);
    assert(gmu_regs[GMU_HOST2GMU_SET / 4] == 1u << 30);

    reset(); prepare(); oob_ok = false;
    assert(!adreno8_gpu_cp_init() && !gx_reads && !gx_writes && adreno8_reset_required);
    reset(); prepare(); smmu_ok = false;
    assert(!adreno8_gpu_cp_init() && !oob_requested && !gx_reads && !gx_writes);
    reset(); prepare(); clock_ok = false;
    assert(!adreno8_gpu_cp_init() && !raw_calls && gpu_state == GPU_FAILED);
    reset(); prepare(); raw_ok = false;
    assert(!adreno8_gpu_cp_init() && raw_calls == 1 && gpu_state == GPU_FAILED);
    unsigned read_stop = gx_reads, write_stop = gx_writes;
    assert(!adreno8_gpu_secure_exit() && !adreno8_gpu_scratch_test());
    assert(gx_reads == read_stop && gx_writes == write_stop);
    reset(); prepare(); smmu_after_raw = true;
    assert(!adreno8_gpu_cp_init() && gpu_state == GPU_FAILED);
    reset(); prepare(); idle_ok = false;
    assert(!adreno8_gpu_cp_init() && gpu_state == GPU_FAILED);
    reset(); prepare(); assert(adreno8_gpu_cp_init() && adreno8_gpu_secure_exit());
    corrupt_scratch = true; assert(!adreno8_gpu_scratch_test() && gpu_state == GPU_FAILED);
    reset(); prepare(); assert(adreno8_gpu_cp_init() && adreno8_gpu_secure_exit());
    acquire_ok = false; assert(!adreno8_gpu_scratch_test() && gpu_state == GPU_FAILED);
    reset(); free(files[0]); free(files[1]);
    puts("gen8.0.0 vendor tables, PM4, firmware, startup ordering and failure retention passed");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix="adreno8-gpu-test-") as temp:
    cfile = Path(temp) / "gpu.c"
    cfile.write_text(source)
    for page in (4096, 16384):
        binary = Path(temp) / f"gpu-{page}"
        subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                        f"-DPAGE_SIZE={page}", "-I", str(DEV), str(cfile), "-o", str(binary)], check=True)
        subprocess.run([str(binary), str(args.firmware_dir / "gen80000_sqe.fw"),
                        str(args.firmware_dir / "gen80000_aqe.fw")], check=True)
