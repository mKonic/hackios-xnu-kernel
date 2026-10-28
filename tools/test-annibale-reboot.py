#!/usr/bin/env python3
"""Exercise the actual reboot probe and SPMI helpers against a bounded MMIO model."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
buttons = (ROOT / "xnu/files/bsd/dev/annibale_buttons.c").read_text()


def function(name):
    start = buttons.index(name + "(")
    end = buttons.index("{", start) + 1
    depth = 1
    while depth:
        depth += (buttons[end] == "{") - (buttons[end] == "}")
        end += 1
    return buttons[start:end]


constants = buttons[buttons.index("#define ARB_CORE_PHYS"):buttons.index("static vm_offset_t arb_core")]
start = buttons.index("struct peripheral {")
peripheral = buttons[start:buttons.index("};", start) + 2]
source = r'''
#include <assert.h>
#include <setjmp.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
typedef uintptr_t vm_offset_t;
#define NSEC_PER_USEC 1000
''' + constants + peripheral + r'''
static vm_offset_t arb_core, arb_cnfg, arb_obsrvr;
static struct { uint64_t reads, failed, total_ns, longest_ns, last_ns; } bus;
static uint64_t ticks, loader_stub_slot[4];
static unsigned maps, writes, reads, returns, failures, lock_depth, prepared;
static unsigned map_fail, read_fail, write_fail, wrong_read, conflict;
static uint32_t version, count, map_ppid, map_owner, read_status, write_status;
static uint8_t memory[256], write_data, read_register;
static bool present, identity, return_returns, should_prepare;
static uint32_t enabled, cpus;
static jmp_buf terminal;
static unsigned cases;

static int quiet(const char *format, ...) {
    if (strstr(format, "reboot-lk: prepared ")) {
        assert(writes == 1 && reads == 3 && (memory[0x48] >> 1) == 2);
        prepared++;
    }
    return 0;
}
#define printf quiet
static uint64_t mach_absolute_time(void) { return ticks += 1000; }
static void nanoseconds_to_absolutetime(uint64_t n, uint64_t *out) { *out = n; }
static void absolutetime_to_nanoseconds(uint64_t n, uint64_t *out) { *out = n; }
static void annibale_spmi_lock(void) { assert(!lock_depth++); }
static void annibale_spmi_unlock(void) { assert(lock_depth-- == 1); }
static bool redwood_platform_is_annibale(void) { return identity; }
static bool PE_parse_boot_argn(const char *key, void *out, unsigned length) {
    assert(length == 4);
    if (!strcmp(key, "annibale_reboot_lk")) { *(uint32_t *)out = enabled; return present; }
    if (!strcmp(key, "cpus")) { *(uint32_t *)out = cpus; return true; }
    if (!strcmp(key, "touchscreen") && conflict) { *(uint32_t *)out = 1; return true; }
    return false;
}
static vm_offset_t ml_io_map(vm_offset_t address, unsigned length) {
    maps++;
    assert((address == ARB_CORE_PHYS && length == ARB_CORE_SIZE) ||
           (address == ARB_CNFG_PHYS && length == ARB_CNFG_SIZE) ||
           (address == ARB_OBSRV_PHYS && length == ARB_OBSRV_SIZE) ||
           (address == ARB_CHNLS_PHYS + 0x3000 && length == 0x1000));
    return maps == map_fail ? 0 : address;
}
static uint32_t rd(vm_offset_t address) {
    if (address == ARB_CORE_PHYS + ARB_VERSION) return version;
    if (address == ARB_CORE_PHYS + ARB_FEATURES) return count;
    if (address >= ARB_CORE_PHYS + 0x2000 && address < ARB_CORE_PHYS + ARB_CORE_SIZE) {
        unsigned apid = (address - ARB_CORE_PHYS - 0x2000) / 4;
        return apid == 3 ? map_ppid << 8 : 0;
    }
    if (address == ARB_CNFG_PHYS + APID_OWNER(3)) return map_owner;
    if (address == ARB_OBSRV_PHYS + 0x20 * 3 + CH_STATUS)
        return reads == read_fail ? read_status : STATUS_DONE;
    if (address == ARB_OBSRV_PHYS + 0x20 * 3 + CH_RDATA0)
        return memory[read_register] ^ (reads == wrong_read ? 2 : 0);
    if (address == ARB_CHNLS_PHYS + 0x3000 + CH_STATUS)
        return writes == write_fail ? write_status : STATUS_DONE;
    assert(!"unexpected MMIO read"); return 0;
}
static void wr(vm_offset_t address, uint32_t value) {
    assert(lock_depth == 1);
    if (address == ARB_OBSRV_PHYS + 0x20 * 3 + CH_CMD) {
        assert((value >> 27) == OPC_EXT_READ && !(value & 7));
        read_register = (value >> 4) & 255;
        assert(read_register == 0x44 || read_register == 0x48);
        reads++; return;
    }
    if (address == ARB_CHNLS_PHYS + 0x3000 + 0x10) {
        assert(value <= 255); write_data = value; return;
    }
    if (address == ARB_CHNLS_PHYS + 0x3000 + CH_CMD) {
        assert(value == CMD_REG(0x48));
        writes++;
        if (writes != write_fail) memory[0x48] = write_data;
        return;
    }
    assert(!"unexpected MMIO write");
}
static void loader_return(void) {
    returns++;
    if (!return_returns) longjmp(terminal, 1);
}
__attribute__((noreturn)) static void panic(const char *message) {
    (void)message; failures++; longjmp(terminal, 3);
}
static const char *
''' + function("spmi_read") + '\nstatic void\n' + function("find_channel") + '\n' + (
    ROOT / "xnu/files/bsd/dev/annibale_reboot_probe.inc"
).read_text() + r'''
static void setup(void) {
    maps = writes = reads = returns = failures = lock_depth = prepared = 0;
    map_fail = read_fail = write_fail = wrong_read = conflict = 0;
    version = ARB_VERSION_7; count = 4; map_ppid = 0x71; map_owner = 0;
    read_status = write_status = STATUS_DONE | STATUS_FAILURE;
    ticks = 0; present = identity = true; return_returns = should_prepare = false;
    enabled = cpus = 1; loader_stub_slot[1] = 1;
    memset(memory, 0xa5, sizeof(memory)); memory[0x44] = 4;
}
static int run(void) {
    cases++;
    int result = setjmp(terminal);
    if (!result) annibale_reboot_probe();
    assert(!lock_depth && prepared == should_prepare);
    for (unsigned i = 0; i < sizeof(memory); i++)
        if (i != 0x44 && i != 0x48) assert(memory[i] == 0xa5);
    return result;
}
int main(void) {
    setup(); present = false; assert(run() == 0 && !maps && !writes);
    setup(); identity = false; assert(run() == 1 && !maps && !writes);
    setup(); enabled = 2; assert(run() == 1 && !maps);
    setup(); cpus = 2; assert(run() == 1 && !maps);
    setup(); conflict = 1; assert(run() == 1 && !maps);
    setup(); loader_stub_slot[1] = 0; assert(run() == 3 && !maps);
    setup(); version = 0x80000000; assert(run() == 1 && !writes);
    setup(); version = 0x50000000; assert(run() == 1 && !writes);
    setup(); count = 0; assert(run() == 1 && !writes);
    setup(); count = APID_MAX + 1; assert(run() == 1 && !writes);
    setup(); map_owner = 1; assert(run() == 1 && !reads && !writes);
    setup(); map_ppid = 0x171; assert(run() == 1 && !reads && !writes);
    setup(); map_ppid = 0; assert(run() == 1 && !reads && !writes);
    setup(); memory[0x44] = 0; assert(run() == 1 && reads == 1 && !writes);
    for (unsigned i = 1; i <= 4; i++) {
        setup(); map_fail = i; assert(run() == 1 && !writes);
    }
    for (unsigned i = 1; i <= 3; i++) {
        setup(); read_fail = i; assert(run() == 1);
        assert(writes == (i == 3));
    }
    const unsigned errors[] = {0, STATUS_FAILURE, STATUS_DENIED, STATUS_DROPPED};
    for (unsigned i = 0; i < 4; i++) {
        setup(); write_fail = 1; write_status = errors[i] ? STATUS_DONE | errors[i] : 0;
        assert(run() == 1 && writes == 1);
        setup(); read_fail = 1; read_status = errors[i] ? STATUS_DONE | errors[i] : 0;
        assert(run() == 1 && !writes);
    }
    setup(); wrong_read = 3; assert(run() == 1 && writes == 1);
    for (unsigned i = 0; i < 256; i++) {
        setup(); memory[0x48] = i; should_prepare = true;
        assert(run() == 1 && writes == 1 && returns == 1 && prepared == 1);
        assert(memory[0x48] == ((i & 1) | 4));
    }
    setup(); identity = false; return_returns = true;
    assert(run() == 3 && returns == 1 && failures == 1 && !maps);
    fprintf(stdout, "annibale reboot probe: %u cases passed\n", cases);
}
'''

with tempfile.TemporaryDirectory(prefix="annibale-reboot-test-") as directory:
    cfile = Path(directory) / "test.c"
    binary = Path(directory) / "test"
    cfile.write_text(source)
    subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-Wno-format", "-std=c11",
                    "-fsanitize=address,undefined", str(cfile), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
