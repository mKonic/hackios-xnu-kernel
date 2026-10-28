#!/usr/bin/env python3
"""Exercise production HWSCHED startup and HFI transport with firmware requests."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
hfi = (DEV / "adreno8_hfi.c").read_text()
transport = hfi[hfi.index("static unsigned adreno8_hfi_seq;"):hfi.index("/* ---- step 12:")]
hwsched = (DEV / "adreno8_hwsched.c").read_text()
protocol = hwsched[hwsched.index("#define printf adreno8_say"):]

source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "adreno8_wire_private.h"
#include "adreno8_hwsched_private.h"
typedef uintptr_t vm_offset_t;
struct block { vm_offset_t base; };
static struct block adreno8_gmu;
static uint32_t table[1024], commands[1024], responses[1024], debug[1024], regs[4];
static bool adreno8_direct_ring;
static vm_offset_t adreno8_hfi_view_table, adreno8_hfi_view_cmd;
static vm_offset_t adreno8_hfi_view_resp, adreno8_hfi_view_debug;
static bool adreno8_gmu_talks, adreno8_gpu8_keep, adreno8_gpu8_return, adreno8_reset_required;
static bool cx_ready, allocation_ok, clock_ok;
static unsigned clock_calls, memory_failures;
static unsigned adreno8_step, mode, alloc_calls, result_fault, failure_snapshots, sleeps;
static unsigned sent_count, fail_at, resource_replies;
static uint32_t hwsched_ack_value;
static uint32_t captured[32][255], lengths[32], start_header, mem_header, counter_header;
static uint64_t clock_ns;
static struct adreno8_hfi_mem_request last_request;
enum { NORMAL, LEGACY, TIMEOUT, FW_ERROR, WRONG_QUEUE, BAD_VERSION, SHORT_ACK,
       BAD_ECHO, REPLY_FULL, UNKNOWN_MESSAGE, SECURE_MEM };
#define GMU_HOST2GMU_SET 0
#define GMU_GMU2HOST_INFO 4
#define GMU_GMU2HOST_CLR 8
#define GMU_FW_INIT_RESULT 12
#define GMU_FW_FAILED 0xe00
static int adreno8_say(const char *format, ...) { return 0; }
static void adreno8_announce(const char *format, ...) { (void)format; }
#define printf adreno8_say
static bool adreno8_cx_ready(void) { return cx_ready; }
static uint64_t mach_absolute_time(void) { return clock_ns; }
static void absolutetime_to_nanoseconds(uint64_t value, uint64_t *out) { *out = value; }
static void adreno8_settle(void) {}
#define SECURE_MODE_TRANSLATION_B 4u
static uint32_t adreno8_gpu8_secure_mode;
static bool adreno8_gpu_faulted(void) { return false; }
void adreno8_hwsched_memory_failed(void) { memory_failures++; }
static void adreno8_gmu_post_mortem(void) { failure_snapshots++; }
static uint32_t header(unsigned id, unsigned words, unsigned type, unsigned seq) {
    return id | (words << 8) | (type << 16) | (seq << 20);
}
static void append(unsigned queue, const uint32_t *msg, unsigned words) {
    uint32_t *h = table + ADRENO8_HFI_HEADER(queue);
    uint32_t *data = queue == 1 ? responses : debug;
    for (unsigned i = 0; i < words; i++) data[(h[11] + i) % 1024] = msg[i];
    h[11] = (h[11] + ((words + 3) & ~3u)) % 1024;
}
static void ack(uint32_t echoed, uint32_t status) {
    unsigned words = (echoed & 0xff) == 11 ? 4 : 3;
    uint32_t msg[] = {header(echoed & 0xff, words, 1, 77), echoed, status, 0};
    append(1, msg, words);
}
static void make_mem(uint32_t *out, unsigned version) {
    uint32_t msg[] = {header(20, version ? 11 : 10, 0, 99), version,
                     0, 0, 0x33f, 1, 0, 7, 0, 4097, 0};
    memcpy(out, msg, sizeof(msg));
}
static void emit_debug(void) {
    uint32_t msg[] = {header(101, 4, 0, 88), 1, 2, 3};
    append(2, msg, 4);
}
static void adreno8_sleep_us(uint32_t us) {
    clock_ns += (uint64_t)us * 1000;
    sleeps++;
    if (mode == TIMEOUT) emit_debug();
    if (mode == BAD_ECHO && sleeps == 1) ack(start_header, 0);
}
static uint32_t adreno8_rd(vm_offset_t address) { return *(uint32_t *)address; }
static void adreno8_wr(vm_offset_t address, uint32_t value) {
    *(uint32_t *)address = value;
    if (address != adreno8_gmu.base + GMU_HOST2GMU_SET) return;
    uint32_t *h = table + ADRENO8_HFI_HEADER(0);
    unsigned read = h[10], words = (commands[read] >> 8) & 0xff;
    assert(sent_count < 32 && words > 0 && adreno8_reset_required);
    uint32_t *msg = captured[sent_count];
    for (unsigned i = 0; i < words; i++) msg[i] = commands[(read + i) % 1024];
    lengths[sent_count++] = words;
    h[10] = (read + ((words + 3) & ~3u)) % 1024;
    assert(h[10] == h[11]);
    if (((msg[0] >> 16) & 0xf) == 1) {
        assert((msg[0] & 0xff) == 20 || (msg[0] & 0xff) == 110);
        resource_replies++;
        if (resource_replies == 2) ack(start_header, hwsched_ack_value);
        return;
    }
    if (fail_at == sent_count) {
        unsigned id = msg[0] & 0xff;
        ack(msg[0], id == 10 || id == 131 ? UINT32_MAX : 7); return;
    }
    if ((msg[0] & 0xff) != 10) {
        ack(msg[0], (msg[0] & 0xff) == 131 ? hwsched_ack_value : 0); return;
    }
    start_header = msg[0];
    if (mode == TIMEOUT) { emit_debug(); return; }
    if (mode == FW_ERROR) {
        uint32_t error[] = {header(100, 2, 0, 1), 0xdead};
        append(2, error, 2); ack(start_header, 0); return;
    }
    if (mode == SHORT_ACK) {
        uint32_t short_ack[] = {header(10, 2, 1, 1), start_header};
        append(1, short_ack, 2); return;
    }
    if (mode == BAD_ECHO) { ack(start_header ^ (1u << 20), 0); return; }
    if (mode == UNKNOWN_MESSAGE) {
        uint32_t unknown[] = {header(111, 1, 0, 2)};
        append(1, unknown, 1); return;
    }
    uint32_t mem[11], counter[] = {header(110, 3, 0, 100), 5, 9};
    make_mem(mem, mode == LEGACY ? 0 : 1);
    if (mode == SECURE_MEM) { mem[4] = 0x1007; mem[5] = 5; mem[9] = 0x138000; }
    if (mode == BAD_VERSION) mem[1] = 2;
    mem_header = mem[0]; counter_header = counter[0];
    append(mode == WRONG_QUEUE ? 2 : 1, mem, mode == LEGACY ? 10 : 11);
    append(1, counter, 3);
    if (mode == REPLY_FULL) h[10] = (h[11] + 4) % 1024;
}
bool adreno8_hwsched_alloc(const struct adreno8_hfi_mem_request *request,
                         struct adreno8_hfi_mem_result *result) {
    alloc_calls++; last_request = *request;
    if (!allocation_ok) return false;
    uint32_t bits = (request->align >> 8) & 0xff;
    uint64_t align = 1ULL << (bits < 14 ? 14 : bits);
    result->size = ((uint64_t)request->size + align - 1) & ~(align - 1);
    result->gmu_address = (request->flags & ADRENO8_HFI_MEM_GMU) || !(request->flags & ADRENO8_HFI_MEM_GFX) ? 0x60010000 : 0;
    result->gpu_address = request->flags & ADRENO8_HFI_MEM_GFX ? 0x10000000 : 0;
    result->secure = request->kind == 5 && request->flags == 0x1007;
    if (result->secure) result->gpu_address = 0xf0000000;
    switch (result_fault) {
    case 1: result->size = request->size - 1; break;
    case 2: result->gmu_address = 0; break;
    case 3: result->gmu_address++; break;
    case 4: result->gmu_address = 0xfffff000; break;
    case 5: result->gpu_address = 0; break;
    case 6: result->gpu_address++; break;
    case 7: result->gpu_address = UINT64_MAX & ~0xfffULL; break;
    case 8: result->size++; break;
    case 9: result->gmu_address = 0x60010000; break;
    case 10: result->gpu_address = 0x10000000; break;
    case 11: result->secure = !result->secure; break;
    }
    return true;
}
''' + transport + r'''
static bool adreno8_clock_started(void) {
    clock_calls++;
    assert(sent_count >= 7 && (captured[6][0] & 0xff) == 10);
    uint32_t *h = table + ADRENO8_HFI_HEADER(1);
    assert(h[10] == h[11]); /* The START response has been consumed. */
    return clock_ok;
}
''' + protocol + r'''
#undef printf
static void reset(unsigned next_mode) {
    memset(commands, 0, sizeof(commands)); memset(responses, 0, sizeof(responses));
    memset(debug, 0, sizeof(debug)); memset(regs, 0, sizeof(regs));
    memset(captured, 0, sizeof(captured)); memset(lengths, 0, sizeof(lengths));
    adreno8_hfi_init_table(table, 0x60006000, false);
    adreno8_hfi_view_table = (uintptr_t)table; adreno8_hfi_view_cmd = (uintptr_t)commands;
    adreno8_hfi_view_resp = (uintptr_t)responses; adreno8_hfi_view_debug = (uintptr_t)debug;
    adreno8_gmu.base = (uintptr_t)regs;
    adreno8_gmu_talks = adreno8_gpu8_keep = cx_ready = allocation_ok = adreno8_hfi_tables_sent = true;
    adreno8_direct_ring = false;
    adreno8_hwsched_attempted = adreno8_hwsched_started = false;
    adreno8_gpu8_return = adreno8_reset_required = false;
    clock_ok = true; clock_calls = memory_failures = 0;
    adreno8_hfi_seq = 4090;
    mode = next_mode; alloc_calls = result_fault = failure_snapshots = sleeps = 0;
    sent_count = fail_at = resource_replies = hwsched_ack_value = 0; clock_ns = 0;
}
static void check_boot(void) {
    assert(adreno8_hwsched_start() && adreno8_hwsched_ready());
    assert(sent_count == 9 && resource_replies == 2 && alloc_calls == 1 && clock_calls == 1);
    const uint32_t features[][3] = {{9,0,0},{1,1,0},{16,1,0}};
    for (unsigned i = 0; i < 3; i++) {
        assert((captured[i][0] & 0xff) == 11 && lengths[i] == 4);
        assert(!memcmp(captured[i] + 1, features[i], 12));
    }
    assert((captured[3][0] & 0xff) == 14 && lengths[3] == 2 && captured[3][1] == 0);
    assert((captured[4][0] & 0xff) == 11 && lengths[4] == 4);
    assert(captured[4][1] == 2 && captured[4][2] == 0 && captured[4][3] == 0);
    assert((captured[5][0] & 0xff) == 11 && captured[5][1] == 29 && captured[5][2] == 1);
    assert((captured[6][0] & 0xff) == 10 && lengths[6] == 1);
    assert((start_header >> 20) == 1 && adreno8_hfi_seq == 3);
    assert(lengths[7] == 11 && captured[7][0] == header(20, 11, 1, 2));
    assert(captured[7][1] == mem_header && captured[7][2] == 0x10000000 && captured[7][3] == 0);
    assert(captured[7][6] == 7 && captured[7][7] == 7 && captured[7][8] == 0x60010000);
    assert(captured[7][9] == 4097 && captured[7][10] == 0);
    assert(lengths[8] == 6 && captured[8][0] == header(110, 6, 1, 3));
    assert(captured[8][1] == counter_header && captured[8][2] == 5 && captured[8][3] == 9);
    assert(captured[8][4] == 0 && captured[8][5] == 0);
    assert(!adreno8_hwsched_start() && sent_count == 9);
}
int main(void) {
    uint32_t msg[11], reply[10], reply_words;
    reset(NORMAL); adreno8_direct_ring = true; adreno8_hwsched_started = true;
    assert(!adreno8_hwsched_ready());
    assert(!adreno8_hwsched_start() && !sent_count && !alloc_calls);
    reset(NORMAL); hwsched_ack_value = 7; check_boot();
    uint32_t pm4[10] = {0x70000000};
    assert(adreno8_hwsched_raw(pm4, 10, "CP init"));
    assert(lengths[9] == 11 && (captured[9][0] & 0xff) == 131);
    assert(!memcmp(captured[9] + 1, pm4, sizeof(pm4)));
    assert(!adreno8_hwsched_raw(NULL, 1, "bad") && !adreno8_hwsched_raw(pm4, 255, "bad"));
    fail_at = sent_count + 1;
    assert(!adreno8_hwsched_raw(pm4, 10, "NACK") && !adreno8_hwsched_ready());
    unsigned count = sent_count;
    assert(!adreno8_hwsched_raw(pm4, 10, "after failure") && sent_count == count);
    reset(LEGACY); check_boot();
    reset(SECURE_MEM);
    assert(adreno8_hwsched_start() && adreno8_hwsched_ready() && sent_count == 9);
    assert(captured[7][2] == 0xf0000000 && captured[7][3] == 0 && captured[7][8] == 0);
    assert(captured[7][4] == 0x1007 && captured[7][5] == 5 && captured[7][9] == 0x138000);
    reset(SECURE_MEM); result_fault = 11;
    assert(!adreno8_hwsched_start() && adreno8_reset_required && resource_replies == 0);
    /* The secure request through the real HFI loop: a failed resource handler
     * answers with nothing (7 pre-START messages, 0 replies), runs the
     * postmortem once, and a repeated start attempt sends nothing new. */
    assert(sent_count == 7 && failure_snapshots == 1);
    reset(SECURE_MEM); allocation_ok = false;
    assert(!adreno8_hwsched_start() && adreno8_reset_required && resource_replies == 0);
    assert(sent_count == 7 && failure_snapshots == 1);
    assert(!adreno8_hwsched_start() && sent_count == 7 && failure_snapshots == 1);
    reset(NORMAL); clock_ok = false;
    assert(!adreno8_hwsched_start() && !adreno8_hwsched_ready());
    assert(clock_calls == 1 && adreno8_reset_required && memory_failures == 1);
    assert(!adreno8_hwsched_start() && clock_calls == 1 && sent_count == 9);
    for (unsigned i = 1; i <= 7; i++) {
        reset(NORMAL); fail_at = i;
        assert(!adreno8_hwsched_start() && !adreno8_hwsched_ready());
        assert(sent_count == i && adreno8_reset_required && clock_calls == 0);
        if (i == 5) assert(captured[4][1] == 2); /* A failed PREEMPTION off stops before AQE/START. */
        assert(!adreno8_hwsched_start() && sent_count == i);
    }
    for (unsigned i = TIMEOUT; i <= UNKNOWN_MESSAGE; i++) {
        reset(i);
        if (i == BAD_ECHO) { assert(adreno8_hwsched_start() && sleeps == 1); continue; }
        assert(!adreno8_hwsched_start() && !adreno8_hwsched_ready() && adreno8_reset_required);
        if (i == TIMEOUT) assert(clock_ns == 5000000000ULL && sleeps == 50000);
        if (i == WRONG_QUEUE || i == BAD_VERSION) assert(alloc_calls == 0);
    }
    for (unsigned i = 0; i < 4; i++) {
        reset(NORMAL);
        if (i == 0) adreno8_hfi_tables_sent = false;
        if (i == 1) adreno8_gpu8_keep = false;
        if (i == 2) adreno8_gpu8_return = true;
        if (i == 3) cx_ready = false;
        assert(!adreno8_hwsched_start() && !adreno8_reset_required && sent_count == 0);
    }
    reset(NORMAL); make_mem(msg, 1);
    assert(adreno8_hwsched_request(msg, 11, reply, &reply_words) && reply_words == 10);
    assert(last_request.size == 4097 && last_request.flags == 0x33f);
    /* Every invalid request fails before the resource owner is called. */
    for (unsigned i = 0; i < 15; i++) {
        reset(NORMAL); make_mem(msg, 1);
        switch (i) {
        case 0: msg[0] ^= 1u << 8; break;
        case 1: msg[1] = 2; break;
        case 2: msg[2] = 0x1000; break;
        case 3: msg[3] = 1; break;
        case 4: msg[6] = 1; break;
        case 5: msg[8] = 0x60000000; break;
        case 6: msg[4] |= 1u << 10; break;
        case 7: msg[9] = 0; break;
        case 8: msg[5] = 27; break;
        case 9: msg[10] = 1u << 16; break;
        case 10: msg[0] |= 1u << 16; break;
        case 11: msg[4] |= ADRENO8_HFI_MEM_SECURE; break;
        case 12: msg[10] = 32; break;
        case 13: msg[10] = 32u << 8; break;
        case 14: msg[9] = UINT32_MAX; break;
        }
        assert(!adreno8_hwsched_request(msg, 11, reply, &reply_words) && alloc_calls == 0);
    }
    const unsigned special[] = {5,11,12,13,19,20,23};
    for (unsigned i = 0; i < sizeof(special)/sizeof(special[0]); i++) {
        reset(NORMAL); make_mem(msg, 1); msg[5] = special[i];
        assert(!adreno8_hwsched_request(msg, 11, reply, &reply_words) && alloc_calls == 0);
    }
    for (unsigned i = 1; i <= 11; i++) {
        reset(NORMAL); make_mem(msg, 1); result_fault = i;
        if (i == 9) msg[4] = ADRENO8_HFI_MEM_GFX;
        if (i == 10) msg[4] = ADRENO8_HFI_MEM_GMU;
        assert(!adreno8_hwsched_request(msg, 11, reply, &reply_words) && alloc_calls == 1);
    }
    reset(NORMAL); allocation_ok = false; make_mem(msg, 1);
    assert(!adreno8_hwsched_request(msg, 11, reply, &reply_words));
    reset(NORMAL); make_mem(msg, 1); msg[4] = 0x1007; msg[5] = 5; msg[9] = 0x138000;
    assert(adreno8_hwsched_request(msg, 11, reply, &reply_words));
    assert(reply[1] == 0xf0000000 && reply[2] == 0 && reply[7] == 0 && reply[3] == 0x1007);
    result_fault = 9;
    assert(!adreno8_hwsched_request(msg, 11, reply, &reply_words));
    for (unsigned i = 0; i < 3; i++) {
        reset(NORMAL); make_mem(msg, 1); msg[4] = 0x1007; msg[5] = 5;
        if (i == 0) msg[4] |= ADRENO8_HFI_MEM_GMU;
        if (i == 1) msg[5] = 4;
        if (i == 2) msg[4] &= ~ADRENO8_HFI_MEM_SECURE;
        assert(!adreno8_hwsched_request(msg, 11, reply, &reply_words) && alloc_calls == 0);
    }
    reset(NORMAL); make_mem(msg, 1); msg[4] = ADRENO8_HFI_MEM_HOST;
    assert(adreno8_hwsched_request(msg, 11, reply, &reply_words) && reply[7] == 0x60010000);
    assert(reply[1] == 0 && reply[3] == ADRENO8_HFI_MEM_HOST);
    msg[4] = 0;
    assert(adreno8_hwsched_request(msg, 11, reply, &reply_words) && reply[7] == 0x60010000);
    assert(!adreno8_hwsched_request(NULL, 0, reply, &reply_words));
    assert(!adreno8_hwsched_request(msg, 0, reply, &reply_words));
    uint32_t counter[] = {header(110,3,0,12), UINT32_MAX, UINT32_MAX};
    assert(adreno8_hwsched_request(counter, 3, reply, &reply_words));
    assert(reply_words == 5 && reply[1] == UINT32_MAX && reply[2] == UINT32_MAX && !reply[3] && !reply[4]);
    counter[0] = header(110,2,0,12);
    assert(!adreno8_hwsched_request(counter, 2, reply, &reply_words));
    puts("HWSCHED startup, MEM_ALLOC v0/v1, counter rejection, ACK routing, raw commands and bounded failures: PASS");
}
'''

with tempfile.TemporaryDirectory(prefix="adreno8-hwsched-") as directory:
    target = Path(directory)
    clock_block = "\tif (!adreno8_clock_started()) {\n\t\tadreno8_hwsched_memory_failed();\n\t\treturn false;\n\t}\n"
    assert source.count(clock_block) == 1
    mutations = [
        source.replace("return !adreno8_direct_ring && adreno8_hwsched_started", "return adreno8_hwsched_started"),
        source.replace("if (adreno8_direct_ring || adreno8_hwsched_attempted", "if (adreno8_hwsched_attempted"),
        source.replace(clock_block, ""),
        source.replace("if (!adreno8_clock_started())", "if (!adreno8_clock_started() && false)"),
        source.replace(clock_block, "").replace("\tif (!adreno8_hfi_send(14,", clock_block + "\tif (!adreno8_hfi_send(14,"),
    ]
    for index, text in enumerate([source] + mutations):
        (target / "check.c").write_text(text)
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Wno-unused-parameter",
                        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(DEV),
                        str(target / "check.c"), "-o", str(target / "check")], check=True)
        result = subprocess.run([str(target / "check")], capture_output=True, text=True)
        if index == 0:
            result.check_returncode()
            print(result.stdout, end="")
        else:
            assert result.returncode != 0, index
    print("HWSCHED ordinary-mode rejection and START clock transition: 5 mutations rejected: PASS")
