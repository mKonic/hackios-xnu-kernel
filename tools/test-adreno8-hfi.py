#!/usr/bin/env python3
"""Compile the production HFI queue and sender functions against simulated GMU memory."""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DRIVER = (ROOT / "xnu/files/bsd/dev/adreno8_hfi.c").read_text()
HEADER = ROOT / "xnu/files/bsd/dev/adreno8_wire_private.h"
FUNCTIONS = DRIVER[DRIVER.index("static unsigned adreno8_hfi_seq;"):DRIVER.index("/* ---- step 12:")]
SOURCE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include "adreno8_wire_private.h"
typedef uintptr_t vm_offset_t;
struct block { vm_offset_t base; };
static uint32_t table[1024], command[1024], response[1024], debug[1024], regs[4];
static struct block adreno8_gmu;
static vm_offset_t adreno8_hfi_view_table, adreno8_hfi_view_cmd;
static vm_offset_t adreno8_hfi_view_resp, adreno8_hfi_view_debug;
static unsigned adreno8_step, scenario, sleeps, failures, response_reads, barriers;
static uint64_t clock_ns;
static uint32_t sent_header;
#define GMU_HOST2GMU_SET 0
#define GMU_GMU2HOST_INFO 4
#define GMU_GMU2HOST_CLR 8
#define GMU_FW_INIT_RESULT 12
#define GMU_FW_FAILED 0xe00
static int adreno8_say(const char *format, ...) { return 0; }
#define printf adreno8_say
static void append(unsigned queue, const uint32_t *msg, unsigned words) {
    uint32_t *h = table + ADRENO8_HFI_HEADER(queue);
    uint32_t *data = queue == 1 ? response : debug;
    for (unsigned i = 0; i < words; i++) data[(h[11] + i) % 1024] = msg[i];
    h[11] = (h[11] + ((words + 3) & ~3u)) % 1024;
}
static void ack(unsigned queue, uint32_t echo, unsigned words, uint32_t status) {
    uint32_t msg[3] = {(1u << 16) | (words << 8), echo, status};
    append(queue, msg, words);
}
static void emit_debug(void) {
    const uint32_t msg[4] = {(4u << 8) | 101, 1, 2, 3};
    append(2, msg, 4);
}
static uint32_t adreno8_rd(vm_offset_t address) {
    if (address >= (uintptr_t)response && address < (uintptr_t)(response + 1024)) response_reads++;
    return *(uint32_t *)address;
}
static void adreno8_wr(vm_offset_t address, uint32_t value) {
    *(uint32_t *)address = value;
    if (address != adreno8_gmu.base + GMU_HOST2GMU_SET) return;
    assert(barriers >= 2);
    sent_header = command[0];
    switch (scenario) {
    case 1: ack(1, sent_header, 3, 0); break;
    case 2: {
        uint32_t fake[3] = {(3u << 8) | 101, sent_header, 0};
        append(1, fake, 3); break;
    }
    case 3: ack(1, sent_header ^ 1, 3, 0); break;
    case 4: ack(1, sent_header, 3, 7); break;
    case 5: {
        uint32_t error[2] = {(2u << 8) | 100, 0x1234};
        append(2, error, 2); ack(1, sent_header, 3, 0); break;
    }
    case 6: emit_debug(); break;
    case 7: regs[1] = 1u << 15; break;
    case 8: ack(1, sent_header, 2, 0); break;
    case 9: ack(1, sent_header ^ 0x100, 3, 0); break;
    case 10: ack(1, sent_header | (1u << 19), 3, 0); break;
    case 11: ack(2, sent_header, 3, 0); break;
    case 12: regs[3] = GMU_FW_FAILED; break;
    }
}
static void adreno8_settle(void) { barriers++; }
static uint64_t mach_absolute_time(void) { return clock_ns; }
static void absolutetime_to_nanoseconds(uint64_t t, uint64_t *ns) { *ns = t; }
static void adreno8_sleep_us(uint32_t us) {
    clock_ns += (uint64_t)us * 1000; sleeps++;
    if (scenario == 6) emit_debug();
    if ((scenario == 2 || scenario == 3 || scenario == 9 || scenario == 11) && sleeps == 1)
        ack(1, sent_header, 3, 0);
}
static void adreno8_gmu_post_mortem(void) { failures++; }
static bool adreno8_hwsched_request(const uint32_t *request, uint32_t words, uint32_t *reply, uint32_t *reply_words) { return false; }
''' + FUNCTIONS + r'''
static void reset(unsigned test) {
    memset(command, 0, sizeof(command)); memset(response, 0, sizeof(response));
    memset(debug, 0, sizeof(debug)); memset(regs, 0, sizeof(regs));
    adreno8_hfi_init_table(table, 0x60006000);
    adreno8_gmu.base = (uintptr_t)regs;
    adreno8_hfi_view_table = (uintptr_t)table;
    adreno8_hfi_view_cmd = (uintptr_t)command;
    adreno8_hfi_view_resp = (uintptr_t)response;
    adreno8_hfi_view_debug = (uintptr_t)debug;
    scenario = test; sleeps = failures = response_reads = barriers = 0;
    clock_ns = 0;
}
int main(void) {
    uint32_t msg[255] = {0x301, 0x10203040, 0x50607080}, output[257];
    uint32_t *cmd = table + ADRENO8_HFI_HEADER(0);
    uint32_t *resp = table + ADRENO8_HFI_HEADER(1);
    reset(0);
    assert(table[1] == 210 && table[2] == 6 && table[3] == 12 && table[4] == 3 && table[5] == 3);
    assert(cmd[1] == 0x60007000 && resp[1] == 0x60008000);
    assert(table[ADRENO8_HFI_HEADER(2) + 1] == 0x60009000);
    for (unsigned q = 0; q < 3; q++) assert(table[ADRENO8_HFI_HEADER(q) + 2] == q);
    for (unsigned i = ADRENO8_HFI_HEADER(3); i < 210; i++) assert(table[i] == 0);
    assert(adreno8_queue_write(msg, 3));
    assert(cmd[11] == 4 && command[0] == msg[0] && command[2] == msg[2] && command[3] == 0xfafafafa);
    reset(0); cmd[10] = 4;
    assert(!adreno8_queue_write(msg, 3) && cmd[11] == 0); /* Four free words cannot consume the empty marker. */
    reset(0); cmd[10] = 8; cmd[11] = 1020;
    assert(adreno8_queue_write(msg, 5));
    assert(cmd[11] == 4 && command[1020] == msg[0] && command[0] == msg[4] && command[3] == 0xfafafafa);
    reset(0); cmd[11] = 1024; assert(!adreno8_queue_write(msg, 3));
    reset(0); cmd[11] = 1; assert(!adreno8_queue_write(msg, 3));
    reset(0); cmd[3] = 0; assert(!adreno8_queue_write(msg, 3));
    reset(0); assert(!adreno8_queue_write(msg, 0) && !adreno8_queue_write(msg, 256));
    reset(0); resp[10] = resp[11] = 1020; msg[0] = 5u << 8;
    append(1, msg, 5); memset(output, 0x5a, sizeof(output));
    assert(adreno8_queue_read(1, output) == 5 && response_reads == 5);
    assert(resp[10] == 4 && output[5] == 0x5a5a5a5a);
    reset(0); response[0] = 0; resp[11] = 4;
    assert(adreno8_queue_read(1, output) == -1 && resp[10] == 0);
    reset(0); response[0] = 5u << 8; resp[11] = 4;
    assert(adreno8_queue_read(1, output) == -1 && resp[10] == 0);
    reset(0); resp[10] = 1024; assert(adreno8_queue_read(1, output) == -1);
    reset(0); msg[0] = 255u << 8; append(1, msg, 255);
    output[255] = 0xdeadbeef;
    assert(adreno8_queue_read(1, output) == 255 && response_reads == 255 && output[255] == 0xdeadbeef);
    reset(1); adreno8_hfi_seq = 4094;
    assert(adreno8_hfi_send(14, NULL, 0, "test") && adreno8_hfi_seq == 4095 && response_reads == 3);
    reset(1); assert(adreno8_hfi_send(14, NULL, 0, "test") && adreno8_hfi_seq == 0);
    for (unsigned i = 2; i <= 3; i++) {
        reset(i); assert(adreno8_hfi_send(14, NULL, 0, "test") && sleeps == 1);
    }
    for (unsigned i = 4; i <= 8; i++) {
        reset(i); assert(!adreno8_hfi_send(14, NULL, 0, "test"));
        assert(clock_ns <= 1000000000ULL);
        if (i == 6) assert(sleeps == 10000 && failures == 1);
    }
    reset(9); assert(adreno8_hfi_send(14, NULL, 0, "test") && sleeps == 1);
    reset(10); assert(adreno8_hfi_send(14, NULL, 0, "test") && sleeps == 0);
    reset(11); assert(adreno8_hfi_send(14, NULL, 0, "test") && sleeps == 1);
    reset(12); assert(!adreno8_hfi_send(14, NULL, 0, "test") && failures == 1);
    reset(0); assert(!adreno8_hfi_send(14, NULL, 0, "test") && sleeps == 10000);
    assert(!adreno8_hfi_send(256, NULL, 0, "test"));
    assert(!adreno8_hfi_send(14, NULL, 1, "test"));
    assert(!adreno8_hfi_send(14, msg, UINT32_MAX, "test"));
    assert(!adreno8_hfi_send(14, msg, 255, "test"));
    puts("HFI layout, wrap, space, lengths, ACK identity, async errors and bounded waits: PASS");
}
'''
STAGE_FUNCTIONS = DRIVER[DRIVER.index("bool\nadreno8_step_queues("):DRIVER.index("/* ---- step 16:")]
private = (ROOT / "xnu/files/bsd/dev/adreno8_private.h").read_text()
bcm_macro_start = private.index("#define BCM_TCS_CMD(")
bcm_macro_end = private.index("\n#define", bcm_macro_start)
BCM_MACRO = private[bcm_macro_start:bcm_macro_end]
BW_DEFINE = re.search(r"^#define BW_COUNT\s+\d+", private, re.M).group()
cx = (ROOT / "xnu/files/bsd/dev/adreno8_cx.c").read_text()
BW_DATA = re.search(r"const uint32_t adreno8_bw_kb\[BW_COUNT\] = \{.*?\n};", cx, re.S).group().replace("const uint32_t", "static uint32_t")
BCM_DATA = re.search(r"const struct adreno8_bcm adreno8_bcms\[\] = \{.*?\n};", cx, re.S).group().replace("const struct", "static const struct")
STAGE_SOURCE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include "adreno8_wire_private.h"
typedef uintptr_t vm_offset_t;
struct block { vm_offset_t base; };
struct adreno8_bcm_db { uint32_t unit; uint16_t width; uint8_t vcd, reserved; };
struct adreno8_bcm { const char *name; uint32_t buswidth; bool fixed; uint32_t perfmode, perfmode_bw; };
static struct block adreno8_gmu;
static uint32_t adreno8_step, adreno8_gx_count = 2;
static uint32_t adreno8_speed_asked;
static bool adreno8_gpu8_keep, adreno8_gpu8_return, adreno8_reset_required;
static bool adreno8_hfi_tables_sent, hwsched_ready = true;
static bool adreno8_hwsched_ready(void) { return hwsched_ready; }
static uint32_t adreno8_gx_hz[17] = {0, 125000000}, adreno8_gx_level[17] = {0, 128};
static uint32_t adreno8_gx_votes[17], adreno8_gx_dep_votes[17], adreno8_cx_votes[3];
static const uint32_t adreno8_gmu_hz[3] = {0, 200000000, 500000000};
static const uint32_t adreno8_gmu_level[3] = {0, 64, 128};
''' + BW_DEFINE + "\n" + BW_DATA + "\n" + BCM_DATA + r'''
static struct adreno8_bcm_db aux[3] = {{4800000, 64, 0, 0}, {4800000, 16, 1, 0}, {4800000, 0, 1, 0}};
static bool adreno8_gmu_runs = true, adreno8_gmu_talks, wait_ok = true, vote_ok = true;
static vm_offset_t adreno8_hfi_view_table = 1, adreno8_hfi_view_cmd = 1;
static vm_offset_t adreno8_hfi_view_resp = 1, adreno8_hfi_view_debug = 1;
static uint32_t version = 0x20070001, mask_written, sent[3][255], ids[3], lengths[3], send_count, fail_id;

#define BCM_COUNT 3
#define GMU_GMU2HOST_CLR 0
#define GMU_GMU2HOST_MASK 4
#define GMU_HFI_CTRL_INIT 8
#define GMU_HFI_CTRL_STATUS 12
#define GMU_DTCM_VERSIONS 16
#define GMU_PWR_CLK_STATUS 36
#define GMU_RPMH_POWER_STATE 40
#define GMU_PWR_GX_UP(value) (((value) & 3) == 0)
static int adreno8_say(const char *format, ...) { return 0; }
#define printf adreno8_say
static bool adreno8_cx_ready(void) { return true; }
static void adreno8_announce(const char *format, ...) {}
static uint32_t adreno8_rd(vm_offset_t address) { return 0; }
static void adreno8_put(struct block *b, const char *name, uint32_t offset, uint32_t value) {
    if (offset == GMU_GMU2HOST_MASK) mask_written = value;
}
static uint32_t adreno8_get(struct block *b, const char *name, uint32_t offset) {
    return offset == GMU_DTCM_VERSIONS + 16 ? version : 0;
}
static bool adreno8_wait_for(struct block *b, unsigned off, unsigned mask, unsigned value, unsigned ms) { return wait_ok; }
static void adreno8_gmu_post_mortem(void) {}
static bool adreno8_arc_vote(const char *a, const char *b, uint32_t level, uint32_t *out) { *out = level + 100; return vote_ok; }
static bool adreno8_dep_vote(uint32_t level, uint32_t *out) { *out = level + 200; return vote_ok; }
static uint32_t adreno8_rpmh_lookup(const char *name, void *levels, void *count, const uint8_t **data, unsigned *length) {
    unsigned i;
    if (!strcmp(name, "CN0")) return 0x2000;
    for (i = 0; i < 3; i++) if (!strcmp(name, adreno8_bcms[i].name)) break;
    if (i == 3) return 0;
    if (data) *data = (const uint8_t *)&aux[i];
    if (length) *length = sizeof(aux[i]);
    return 0x3000 + i * 4;
}
static bool adreno8_hfi_send(uint32_t id, const uint32_t *payload, uint32_t words, const char *what) {
    assert(send_count < 3 && words <= 255);
    if (id == 30) assert(adreno8_reset_required);
    ids[send_count] = id; lengths[send_count] = words;
    if (words) memcpy(sent[send_count], payload, words * 4);
    send_count++;
    return id != fail_id;
}
''' + BCM_MACRO + "\n" + STAGE_FUNCTIONS + r'''
int main(void) {
    assert(adreno8_step_queues() && adreno8_gmu_talks && mask_written == UINT32_MAX);
    adreno8_gmu_talks = false; version = 0x20060000;
    assert(!adreno8_step_queues() && !adreno8_gmu_talks);
    version = 0x30000000; assert(!adreno8_step_queues());
    version = 0x20060001; assert(adreno8_step_queues());
    adreno8_gmu_talks = false; adreno8_hfi_view_debug = 0;
    assert(!adreno8_step_queues()); adreno8_hfi_view_debug = 1;
    wait_ok = false; assert(!adreno8_step_queues()); wait_ok = true;
    adreno8_gmu_talks = true;
    assert(adreno8_step_tables() && send_count == 2);
    assert(ids[0] == 15 && lengths[0] == 18);
    assert(sent[0][0] == 0 && sent[0][1] == 1 && sent[0][2] == 2 && sent[0][3] == 3);
    assert(sent[0][4] == 100 && sent[0][5] == 200 && sent[0][6] == 0);
    assert(sent[0][7] == 228 && sent[0][8] == 328 && sent[0][9] == 125000);
    assert(sent[0][10] == 3 && sent[0][11] == 2 && sent[0][16] == 228 && sent[0][17] == 500000);
    assert(ids[1] == 3 && lengths[1] == 159);
    assert(sent[1][0] == BW_COUNT && sent[1][1] == 1 && sent[1][2] == 3 && sent[1][3] == 1 && sent[1][4] == 5);
    assert(sent[1][5] == 0x2000 && sent[1][23] == 0x3000 && sent[1][25] == 0x3008);
    assert(sent[1][31] == 0x40000000 && sent[1][32] == 0 && sent[1][33] == 0x40000000);
    assert(sent[1][31 + 8 + 0] == 0x6000028b && sent[1][31 + 8 + 1] == 0x2000028b);
    for (unsigned i = 1; i < BW_COUNT; i++) {
        assert(((sent[1][31 + i * 8] >> 14) & 0x3fff) == 0);
        assert(((sent[1][32 + i * 8] >> 14) & 0x3fff) == 0);
        assert(sent[1][33 + i * 8] == (i < 7 ? 0x60000000u : 0x60000004u));
    }
    /* Exercise values immediately below, at and above the ACV threshold. */
    assert(adreno8_bcms[2].perfmode_bw == 10687500);
    adreno8_bw_kb[6] = 10687499; adreno8_bw_kb[7] = 10687500; adreno8_bw_kb[8] = 10687501;
    assert(adreno8_fill_bw_votes());
    assert(adreno8_bw_votes[6][2] == 0x60000000 && adreno8_bw_votes[7][2] == 0x60000004 && adreno8_bw_votes[8][2] == 0x60000004);
    assert(adreno8_bw_votes[7][0] == 0x600022ca && adreno8_bw_votes[7][1] == 0x200022ca);
    adreno8_bw_kb[1] = 1; adreno8_bw_kb[2] = UINT32_MAX;
    assert(adreno8_fill_bw_votes());
    assert(adreno8_bw_votes[1][0] == 0x60000001 && adreno8_bw_votes[1][1] == 0x20000001);
    assert(adreno8_bw_votes[2][0] == 0x60003fff && adreno8_bw_votes[2][1] == 0x20003fff);
    assert(sent[1][158] == 0);
    send_count = 0; adreno8_gx_count = 18;
    assert(!adreno8_step_tables() && send_count == 0); adreno8_gx_count = 2;
    aux[0].unit = 0; assert(!adreno8_step_tables() && send_count == 1); aux[0].unit = 4800000;
    send_count = 0; vote_ok = false;
    assert(!adreno8_step_tables() && send_count == 0); vote_ok = true;
    send_count = fail_id = 0;
    assert(!adreno8_step_gx_power() && send_count == 0 && !adreno8_reset_required);
    adreno8_gpu8_keep = adreno8_gpu8_return = true;
    assert(!adreno8_step_gx_power() && send_count == 0);
    adreno8_gpu8_return = false; adreno8_speed_asked = 2;
    assert(!adreno8_step_gx_power() && send_count == 0);
    adreno8_speed_asked = 1; adreno8_gx_hz[1] = 200000000;
    assert(!adreno8_step_gx_power() && send_count == 0);
    adreno8_gx_hz[1] = 125000000;
    hwsched_ready = false; assert(!adreno8_step_gx_power() && send_count == 0);
    hwsched_ready = true;
    assert(adreno8_step_gx_power() && send_count == 1 && adreno8_reset_required);
    assert(ids[0] == 30 && lengths[0] == 3 && sent[0][0] == 1 && sent[0][1] == 1 && sent[0][2] == 2);
    send_count = 0; fail_id = 30; adreno8_reset_required = false;
    assert(!adreno8_step_gx_power() && adreno8_reset_required);
    puts("HFI version, perf/BW payloads, ACV threshold, AB=0 and minimum DCVS: PASS");
}
'''

for source in (SOURCE, STAGE_SOURCE):
    with tempfile.TemporaryDirectory(prefix="adreno8-hfi-") as directory:
        src = Path(directory) / "check.c"
        binary = Path(directory) / "check"
        src.write_text(source)
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Wno-unused-parameter", "-fsanitize=address,undefined", "-I", str(HEADER.parent), str(src), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
