#!/usr/bin/env python3
"""Execute production sequential CX/MX ARC reads with tracked MMIO and retained failures."""
from pathlib import Path
import argparse
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
PRODUCTION = (DEV / "adreno8_rpmh.c").read_text()
HEADER = r'''
#ifndef _TEST_ADRENO8_PRIVATE_H
#define _TEST_ADRENO8_PRIVATE_H
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uintptr_t vm_offset_t;
struct adreno8_block { const char *name; uint32_t phys, size; vm_offset_t base; };
#define RPMH_LEVELS 64
static bool adreno8_reset_required;
static uint32_t adreno8_gmu_from_pll;
static uint32_t adreno8_step = 5, adreno8_newest_step = 5;
static bool PE_parse_boot_argn(const char *, void *, unsigned);
static uint32_t adreno8_get(struct adreno8_block *, const char *, uint32_t);
static void adreno8_put(struct adreno8_block *, const char *, uint32_t, uint32_t);
static uint32_t adreno8_rd(vm_offset_t);
static void adreno8_settle(void);
static void adreno8_sleep_us(uint32_t);
static uint32_t adreno8_rpmh_lookup(const char *, uint16_t *, unsigned *, const uint8_t **, unsigned *);
static int adreno8_say(const char *format, ...) { (void)format; return 0; }
#endif
'''
HARNESS = r'''
#include "adreno8_rpmh.c"
#undef printf
static uint32_t regs[0x10000 / 4], before[0x10000 / 4];
static unsigned scenario, fail_at, reads, writes, polls, transaction_polls, sleeps, responses, triggers, lookups;
static bool flag_present = true, touch_present = true, triggered, done, poisoned_poll;
static uint32_t flag = 1, touch, cx_address = 0x30000, level_count = 11, response_value = 3;
static uint32_t mx_address = 0x30004, mx_level_count = 9, mx_response_value = 5;
static uint32_t initial_control = ARC_AMC_ENABLE, initial_irq = 1;
static bool PE_parse_boot_argn(const char *name, void *out, unsigned size) {
    assert(size == sizeof(uint32_t));
    if (!strcmp(name, "adreno8_arc_read")) {
        if (flag_present) *(uint32_t *)out = flag;
        return flag_present;
    }
    assert(!strcmp(name, "touchscreen"));
    if (touch_present) *(uint32_t *)out = touch;
    return touch_present;
}
static void adreno8_settle(void) {}
static void adreno8_sleep_us(uint32_t us) { assert(us == 100); sleeps++; }
static uint32_t adreno8_rpmh_lookup(const char *name, uint16_t *levels, unsigned *count,
                                  const uint8_t **data, unsigned *length) {
    assert(!data && !length && lookups < 2);
    assert(!strcmp(name, lookups ? "mx.lvl" : "cx.lvl"));
    if (lookups) {
        assert(writes == 14 && triggers == 1 && responses == 1 && done);
        assert(regs[ARC_CONTROL / 4] == initial_control && !regs[ARC_ENABLE / 4]);
        assert(!(regs[ARC_IRQ_STATUS / 4] & ARC_TCS_BIT));
        assert(regs[ARC_IRQ_ENABLE / 4] == (initial_irq | (scenario == 28 ? 1u << 5 : 0)));
        if (scenario == 50) regs[0x1268 / 4] = 0;
        if (scenario == 51) regs[ARC_IRQ_STATUS / 4] |= ARC_TCS_BIT;
        if (scenario == 52) regs[0] = 0x30700;
    }
    lookups++;
    *count = lookups == 1 ? level_count : mx_level_count;
    for (unsigned i = 0; i < *count && i < RPMH_LEVELS; i++) levels[i] = i * 16;
    return lookups == 1 ? cx_address : mx_address;
}
static uint32_t adreno8_get(struct adreno8_block *b, const char *name, uint32_t off) {
    assert(!poisoned_poll);
    assert(b->phys == 0x16520000 && b->size == 0x10000 && off < b->size && !(off & 3));
    b->base = 0x100000; reads++;
    if ((scenario == 56 && !strcmp(name, "irq enable before restore")) ||
        (scenario == 57 && !strcmp(name, "irq status after request")) ||
        (scenario == 58 && off == ARC_COMMAND_STATUS)) return UINT32_MAX;
    if ((scenario == 27 || (scenario == 47 && lookups == 2)) &&
        !strcmp(name, "irq enable before request"))
        regs[ARC_IRQ_ENABLE / 4] |= 1u << 5;
    if ((scenario == 28 || (scenario == 54 && lookups == 2)) &&
        !strcmp(name, "irq enable before restore"))
        regs[ARC_IRQ_ENABLE / 4] |= 1u << 5;
    if ((scenario == 30 || (scenario == 49 && lookups == 2)) &&
        !strcmp(name, "TCS1 control after completion"))
        regs[ARC_CONTROL / 4] |= 2;
    if (off == ARC_RESPONSE) { assert(done); responses++; }
    return regs[off / 4];
}
static void adreno8_put(struct adreno8_block *b, const char *name, uint32_t off, uint32_t value) {
    (void)name;
    assert(!poisoned_poll && adreno8_reset_required && b->phys == 0x16520000);
    assert(off == ARC_CONTROL || off == ARC_ENABLE || off == ARC_MSGID ||
           off == ARC_ADDRESS || off == ARC_IRQ_ENABLE || off == ARC_IRQ_CLEAR);
    writes++;
    if (off == ARC_MSGID) assert(value == 0x108);
    if (off == ARC_ADDRESS) assert(value == (lookups == 1 ? cx_address : mx_address) && (value >> 16) == 3);
    if (fail_at == writes) { regs[off / 4] = value ^ 1; return; }
    if (off == ARC_IRQ_CLEAR) {
        assert(done && value == ARC_TCS_BIT);
        if (scenario != 29 && !(scenario == 48 && lookups == 2)) regs[ARC_IRQ_STATUS / 4] &= ~value;
        return;
    }
    if (off == ARC_CONTROL && value == (ARC_AMC_ENABLE | ARC_AMC_TRIGGER)) {
        assert((!triggered || done) && regs[ARC_CONTROL / 4] == ARC_AMC_ENABLE);
        assert(regs[ARC_MSGID / 4] == 0x108 &&
            regs[ARC_ADDRESS / 4] == (lookups == 1 ? cx_address : mx_address));
        assert(regs[ARC_ENABLE / 4] == 1 && regs[ARC_WAIT / 4] == 0);
        assert(regs[ARC_IRQ_ENABLE / 4] & ARC_TCS_BIT);
        triggered = true; done = false; transaction_polls = 0; triggers++;
    } else if (triggered && !done) {
        assert(!"cleanup before confirmed completion");
    }
    regs[off / 4] = value;
}
static uint32_t adreno8_rd(vm_offset_t address) {
    assert(address == 0x100000 + ARC_IRQ_STATUS && triggered);
    reads++; polls++; transaction_polls++;
    if (scenario == 59) { poisoned_poll=true; return UINT32_MAX; }
    if (transaction_polls == 3 && scenario != 23 && !(scenario == 43 && lookups == 2)) {
        done = true;
        regs[ARC_IRQ_STATUS / 4] |= ARC_TCS_BIT | 4;
        regs[ARC_COMMAND_STATUS / 4] = scenario == 24 || (scenario == 44 && lookups == 2) ?
            1u << 8 : ARC_COMMAND_DONE;
        regs[ARC_STATUS / 4] = scenario == 25 || (scenario == 45 && lookups == 2) ? 0 : 1;
        regs[ARC_RESPONSE / 4] = lookups == 1 ? response_value : mx_response_value;
    }
    return regs[ARC_IRQ_STATUS / 4];
}
static void prepare(void) {
    assert(ARC_WAIT == 0xfc0 && ARC_CONTROL == 0xfc4 && ARC_STATUS == 0xfc8 && ARC_ENABLE == 0xfcc);
    assert(ARC_MSGID == 0xfd4 && ARC_ADDRESS == 0xfd8 && ARC_DATA == 0xfdc);
    assert(ARC_COMMAND_STATUS == 0xfe0 && ARC_RESPONSE == 0xfe4 && ARC_TCS_BIT == 2);
    regs[0] = 0x30800; regs[1] = 0x04010100; regs[3] = 0x800c8104;
    regs[ARC_STATUS / 4] = 1; regs[ARC_CONTROL / 4] = initial_control;
    regs[0xd28 / 4] = regs[0x1268 / 4] = 1;
    regs[ARC_IRQ_ENABLE / 4] = initial_irq; regs[ARC_DATA / 4] = 0xdeadbeef;
    regs[0xd2c / 4] = 0x7f; regs[0xfd0 / 4] = 0x5a5a;
    switch (scenario) {
    case 0: flag_present = false; break;
    case 1: flag = 0; break;
    case 2: flag = 2; break;
    case 3: flag = UINT32_MAX; break;
    case 4: touch_present = false; break;
    case 5: touch = 2; break;
    case 6: regs[0] = 0x30700; break;
    case 7: regs[1] |= 1u << 24; break;
    case 8: regs[3] = 16u << 27 | 7u << 12; break;
    case 9: regs[3] = 8u << 12; break;
    case 10: regs[ARC_STATUS / 4] = 0; break;
    case 11: regs[ARC_ENABLE / 4] = 1; break;
    case 12: regs[ARC_WAIT / 4] = 1; break;
    case 13: regs[ARC_IRQ_STATUS / 4] = ARC_TCS_BIT; break;
    case 14: regs[ARC_CONTROL / 4] |= ARC_AMC_TRIGGER; break;
    case 15: regs[ARC_CONTROL / 4] |= 2; break;
    case 16: cx_address = 0; break;
    case 17: cx_address = 0x40000; break;
    case 18: level_count = 0; break;
    case 19: level_count = 65; break;
    case 21: initial_control = regs[ARC_CONTROL / 4] = 0; break;
    case 22: initial_irq = regs[ARC_IRQ_ENABLE / 4] = 3; break;
    case 26: response_value = level_count; break;
    case 33: response_value = 0; break;
    case 34: regs[0] = UINT32_MAX; break;
    case 35: regs[0xd28 / 4] = 0; break;
    case 36: regs[0x1268 / 4] = 0; break;
    case 37: regs[ARC_IRQ_ENABLE / 4] = UINT32_MAX; break;
    case 38: mx_address = 0; break;
    case 39: mx_address = 0x40000; break;
    case 40: mx_address = 0x30005; break;
    case 41: mx_level_count = 0; break;
    case 42: mx_level_count = 65; break;
    case 46: mx_response_value = mx_level_count; break;
    case 53: cx_address = 0x30001; break;
    case 55: mx_response_value = 0; break;
    }
    memcpy(before, regs, sizeof(regs));
}
int main(int argc, char **argv) {
    assert(argc >= 2); scenario = strtoul(argv[1], NULL, 0);
    if (argc == 3) fail_at = strtoul(argv[2], NULL, 0);
    prepare();
    bool result = adreno8_rpmh_arc_read();
    bool success = scenario == 20 || scenario == 21 || scenario == 22 || scenario == 28 ||
        scenario == 33 || scenario == 54 || scenario == 55;
    if (fail_at) assert(!result && writes == fail_at && adreno8_reset_required);
    else if (scenario <= 1) assert(result && !writes && !reads && !lookups);
    else if (success) {
        assert(result && writes == 28 && triggers == 2 && responses == 2 && lookups == 2 && adreno8_reset_required);
        assert(regs[ARC_CONTROL / 4] == initial_control && !regs[ARC_ENABLE / 4] && !regs[ARC_WAIT / 4]);
        assert(regs[ARC_IRQ_ENABLE / 4] == (initial_irq | (scenario == 28 || scenario == 54 ? 1u << 5 : 0)));
        assert(regs[ARC_IRQ_STATUS / 4] == 4);
    } else {
        assert(!result);
        if (scenario <= 19 || (scenario >= 34 && scenario <= 37) || scenario == 53)
            assert(!writes && !adreno8_reset_required);
        else if ((scenario >= 38 && scenario <= 42) || (scenario >= 50 && scenario <= 52))
            assert(writes == 14 && triggers == 1 && responses == 1 && lookups == 2 && adreno8_reset_required);
        else if (scenario == 47) assert(writes == 19 && triggers == 1 && responses == 1 && adreno8_reset_required);
        else if (scenario == 48) assert(writes == 26 && triggers == 2 && responses == 2 && adreno8_reset_required);
        else if (scenario == 56) assert(writes == 13 && triggers == 1 && responses == 1 && lookups == 1);
        else if (scenario >= 43 && scenario <= 49)
            assert(writes == 22 && triggers == 2 && lookups == 2 && adreno8_reset_required);
        else if (scenario == 27) assert(writes == 5 && !triggers && adreno8_reset_required);
        else if (scenario == 29) assert(writes == 12 && triggers == 1 && adreno8_reset_required);
        else assert(writes == 8 && triggers == 1 && adreno8_reset_required);
        if (scenario == 23) assert(polls == 100000 && sleeps == 100000 && !responses);
        if (scenario == 24 || scenario == 25) assert(!responses);
        if (scenario == 43) assert(polls == 100003 && sleeps == 100002 && responses == 1);
        if (scenario == 44 || scenario == 45) assert(responses == 1);
        if (scenario >= 57) assert(writes == 8 && triggers == 1 && !responses && lookups == 1);
        if (scenario == 59) assert(polls == 1 && !sleeps && !done);
        if (scenario >= 20 && scenario <= 30 && !success) assert(lookups == 1);
    }
    assert(regs[ARC_DATA / 4] == 0xdeadbeef);
    assert(regs[0xd2c / 4] == before[0xd2c / 4] && regs[0xfd0 / 4] == before[0xfd0 / 4]);
    unsigned saved_writes = writes, saved_reads = reads;
    if (scenario <= 1) assert(adreno8_rpmh_arc_read());
    else assert(!adreno8_rpmh_arc_read());
    assert(writes == saved_writes && reads == saved_reads);
}
'''


def run(source, directory, scenarios, reject=False):
    (directory / "adreno8_private.h").write_text(HEADER)
    (directory / "adreno8_rpmh_private.h").write_text((DEV / "adreno8_rpmh_private.h").read_text())
    (directory / "adreno8_rpmh.c").write_text(source)
    path, binary = directory / "check.c", directory / "check"
    path.write_text(HARNESS)
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-missing-field-initializers",
                    "-fsanitize=address,undefined", str(path), "-o", str(binary)], check=True)
    for args in scenarios:
        result = subprocess.run([str(binary), *map(str, args)], capture_output=True)
        if reject:
            assert result.returncode != 0, f"mutation survived {args}"
        else:
            result.check_returncode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()
    scenarios = [(s,) for s in range(60) if s not in (31, 32)]
    scenarios += [(20, n) for n in range(1, 29) if n not in (8, 12, 22, 26)]
    with tempfile.TemporaryDirectory(prefix="adreno8-arc-read-") as directory:
        directory = Path(directory)
        run(PRODUCTION, directory, scenarios)
        if args.mutations:
            mutations = [
                ("resource_write", "write ? 0x10108u : 0x108u", "write ? 0x10108u : 0x10108u", (20,)),
                ("command_data_write", "ARC_ADDRESS, address)", "0xfdcu, address)", (20,)),
                ("missing_reset", "adreno8_reset_required = true;", "adreno8_reset_required = false;", (20,)),
                ("repeat_allowed", "asked != 1 || arc_state != ARC_COLD", "asked != 1 || false", (20,)),
                ("touch_implicit_zero", '!PE_parse_boot_argn("touchscreen", &touch, sizeof(touch)) || touch != 0',
                 'PE_parse_boot_argn("touchscreen", &touch, sizeof(touch)) && touch != 0', (4,)),
                ("unknown_parameter", "asked != 1 || arc_state != ARC_COLD", "asked == 0 || arc_state != ARC_COLD", (2,)),
                ("response_from_data", 'arc_read("TCS1 ARC response", ARC_RESPONSE)',
                 'arc_read("TCS1 ARC response", 0xfdc)', (20,)),
                ("timeout_cleanup", 'printf("adreno8: ARC transaction completion unconfirmed;',
                 'arc_write("premature clear", ARC_ENABLE, 0); printf("adreno8: ARC transaction completion unconfirmed;', (23,)),
                ("irq_snapshot_restore", "irq & ~(ARC_TCS_BIT & ~irq_before)", "irq_before", (28,)),
                ("clear_peer_irq", "ARC_IRQ_CLEAR, ARC_TCS_BIT", "ARC_IRQ_CLEAR, 0xffffffffu", (20,)),
                ("incomplete_command", '(command & ARC_COMMAND_DONE) != ARC_COMMAND_DONE',
                 "false", (24,)),
                ("wrong_slot_offset", "#define ARC_MSGID 0xfd4u", "#define ARC_MSGID 0x1274u", (20,)),
                ("wrong_slot_irq", "#define ARC_TCS_BIT (1u << 1)", "#define ARC_TCS_BIT (1u << 2)", (20,)),
                ("tcs1_busy", 'arc_read("TCS1 status", 0xfc8) != 1', "false", (10,)),
                ("tcs2_busy", 'arc_read("TCS2 status", 0x1268) != 1', "false", (36,)),
                ("other_tcs_busy", 'arc_read("TCS0 status", 0xd28) != 1', "false", (35,)),
                ("skip_register_verify", "if (arc_read(name, offset) == value)", "if (true)", (20, 3)),
                ("commands_stay_enabled", '"TCS1 completed command disable", ARC_ENABLE, 0',
                 '"TCS1 completed command disable", ARC_ENABLE, 1', (20,)),
                ("restore_trigger", '"TCS1 restore initial control", ARC_CONTROL, control)',
                 '"TCS1 restore initial control", ARC_CONTROL, control | ARC_AMC_TRIGGER)', (20,)),
                ("stale_irq", '(arc_read("irq status", ARC_IRQ_STATUS) & ARC_TCS_BIT) != 0', "false", (13,)),
                ("read_mx_after_cx_failure", '!arc_resource_read("cx.lvl", &vote) || !arc_resource_read("mx.lvl", &vote)',
                 '(unsigned)!arc_resource_read("cx.lvl", &vote) | (unsigned)!arc_resource_read("mx.lvl", &vote)', (23,)),
                ("skip_mx", '|| !arc_resource_read("mx.lvl", &vote)', '|| false', (20,)),
            ]
            for name, old, new, scenario in mutations:
                assert PRODUCTION.count(old) == 1, name
                run(PRODUCTION.replace(old, new, 1), directory, [scenario], reject=True)
            print(f"ARC CX/MX failure mutations: {len(mutations)} rejected")
    print(f"ARC CX/MX production probe: {len(scenarios)} success, failure and replay cases PASS")


if __name__ == "__main__":
    main()
