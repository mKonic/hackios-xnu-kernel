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
structure = re.search(r"struct adreno8_bcm_db \{.*?\n};", header, re.S).group()
constants = "\n".join(line for line in header.splitlines() if line.startswith(("#define RCG_SOURCE_MASK ", "#define RCG_UPDATE ", "#define GPUCC_CX_GDSCR ", "#define GPUCC_CX_HW_CTRL ", "#define GDSC_")))
constants += "\n" + re.search(r"#define BCM_TCS_CMD[^\n]*\n[^\n]*", header).group()
source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
struct adreno8_block { unsigned base; };
static struct adreno8_block adreno8_gpucc;
static unsigned adreno8_step, writes;
static uint32_t regs[0x10000 / 4];
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
static uint32_t adreno8_cx_saved_gdscr;
static const void *adreno8_speeds;
static bool adreno8_gpucc_answers(void) { return true; }
static bool adreno8_cx_is_on(void) { return powered; }
static void adreno8_announce(const char *s) {}
static void adreno8_sleep_us(unsigned us) {}
static uint64_t mach_absolute_time(void) { return 0; }
static void absolutetime_to_nanoseconds(uint64_t t, uint64_t *ns) { *ns = 0; }
static uint32_t adreno8_get(struct adreno8_block *b, const char *n, uint32_t off) { return regs[off / 4]; }
static uint32_t adreno8_rd(unsigned off) { return regs[off / 4]; }
static void adreno8_put(struct adreno8_block *b, const char *n, uint32_t off, uint32_t value) {
    regs[off / 4] = value; writes++;
    if (off == 0) assert(value == 3); /* ROOT_EN is preserved; UPDATE is bit 0. */
}
static bool adreno8_wait_for(struct adreno8_block *b, unsigned off, unsigned mask, unsigned value, unsigned ms) {
    assert(off == 0 && mask == 1 && value == 0);
    if (accept) regs[0] &= ~1u;
    return accept;
}
''' + constants + "\n" + structure + "\nbool\n" + function + "\nbool\n" + power_function + "\nbool\n" + scm_function + r'''
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
}
'''
with tempfile.TemporaryDirectory(prefix="adreno8-config-") as directory:
    src = Path(directory) / "check.c"
    binary = Path(directory) / "check"
    src.write_text(source)
    subprocess.run(["cc", "-std=c11", str(src), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
print("BCM layout, RCG update, preserved bits, power failure and inherited power: PASS")
