#!/usr/bin/env python3
"""Compare gen8 tables with pinned sources and the annibale KGSL module; exercise GPU startup."""
import argparse
import hashlib
from pathlib import Path
import re
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
PIN = "d50b305f7da9e14715a25120a4ac7b1a4b8b97c3"
KGSL_SHA256 = "14aea73aa60814273a51f8e145e3b491471600466ddf6f42b7c45e1716b2bfd7"
HASHES = {
    "adreno_gen8.c": "3379f615fd4f350cf22ef60920891df3b7f479044947f3a714e26d22c8e2b56e",
    "adreno-gpulist.h": "7faff3fd1afc101dd1db984a9ca632ce884e8406fa856d4f60632e281077f3f6",
    "gen8_reg.h": "e85cb422ed221896433afcd6d931fd129f4c8e2edb730d15dfc1fc71c9aa51e2",
    "adreno_pm4types.h": "f66d80cd679aa0e0dd032f230bf960a964dfda282944bcb32c6643d1ebe5d731",
    "sun-gpu.dtsi": "b91bf41b0f6e8f75ea39601a3949debfbbc3b83cb7b52c11142bb6625840bade",
}

def kgsl_tpl1(path):
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == KGSL_SHA256, "unexpected KGSL module"
    assert data[:7] == b"\x7fELF\x02\x01\x01" and struct.unpack_from("<HH", data, 16) == (1, 183)
    shoff = struct.unpack_from("<Q", data, 40)[0]
    shentsize, shnum = struct.unpack_from("<HH", data, 58)
    assert shentsize == 64
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
    symtab_index, symtab = next((i, s) for i, s in enumerate(sections) if s[1] == 2)
    assert symtab[9] == 24
    strings = sections[symtab[6]]
    names = data[strings[4]:strings[4] + strings[5]]
    entries = [struct.unpack_from("<IBBHQQ", data, at)
               for at in range(symtab[4], symtab[4] + symtab[5], 24)]
    symbols = {names[s[0]:names.index(b"\0", s[0])].decode(): s for s in entries}
    _, _, _, table_section, table_value, table_size = symbols["gen8_0_0_nonctxt_regs"]
    assert table_size == 41 * 12
    # Both catalog entries select this table through their +0xa8 ABS64 relocation.
    for core in ("adreno_gpu_core_gen8_0_0", "adreno_gpu_core_gen8_0_1"):
        _, _, _, core_section, core_value, core_size = symbols[core]
        assert core_size == 256
        targets = []
        for section in sections:
            if section[1] != 4 or section[7] != core_section:
                continue
            assert section[6] == symtab_index and section[9] == 24
            for at in range(section[4], section[4] + section[5], 24):
                offset, info, addend = struct.unpack_from("<QQq", data, at)
                if offset == core_value + 0xa8:
                    symbol = entries[info >> 32]
                    targets.append((info & 0xffffffff, symbol[3], symbol[4] + addend))
        assert targets == [(257, table_section, table_value)], core
    offset = sections[table_section][4] + table_value + 25 * 12
    row = struct.unpack_from("<III", data, offset)
    assert offset == 0x4f29c and row == (0xb600, 0x10000000, 1), "KGSL TPL1 catalog entry"
    print(f"KGSL catalog TPL1: register {row[0]:#x}, value {row[1]:#x}, pipes {row[2]:#x}")
    return row

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--vendor-dir", required=True, type=Path, help=f"graphics-kernel sources and sun-gpu.dtsi at {PIN}")
parser.add_argument("--kgsl-module", required=True, type=Path, help=f"annibale msm_kgsl.ko with SHA-256 {KGSL_SHA256}")
parser.add_argument("--firmware-dir", type=Path, default=ROOT.parent / "hackios-xnu-userland/firmware-gen8")
args = parser.parse_args()
tpl1_reg, tpl1_value, _ = kgsl_tpl1(args.kgsl_module)
vendor = {}
for name, expected in HASHES.items():
    data = (args.vendor_dir / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected, f"unexpected vendor input: {name}"
    vendor[name] = data.decode()

# https://github.com/OnePlusOSS/android_kernel_modules_and_devicetree_oneplus_sm8750/blob/d50b305f7da9e14715a25120a4ac7b1a4b8b97c3/vendor/qcom/opensource/graphics-devicetree/gpu/sun-gpu.dtsi#L32-L40
gpu_node = vendor["sun-gpu.dtsi"].split("&msm_gpu {", 1)[1]
cells = [int(n, 16) for n in re.findall(r"0x[0-9a-f]+", re.search(r"\breg\s*=\s*(.*?);", gpu_node, re.S)[1])]
names = re.findall(r'"([^"]+)"', re.search(r"\breg-names\s*=\s*(.*?);", gpu_node, re.S)[1])
assert len(cells) == 2 * len(names)
resources = dict(zip(names, zip(cells[::2], cells[1::2])))
gpu_phys, gpu_size = resources["kgsl_3d0_reg_memory"]
for name, (phys, size) in resources.items():
    if name != "kgsl_3d0_reg_memory":
        assert gpu_phys + gpu_size <= phys or phys + size <= gpu_phys, name

# Use the production mapping declarations; an independent test-only size hid the 64 KiB mapping.
gmu_source = (DEV / "adreno8_gmu.c").read_text()
block_declarations = ""
for name, base in (("gpu", 0x10000000), ("gmu", 0x20000000), ("smmu", 0x30000000)):
    block_source = (DEV / "adreno8_smmu.c").read_text() if name == "smmu" else gmu_source
    initializer = re.search(r"struct adreno8_block adreno8_" + name + r"\s*=\s*\{([^}]+)\};", block_source)[1]
    phys, size = [int(n, 16) for n in re.findall(r"0x[0-9a-f]+", initializer)]
    if name == "gpu":
        assert (phys, size) == (gpu_phys, gpu_size), "GPU mapping differs from kgsl_3d0_reg_memory"
    block_declarations += f"struct adreno8_block adreno8_{name} = {{{initializer}, {base}}};\n"

registers = dict(re.findall(r"^#define\s+(GEN8_\w+)\s+(0x[0-9a-fA-F]+|[0-9]+)\s*(?:/\*.*)?$", vendor["gen8_reg.h"], re.M))
reference = "#define BIT(n) (1u << (n))\n"
reference += "enum { PIPE_NONE, PIPE_BR, PIPE_BV, PIPE_LPAC, PIPE_AQE0, PIPE_AQE1, PIPE_DDE_BR, PIPE_DDE_BV };\n"
reference += f"enum {{ KGSL_TPL1_REG = {tpl1_reg}, KGSL_TPL1_VALUE = {tpl1_value} }};\n"
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
    if old == "gen8_0_0_nonctxt_regs":
        original = "{ GEN8_TPL1_DBG_ECO_CNTL, 0x10100000, BIT(PIPE_NONE) }"
        assert body.count(original) == 1
        # This one catalog value follows the verified annibale module.
        body = body.replace(original, f"{{ GEN8_TPL1_DBG_ECO_CNTL, {tpl1_value:#x}, BIT(PIPE_NONE) }}")
    # Resolve against the independently supplied vendor header, never the production constants.
    body = re.sub(r"\bGEN8_[A-Z0-9_]+", lambda m: registers[m[0]], body)
    reference += f"static const {ctype} ref_{new}[] = {{{body}\n}};\n"
    table_checks += f"assert(sizeof({new}) == sizeof(ref_{new})); assert(!memcmp({new},ref_{new},sizeof({new})));\n"

pm4 = vendor["adreno_pm4types.h"]
assert int(re.search(r"^#define\s+CP_NOP\s+(0x[0-9a-fA-F]+)", pm4, re.M)[1], 16) == 0x10
reference += re.search(r"^#define CP_TYPE7_PKT[^\n]+", pm4, re.M)[0] + "\n"
for name in ["pm4_calc_odd_parity_bit", "cp_type7_packet"]:
    reference += re.search(r"static inline uint " + name + r"\([^}]+\}", pm4)[0] + "\n"

shared = (DEV / "adreno8_private.h").read_text()
constants = "\n".join(line for line in shared.splitlines() if line.startswith(("#define GMU", "#define SMMU_SGFSR")))
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
struct adreno8_gpu_space { unsigned unused; };
struct adreno8_gpu_space adreno8_gpu_space;
''' + block_declarations + f"enum {{ GPU_MAPPING_SIZE = {gpu_size} }};\n" + r'''
bool adreno8_reset_required;
uint32_t adreno8_pause_ms, adreno8_step, adreno8_newest_step;
uint32_t adreno8_skip_steps;
bool adreno8_gpu8_keep, adreno8_gpu8_return;
bool adreno8_direct_ring;
bool adreno8_ordinary_ready(void);
bool adreno8_translation_walk(struct adreno8_gpu_space *, uint32_t, vm_offset_t, uint32_t, uint32_t);
static unsigned ring_completed, ring_setup_completed;
static uint32_t raw_nop_asked, empty_powerup_asked;
bool PE_parse_boot_argn(const char *name, void *value, int size)
{
    assert(size == sizeof(uint32_t));
    uint32_t asked;
    if (!strcmp(name, "adreno8_raw_nop")) asked = raw_nop_asked;
    else { assert(!strcmp(name, "adreno8_empty_powerup")); asked = empty_powerup_asked; }
    if (asked) memcpy(value, &asked, size);
    return asked != 0;
}
int adreno8_say(const char *fmt, ...) {
    if (strstr(fmt, "direct ring CP_INIT consumed and idle:")) ring_completed++;
    if (strstr(fmt, "ordinary setup-only remained healthy for 10 seconds")) ring_setup_completed++;
    return 0;
}
uint32_t adreno8_rd(vm_offset_t);
void adreno8_wr(vm_offset_t, uint32_t);
uint32_t adreno8_get(struct adreno8_block *, const char *, uint32_t);
void adreno8_put(struct adreno8_block *, const char *, uint32_t, uint32_t);
void adreno8_settle(void);
void loader_log_line(const char *line) { (void)line; }
void adreno8_hold(bool single_access) { (void)single_access; }
static unsigned setup_waits;
static void ring_setup_wait(void);
void adreno8_sleep_us(uint32_t us) { if (us == 100000) { setup_waits++; if (adreno8_direct_ring) ring_setup_wait(); } }
const uint8_t *redwood_image_file(const char *, const char *, uint64_t *, int *);
#define _DEV_ADRENO8_PRIVATE_H
''' + constants + '\n#include "adreno8_gpu.c"\n#undef printf\n' + reference + r'''
static uint32_t gpu_regs[8][0x10000], gmu_regs[0x1a000], current_pipe;
static unsigned gx_reads, gx_writes, raw_calls, allocation_count, publish_count;
static unsigned walk_calls, walk_fail_at;
static unsigned diagnostic_reads, diagnostic_writes;
static uint32_t diagnostic_offset;
static uint32_t max_gx_offset;
static enum { MMIO_NONE, MMIO_BARRIER, MMIO_READ, MMIO_WRITE } last_mmio;
static unsigned barriers, gmu_reads, gmu_writes;
static unsigned normal_powerup_publications, empty_powerup_publications;
static bool empty_publish_ok = true;
static bool smmu_ok = true, hfi_ok = true, oob_ok = true, clock_ok = true;
static bool raw_ok = true, idle_ok = true, memory_on, smmu_after_raw, corrupt_scratch, acquire_ok = true;
static struct adreno8_memory allocations[5];
static uint8_t *device_memory[5], *files[2];
static uint64_t file_sizes[2];
static uint32_t allocation_prot[5];
static unsigned ring_input_mask, ring_payloads, ring_wptr_writes, ring_fence_drops;
static unsigned ring_setup_read_mask, ring_publish_fail, ring_read_fault, ring_drop_write;
static bool ordinary_ok, poll_ok, gmu_smmu_ok, ring_rptr_moves, ring_shadow_moves, ring_alloc_ok;
static uint32_t ring_fault, ring_bv, ring_bv_shadow, ring_rbbm, ring_fence_extra;
static const uint32_t ring_setup_regs[] = {0x800,0x801,0x802,0x804,0x805,0x808,0x809};
static unsigned ring_sqe_writes, ring_setup_fault_at, ring_setup_fault;
static unsigned ring_halt_reads;
static unsigned ring_halt_clears, ring_halt_drop, ring_halt_observe_fault;
static const uint32_t halt_regs[]={0x3c45,0x3c46,0x00a,0x00b};

bool adreno8_translation_walk(struct adreno8_gpu_space *space, uint32_t iova,
    vm_offset_t memory, uint32_t size, uint32_t prot)
{
    assert(adreno8_direct_ring && space==&adreno8_gpu_space && prot==5);
    assert(!oob_requested && !gx_reads && !gx_writes && !gmu_reads && !gmu_writes);
    unsigned i=walk_calls++;
    assert(i<2 && iova==allocations[i].gpu_address && memory==allocations[i].memory);
    assert(size==allocations[i].size);
    return walk_calls!=walk_fail_at;
}

static void ring_setup_wait(void)
{
    assert(ring_input_mask==7 && ring_setup_read_mask==127);
    assert(ring_halt_reads==4);
    assert(ring_sqe_writes==(raw_nop_asked==2) && !ring_wptr_writes && !ring_payloads && !raw_calls);
    if (setup_waits==ring_setup_fault_at) {
        if (ring_setup_fault==1) poll_ok=false;
        if (ring_setup_fault==2) smmu_ok=false;
        if (ring_setup_fault==3) gmu_smmu_ok=false;
        if (ring_setup_fault==4) gpu_regs[0][GEN8_RBBM_INT_0_STATUS]=1u<<1;
    }
}

void adreno8_settle(void) { last_mmio = MMIO_BARRIER; barriers++; }

uint32_t adreno8_rd(vm_offset_t address)
{
    if (address >= adreno8_smmu.base && address < adreno8_smmu.base + adreno8_smmu.size) return 0;
    assert(last_mmio != MMIO_READ);
    if (adreno8_direct_ring && address == adreno8_gmu.base + GMU_AHB_FENCE_STATUS)
        assert(last_mmio == MMIO_BARRIER);
    if (adreno8_direct_ring && ring_setup_read_mask!=127 &&
        (address==adreno8_gpu.base+4*0x3c45 || address==adreno8_gpu.base+4*0x00a))
        assert(last_mmio==MMIO_BARRIER);
    last_mmio = MMIO_READ;
    if (address >= adreno8_gmu.base && address < adreno8_gmu.base + adreno8_gmu.size) {
        gmu_reads++;
        return gmu_regs[(address - adreno8_gmu.base) / 4];
    }
    assert(address >= adreno8_gpu.base && address + 4 <= adreno8_gpu.base + adreno8_gpu.size && !(address & 3));
    gx_reads++;
    if (address - adreno8_gpu.base > max_gx_offset) max_gx_offset = address - adreno8_gpu.base;
    uint32_t reg = (address - adreno8_gpu.base) / 4;
    if (adreno8_direct_ring) {
        for(unsigned i=0;i<4;i++) if(reg==halt_regs[i]) {
            if(ring_setup_read_mask==127) ring_halt_reads++;
        }
        if(reg==ring_halt_observe_fault && ring_setup_read_mask==127) return UINT32_MAX;
        if (reg == ring_read_fault) return UINT32_MAX;
        for (unsigned i=0;i<7;i++) if (reg == ring_setup_regs[i]) ring_setup_read_mask |= 1u<<i;
    }
    if (reg == GEN8_CP_APERTURE_CNTL_HOST) return current_pipe << 12;
    if (reg == GEN8_RBBM_CGC_P2S_STATUS) return clock_ok;
    return gpu_regs[current_pipe][reg];
}

void adreno8_wr(vm_offset_t address, uint32_t value)
{
    assert(last_mmio == MMIO_BARRIER);
    last_mmio = MMIO_WRITE;
    if (address >= adreno8_gmu.base && address < adreno8_gmu.base + adreno8_gmu.size) {
        gmu_writes++;
        uint32_t reg = (address - adreno8_gmu.base) / 4;
        gmu_regs[reg] = value;
        if (reg == GMU_HOST2GMU_SET / 4 && value == 1u << 30 && oob_ok) {
            assert(!adreno8_direct_ring || walk_calls==2);
            gmu_regs[GMU_GMU2HOST_INFO / 4] |= 1u << 31;
            gmu_regs[GMU_PWR_CLK_STATUS / 4] = 0;
        }
        if (reg == GMU_GMU2HOST_CLR / 4) gmu_regs[GMU_GMU2HOST_INFO / 4] &= ~value;
        return;
    }
    assert(address >= adreno8_gpu.base && address + 4 <= adreno8_gpu.base + adreno8_gpu.size && !(address & 3));
    gx_writes++;
    if (address - adreno8_gpu.base > max_gx_offset) max_gx_offset = address - adreno8_gpu.base;
    uint32_t reg = (address - adreno8_gpu.base) / 4;
    if (adreno8_direct_ring) {
        assert(!raw_calls);
        for(unsigned i=0;i<4;i++) if(reg==halt_regs[i]) {
            assert(!(i&1) && value==0 && ring_halt_clears==(i?1u:0u));
            ring_halt_clears |= i?2:1;
            if(reg==ring_halt_drop) return;
        }
        if(reg!=0x81c && reg!=0x3c45 && reg!=0x00a) assert(ring_halt_clears==3);
        if (reg == 0x81c) assert(value != 3u<<12);
        if (reg == 0x84d) assert(value == (current_pipe == 1 ? 0x6f : 0xf));
        if (reg >= 0xf800 && reg <= 0xf803) assert(value == 0);
        assert(reg != 0x9a && reg != 0x9b);
        if (reg == 0x9c) assert(false);
        for (unsigned i=0;i<7;i++) if (reg == ring_setup_regs[i]) {
            assert(ring_input_mask == 7 && !ring_wptr_writes);
            if (reg == ring_drop_write) return;
        }
        if (reg == 0x815) {
            ring_sqe_writes++;
            assert(ring_input_mask == 7 && ring_setup_read_mask == 127 && value == (raw_nop_asked==2 ? 0u : 1u));
            if (reg == ring_drop_write) return;
        }
        if (reg == 0x807) {
            assert(value == 10 && ring_payloads == 1 && ring_input_mask == 7);
            assert(gpu_regs[0][0x815] == 1 && ring_setup_read_mask == 127);
            ring_wptr_writes++;
            if (ring_fence_drops) {
                ring_fence_drops--; gmu_regs[GMU_AHB_FENCE_STATUS/4]=1; return;
            }
            gmu_regs[GMU_AHB_FENCE_STATUS/4]=ring_fence_extra;
            if (ring_rptr_moves) gpu_regs[0][0x806]=10;
            if (ring_shadow_moves) ((uint32_t *)device_memory[3])[0]=10;
            gpu_regs[0][0x80a]=ring_bv;
            ((uint32_t *)device_memory[3])[2]=ring_bv_shadow;
            gpu_regs[0][0x12]=ring_rbbm;
            if (ring_fault==1) poll_ok=false;
            if (ring_fault==2) smmu_ok=false;
            if (ring_fault==3) gmu_smmu_ok=false;
            if (ring_fault==4) gpu_regs[0][GEN8_RBBM_INT_0_STATUS]=1u<<1;
            if (ring_fault==5) gmu_regs[GMU(GEN8_GMUAO_GPU_CX_BUSY_STATUS)/4]=1u<<23;
            if (ring_fault==6) gmu_regs[GMU_AHB_FENCE_STATUS/4]=UINT32_MAX;
        }
    }
    if (reg == GEN8_CP_APERTURE_CNTL_HOST) { current_pipe = value >> 12; assert(current_pipe < 8); }
    else gpu_regs[current_pipe][reg] = value;
}

uint32_t adreno8_get(struct adreno8_block *block, const char *name, uint32_t offset)
{
    assert(block == &adreno8_gpu && !strcmp(name, "command register"));
    assert(adreno8_pause_ms && adreno8_step == adreno8_newest_step && offset <= block->size - 4);
    diagnostic_reads++; diagnostic_offset = offset;
    return adreno8_rd(block->base + offset);
}

void adreno8_put(struct adreno8_block *block, const char *name, uint32_t offset, uint32_t value)
{
    assert(block == &adreno8_gpu && !strcmp(name, "command register"));
    assert(adreno8_pause_ms && adreno8_step == adreno8_newest_step && offset <= block->size - 4);
    assert(!adreno8_direct_ring || offset != 4 * 0x807);
    diagnostic_writes++; diagnostic_offset = offset;
    adreno8_wr(block->base + offset, value);
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
    assert(!memory_on && allocation_count < 5 && align == 4096);
    if (!strcmp(name,"direct ring") && !ring_alloc_ok) return NULL;
    unsigned i = allocation_count++;
    struct adreno8_memory *m = &allocations[i];
    uint32_t allocated = (size + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1);
    m->name = name; m->size = size;
    m->memory = (vm_offset_t)aligned_alloc(PAGE_SIZE, allocated);
    assert(m->memory); memset((void *)m->memory, 0, allocated);
    m->gpu_address = 0xc0000000 + i * 0x200000;
    device_memory[i] = calloc(1, allocated); assert(device_memory[i]);
    allocation_prot[i] = prot;
    return m;
}

bool adreno8_memory_publish(const struct adreno8_memory *m, uint32_t offset, uint32_t size)
{
    assert(offset <= m->size && size <= m->size - offset);
    if (adreno8_direct_ring) {
        unsigned input = m == &allocations[4] ? 1 : m == scratch ? 2 : m == powerup ? 4 : 0;
        if (input && oob_requested) {
            unsigned phase = m == &allocations[4] && size == 40 ? 4 : input == 1 ? 1 : input == 2 ? 2 : 3;
            assert(offset == 0 && size == (phase == 4 ? 40 : m->size));
            if (phase == 4) {
                const uint32_t expected[]={0x70970001,0x08000000,0x70c80007,0x14b,3,0x20000000,2,0xc0400000,0,0x80000000};
                assert(gpu_regs[0][0x815] == 1 && !memcmp((void *)m->memory,expected,40));
                for(unsigned i=0;i<4096;i++) assert(!device_memory[2][i]);
            } else for(unsigned i=0;i<size;i++) assert(!((uint8_t *)m->memory)[i]);
            if (ring_publish_fail == phase) return false;
            if (phase == 4) ring_payloads++; else ring_input_mask |= input;
        }
    } else if (m == powerup) {
        uint32_t *p = (uint32_t *)m->memory;
        if (p[3]) normal_powerup_publications++;
        else {
            assert(empty_powerup_asked == 1 && normal_powerup_publications == 1);
            assert(offset == 0 && size == 4096 && m->size == 4096);
            assert(oob_requested && oob_acked && adreno8_reset_required && !raw_calls);
            assert(gpu_regs[0][GEN8_CP_SQE_INSTR_BASE_LO] == sqe->gpu_address);
            assert(gpu_regs[0][GEN8_CP_AQE_INSTR_BASE_LO_0] == aqe->gpu_address);
            for (unsigned i = 0; i < size; i++) assert(!((uint8_t *)m->memory)[i]);
            empty_powerup_publications++;
            if (!empty_publish_ok) return false;
        }
    }
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
bool adreno8_gpu_faulted(void) { return !smmu_ok || !gmu_smmu_ok; }
bool adreno8_hwsched_ready(void) { return !adreno8_direct_ring && hfi_ok; }
bool adreno8_ordinary_ready(void) { return adreno8_direct_ring && ordinary_ok; }
bool adreno8_hfi_poll(void) {
    if (!poll_ok || adreno8_gpu_faulted()) { ordinary_ok=false; return false; }
    return true;
}
bool adreno8_hwsched_raw(const uint32_t *pm4_words, uint32_t words, const char *name)
{
    (void)name;
    assert(!adreno8_direct_ring);
    assert(last_mmio != MMIO_READ);
    assert(oob_requested && oob_acked && memory_on && adreno8_reset_required);
    raw_calls++;
    if (!raw_ok) return false;
    if (raw_nop_asked) {
        assert(raw_calls == 1 && words == (raw_nop_asked == 1 ? 2 : 10));
        for (unsigned i = 0; i < words; i += 2)
            assert(pm4_words[i] == cp_type7_packet(0x10, 1) && pm4_words[i + 1] == 0);
    } else if (raw_calls == 1) {
        assert(words == 10 && pm4_words[0] == cp_type7_packet(0x17, 1));
        assert(pm4_words[2] == cp_type7_packet(0x48, 7));
        assert(pm4_words[7] == powerup->gpu_address && pm4_words[8] == 0);
        uint32_t *p = (uint32_t *)device_memory[2];
        assert(p[0] == 0 && p[1] == 0 && p[2] == 0);
        assert(gpu_regs[0][KGSL_TPL1_REG] == KGSL_TPL1_VALUE);
        assert(ref_a8_powerup_ifpc[11] == KGSL_TPL1_REG);
        if (empty_powerup_asked) {
            assert(normal_powerup_publications == 1 && empty_powerup_publications == 1);
            for (unsigned i = 0; i < 4096; i++) assert(!device_memory[2][i]);
        } else {
            assert(p[26] == KGSL_TPL1_REG && p[27] == KGSL_TPL1_VALUE);
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
    assert(last_mmio != MMIO_READ);
    for (unsigned i = 0; i < allocation_count; i++) { free((void *)allocations[i].memory); free(device_memory[i]); }
    memset(allocations, 0, sizeof(allocations)); memset(gpu_regs, 0, sizeof(gpu_regs)); memset(gmu_regs, 0, sizeof(gmu_regs));
    gpu_state = GPU_COLD; sqe = aqe = powerup = scratch = NULL; oob_requested = oob_acked = false;
    gx_reads = gx_writes = raw_calls = allocation_count = publish_count = current_pipe = 0;
    max_gx_offset = 0; adreno8_gpu.size = GPU_MAPPING_SIZE;
    adreno8_pause_ms = adreno8_step = adreno8_newest_step = 0;
    raw_nop_asked = empty_powerup_asked = adreno8_skip_steps = 0;
    adreno8_gpu8_keep = true; adreno8_gpu8_return = false;
    normal_powerup_publications = empty_powerup_publications = 0; empty_publish_ok = true;
    adreno8_direct_ring=false; ring_completed=ring_input_mask=ring_payloads=ring_wptr_writes=ring_fence_drops=0;
    ring_setup_read_mask=ring_publish_fail=ring_read_fault=ring_drop_write=ring_fault=0;
    ring_bv=ring_bv_shadow=ring_rbbm=ring_fence_extra=0;
    ordinary_ok=poll_ok=gmu_smmu_ok=ring_rptr_moves=ring_shadow_moves=ring_alloc_ok=true;
    ring_sqe_writes=ring_setup_fault_at=ring_setup_fault=ring_setup_completed=0;
    ring_halt_reads=ring_halt_clears=ring_halt_drop=ring_halt_observe_fault=0;
    setup_waits = 0;
    walk_calls=walk_fail_at=0;
    diagnostic_reads = diagnostic_writes = diagnostic_offset = 0;
    last_mmio = MMIO_NONE; barriers = gmu_reads = gmu_writes = 0;
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

static void prepare_empty(void)
{
    reset(); prepare(); empty_powerup_asked = 1;
    adreno8_step = adreno8_newest_step = 17;
    memset((void *)scratch->memory, 0x5a, scratch->size);
    memset(device_memory[3], 0x5a, scratch->size);
    /* Bytes outside the 4 KiB device buffer belong to its XNU allocation padding. */
    for (unsigned i = powerup->size; i < PAGE_SIZE; i++) {
        ((uint8_t *)powerup->memory)[i] = 0xa5; device_memory[2][i] = 0xa5;
    }
}

static void empty_retained(void)
{
    assert(gpu_state == GPU_FAILED && allocation_count == 4 && memory_on);
    assert(oob_requested && oob_acked && adreno8_reset_required);
    assert(gmu_regs[GMU_HOST2GMU_SET / 4] == 1u << 30);
    assert(!adreno8_gpu_cp_ready() && !adreno8_gpu_secure_exit() && !adreno8_gpu_scratch_test());
    assert(raw_calls <= 1 && powerup->gpu_address == 0xc0400000);
    for (unsigned i = 0; i < 2; i++) {
        assert(!memcmp(device_memory[i], files[i] + 4, file_sizes[i] - 4));
        assert(!memcmp((void *)allocations[i].memory, files[i] + 4, file_sizes[i] - 4));
    }
    for (unsigned i = 0; i < scratch->size; i++)
        assert(device_memory[3][i] == 0x5a && ((uint8_t *)scratch->memory)[i] == 0x5a);
    for (unsigned i = powerup->size; i < PAGE_SIZE; i++)
        assert(device_memory[2][i] == 0xa5 && ((uint8_t *)powerup->memory)[i] == 0xa5);
    unsigned reads = gx_reads + gmu_reads, writes = gx_writes + gmu_writes;
    assert(!adreno8_gpu_cp_init());
    assert(reads == gx_reads + gmu_reads && writes == gx_writes + gmu_writes);
}

static void prepare_ring(void)
{
    reset(); adreno8_direct_ring=true;
    assert(adreno8_gpu_prepare() && allocation_count==5);
    assert(allocations[4].size==32768 && allocation_prot[4]==9 && allocation_prot[3]==11);
    assert(!adreno8_hwsched_ready() && adreno8_ordinary_ready());
    adreno8_step=adreno8_newest_step=17; adreno8_pause_ms=100;
    memory_on=true;
    for(unsigned i=0;i<7;i++) gpu_regs[0][ring_setup_regs[i]]=0xa5;
    gpu_regs[0][0x815]=0xa5; gpu_regs[0][0x3c00]=0xabcdef01;
    gpu_regs[0][0x3c45]=8; gpu_regs[0][0x3c46]=3; gpu_regs[0][0x00a]=gpu_regs[0][0x00b]=1;
    for(unsigned b=2;b<5;b++) {
        memset((void *)allocations[b].memory,0x5a,allocations[b].size);
        memset(device_memory[b],0x5a,allocations[b].size);
    }
}

static void ring_retained(unsigned completed)
{
    assert(gpu_state==GPU_FAILED && ring_completed==completed && !raw_calls);
    assert(allocation_count==5 && oob_requested && oob_acked && adreno8_reset_required && memory_on);
    assert(!adreno8_hwsched_ready() && !adreno8_gpu_cp_ready());
    unsigned reads=gx_reads+gmu_reads, writes=gx_writes+gmu_writes;
    assert(!adreno8_gpu_cp_init() && !adreno8_gpu_secure_exit() && !adreno8_gpu_scratch_test());
    assert(reads==gx_reads+gmu_reads && writes==gx_writes+gmu_writes && !raw_calls);
    for(unsigned i=0;i<2;i++) {
        assert(!memcmp(device_memory[i],files[i]+4,file_sizes[i]-4));
        assert(!memcmp((void *)allocations[i].memory,files[i]+4,file_sizes[i]-4));
    }
}

static void check_direct_ring(void)
{
    prepare_ring(); assert(!adreno8_gpu_cp_init()); ring_retained(1);
    assert(!ring_halt_reads);
    assert(ring_wptr_writes==1 && ring_payloads==1 && ring_input_mask==7);
    assert(gpu_regs[0][0x800]==0xc0800000 && gpu_regs[0][0x801]==0 && gpu_regs[0][0x802]==0x20c);
    assert(gpu_regs[0][0x804]==0xc0600000 && gpu_regs[0][0x805]==0);
    assert(gpu_regs[0][0x808]==0xc0600008 && gpu_regs[0][0x809]==0);
    assert(gpu_regs[0][0x806]==10 && gpu_regs[0][0x807]==10 && gpu_regs[0][0x80a]==0);
    assert(((uint32_t *)device_memory[3])[0]==10 && ((uint32_t *)device_memory[3])[2]==0);
    assert(gpu_regs[0][0x9a]==0 && gpu_regs[0][0x9b]==0 && gpu_regs[0][0x3c00]==0xabcdef01);
    assert(!gmu_regs[GMU(0x23b09)/4] && !gmu_regs[GMU(0x23b0a)/4] && !gmu_regs[GMU(0x23b0b)/4]);
    for(unsigned i=0;i<4096;i++) assert(!device_memory[2][i]);
    prepare_ring(); ring_fence_drops=3; ring_fence_extra=2; ring_rbbm=1;
    assert(!adreno8_gpu_cp_init()); ring_retained(1); assert(ring_wptr_writes==4);
    prepare_ring(); ring_fence_drops=201;
    assert(!adreno8_gpu_cp_init()); ring_retained(0); assert(ring_wptr_writes==200);
    for(unsigned phase=1;phase<=4;phase++) {
        prepare_ring(); ring_publish_fail=phase;
        assert(!adreno8_gpu_cp_init() && !ring_wptr_writes); ring_retained(0);
    }
    for(unsigned i=0;i<8;i++) {
        uint32_t reg=i<7?ring_setup_regs[i]:0x815;
        prepare_ring(); ring_read_fault=reg;
        assert(!adreno8_gpu_cp_init() && !ring_wptr_writes); ring_retained(0);
        prepare_ring(); ring_drop_write=reg;
        assert(!adreno8_gpu_cp_init() && !ring_wptr_writes); ring_retained(0);
    }
    for(unsigned fault=1;fault<=6;fault++) {
        prepare_ring(); ring_fault=fault;
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        if(fault<=3) assert(!adreno8_ordinary_ready());
    }
    prepare_ring(); ring_rptr_moves=false;
    assert(!adreno8_gpu_cp_init()); ring_retained(0);
    prepare_ring(); ring_shadow_moves=false;
    assert(!adreno8_gpu_cp_init()); ring_retained(0);
    prepare_ring(); acquire_ok=false;
    assert(!adreno8_gpu_cp_init()); ring_retained(0);
    prepare_ring(); ring_rbbm=2;
    assert(!adreno8_gpu_cp_init()); ring_retained(0);
    prepare_ring(); ring_bv=8192;
    assert(!adreno8_gpu_cp_init()); ring_retained(0);
    prepare_ring(); ring_bv_shadow=UINT32_MAX;
    assert(!adreno8_gpu_cp_init()); ring_retained(0);
    for(unsigned i=0;i<2;i++) {
        prepare_ring(); gpu_regs[0][i?0x807:0x806]=1;
        assert(!adreno8_gpu_cp_init() && !ring_wptr_writes); ring_retained(0);
    }
    for(unsigned bad=0;bad<8;bad++) {
        prepare_ring();
        switch(bad) {
        case 0: ordinary_ok=false; break;
        case 1: adreno8_step=16; break;
        case 2: adreno8_newest_step=18; break;
        case 3: adreno8_gpu8_keep=false; break;
        case 4: adreno8_gpu8_return=true; break;
        case 5: raw_nop_asked=1; break;
        case 6: empty_powerup_asked=1; break;
        case 7: adreno8_skip_steps=1; break;
        }
        assert(!adreno8_gpu_cp_init() && !oob_requested && !gx_writes && !gmu_writes);
        if(bad) assert(!walk_calls);
    }
    reset(); adreno8_direct_ring=true; ring_alloc_ok=false;
    assert(!adreno8_gpu_prepare() && allocation_count==4 && gpu_state==GPU_FAILED && !gx_writes && !gmu_writes);
    puts("ordinary direct ring: publication, fixed setup/readback, fenced WPTR, BR completion/BV0, idle/fault and retention passed");
}

static void check_ring_setup_only(void)
{
    prepare_ring(); raw_nop_asked=3;
    assert(!adreno8_gpu_cp_init()); ring_retained(0);
    assert(setup_waits==100 && ring_setup_completed==1);
    assert(ring_input_mask==7 && ring_setup_read_mask==127);
    assert(ring_halt_reads==4 && gpu_regs[0][0x3c45]==0 && gpu_regs[0][0x3c46]==3);
    assert(gpu_regs[0][0x00a]==0 && gpu_regs[0][0x00b]==1);
    assert(!ring_sqe_writes && !ring_wptr_writes && !ring_payloads);
    for(unsigned fault=1;fault<=4;fault++) for(unsigned at=1;at<=100;at+=99) {
        prepare_ring(); raw_nop_asked=3; ring_setup_fault=fault; ring_setup_fault_at=at;
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(setup_waits==at && !ring_setup_completed && !ring_sqe_writes && !ring_wptr_writes && !ring_payloads);
    }
    prepare_ring(); raw_nop_asked=3; ring_read_fault=0x802;
    assert(!adreno8_gpu_cp_init()); ring_retained(0);
    assert(!setup_waits && !ring_setup_completed && !ring_sqe_writes && !ring_wptr_writes);
    for(unsigned i=0;i<4;i++) {
        prepare_ring(); raw_nop_asked=3; ring_halt_observe_fault=halt_regs[i];
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(ring_halt_reads==i+1 && !setup_waits && !ring_sqe_writes && !ring_wptr_writes);
    }
    puts("ordinary setup-only: published inputs and ring readbacks, 10 seconds including final health, no SQE/WPTR/RAW and retained failures passed");
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
    for (unsigned i = 0; i < A8_COUNT(a8_noncontext); i++) assert(a8_noncontext[i].reg < adreno8_gpu.size / 4);
    for (unsigned i = 0; i < A8_COUNT(a8_protected); i++) assert(a8_protected[i].reg < adreno8_gpu.size / 4);
    for (unsigned i = 0; i < A8_COUNT(a8_bicubic); i++) assert(a8_bicubic[i].reg < adreno8_gpu.size / 4);
    for (unsigned i = 0; i < A8_COUNT(a8_powerup); i++) assert(a8_powerup[i] < adreno8_gpu.size / 4);
    for (unsigned i = 0; i < A8_COUNT(a8_powerup_ifpc); i++) assert(a8_powerup_ifpc[i] < adreno8_gpu.size / 4);
    for (unsigned i = 0; i < A8_COUNT(a8_powerup_ext); i++) assert(a8_powerup_ext[i].reg < adreno8_gpu.size / 4);
    for (unsigned i = 0; i < A8_COUNT(a8_ao_hwcg); i++) assert(GMU(a8_ao_hwcg[i].reg) <= adreno8_gmu.size - 4);
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
    assert(gpu_regs[0][GEN8_UCHE_CCHE_GC_GMEM_RANGE_MIN_LO] == 0x04000000);
    assert(gpu_regs[0][GEN8_RBBM_SECVID_TSB_TRUSTED_BASE_LO] == 0xf0000000u);
    assert(gpu_regs[0][GEN8_RBBM_SECVID_TSB_TRUSTED_BASE_HI] == 0);
    assert(gpu_regs[0][GEN8_RBBM_SECVID_TSB_TRUSTED_SIZE] == 0xfffff000u);
    assert(max_gx_offset == 4 * GEN8_RBBM_SECVID_TSB_CNTL && max_gx_offset == 0x3e00c);
    assert(!diagnostic_reads && !diagnostic_writes);
    assert(gmu_reads && gmu_writes && last_mmio == MMIO_BARRIER);

    reset(); gpu_write(0, 1);
    assert(last_mmio == MMIO_WRITE && barriers == 1);
    assert(gpu_read(0) == 1 && last_mmio == MMIO_BARRIER && barriers == 2);
    reset(); gpu_gmu_write(GMU(GEN8_GMUCX_POWER_COUNTER_ENABLE), 1);
    assert(last_mmio == MMIO_WRITE && barriers == 1);
    assert(gpu_gmu_read(GMU(GEN8_GMUCX_POWER_COUNTER_ENABLE)) == 1);
    assert(last_mmio == MMIO_BARRIER && barriers == 2 && gmu_reads == 1 && gmu_writes == 1);

    reset(); adreno8_pause_ms = 100; adreno8_step = adreno8_newest_step = 17;
    gpu_write(GEN8_SP_HLSQ_GC_GMEM_RANGE_MIN_LO, 0x04000000);
    assert(last_mmio == MMIO_WRITE && barriers == 1);
    assert(gpu_read(GEN8_SP_HLSQ_GC_GMEM_RANGE_MIN_LO) == 0x04000000);
    assert(last_mmio == MMIO_BARRIER && barriers == 2);
    assert(diagnostic_reads == 1 && diagnostic_writes == 1 && diagnostic_offset == 0x2b840);
    reset(); prepare(); adreno8_pause_ms = 100; adreno8_step = adreno8_newest_step = 17;
    assert(adreno8_gpu_cp_init() && adreno8_gpu_secure_exit() && adreno8_gpu_scratch_test());
    assert(diagnostic_reads == gx_reads && diagnostic_writes == gx_writes);
    assert(gmu_reads && gmu_writes && last_mmio == MMIO_BARRIER);
    reset(); adreno8_pause_ms = 100; adreno8_step = 16; adreno8_newest_step = 17;
    gpu_write(0, 1); assert(gpu_read(0) == 1 && !diagnostic_reads && !diagnostic_writes);
    reset(); adreno8_step = adreno8_newest_step = 17;
    gpu_write(0, 1); assert(gpu_read(0) == 1 && !diagnostic_reads && !diagnostic_writes);
    reset(); adreno8_pause_ms = 100; adreno8_step = adreno8_newest_step = 17;
    gpu_write(GPU_MAPPING_SIZE / 4, 1);
    assert(gpu_state == GPU_FAILED && !diagnostic_writes && !gx_writes);
    reset(); adreno8_pause_ms = 100; adreno8_step = adreno8_newest_step = 17;
    assert(!gpu_read(GPU_MAPPING_SIZE / 4));
    assert(gpu_state == GPU_FAILED && !diagnostic_reads && !gx_reads);

    reset(); gpu_write(adreno8_gpu.size / 4 - 1, 0x12345678);
    assert(gpu_read(adreno8_gpu.size / 4 - 1) == 0x12345678 && gpu_state != GPU_FAILED);
    const uint32_t bad_registers[] = { GPU_MAPPING_SIZE / 4, 0x50000 / 4, 0x40000000, UINT32_MAX };
    for (unsigned i = 0; i < A8_COUNT(bad_registers); i++) {
        reset(); gpu_write(bad_registers[i], 1);
        assert(gpu_state == GPU_FAILED && !gx_reads && !gx_writes);
        gpu_write(0, 1); assert(!gpu_read(0) && !gx_reads && !gx_writes);
        reset(); assert(!gpu_read(bad_registers[i]));
        assert(gpu_state == GPU_FAILED && !gx_reads && !gx_writes);
    }
    reset(); prepare(); adreno8_gpu.size = 0x10000;
    assert(!adreno8_gpu_cp_init() && gpu_state == GPU_FAILED && !raw_calls);
    assert(max_gx_offset < 0x10000);
    reset(); prepare(); assert(gpu_oob_get()); adreno8_gpu.size = 4;
    assert(!gpu_raw(words, 10, "invalid status register") && gpu_state == GPU_FAILED && !raw_calls);

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
    for (unsigned mode = 1; mode <= 2; mode++) {
        reset(); prepare(); raw_nop_asked = mode;
        assert(!adreno8_gpu_cp_init() && raw_calls == 1 && gpu_state == GPU_FAILED);
        assert(!adreno8_gpu_cp_ready() && !adreno8_gpu_secure_exit() && !adreno8_gpu_scratch_test());
        assert(raw_calls == 1 && allocation_count == 4 && oob_requested && oob_acked);
        reset(); prepare(); raw_nop_asked = mode; raw_ok = false;
        assert(!adreno8_gpu_cp_init() && raw_calls == 1 && gpu_state == GPU_FAILED);
    }
    reset(); prepare(); raw_nop_asked = 3;
    assert(!adreno8_gpu_cp_init() && !raw_calls && gpu_state == GPU_FAILED);
    assert(setup_waits == 100 && allocation_count == 4 && oob_requested && oob_acked);
    assert(!adreno8_gpu_cp_ready() && !adreno8_gpu_secure_exit() && !adreno8_gpu_scratch_test());
    reset(); prepare(); raw_nop_asked = 4;
    assert(!adreno8_gpu_cp_init() && !raw_calls && !gx_reads && !gx_writes);
    assert(!oob_requested && !oob_acked);
    prepare_empty(); assert(!adreno8_gpu_cp_init() && raw_calls == 1);
    assert(empty_powerup_publications == 1); empty_retained();
    prepare_empty(); empty_publish_ok = false;
    assert(!adreno8_gpu_cp_init() && !raw_calls && empty_powerup_publications == 1); empty_retained();
    assert(((uint32_t *)device_memory[2])[3] != 0);
    prepare_empty(); raw_ok = false;
    assert(!adreno8_gpu_cp_init() && raw_calls == 1); empty_retained();
    prepare_empty(); idle_ok = false;
    assert(!adreno8_gpu_cp_init() && raw_calls == 1); empty_retained();
    prepare_empty(); smmu_after_raw = true;
    assert(!adreno8_gpu_cp_init() && raw_calls == 1); empty_retained();
    for (unsigned bad = 0; bad < 11; bad++) {
        prepare_empty();
        switch (bad) {
        case 0: empty_powerup_asked = 2; break;
        case 1: empty_powerup_asked = UINT32_MAX; break;
        case 2: adreno8_step = 16; break;
        case 3: adreno8_newest_step = 18; break;
        case 4: adreno8_gpu8_keep = false; break;
        case 5: adreno8_gpu8_return = true; break;
        case 6: adreno8_skip_steps = 1; break;
        default: raw_nop_asked = bad - 6; break;
        }
        assert(!adreno8_gpu_cp_init() && gpu_state == GPU_FAILED);
        assert(!raw_calls && !gx_reads && !gx_writes && !gmu_reads && !gmu_writes);
        assert(!oob_requested && !adreno8_reset_required && !normal_powerup_publications && !empty_powerup_publications);
    }
    reset(); prepare(); smmu_after_raw = true;
    assert(!adreno8_gpu_cp_init() && gpu_state == GPU_FAILED);
    reset(); prepare(); idle_ok = false;
    assert(!adreno8_gpu_cp_init() && gpu_state == GPU_FAILED);
    reset(); prepare(); assert(adreno8_gpu_cp_init() && adreno8_gpu_secure_exit());
    corrupt_scratch = true; assert(!adreno8_gpu_scratch_test() && gpu_state == GPU_FAILED);
    reset(); prepare(); assert(adreno8_gpu_cp_init() && adreno8_gpu_secure_exit());
    acquire_ok = false; assert(!adreno8_gpu_scratch_test() && gpu_state == GPU_FAILED);
    check_direct_ring();
    check_ring_setup_only();
    for (unsigned failed=1;failed<=2;failed++) {
        prepare_ring(); walk_fail_at=failed;
        assert(!adreno8_gpu_cp_init() && gpu_state==GPU_FAILED && walk_calls==failed);
        assert(!oob_requested && !gx_reads && !gx_writes && !gmu_reads && !gmu_writes);
        assert(allocation_count==5 && !adreno8_gpu_cp_ready());
        assert(!adreno8_gpu_cp_init() && walk_calls==failed);
    }
    reset(); free(files[0]); free(files[1]);
    puts("gen8.0.0 vendor aperture/tables, bounded ordered MMIO, PM4, firmware and startup failures passed");
    return 0;
}
'''

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="adreno8-gpu-test-") as temp:
        cfile = Path(temp) / "gpu.c"
        cfile.write_text(source)
        for page in (4096, 16384):
            binary = Path(temp) / f"gpu-{page}"
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                            f"-DPAGE_SIZE={page}", "-I", str(DEV), str(cfile), "-o", str(binary)], check=True)
            subprocess.run([str(binary), str(args.firmware_dir / "gen80000_sqe.fw"),
                            str(args.firmware_dir / "gen80000_aqe.fw")], check=True)

        production = (DEV / "adreno8_gpu.c").read_text()
        variants = {}
        for function in ("gpu_read", "gpu_write", "gpu_gmu_read", "gpu_gmu_write"):
            start = production.index(function + "(")
            end = production.index("\n}\n", start) + 3
            body = production[start:end]
            without = body.replace("\tadreno8_settle();\n", "", 1)
            variants[function + "_missing_barrier"] = production[:start] + without + production[end:]
            if function in ("gpu_read", "gpu_gmu_read"):
                needle = "\tuint32_t value;\n" if function == "gpu_read" else "\tuint32_t value ="
                moved = without.replace(needle, "\tadreno8_settle();\n" + needle, 1)
            elif function == "gpu_write":
                moved = without[:-2] + "\tadreno8_settle();\n}\n"
            else:
                moved = without.replace("\tadreno8_wr(adreno8_gmu.base + offset, value);",
                    "\tadreno8_wr(adreno8_gmu.base + offset, value);\n\tadreno8_settle();", 1)
            variants[function + "_barrier_wrong_side"] = production[:start] + moved + production[end:]
        checks = [(name, mutated, None) for name, mutated in variants.items()]
        private = (DEV / "adreno8_gpu_private.h").read_text()
        tpl1_entry = "{ GEN8_TPL1_DBG_ECO_CNTL, 0x10000000, A8_BIT(A8_PIPE_NONE) }"
        assert private.count(tpl1_entry) == 1
        checks.append(("tpl1_bit20_restored", production,
                       private.replace(tpl1_entry, tpl1_entry.replace("0x10000000", "0x10100000"))))
        for name, before, after in (
            ("tpl1_startup_bit20", "gpu_write(a8_noncontext[i].reg, a8_noncontext[i].value);",
             "gpu_write(a8_noncontext[i].reg, a8_noncontext[i].value | (a8_noncontext[i].reg == 0xb600 ? 1u << 20 : 0));"),
            ("tpl1_powerup_bit20", "words[at++] = gpu_read(a8_powerup_ifpc[i]);",
             "words[at++] = gpu_read(a8_powerup_ifpc[i]) | (a8_powerup_ifpc[i] == 0xb600 ? 1u << 20 : 0);"),
        ):
            assert production.count(before) == 1, name
            checks.append((name, production.replace(before, after), None))
        empty_variants = [
            ("empty-not-cleared", "memset((void *)powerup->memory, 0, powerup->size);", ""),
            ("empty-partial-clear", "memset((void *)powerup->memory, 0, powerup->size);",
             "memset((void *)powerup->memory, 0, 64);"),
            ("empty-wrong-buffer", "memset((void *)powerup->memory, 0, powerup->size);",
             "memset((void *)scratch->memory, 0, scratch->size);"),
            ("empty-not-published", "if (!adreno8_memory_publish(powerup, 0, powerup->size))", "if (false)"),
            ("empty-partial-publish", "if (!adreno8_memory_publish(powerup, 0, powerup->size))",
             "if (!adreno8_memory_publish(powerup, 0, 64))"),
            ("empty-publish-failure-ignored", "if (!adreno8_memory_publish(powerup, 0, powerup->size))",
             "if ((void)adreno8_memory_publish(powerup, 0, powerup->size), false)"),
            ("empty-wrong-IOVA", "adreno8_pm4_cp_init(words, A8_COUNT(words), powerup->gpu_address)",
             "adreno8_pm4_cp_init(words, A8_COUNT(words), scratch->gpu_address)"),
            ("empty-omitted-original-config", "!gpu_oob_get() || !gpu_register_config() ||",
             "!gpu_oob_get() || (!empty_powerup && !gpu_register_config()) ||"),
            ("empty-promoted-ready", '\tif (empty_powerup)\n\t\treturn gpu_fail("empty powerup diagnostic acknowledged and idle; stop");', ""),
            ("empty-value-guard", "empty_powerup != 1 || ", ""),
            ("empty-step-guard", "adreno8_step != 17 || ", ""),
            ("empty-limit-guard", "adreno8_newest_step != 17 ||", ""),
            ("empty-keep-guard", "!adreno8_gpu8_keep || ", ""),
            ("empty-return-guard", "adreno8_gpu8_return || ", ""),
            ("empty-skip-guard", "adreno8_skip_steps != 0 || ", ""),
            ("empty-NOP-guard", "|| raw_nop != 0", ""),
        ]
        cp_source = production[production.index("\nbool\nadreno8_gpu_cp_init("):production.index("\nbool\nadreno8_gpu_cp_ready(")]
        for name, before, after in empty_variants:
            assert before in cp_source, name
            changed = cp_source.replace(before, after, 1)
            checks.append((name, production.replace(cp_source, changed, 1), None))
        ring_variants = [
            ("ring-size", '"direct ring", 32768, 4096', '"direct ring", 4096, 4096'),
            ("ring-write-permission", '32768, 4096, ADRENO8_MEM_READ | ADRENO8_MEM_PRIV',
             '32768, 4096, ADRENO8_MEM_READ | ADRENO8_MEM_WRITE | ADRENO8_MEM_PRIV'),
            ("ring-zero", 'memset((void *)ring->memory, 0, ring->size);', ''),
            ("ring-initial-publication", '!adreno8_memory_publish(ring, 0, ring->size)', 'false'),
            ("ring-shadow-publication", '!adreno8_memory_publish(scratch, 0, scratch->size)', 'false'),
            ("ring-powerup-publication", '!adreno8_memory_publish(powerup, 0, powerup->size)', 'false'),
            ("ring-control", '{ GEN8_CP_RB_CNTL_GC, 0x20c }', '{ GEN8_CP_RB_CNTL_GC, 0x30c }'),
            ("ring-BV-shadow-offset", 'scratch->gpu_address + 8', 'scratch->gpu_address + 4'),
            ("ring-readback", 'if (gpu_read(setup[i].reg) != setup[i].value)', 'if (false)'),
            ("ring-SQE-readback", 'if (gpu_read(GEN8_CP_SQE_CNTL) != 1)', 'if (false)'),
            ("ring-command-publication", '!adreno8_memory_publish(ring, 0, count * 4)', 'false'),
            ("ring-fence-mask", 'if (!(fence & 1u))', 'if (!(fence & 2u))'),
            ("ring-fence-bound", 'i < 200; i++', 'i < 201; i++'),
            ("ring-HFI-health", '!adreno8_hfi_poll() || !gpu_status_ok()', '!gpu_status_ok()'),
            ("ring-RPTR-completion", 'rptr == count && br_shadow == count', 'br_shadow == count'),
            ("ring-shadow-completion", 'rptr == count && br_shadow == count', 'rptr == count'),
            ("ring-BV-too-strict", 'rptr == count && br_shadow == count', 'rptr == count && br_shadow == count && bv == count'),
            ("ring-GMU-idle", '!(gpu_gmu_read(GMU(GEN8_GMUAO_GPU_CX_BUSY_STATUS)) & (1u << 23))', 'true'),
            ("ring-RBBM-idle", '!(gpu_read(GEN8_RBBM_STATUS) & ~1u)', 'true'),
            ("ring-ready-promotion", 'return gpu_fail("direct ring diagnostic complete; stop");', 'gpu_state=GPU_CP_READY; return true;'),
            ("ring-LPAC", 'if (adreno8_direct_ring && pipe == A8_PIPE_LPAC)', 'if (false)'),
            ("ring-trusted-range", 'adreno8_direct_ring ? 0 : ADRENO8_SECURE_IOVA_START', 'ADRENO8_SECURE_IOVA_START'),
        ]
        ring_body = re.search(r"static bool\ngpu_direct_ring_init\(.*?\n}", production, re.S)[0]
        for name, before, after in ring_variants:
            owner = ring_body if before in ring_body else production
            assert before in owner, name
            checks.append((name, production.replace(owner, owner.replace(before, after, 1), 1), None))
        for name, before, after in (
            ("ring-cold-indices", 'if (gpu_read(GEN8_CP_RB_RPTR_BR) != 0 || gpu_read(GEN8_CP_RB_WPTR_GC) != 0)', 'if (false)'),
            ("ring-WPTR-value", 'if (!gpu_ring_wptr(count))', 'if (!gpu_ring_wptr(count + 1))'),
        ):
            assert before in production, name
            checks.append((name, production.replace(before, after, 1), None))
        fenced = re.search(r"static bool\ngpu_ring_wptr\(.*?\n}", production, re.S)[0]
        missing_barrier = fenced.replace('adreno8_wr(adreno8_gpu.base + 4u * GEN8_CP_RB_WPTR_GC, value);\n\t\tadreno8_settle();',
                                         'adreno8_wr(adreno8_gpu.base + 4u * GEN8_CP_RB_WPTR_GC, value);')
        assert missing_barrier != fenced
        checks.append(("ring-WPTR-post-barrier", production.replace(fenced, missing_barrier, 1), None))
        hwcg = re.search(r"static bool\ngpu_hwcg\(.*?\n}", production, re.S)[0]
        checks.append(("ring-HWCG-enabled", production.replace(hwcg, hwcg.replace('if (adreno8_direct_ring)', 'if (false)', 1), 1), None))
        setup_only = re.search(r"\tif \(raw_mode\) \{.*?\n\t}", ring_body, re.S)[0]
        setup_mutations = [
            ("setup-dispatch", 'gpu_direct_ring_init(raw_nop)', 'gpu_direct_ring_init(false)'),
            ("setup-SQE-started", 'if (raw_mode) {', 'if (raw_mode) { gpu_write(GEN8_CP_SQE_CNTL,1);'),
            ("setup-window-short", 'i < 100; i++', 'i < 99; i++'),
            ("setup-HFI-health", '!adreno8_hfi_poll() || !gpu_status_ok()', '!gpu_status_ok()'),
            ("setup-CP-health", '!adreno8_hfi_poll() || !gpu_status_ok()', '!adreno8_hfi_poll()'),
            ("setup-ready", 'return gpu_fail("ordinary setup-only diagnostic stop");', 'gpu_state=GPU_CP_READY; return true;'),
            ("setup-mode-guard", '(raw_nop != 0 && raw_nop != 2 && raw_nop != 3)', 'false'),
            ("setup-halt-read-missing", 'i < A8_COUNT(halt); i++', 'i < A8_COUNT(halt)-1; i++'),
            ("setup-halt-all1", 'if (value == 0xffffffffu)', 'if (false)'),
            ("setup-halt-cleared", 'uint32_t value = gpu_read(halt[i]);', 'gpu_write(halt[i],0); uint32_t value = gpu_read(halt[i]);'),
        ]
        for name,before,after in setup_mutations:
            owner=setup_only if before in setup_only else production
            assert before in owner,name
            checks.append((name,production.replace(owner,owner.replace(before,after,1),1),None))
        final_check = setup_only.replace('\t\t\tadreno8_sleep_us(100000);\n','').replace(
            '\t\t\t\treturn gpu_fail("ordinary setup-only HFI or GPU fault");',
            '\t\t\t\treturn gpu_fail("ordinary setup-only HFI or GPU fault");\n\t\t\tadreno8_sleep_us(100000);')
        checks.append(("setup-final-check",production.replace(setup_only,final_check,1),None))
        for name, mutated, mutated_header in checks:
            assert mutated != production or (mutated_header is not None and mutated_header != private), name
            variant = Path(temp) / name
            variant.mkdir()
            (variant / "adreno8_gpu.c").write_text(mutated)
            if mutated_header is not None:
                (variant / "adreno8_gpu_private.h").write_text(mutated_header)
            (variant / "gpu.c").write_text(source)
            for page in (4096, 16384):
                binary = variant / f"gpu-{page}"
                subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                                f"-DPAGE_SIZE={page}", "-I", str(DEV), str(variant / "gpu.c"), "-o", str(binary)], check=True)
                result = subprocess.run([str(binary), str(args.firmware_dir / "gen80000_sqe.fw"),
                                         str(args.firmware_dir / "gen80000_aqe.fw")], capture_output=True, text=True)
                assert result.returncode != 0 and "assert" in result.stderr.lower(), (name, page, result.stderr)
        print(f"GPU MMIO barrier mutations rejected: {len(variants)} variants x 2 page sizes")
        print("GPU TPL1 catalog/startup/powerup mutations rejected: 3 variants x 2 page sizes")
        print(f"GPU empty powerup mutations rejected: {len(empty_variants)} variants x 2 page sizes")
        print(f"GPU ordinary ring mutations rejected: {len(ring_variants) + 4} variants x 2 page sizes")
        print(f"GPU ordinary setup-only mutations rejected: {len(setup_mutations) + 1} variants x 2 page sizes")
