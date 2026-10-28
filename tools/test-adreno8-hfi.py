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
static unsigned adreno8_step, scenario, sleeps, failures, response_reads, barriers, diagnostic_calls;
static unsigned memory_failures, feature_logs;
static bool adreno8_direct_ring;
static bool publication_pending;
static char feature_log[160];
static uint64_t clock_ns, publication_ns;
static uint32_t sent_header;
#define GMU_HOST2GMU_SET 0
#define GMU_GMU2HOST_INFO 4
#define GMU_GMU2HOST_CLR 8
#define GMU_FW_INIT_RESULT 12
#define GMU_FW_FAILED 0xe00
static int adreno8_say(const char *format, ...) {
    if (strstr(format, "HFI 11 ACK:")) {
        va_list args;
        va_start(args, format);
        vsnprintf(feature_log, sizeof(feature_log), format, args);
        va_end(args);
        feature_logs++;
    }
    return 0;
}
static void adreno8_announce(const char *format, ...) {
    assert(!publication_pending);
    (void)format; diagnostic_calls++; clock_ns += 2000000000ULL;
}
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
static void feature_ack(unsigned queue, uint32_t echo, uint32_t data, uint32_t error) {
    const uint32_t msg[4] = {(1u << 16) | (4u << 8), echo, data, error};
    append(queue, msg, 4);
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
    if (address == (uintptr_t)(table + ADRENO8_HFI_HEADER(0) + 11)) {
        publication_pending = true; publication_ns = clock_ns;
    }
    if (address != adreno8_gmu.base + GMU_HOST2GMU_SET) return;
    assert(publication_pending && clock_ns == publication_ns);
    publication_pending = false;
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
    case 13: feature_ack(1, sent_header, 0, 0); break;
    case 14: feature_ack(1, sent_header, 0, 6); break;
    case 15: feature_ack(1, sent_header, 0, 8); break;
    case 16: feature_ack(1, sent_header, 1, 0); break;
    case 17: feature_ack(1, sent_header, UINT32_MAX, 0); break;
    case 18: feature_ack(1, sent_header ^ (1u << 20), 0, 6); break;
    case 19: feature_ack(2, sent_header, 0, 8); break;
    case 20: feature_ack(1, sent_header | (1u << 19), 0, 0); break;
    case 21: feature_ack(1, sent_header, 0, 0x1234); break;
    case 22: ack(1, sent_header ^ (1u << 20), 3, 0); break;
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
    if ((scenario == 18 || scenario == 19 || scenario == 22) && sleeps == 1)
        feature_ack(1, sent_header, 0, 0);
}
#define SECURE_MODE_TRANSLATION_B 4u
static uint32_t adreno8_gpu8_secure_mode;
static bool adreno8_gpu_faulted(void) { return false; }
void adreno8_hwsched_memory_failed(void) { memory_failures++; }
static void adreno8_gmu_post_mortem(void) { failures++; }
static bool adreno8_hwsched_request(const uint32_t *request, uint32_t words, uint32_t *reply, uint32_t *reply_words) { return false; }
''' + FUNCTIONS + r'''
static void reset(unsigned test) {
    memset(command, 0, sizeof(command)); memset(response, 0, sizeof(response));
    memset(debug, 0, sizeof(debug)); memset(regs, 0, sizeof(regs));
    adreno8_hfi_init_table(table, 0x60006000, false);
    adreno8_gmu.base = (uintptr_t)regs;
    adreno8_hfi_view_table = (uintptr_t)table;
    adreno8_hfi_view_cmd = (uintptr_t)command;
    adreno8_hfi_view_resp = (uintptr_t)response;
    adreno8_hfi_view_debug = (uintptr_t)debug;
    scenario = test; sleeps = failures = response_reads = barriers = diagnostic_calls = 0;
    publication_pending = false;
    memory_failures = feature_logs = 0; feature_log[0] = 0;
    clock_ns = 0;
}
int main(void) {
    uint32_t msg[255] = {0x301, 0x10203040, 0x50607080}, output[257];
    uint32_t *cmd = table + ADRENO8_HFI_HEADER(0);
    uint32_t *resp = table + ADRENO8_HFI_HEADER(1);
    reset(0);
    assert(table[1] == 210 && table[2] == 6 && table[3] == 12 && table[4] == 3 && table[5] == 3);
    assert(cmd[1] == 0x60006350 && resp[1] == 0x60007350);
    assert(table[ADRENO8_HFI_HEADER(2) + 1] == 0x60008350);
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
    reset(1); assert(adreno8_hfi_send(131, msg, 2, "RAW diagnostic pauses"));
    assert(clock_ns == 2000000000ULL && diagnostic_calls == 1 && sleeps == 0);
    reset(0); assert(!adreno8_hfi_send(131, msg, 2, "RAW timeout after pauses"));
    assert(clock_ns == 3000000000ULL && diagnostic_calls == 1 && sleeps == 10000);
    const uint32_t feature[3] = {1, 1, 0};
    for (unsigned i = 14; i <= 17; i++) {
        reset(i); assert(!adreno8_hfi_send(11, feature, 3, "FEATURE_CTRL v5 rejected"));
        assert(feature_logs == 1 && memory_failures == 1 && failures == 1 && sleeps == 0);
        if (i == 14) assert(strstr(feature_log, "data 0 error 0x6"));
        if (i == 15) assert(strstr(feature_log, "data 0 error 0x8"));
        if (i == 16) assert(strstr(feature_log, "data 0x1 error 0"));
    }
    reset(13); assert(adreno8_hfi_send(11, feature, 3, "FEATURE_CTRL v5"));
    assert(response_reads == 4 && feature_logs == 1 && !memory_failures && !failures);
    assert(strstr(feature_log, "data 0 error 0"));
    reset(1); assert(!adreno8_hfi_send(11, feature, 3, "short FEATURE_CTRL v5"));
    assert(response_reads == 3 && !feature_logs && memory_failures == 1 && failures == 1);
    for (unsigned i = 18; i <= 20; i++) {
        reset(i); assert(adreno8_hfi_send(11, feature, 3, "FEATURE_CTRL ACK identity"));
        assert(feature_logs == 1 && !memory_failures && !failures && sleeps == (i == 20 ? 0 : 1));
    }
    reset(21); assert(!adreno8_hfi_send(11, feature, 3, "unknown FEATURE_CTRL error"));
    assert(memory_failures == 1 && failures == 1);
    reset(22); assert(adreno8_hfi_send(11, feature, 3, "unrelated short ACK"));
    assert(feature_logs == 1 && sleeps == 1 && !memory_failures && !failures);
    const unsigned ordinary[] = {3, 14, 15};
    for (unsigned i = 0; i < sizeof(ordinary) / sizeof(*ordinary); i++) {
        reset(1); assert(adreno8_hfi_send(ordinary[i], NULL, 0, "ordinary three-word ACK"));
        reset(4); assert(!adreno8_hfi_send(ordinary[i], NULL, 0, "ordinary nonzero status"));
    }
    const unsigned asynchronous[] = {10, 128, 129, 131, 134};
    for (unsigned i = 0; i < sizeof(asynchronous) / sizeof(*asynchronous); i++) {
        reset(4); assert(adreno8_hfi_send(asynchronous[i], msg, 2, "async positive data"));
        reset(17); assert(!adreno8_hfi_send(asynchronous[i], msg, 2, "async failure"));
    }
    puts("HFI layout, wrap, lengths, FEATURE_CTRL v5, ACK identity, async errors and bounded waits: PASS");
}
'''
STAGE_FUNCTIONS = DRIVER[DRIVER.index("bool\nadreno8_step_queues("):DRIVER.index("/* ---- step 16:")]
private = (ROOT / "xnu/files/bsd/dev/adreno8_private.h").read_text()
bcm_macro_start = private.index("#define BCM_TCS_CMD(")
bcm_macro_end = private.index("\n#define", bcm_macro_start)
BCM_MACRO = private[bcm_macro_start:bcm_macro_end]
BW_DEFINE = re.search(r"^#define BW_COUNT\s+\d+", private, re.M).group()
cx = (ROOT / "xnu/files/bsd/dev/adreno8_cx.c").read_text()
BOOT_DATA = re.search(r"static const struct adreno8_gpu_level adreno8_boot_speeds\[\] = \{.*?\n};", cx, re.S).group()
BW_DATA = re.search(r"const uint32_t adreno8_bw_kb\[BW_COUNT\] = \{.*?\n};", cx, re.S).group().replace("const uint32_t", "static uint32_t")
BCM_DATA = re.search(r"const struct adreno8_bcm adreno8_bcms\[\] = \{.*?\n};", cx, re.S).group().replace("const struct", "static const struct")
GMU_DATA = "\n".join(re.findall(r"^const uint32_t adreno8_gmu_(?:hz|level)\[3\] = .*?;", cx, re.M))
VOTE_FUNCTIONS = cx[cx.index("bool\nadreno8_arc_vote("):cx.index("static bool\nadreno8_rsc_read(")]
RPMH_DEFINE = re.search(r"^#define RPMH_LEVELS\s+\d+", private, re.M).group()
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
struct adreno8_gpu_level { uint32_t hz, level, bus; };
''' + BOOT_DATA + r'''
static const struct adreno8_gpu_level *adreno8_speeds = adreno8_boot_speeds;
static uint64_t clock_now = 1, hot = 450; static bool hot_read = true;
static uint64_t mach_absolute_time(void) { return clock_now; }
static void absolutetime_to_nanoseconds(uint64_t a, uint64_t *n) { *n = a; }
static bool annibale_gpu_temperature(int32_t *t) { *t = (int32_t)hot; return hot_read; }
static uint64_t adreno8_retired_total, touched_at;
static uint64_t annibale_cpufreq_touched(void) { return touched_at; }
static struct block adreno8_gmu;
static uint32_t adreno8_step, adreno8_gx_count = 2;
static uint32_t adreno8_speed_asked;
static bool adreno8_gpu8_keep, adreno8_gpu8_return, adreno8_reset_required;
static bool adreno8_direct_ring;
static bool ordinary_ready;
static bool adreno8_ordinary_ready(void) { return ordinary_ready; }
static bool adreno8_hfi_tables_sent, hwsched_ready = true;
static bool adreno8_hwsched_ready(void) { return hwsched_ready; }
static uint32_t adreno8_gx_hz[17], adreno8_gx_level[17];
static uint32_t adreno8_gx_votes[17], adreno8_gx_dep_votes[17], adreno8_cx_votes[3];
''' + GMU_DATA + "\n" + RPMH_DEFINE + "\n" + BW_DEFINE + "\n" + BW_DATA + "\n" + BCM_DATA + r'''
static struct adreno8_bcm_db aux[3] = {{4800000, 64, 0, 0}, {4800000, 16, 1, 0}, {4800000, 0, 1, 0}};
static bool adreno8_gmu_runs = true, adreno8_gmu_talks, wait_ok = true, vote_ok = true;
static vm_offset_t adreno8_hfi_view_table = 1, adreno8_hfi_view_cmd = 1;
static vm_offset_t adreno8_hfi_view_resp = 1, adreno8_hfi_view_debug = 1;
static uint32_t version = 0x20070001, mask_written, sent[8][255], ids[8], lengths[8], send_count, fail_id;

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
static uint32_t adreno8_rpmh_lookup(const char *name, uint16_t *levels, unsigned *count, const uint8_t **data, unsigned *length) {
    unsigned i;
    if (!vote_ok) return 0;
    /* Level maps captured from the live CMD DB, independently of the encoder. */
    static const uint16_t gfx[] = {0,50,52,56,60,64,80,128,144,192,224,256,320,416,448,452};
    static const uint16_t cx_levels[] = {0,16,64,128,192,256,320,384,416,448,480};
    static const uint16_t mx[] = {0,16,64,128,192,256,320,384,416,448};
    const uint16_t *rail = NULL; unsigned entries = 0;
    if (!strcmp(name,"gfx.lvl")) { rail=gfx; entries=sizeof(gfx)/sizeof(*gfx); }
    if (!strcmp(name,"cx.lvl")) { rail=cx_levels; entries=sizeof(cx_levels)/sizeof(*cx_levels); }
    if (!strcmp(name,"mx.lvl")) { rail=mx; entries=sizeof(mx)/sizeof(*mx); }
    if (rail) {
        if (count) *count=entries;
        if (levels) memcpy(levels,rail,entries*sizeof(*rail));
        return 0x30000;
    }
    if (!strcmp(name, "CN0")) return 0x2000;
    for (i = 0; i < 3; i++) if (!strcmp(name, adreno8_bcms[i].name)) break;
    if (i == 3) return 0;
    if (data) *data = (const uint8_t *)&aux[i];
    if (length) *length = sizeof(aux[i]);
    return 0x3000 + i * 4;
}
static bool adreno8_hfi_send(uint32_t id, const uint32_t *payload, uint32_t words, const char *what) {
    assert(send_count < 8 && words <= 255);
    if (id == 30) assert(adreno8_reset_required);
    ids[send_count] = id; lengths[send_count] = words;
    if (words) memcpy(sent[send_count], payload, words * 4);
    send_count++;
    return id != fail_id;
}
''' + VOTE_FUNCTIONS + "\n" + BCM_MACRO + "\n" + STAGE_FUNCTIONS + r'''
int main(void) {
    uint32_t dependency;
    assert(adreno8_dep_vote(0,&dependency) && dependency==UINT32_MAX);
    assert(adreno8_dep_vote(50,&dependency) && dependency==0xffffc2ffu);
    assert(adreno8_dep_vote(128,&dependency) && dependency==0xffffc3ffu);
    assert(adreno8_dep_vote(448,&dependency) && dependency==0xffffc9ffu);
    assert(!adreno8_dep_vote(480,&dependency));
    assert(sizeof(adreno8_boot_speeds) / sizeof(adreno8_boot_speeds[0]) == 3);
    assert(adreno8_boot_speeds[0].hz == 160000000 && adreno8_boot_speeds[0].level == 50 && adreno8_boot_speeds[0].bus == 2);
    assert(adreno8_boot_speeds[1].hz == 222000000 && adreno8_boot_speeds[1].level == 52 && adreno8_boot_speeds[1].bus == 2);
    assert(adreno8_boot_speeds[2].hz == 342000000 && adreno8_boot_speeds[2].level == 56 && adreno8_boot_speeds[2].bus == 3);
    adreno8_gx_hz[1] = adreno8_boot_speeds[0].hz;
    adreno8_gx_level[1] = adreno8_boot_speeds[0].level;
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
    assert(sent[0][4] == 0 && sent[0][5] == UINT32_MAX && sent[0][6] == 0);
    assert(sent[0][7] == 0x00320201 && sent[0][8] == 0xffffc2ffu && sent[0][9] == 160000);
    assert(sent[0][10] == 3 && sent[0][11] == 2);
    assert(sent[0][12] == 0 && sent[0][13] == 0);
    assert(sent[0][14] == 0x00400202 && sent[0][15] == 500000);
    assert(sent[0][16] == 0x00800303 && sent[0][17] == 650000);
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
    adreno8_speed_asked = 1; adreno8_gx_hz[1] = 125000000;
    assert(!adreno8_step_gx_power() && send_count == 0);
    adreno8_gx_hz[1] = adreno8_boot_speeds[0].hz;
    hwsched_ready = false; assert(!adreno8_step_gx_power() && send_count == 0);
    hwsched_ready = true;
    assert(adreno8_step_gx_power() && send_count == 1 && adreno8_reset_required);
    assert(ids[0] == 30 && lengths[0] == 3 && sent[0][0] == 1 && sent[0][1] == 1 && sent[0][2] == 2);
    send_count = 0; adreno8_direct_ring = true;
    assert(!adreno8_step_gx_power() && send_count == 0);
    hwsched_ready = false; ordinary_ready = true;
    assert(adreno8_step_gx_power() && send_count == 1);
    adreno8_direct_ring = false;
    send_count = 0; assert(!adreno8_step_gx_power() && send_count == 0);
    hwsched_ready = true;
    send_count = 0; fail_id = 30; adreno8_reset_required = false;
    assert(!adreno8_step_gx_power() && adreno8_reset_required);
    /*
     * The speed tick, every 100 ms at most: 342 MHz from 20% of the time busy at 160 MHz or a touch in the last
     * 300 ms, back to 160 after 500 ms under 10%, and 160 at 70 C or without a reading until below 60 C.
     * 384000 always-on ticks are 20 ms, 20% of a 100 ms tick at 160 MHz.
     */
    for (unsigned i = 0; i < 4; i++) { adreno8_gx_hz[i] = i ? adreno8_boot_speeds[i - 1].hz : 0; }
    adreno8_gx_count = 4; adreno8_hfi_tables_sent = true; adreno8_reset_required = true; fail_id = 0;
    adreno8_speed_asked = 1; send_count = 0; adreno8_retired_total = 1000000; adreno8_speed_tick(); assert(send_count == 0);
    adreno8_speed_asked = 4; adreno8_speed_tick(); assert(send_count == 0);
    adreno8_speed_asked = 3; adreno8_hfi_tables_sent = false; adreno8_speed_tick(); assert(send_count == 0);
    adreno8_hfi_tables_sent = true; adreno8_speed_tick(); assert(send_count == 0);
    clock_now += 100000000; adreno8_retired_total += 383999; adreno8_speed_tick(); assert(send_count == 0);
    clock_now += 50000000; adreno8_retired_total += 384000; adreno8_speed_tick(); assert(send_count == 0);
    clock_now += 50000000; adreno8_speed_tick();
    assert(send_count == 1 && ids[0] == 30 && sent[0][0] == 1 && sent[0][1] == 3 && sent[0][2] == 3);
    /* 9 ms of 100 at 342 MHz would be 19 at 160: between the two limits, the speed stays, and light ticks before count no more. */
    for (unsigned i = 0; i < 3; i++) { clock_now += 100000000; adreno8_retired_total += 76800; adreno8_speed_tick(); }
    for (unsigned i = 0; i < 8; i++) { clock_now += 100000000; adreno8_retired_total += 172800; adreno8_speed_tick(); }
    assert(send_count == 1);
    /* 4 ms of 100 at 342 MHz is 8.6 at 160: down once that lasted 500 ms. */
    for (unsigned i = 0; i < 5; i++) { clock_now += 100000000; adreno8_retired_total += 76800; adreno8_speed_tick(); }
    assert(send_count == 1);
    clock_now += 100000000; adreno8_retired_total += 76800; adreno8_speed_tick();
    assert(send_count == 2 && sent[1][1] == 1 && sent[1][2] == 2);
    clock_now += 100000000; touched_at = clock_now - 300000000; adreno8_speed_tick(); assert(send_count == 2);
    clock_now += 100000000; touched_at = clock_now - 299999999; adreno8_speed_tick();
    assert(send_count == 3 && sent[2][1] == 3);
    clock_now += 100000000; hot = 700; adreno8_speed_tick();
    assert(send_count == 4 && sent[3][1] == 1);
    clock_now += 100000000; hot = 650; touched_at = clock_now; adreno8_speed_tick(); assert(send_count == 4);
    clock_now += 100000000; hot = 599; touched_at = clock_now; adreno8_speed_tick();
    assert(send_count == 5 && sent[4][1] == 3);
    send_count = 0; clock_now += 100000000; hot_read = false; adreno8_speed_tick();
    assert(send_count == 1 && sent[0][1] == 1);
    send_count = 0; clock_now += 100000000; hot_read = true; hot = 400; touched_at = clock_now; fail_id = 30;
    adreno8_speed_tick(); assert(send_count == 1 && adreno8_speed_asked == 1);
    clock_now += 100000000; fail_id = 0; adreno8_speed_tick(); assert(send_count == 1);
    { uint32_t mhz, changes; adreno8_speed_state(&mhz, &changes); assert(mhz == 160 && changes == 6); }
    puts("HFI version, perf/BW payloads, ACV threshold, AB=0, minimum DCVS and the load, touch and temperature speed rules: PASS");
}
'''

checks = [("queues", SOURCE, False), ("power tables", STAGE_SOURCE, False)]
publication = "\tadreno8_wr(header + 44, write);\n\tadreno8_settle();\n"
assert SOURCE.count(publication) == 1
for label, delayed in (("pause after producer", 'adreno8_announce("published");'),
                       ("delay after producer", "adreno8_sleep_us(1);")):
    checks.append((label, SOURCE.replace(publication, publication + "\t" + delayed + "\n", 1), True))
feature_mutations = [
    ("missing FEATURE_CTRL error", "if (resp[3] != 0) return false;", "if (false) return false;"),
    ("limited FEATURE_CTRL errors", "if (resp[3] != 0) return false;", "if (resp[3] == 6 || resp[3] == 8) return false;"),
    ("wrong FEATURE_CTRL error field", "if (resp[3] != 0) return false;", "if (resp[2] != 0) return false;"),
    ("short FEATURE_CTRL ACK accepted", "if (words < 4)", "if (words < 3)"),
    ("positive FEATURE_CTRL data accepted", "hwsched ? resp[2] == 0xffffffffu : resp[2] != 0", "hwsched ? resp[2] == 0xffffffffu : (id != 11 && resp[2] != 0)"),
    ("FEATURE_CTRL contract for ordinary ACK", "if (id == 11)", "if (true)"),
]
for label, before, after in feature_mutations:
    assert SOURCE.count(before) == 1, label
    checks.append((label, SOURCE.replace(before, after, 1), True))
feature_start = SOURCE.index("\t\t\t\t/* FEATURE_CTRL uses")
feature_end = SOURCE.index("\t\t\t\t/* These HWSCHED", feature_start)
feature_block = SOURCE[feature_start:feature_end]
without_feature = SOURCE[:feature_start] + SOURCE[feature_end:]
identity_start = without_feature.index("\t\t\t\t/* Gen8 echoes")
checks.append(("FEATURE_CTRL checked before ACK identity",
    without_feature[:identity_start] + feature_block + without_feature[identity_start:], True))
vote_mutations = [
    ("missing dependency high nibble", "0xffffc000u", "0x0fffc000u"),
    ("wrong MX dependency field", "(j << 8) | 0xff", "(j << 6) | 0xff"),
    ("missing no-CX dependency", "(j << 8) | 0xff", "(j << 8) | 0"),
    ("invalid zero-speed dependency", "*vote = 0xffffffff;", "*vote = 0;"),
    ("primary ARC index as level", "((uint32_t)pri[pindex] << 16)", "(pindex << 16)"),
    ("omitted HFI dependency word", "tbl[at++] = adreno8_gx_dep_votes[i];", "tbl[at++] = 0;"),
    ("incorrect GMU startup rate", "{ 0, 500000000, 650000000 }", "{ 0, 500000000, 500000000 }"),
    ("incorrect GMU runtime level", "{ 0, 0x40, 0x80 }", "{ 0, 0x10, 0x80 }"),
]
speed_mutations = [
    ("70 C kept at speed", "tenths >= SPEED_HOT_TENTHS", "tenths > SPEED_HOT_TENTHS"),
    ("cooling below 60 C not needed", "tenths < SPEED_COOL_TENTHS", "tenths < SPEED_HOT_TENTHS"),
    ("load at the speed it ran, not at the slowest", "* adreno8_gx_hz[adreno8_speed_now] / adreno8_gx_hz[1]", "* 1"),
    ("20% busy kept slow", "permille >= SPEED_UP_PERMILLE", "permille > SPEED_UP_PERMILLE"),
    ("down at once when light", "light >= SPEED_DOWN_NS", "light >= 0"),
    ("medium load counted as light", "\t} else {\n\t\tadreno8_speed_light_since = 0;\n\t}", "\t}"),
    ("touch ignored", "touch_age < SPEED_TOUCH_NS", "touch_age < 0"),
    ("missing reading kept at speed", "if (!reading || tenths", "if (tenths"),
    ("speed vote without rate limit", "since < SPEED_TICK_NS", "since < 0"),
    ("bus row of the slowest speed", "adreno8_speeds[want - 1].bus", "adreno8_speeds[0].bus"),
]
for label, before, after in vote_mutations + speed_mutations:
    assert STAGE_SOURCE.count(before) == 1, label
    checks.append((label, STAGE_SOURCE.replace(before, after, 1), True))

for label, source, rejected in checks:
    with tempfile.TemporaryDirectory(prefix="adreno8-hfi-") as directory:
        src = Path(directory) / "check.c"
        binary = Path(directory) / "check"
        src.write_text(source)
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Wno-unused-parameter", "-fsanitize=address,undefined", "-I", str(HEADER.parent), str(src), "-o", str(binary)], check=True)
        result = subprocess.run([str(binary)], capture_output=True, text=True)
        if rejected:
            assert result.returncode != 0, "mutation survived: " + label
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout, end="")
print("HFI power encoding and speed mutations rejected:", len(vote_mutations) + len(speed_mutations))
print("HFI FEATURE_CTRL v5 mutations rejected:", len(feature_mutations) + 1)
print("HFI publication timing mutations rejected: 2")
