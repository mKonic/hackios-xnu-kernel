#!/usr/bin/env python3
"""Check GPU clock writes and decode captured CMD DB bytes with production definitions."""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
header = (ROOT / "xnu/files/bsd/dev/adreno8_private.h").read_text()
driver = (ROOT / "xnu/files/bsd/dev/adreno8_cx.c").read_text()
start = driver.index("adreno8_clock_source(")
end = driver.index("\n}\n", start) + 2
function = driver[start:end]
start = driver.index("adreno8_step_cx_power(")
end = driver.index("\n}\n", start) + 2
power_function = driver[start:end]
start = driver.index("adreno8_scm_gpu_init(")
end = driver.index("\n}\n", start) + 2
scm_function = driver[start:end]
start = driver.index("adreno8_gemnoc_on(")
end = driver.index("\n}\n", start) + 2
gemnoc_function = driver[start:end]
start = driver.index("adreno8_step_clocks(")
end = driver.index("\n}\n", start) + 2
clocks_function = driver[start:end]
common = (ROOT / "xnu/files/bsd/dev/adreno8.c").read_text()
start = common.index("adreno8_set(")
end = common.index("\n}\n", start) + 2
set_function = common[start:end]
structure = re.search(r"struct adreno8_bcm_db \{.*?\n};", header, re.S).group()
constants = "\n".join(line for line in header.splitlines() if line.startswith(("#define RCG_SOURCE_MASK ", "#define RCG_UPDATE ", "#define GPUCC_", "#define GCC_", "#define CBCR_", "#define GDSC_")))
constants += "\n" + re.search(r"#define BCM_TCS_CMD[^\n]*\n[^\n]*", header).group()
source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
''' + constants + r'''
struct adreno8_block { unsigned base; };
static struct adreno8_block adreno8_gpucc, adreno8_gcc = {0x10000};
static struct adreno8_block adreno8_cx_misc = {0x88000};
static unsigned adreno8_step, writes, branches, gemnoc_sleeps, gemnoc_ready_after, ahb_reads;
static unsigned fail_branch, ahb_writes, ahb_scan_mask, ahb_write_mask;
static int ahb_fault_index = -1;
static unsigned ahb_fault;
static bool adreno8_reset_required, ahb_pending_read, ahb_settled;
static uint32_t regs[0x90000 / 4], adreno8_gmu_from_pll;
static bool reject_gemnoc;
static bool accept = true;
static int scm_status[2], scm_calls;
static uint64_t scm_answer;
static int32_t adreno8_scm_call(uint64_t function, uint64_t arginfo, uint64_t argument,
                              uint64_t argument1, uint64_t argument2, uint64_t argument3, uint64_t *answer) {
    assert(arginfo == 1 && argument1 == 0 && argument2 == 0 && argument3 == 0);
    assert(scm_calls < 2);
    assert(function == (scm_calls == 0 ? 0x42000601u : 0x42002801u));
    assert(argument == (scm_calls == 0 ? 0x02002801u : 9u));
    *answer = scm_answer;
    return scm_status[scm_calls++];
}
static bool adreno8_cx_changed, powered;
static bool clock_boot_ok = true;
static unsigned clock_boot_calls;
static bool adreno8_clock_boot(void) { clock_boot_calls++; ahb_scan_mask = 0; return clock_boot_ok; }
static uint32_t adreno8_cx_saved_gdscr;
static const void *adreno8_speeds;
static bool adreno8_gpucc_answers(void) { return true; }
static bool adreno8_cx_is_on(void) { return powered; }
static void adreno8_announce(const char *s) {}
static void adreno8_sleep_us(unsigned us) {
    if (us == 1 && ++gemnoc_sleeps == gemnoc_ready_after)
        regs[(adreno8_gcc.base + GCC_GPU_GEMNOC_GFX) / 4] &= ~CBCR_OFF;
}
static uint64_t mach_absolute_time(void) { return 0; }
static void absolutetime_to_nanoseconds(uint64_t t, uint64_t *ns) { *ns = 0; }
static uint32_t adreno8_get(struct adreno8_block *b, const char *n, uint32_t off) {
    if (b == &adreno8_cx_misc) {
        assert(off >= 0x40 && off <= 0x50 && !(off & 3) && branches == 13);
        ahb_reads++; ahb_scan_mask |= 1u << ((off - 0x40) / 4);
        if (ahb_pending_read) {
            assert(ahb_settled);
            ahb_pending_read = false;
        }
    }
    return regs[(b->base + off) / 4];
}
static uint32_t adreno8_rd(unsigned off) { return regs[off / 4]; }
static void adreno8_put(struct adreno8_block *b, const char *n, uint32_t off, uint32_t value) {
    uint32_t previous = regs[(b->base + off) / 4];
    if (b == &adreno8_cx_misc) {
        assert(adreno8_gmu_from_pll && clock_boot_ok && branches == 13);
        assert(off >= 0x40 && off <= 0x50 && !(off & 3));
        assert(ahb_scan_mask == 31 && adreno8_reset_required && !ahb_pending_read);
        assert(value == 0x1b12);
        ahb_writes++; ahb_write_mask |= 1u << ((off - 0x40) / 4);
        ahb_pending_read = true; ahb_settled = false;
    }
    regs[(b->base + off) / 4] = value; writes++;
    if (b == &adreno8_cx_misc && (int)((off - 0x40) / 4) == ahb_fault_index) {
        if (ahb_fault == 1) regs[(b->base + off) / 4] = previous;
        if (ahb_fault == 2) regs[(b->base + off) / 4] ^= 1;
        if (ahb_fault == 3) regs[(b->base + off) / 4] = UINT32_MAX;
    }
    if (b == &adreno8_gcc && off == GCC_GPU_GEMNOC_GFX && reject_gemnoc)
        regs[(b->base + off) / 4] &= ~CBCR_ENABLE;
    if (off == 0) assert(value == 3); /* ROOT_EN is preserved; UPDATE is bit 0. */
}
static bool adreno8_wait_for(struct adreno8_block *b, unsigned off, unsigned mask, unsigned value, unsigned ms) {
    assert((off == 0 || off == GPUCC_GMU_RCGR) && mask == 1 && value == 0);
    if (accept) regs[off / 4] &= ~1u;
    return accept;
}
static bool adreno8_clock_on(struct adreno8_block *b, const char *name, uint32_t cbcr, bool must) {
    branches++; return branches != fail_branch;
}
static void adreno8_settle(void) { if (ahb_pending_read) ahb_settled = true; }
''' + structure + "\nuint32_t\n" + set_function + "\nstatic bool\n" + gemnoc_function + "\nbool\n" + function + "\nbool\n" + power_function + "\nbool\n" + scm_function + "\nbool\n" + clocks_function + r'''
static uint32_t *ahb_control(unsigned index) {
    assert(index < 5);
    return &regs[(adreno8_cx_misc.base + 0x40 + 4 * index) / 4];
}
static void ahb_reset(unsigned mode) {
    memset(regs, 0, sizeof(regs));
    powered = accept = clock_boot_ok = true; reject_gemnoc = false;
    adreno8_gmu_from_pll = mode; adreno8_reset_required = false;
    clock_boot_calls = writes = branches = fail_branch = ahb_reads = ahb_writes = 0;
    ahb_scan_mask = ahb_write_mask = ahb_fault = 0; ahb_fault_index = -1;
    ahb_pending_read = ahb_settled = false;
    regs[GPUCC_GMU_RCGR / 4 + 1] = 0x100000;
}
int main(void) {
    scm_answer = 1; assert(adreno8_scm_gpu_init() && scm_calls == 2);
    scm_calls = 0; scm_answer = 0; assert(adreno8_scm_gpu_init() && scm_calls == 1);
    scm_calls = 0; scm_status[0] = -4; assert(adreno8_scm_gpu_init() && scm_calls == 1);
    scm_calls = 0; scm_status[0] = -1; assert(!adreno8_scm_gpu_init() && scm_calls == 1);
    scm_calls = 0; scm_status[0] = 0; scm_answer = 1; scm_status[1] = 2;
    assert(!adreno8_scm_gpu_init() && scm_calls == 2);
    scm_calls = 0; scm_status[1] = -4; assert(adreno8_scm_gpu_init() && scm_calls == 2);
    assert(BCM_TCS_CMD(1, 1, 0, 0) == 0x60000000u);
    assert(BCM_TCS_CMD(0, 1, 0x3fff, 0x3fff) == 0x2fffffffu);
    assert(BCM_TCS_CMD(1, 0, 0, 0) == 0x40000000u);
    struct adreno8_bcm_db bcm;
    const uint8_t sh0[8] = {0x00, 0x3e, 0x49, 0x00, 0x40, 0x00, 0x01, 0x00};
    const uint8_t mc0[8] = {0x00, 0x3e, 0x49, 0x00, 0x10, 0x00, 0x00, 0x00};
    assert(sizeof(bcm) == 8);
    memcpy(&bcm, sh0, sizeof(bcm)); assert(bcm.unit == 4800000 && bcm.width == 64 && bcm.vcd == 1);
    memcpy(&bcm, mc0, sizeof(bcm)); assert(bcm.unit == 4800000 && bcm.width == 16 && bcm.vcd == 0);
    regs[0] = 2; regs[1] = 0x100501;
    assert(adreno8_clock_source("gmu", 0, 0));
    assert(writes == 2 && regs[1] == 0x100000 && regs[0] == 2);
    assert(adreno8_clock_source("gmu", 0, 0) && writes == 2);
    assert(adreno8_clock_source("gmu", 0, 0x201));
    assert(regs[1] == 0x100201 && writes == 4);
    accept = false;
    assert(!adreno8_clock_source("gmu", 0, 0));
    writes = 0;
    regs[GPUCC_CX_GDSCR / 4] = 0x222f801;
    regs[GPUCC_CX_HW_CTRL / 4] = 0x2a00010a;
    assert(!adreno8_step_cx_power());
    assert(writes == 1 && adreno8_cx_changed);
    assert(adreno8_cx_saved_gdscr == 0x222f801);
    assert(regs[GPUCC_CX_GDSCR / 4] == 0x222f800);
    writes = 0; adreno8_cx_changed = false;
    regs[GPUCC_CX_GDSCR / 4] = 0x8222f800;
    assert(adreno8_step_cx_power());
    assert(writes == 0 && !adreno8_cx_changed);
    assert(GCC_GPU_GEMNOC_GFX == 0x71010);
    assert(CBCR_HWCG == 2 && CBCR_OFF == 0x80000000u);
    assert(CBCR_NOC_STATUS == 0x70000000u && CBCR_NOC_ON == 0x20000000u);
    uint32_t *gemnoc = &regs[(adreno8_gcc.base + GCC_GPU_GEMNOC_GFX) / 4];
    const uint32_t shared = (1u << 22) | (1u << 9);
    *gemnoc = shared | CBCR_OFF | CBCR_HWCG;
    assert(adreno8_gemnoc_on() && writes == 1 && gemnoc_sleeps == 0);
    assert(*gemnoc == (shared | CBCR_OFF | CBCR_HWCG | CBCR_ENABLE));
    assert(adreno8_gemnoc_on() && writes == 1); /* Inherited enable stays unchanged. */
    reject_gemnoc = true; *gemnoc &= ~CBCR_ENABLE;
    assert(!adreno8_gemnoc_on() && writes == 2 && gemnoc_sleeps == 0);
    reject_gemnoc = false; *gemnoc = shared;
    assert(adreno8_gemnoc_on() && *gemnoc == (shared | CBCR_ENABLE));
    *gemnoc = shared | CBCR_OFF | CBCR_NOC_ON;
    assert(adreno8_gemnoc_on() && gemnoc_sleeps == 0);
    *gemnoc = shared | CBCR_OFF;
    gemnoc_ready_after = 3;
    assert(adreno8_gemnoc_on() && gemnoc_sleeps == 3);
    *gemnoc = shared | CBCR_OFF | (1u << 28);
    gemnoc_sleeps = gemnoc_ready_after = 0;
    assert(!adreno8_gemnoc_on() && gemnoc_sleeps == 200);
    *gemnoc = UINT32_MAX; writes = 0; gemnoc_sleeps = 0;
    assert(!adreno8_gemnoc_on() && writes == 0 && gemnoc_sleeps == 0);
    powered = true; accept = true; branches = 0; reject_gemnoc = true;
    *gemnoc = CBCR_HWCG | CBCR_OFF;
    regs[GPUCC_GMU_RCGR / 4 + 1] = 0x100501;
    assert(!adreno8_step_clocks() && branches == 1);
    assert(regs[GPUCC_GMU_RCGR / 4 + 1] == 0x100501);
    reject_gemnoc = false; branches = 0;
    regs[GPUCC_GMU_RCGR / 4 + 1] = 0x100000;
    assert(adreno8_step_clocks() && branches > 1 && (*gemnoc & CBCR_ENABLE));
    assert(clock_boot_calls == 0);
    clock_boot_ok = false; adreno8_gmu_from_pll = 1;
    regs[GPUCC_GMU_RCGR / 4 + 1] = 0x100000;
    unsigned prior_writes = writes; branches = 0;
    assert(!adreno8_step_clocks() && clock_boot_calls == 1 && writes == prior_writes && branches == 1);
    assert(regs[GPUCC_GMU_RCGR / 4 + 1] == 0x100000);
    clock_boot_ok = true; branches = 0;
    assert(adreno8_step_clocks() && clock_boot_calls == 2 && branches > 1);
    assert(ahb_writes == 5 && ahb_reads == 15 && adreno8_reset_required);
    for (unsigned i = 0; i < 5; i++) {
        uint32_t *control = &regs[(adreno8_cx_misc.base + 0x40 + 4 * i) / 4];
        *control = UINT32_MAX; unsigned before = ahb_reads; branches = 0;
        assert(!adreno8_step_clocks() && ahb_reads == before + i + 1);
        *control = 0;
    }
    ahb_reset(0);
    for (unsigned i = 0; i < 5; i++) *ahb_control(i) = 0xa5000000u + i;
    assert(adreno8_step_clocks() && ahb_reads == 5 && !ahb_writes && !adreno8_reset_required);
    for (unsigned i = 0; i < 5; i++) assert(*ahb_control(i) == 0xa5000000u + i);
    for (unsigned bad = 0; bad < 5; bad++) {
        ahb_reset(0); *ahb_control(bad) = UINT32_MAX;
        assert(!adreno8_step_clocks() && ahb_reads == bad + 1 && !ahb_writes);
        assert(!adreno8_reset_required);
    }
    for (unsigned mode = 1; mode <= 2; mode++) {
        ahb_reset(mode);
        for (unsigned i = 0; i < 5; i++) *ahb_control(i) = 0xa5000000u + i;
        assert(adreno8_step_clocks() && ahb_reads == 10 && ahb_writes == 5);
        assert(ahb_write_mask == 31 && adreno8_reset_required && !ahb_pending_read);
        for (unsigned i = 0; i < 5; i++) assert(*ahb_control(i) == 0x1b12);
        adreno8_reset_required = false; branches = 0;
        assert(adreno8_step_clocks() && ahb_reads == 15 && ahb_writes == 5);
        assert(!adreno8_reset_required && !ahb_pending_read);
        for (unsigned unchanged = 0; unchanged < 5; unchanged++) {
            ahb_reset(mode); *ahb_control(unchanged) = 0x1b12;
            assert(adreno8_step_clocks() && ahb_reads == 9 && ahb_writes == 4);
            assert(ahb_write_mask == (31u ^ (1u << unchanged)) && adreno8_reset_required);
            for (unsigned i = 0; i < 5; i++) assert(*ahb_control(i) == 0x1b12);
        }
        for (unsigned bad = 0; bad < 5; bad++) {
            ahb_reset(mode); *ahb_control(bad) = UINT32_MAX;
            assert(!adreno8_step_clocks() && ahb_reads == bad + 1);
            assert(!ahb_writes && !adreno8_reset_required);
        }
        for (unsigned fault = 1; fault <= 3; fault++) {
            for (unsigned bad = 0; bad < 5; bad++) {
                ahb_reset(mode); ahb_fault = fault; ahb_fault_index = (int)bad;
                assert(!adreno8_step_clocks() && ahb_writes == bad + 1);
                assert(ahb_reads == 5 + bad + 1 && ahb_write_mask == (1u << (bad + 1)) - 1);
                assert(adreno8_reset_required && !ahb_pending_read);
                for (unsigned i = 0; i < bad; i++) assert(*ahb_control(i) == 0x1b12);
                assert(*ahb_control(bad) != 0x1b12);
                for (unsigned i = bad + 1; i < 5; i++) assert(*ahb_control(i) == 0);
            }
        }
        ahb_reset(mode); clock_boot_ok = false;
        assert(!adreno8_step_clocks() && !ahb_reads && !ahb_writes);
        for (unsigned branch = 1; branch <= 13; branch++) {
            ahb_reset(mode); fail_branch = branch;
            assert(!adreno8_step_clocks() && !ahb_reads && !ahb_writes && branches == branch);
            assert(clock_boot_calls == (branch == 1 ? 0 : 1));
        }
    }
}
'''
mutations = [
    ("AHB writes on XO", "\tif (!adreno8_gmu_from_pll) return true;\n", ""),
    ("all-ones inherited AHB accepted", "if (inherited[i] == 0xffffffffu) return false;", "if (false) return false;"),
    ("missing AHB retention", "\t\tadreno8_reset_required = true;\n", ""),
    ("missing AHB write", "\t\tadreno8_put(&adreno8_cx_misc, ahb[i], 0x40 + 4 * i, 0x1b12);\n", ""),
    ("missing AHB write barrier", "\t\tadreno8_settle();\n", ""),
    ("ignored AHB readback", "!= 0x1b12) return false;", "!= 0x1b12 && false) return false;"),
    ("AHB rewrite after confirmation", "if (inherited[i] == 0x1b12) continue;", "if (false) continue;"),
    ("wrong AHB timeout", "0x40 + 4 * i, 0x1b12);", "0x40 + 4 * i, 0x1b11);"),
    ("shifted AHB write offset", "0x40 + 4 * i, 0x1b12);", "0x44 + 4 * i, 0x1b12);"),
    ("AHB RMW preserves unknown fields", "0x40 + 4 * i, 0x1b12);", "0x40 + 4 * i, inherited[i] | 0x1b12);"),
]
checks = [("production", source, False)]
for label, before, after in mutations:
    assert clocks_function.count(before) == 1, label
    changed = clocks_function.replace(before, after, 1)
    assert source.count(clocks_function) == 1
    checks.append((label, source.replace(clocks_function, changed, 1), True))
for label, code, rejected in checks:
    with tempfile.TemporaryDirectory(prefix="adreno8-config-") as directory:
        src = Path(directory) / "check.c"
        binary = Path(directory) / "check"
        src.write_text(code)
        subprocess.run(["cc", "-std=c11", "-fsanitize=address,undefined", str(src), "-o", str(binary)], check=True)
        result = subprocess.run([str(binary)], capture_output=True, text=True)
        if rejected:
            assert result.returncode != 0, "mutation survived: " + label
        else:
            assert result.returncode == 0, result.stdout + result.stderr
print("BCM, RCG, power, GEMNOC and AHB timeout ordering/retention/failures/idempotence: PASS")
print("AHB timeout mutations rejected:", len(mutations))
