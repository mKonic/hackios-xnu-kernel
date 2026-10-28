#!/usr/bin/env python3
"""Exercise production context submission and the shared HFI pump on the host."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
hfi = (DEV / "adreno8_hfi.c").read_text()
transport = hfi[hfi.index("static unsigned adreno8_hfi_seq;"):hfi.index("/* ---- step 12:")]

source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uintptr_t vm_offset_t;
#define _DEV_ADRENO8_PRIVATE_H
#include "adreno8_wire_private.h"
#include "adreno8_memory_private.h"
#include "adreno8_hwsched_private.h"
#include "adreno8_context_private.h"
struct adreno8_block { vm_offset_t base; };
struct adreno8_gpu_space {
    uint32_t context;
    struct { uint64_t phys; } tables[1];
    unsigned table_count;
    bool is_on;
};
struct adreno8_gpu_space adreno8_gpu_space;
static struct adreno8_block adreno8_gmu;
static uint32_t table[1024], commands[1024], responses[1024], debug[1024], registers[4];
static bool adreno8_direct_ring;
static vm_offset_t adreno8_hfi_view_table, adreno8_hfi_view_cmd;
static vm_offset_t adreno8_hfi_view_resp, adreno8_hfi_view_debug;
static uint32_t adreno8_step = 22, adreno8_gpu8_secure_mode = 4, adreno8_inflight;
static bool adreno8_reset_required;
static bool gpu_ready, hardware_fault, closed, idle_ok = true;
static unsigned failures, snapshots, idle_calls, command_count, dispatches, sleeps, mode;
static uint64_t clock_ns;
static uint32_t ack_status, last_packet[255], last_packet_words, pending_timestamp, pending_read;
enum { NORMAL, DELAY_EOP, RETIRE_ONLY, NACK_REGISTER, NACK_POINTERS, NACK_CLOSE,
       BAD_ACK, CONTEXT_BAD, ERROR_MESSAGE, CORRUPT_HFI, CORRUPT_RESULT, BAD_SOP,
       GMU_FAULT, SMMU_FAULT, ALIAS_FAILURE, CORRUPT_CONTEXT, EOP_AHEAD, DELAY_READ, STUCK_READ, HOLD };
#define GMU_HOST2GMU_SET 0
#define GMU_GMU2HOST_INFO 4
#define GMU_GMU2HOST_CLR 8
#define GMU_FW_INIT_RESULT 12
#define GMU_FW_FAILED 0xe00
#define SECURE_MODE_TRANSLATION_B 4u
static int adreno8_say(const char *format, ...) { (void)format; return 0; }
static void adreno8_announce(const char *format, ...) { (void)format; }
#define printf adreno8_say
static void adreno8_settle(void) {}
static uint64_t mach_absolute_time(void) { return clock_ns; }
static void absolutetime_to_nanoseconds(uint64_t value, uint64_t *out) { *out = value; }
bool adreno8_gpu_faulted(void) { return hardware_fault; }
static void adreno8_gmu_post_mortem(void) { snapshots++; }
void adreno8_hwsched_memory_failed(void) { failures++; closed = true; }
bool adreno8_hwsched_ready(void) { return !closed; }
bool adreno8_gpu_memory_ready(void) { return adreno8_gpu_space.is_on && !closed; }
bool adreno8_gpu_commands_ready(void) { return gpu_ready && !closed; }
bool adreno8_gpu_wait_idle(const char *what) { (void)what; idle_calls++; return idle_ok; }
bool adreno8_hwsched_request(const uint32_t *msg, uint32_t words, uint32_t *reply, uint32_t *reply_words) {
    (void)msg; (void)words; (void)reply; (void)reply_words; return false;
}

static struct {
    struct adreno8_memory memory;
    uint8_t *device;
    uint32_t gpu_prot, gmu_prot;
    unsigned publishes, acquires;
} allocations[8];
static unsigned allocation_count;

static struct adreno8_memory *allocate(const char *name, uint32_t size, uint32_t alignment,
                                      uint32_t gpu_prot, uint32_t gmu_prot) {
    assert(!closed && allocation_count < 8 && size == 4096 && alignment == 4096);
    unsigned i = allocation_count++;
    struct adreno8_memory *m = &allocations[i].memory;
    m->name = name; m->size = size;
    m->memory = (uintptr_t)calloc(1, size); assert(m->memory);
    allocations[i].device = calloc(1, size); assert(allocations[i].device);
    allocations[i].gpu_prot = gpu_prot; allocations[i].gmu_prot = gmu_prot;
    m->gpu_address = gpu_prot ? 0x10000000 + i * 0x10000 : 0;
    m->gmu_address = gmu_prot ? 0x60000000 + i * 0x10000 : 0;
    return m;
}
struct adreno8_memory *adreno8_gpu_alloc(const char *name, uint32_t size, uint32_t align, uint32_t prot) {
    return allocate(name, size, align, prot, 0);
}
struct adreno8_memory *adreno8_gmu_alloc(const char *name, uint32_t size, uint32_t align, uint32_t prot) {
    return allocate(name, size, align, 0, prot);
}
static unsigned allocation_index(const struct adreno8_memory *memory) {
    for (unsigned i = 0; i < allocation_count; i++)
        if (memory == &allocations[i].memory) return i;
    abort();
}
bool adreno8_memory_publish(const struct adreno8_memory *m, uint32_t offset, uint32_t size) {
    unsigned i = allocation_index(m);
    assert(offset <= m->size && size <= m->size - offset);
    memcpy(allocations[i].device + offset, (void *)(m->memory + offset), size);
    allocations[i].publishes++; return true;
}
bool adreno8_memory_acquire(const struct adreno8_memory *m, uint32_t offset, uint32_t size) {
    unsigned i = allocation_index(m);
    assert(offset <= m->size && size <= m->size - offset);
    memcpy((void *)(m->memory + offset), allocations[i].device + offset, size);
    allocations[i].acquires++; return true;
}
static uint64_t kvtophys(vm_offset_t address) { return address; }
static vm_offset_t ml_io_map_wcomb(uint64_t physical, uint32_t size) {
    assert(size == 4096);
    if (mode == ALIAS_FAILURE) return 0;
    for (unsigned i = 0; i < allocation_count; i++)
        if (physical == allocations[i].memory.memory) return (uintptr_t)allocations[i].device;
    abort();
}
static uint32_t header(unsigned id, unsigned words, unsigned type, unsigned seq) {
    return id | (words << 8) | (type << 16) | (seq << 20);
}
static void append(unsigned queue, const uint32_t *msg, unsigned words) {
    uint32_t *h = table + ADRENO8_HFI_HEADER(queue);
    uint32_t *data = queue == 1 ? responses : debug;
    for (unsigned i = 0; i < words; i++) data[(h[11] + i) % 1024] = msg[i];
    h[11] = (h[11] + ((words + 3) & ~3u)) % 1024;
}
static uint32_t *device_gpu(uint64_t address, unsigned bytes, uint32_t prot) {
    for (unsigned i = 0; i < allocation_count; i++) {
        struct adreno8_memory *m = &allocations[i].memory;
        if (m->gpu_address && address >= m->gpu_address && address + bytes <= (uint64_t)m->gpu_address + m->size) {
            assert((allocations[i].gpu_prot & prot) == prot);
            return (uint32_t *)(allocations[i].device + address - m->gpu_address);
        }
    }
    abort();
}
static void consume_context(void) {
    uint32_t *q = (uint32_t *)allocations[0].device;
    unsigned read = q[5], words = (q[12 + read] >> 8) & 0xff;
    assert(words >= 11 && words <= 254 && q[2] == 1012);
    for (unsigned i = 0; i < words; i++) last_packet[i] = q[12 + (read + i) % 1012];
    last_packet_words = words;
    unsigned padded = (words + 3) & ~3u;
    for (unsigned i = words; i < padded; i++) assert(q[12 + (read + i) % 1012] == 0xfafafafa);
    assert((last_packet[0] & 0xff) == 130 && last_packet[1] == 1 && last_packet[2] == 0x20);
    assert(last_packet[4] == 0 && last_packet[5] == 0 && last_packet[7] == 0);
    assert(words == 8 + last_packet[6] * 3 && last_packet[6] <= 82);
    uint32_t timestamp = last_packet[3];
    uint32_t *timestamps = (uint32_t *)allocations[1].device;
    timestamps[0] = mode == BAD_SOP ? timestamp + 1 : timestamp;
    for (unsigned i = 0; i < last_packet[6]; i++) {
        uint64_t addr = last_packet[8 + i * 3] | (uint64_t)last_packet[9 + i * 3] << 32;
        unsigned size = last_packet[10 + i * 3];
        assert(size == 24); /* HFI byte length; CP IB length is six dwords. */
        uint32_t *ib = device_gpu(addr, size, ADRENO8_MEM_READ | ADRENO8_MEM_EXEC);
        assert(ib[0] == 0x703d8003 && ib[2] == 0); /* CP_MEM_WRITE, odd parity. */
        assert(ib[4] == 0x70928000 && ib[5] == 0x70268000);
        assert(allocations[2].gpu_prot == 5 && allocations[3].gpu_prot == 3);
        uint32_t *result = device_gpu(ib[1], 4, ADRENO8_MEM_WRITE);
        *result = mode == CORRUPT_RESULT ? ib[3] ^ 1 : ib[3];
    }
    /* pending_read holds the index plus one, so index 0 stays distinguishable. */
    if (mode == DELAY_READ) pending_read = (read + padded) % 1012 + 1;
    else if (mode != STUCK_READ) q[5] = (read + padded) % 1012;
    uint32_t retire[15] = {header(133, 15, 0, 7), 1, timestamp, 0};
    append(1, retire, 15);
    if (mode == DELAY_EOP) pending_timestamp = timestamp;
    else if (mode != RETIRE_ONLY) timestamps[2] = timestamp;
    if (mode == CONTEXT_BAD || mode == ERROR_MESSAGE) {
        uint32_t error[] = {header(mode == CONTEXT_BAD ? 150 : 100, 3, 0, 8), 1, 0xdead};
        append(1, error, 3);
    }
    if (mode == CORRUPT_HFI) table[ADRENO8_HFI_HEADER(1) + 10] = 1;
    if (mode == GMU_FAULT) registers[1] = 1u << 15;
    if (mode == SMMU_FAULT) hardware_fault = true;
    if (mode == CORRUPT_CONTEXT) q[5] = 1;
    if (mode == EOP_AHEAD) timestamps[2] = timestamp + 1;
    dispatches++;
}
static uint32_t adreno8_rd(vm_offset_t address) { return *(uint32_t *)address; }
static void adreno8_wr(vm_offset_t address, uint32_t value) {
    if (address == adreno8_gmu.base + GMU_GMU2HOST_CLR) {
        registers[1] &= ~value; return;
    }
    *(uint32_t *)address = value;
    if (address != adreno8_gmu.base + GMU_HOST2GMU_SET) return;
    if (value == 4) { if (mode != HOLD) consume_context(); return; }
    assert(value == 1);
    uint32_t *q = table + ADRENO8_HFI_HEADER(0);
    unsigned read = q[10], size = (commands[read] >> 8) & 0xff;
    uint32_t msg[10]; assert(size <= 10 && command_count < 16);
    for (unsigned i = 0; i < size; i++) msg[i] = commands[(read + i) % 1024];
    q[10] = (read + ((size + 3) & ~3u)) % 1024;
    command_count++;
    switch (msg[0] & 0xff) {
    case 128:
        assert(size == 7 && msg[1] == 1 && msg[2] == 0x101072);
        assert(msg[3] == 0x123000 && msg[4] == 3 && msg[5] == 1 && msg[6] == 0);
        assert(allocations[0].gmu_prot == 11 && allocations[1].gpu_prot == 11);
        break;
    case 134:
        assert(size == 10 && msg[1] == 1 && msg[2] == allocations[1].memory.gpu_address);
        assert(msg[3] == 0 && msg[4] == msg[2] + 8 && msg[5] == 0);
        assert(msg[6] == 0 && msg[7] == 0 && msg[8] == 0 && msg[9] == allocations[0].memory.gmu_address);
        break;
    case 129:
        assert(size == 3 && msg[1] == 1 && msg[2] == ((uint32_t *)allocations[1].device)[2]);
        break;
    default: abort();
    }
    unsigned id = msg[0] & 0xff;
    uint32_t status = (mode == NACK_REGISTER && id == 128) || (mode == NACK_POINTERS && id == 134) ||
                      (mode == NACK_CLOSE && id == 129) ? UINT32_MAX : ack_status;
    uint32_t reply[] = {header(id, 3, 1, 2), msg[0] ^ (mode == BAD_ACK ? 1u << 20 : 0), status};
    append(1, reply, 3);
}
static void adreno8_sleep_us(uint32_t us) {
    clock_ns += (uint64_t)us * 1000; sleeps++;
    /* HOLD: the GPU takes one queued submission per sleep instead of at the doorbell. */
    uint32_t *held = allocation_count ? (uint32_t *)allocations[0].device : NULL;
    if (mode == HOLD && held && held[5] != held[6]) consume_context();
    if (pending_timestamp && sleeps % 5 == 0) {
        ((uint32_t *)allocations[1].device)[2] = pending_timestamp;
        pending_timestamp = 0;
    }
    if (pending_read && sleeps % 3 == 0) {
        ((uint32_t *)allocations[0].device)[5] = pending_read - 1;
        pending_read = 0;
    }
}
''' + transport + r'''
#include "adreno8_context.c"
#undef printf

static void reset(unsigned next_mode) {
    for (unsigned i = 0; i < allocation_count; i++) {
        free((void *)allocations[i].memory.memory); free(allocations[i].device);
    }
    memset(allocations, 0, sizeof(allocations)); allocation_count = 0;
    memset(commands, 0, sizeof(commands)); memset(responses, 0, sizeof(responses));
    memset(debug, 0, sizeof(debug)); memset(registers, 0, sizeof(registers));
    adreno8_hfi_init_table(table, 0x60006000, false);
    adreno8_hfi_view_table = (uintptr_t)table; adreno8_hfi_view_cmd = (uintptr_t)commands;
    adreno8_hfi_view_resp = (uintptr_t)responses; adreno8_hfi_view_debug = (uintptr_t)debug;
    adreno8_gmu.base = (uintptr_t)registers;
    gpu_ready = idle_ok = true;
    hardware_fault = closed = adreno8_reset_required = false;
    failures = snapshots = idle_calls = command_count = dispatches = sleeps = 0;
    clock_ns = ack_status = pending_timestamp = pending_read = last_packet_words = 0;
    adreno8_gpu_space.context = 0; adreno8_gpu_space.is_on = true;
    adreno8_gpu_space.table_count = 1; adreno8_gpu_space.tables[0].phys = 0x300123000ULL;
    context_state = CONTEXT_COLD;
    context_queue = context_timestamps = test_ib = test_result = NULL;
    context_queue_view = context_timestamp_view = 0;
    context_last = context_sequence = 0;
    context_tested = false;
    adreno8_inflight = 4;
    mode = next_mode;
}
static void prepare_ib(void) {
    assert(adreno8_context_prepare());
    test_ib = adreno8_gpu_alloc("test", 4096, 4096, 5);
    test_result = adreno8_gpu_alloc("result", 4096, 4096, 3);
    assert(adreno8_pm4_scratch((uint32_t *)test_ib->memory, 1024, test_result->gpu_address, 0x11223344) == 6);
    assert(adreno8_memory_publish(test_ib, 0, 24));
}
static uint32_t submit(void) {
    uint64_t address = test_ib->gpu_address; uint32_t words = 6;
    return adreno8_context_submit(&address, &words, 1);
}
int main(void) {
    reset(NORMAL);
    assert(!adreno8_context_wait_idle("before self-test") && idle_calls == 0 && !closed);
    reset(NORMAL); ack_status = 7; /* Vendor accepts every status except UINT32_MAX. */
    assert(adreno8_context_test() && allocation_count == 4 && dispatches == 2 && command_count == 3);
    assert(context_state == CONTEXT_PREPARED && context_last == 2 && idle_calls == 4);
    assert(adreno8_context_wait_idle("after self-test") && idle_calls == 5);
    context_last = 0;
    assert(adreno8_context_wait_idle("without a pending timestamp") && idle_calls == 6);
    context_last = 2;
    assert(allocations[0].publishes == 1 && allocations[1].publishes == 1);
    assert(allocations[0].acquires == 0 && allocations[1].acquires == 0);
    assert(!adreno8_context_done(3) && adreno8_context_done(2));
    assert(adreno8_context_prepare() && allocation_count == 4 && submit() == 3);
    assert(command_count == 5 && adreno8_context_done(3) && adreno8_context_close());
    assert(command_count == 6 && context_state == CONTEXT_PREPARED);

    reset(DELAY_EOP); assert(adreno8_context_test() && sleeps == 10);
    /* EOP and idle can be visible before the GMU advances the queue read index. */
    reset(DELAY_READ); assert(adreno8_context_test() && dispatches == 2 && sleeps == 6);
    reset(STUCK_READ); assert(!adreno8_context_test() && dispatches == 1 && context_state == CONTEXT_FAILED);
    assert(clock_ns >= 20000000ULL && closed);
    reset(NORMAL); prepare_ib();
    uint32_t *q = (uint32_t *)context_queue_view;
    q[5] = q[6] = 1008; context_sequence = 4095;
    assert(submit() == 1 && q[5] == 8 && q[6] == 8);
    assert(last_packet_words == 11 && last_packet[0] >> 20 == 0 && adreno8_context_done(1));

    reset(NORMAL); prepare_ib(); q = (uint32_t *)context_queue_view; q[5] = 12;
    assert(submit() == 0 && dispatches == 0 && context_state == CONTEXT_FAILED && closed && clock_ns >= 200000000ULL);
    assert(q[6] == 0 && allocation_count == 4);
    reset(NORMAL); prepare_ib(); q = (uint32_t *)context_queue_view; q[5] = 3;
    assert(submit() == 0 && dispatches == 0 && context_state == CONTEXT_FAILED);

    reset(NORMAL); prepare_ib();
    uint64_t addresses[83]; uint32_t lengths[83];
    for (unsigned i = 0; i < 83; i++) { addresses[i] = test_ib->gpu_address; lengths[i] = 6; }
    assert(adreno8_context_submit(addresses, lengths, 82) == 1 && last_packet_words == 254);
    reset(NORMAL);
    assert(adreno8_context_submit(addresses, lengths, 83) == 0 && allocation_count == 0 && command_count == 0);

    reset(RETIRE_ONLY); assert(!adreno8_context_test() && dispatches == 1 && clock_ns == 200000000ULL);
    assert(allocation_count == 4 && closed && context_last == 1 && !adreno8_context_done(1));
    assert(!adreno8_context_prepare() && !adreno8_context_close() && command_count == 2);
    reset(BAD_ACK); assert(!adreno8_context_test() && command_count == 1 && clock_ns == 1000000000ULL);
    for (unsigned m = NACK_REGISTER; m <= NACK_CLOSE; m++) {
        reset(m); assert(!adreno8_context_test() && closed && context_state == CONTEXT_FAILED);
        unsigned count = command_count;
        assert(!adreno8_context_prepare() && !adreno8_context_close() && command_count == count && allocation_count == 4);
    }
    for (unsigned m = CONTEXT_BAD; m <= SMMU_FAULT; m++) {
        reset(m); assert(!adreno8_context_test() && closed && context_state == CONTEXT_FAILED && allocation_count == 4);
        assert(!adreno8_context_done(context_last));
    }
    reset(ALIAS_FAILURE); assert(!adreno8_context_test() && allocation_count == 2 && command_count == 0);
    reset(CORRUPT_CONTEXT); assert(!adreno8_context_test() && context_state == CONTEXT_FAILED);
    reset(EOP_AHEAD); assert(!adreno8_context_test() && context_state == CONTEXT_FAILED);
    reset(NORMAL); prepare_ib(); context_last = UINT32_MAX;
    ((uint32_t *)context_timestamp_view)[2] = UINT32_MAX;
    assert(submit() == 1 && adreno8_context_done(1));
    /* Four submissions queue without waiting; the fifth waits for the first to retire. */
    reset(NORMAL); prepare_ib(); mode = HOLD;
    for (uint32_t i = 1; i <= 4; i++) assert(submit() == i);
    assert(sleeps == 0 && dispatches == 0 && !adreno8_context_done(1));
    assert(submit() == 5 && sleeps == 1 && dispatches == 1 && adreno8_context_done(1) && !adreno8_context_done(2));
    /* adreno8_inflight=1: each submission waits for the one before. */
    reset(NORMAL); prepare_ib(); mode = HOLD; adreno8_inflight = 1;
    assert(submit() == 1 && sleeps == 0);
    assert(submit() == 2 && sleeps == 1 && dispatches == 1);
    /* Out-of-range values fall back to 1. */
    reset(NORMAL); prepare_ib(); mode = HOLD; adreno8_inflight = 9;
    assert(submit() == 1 && submit() == 2 && sleeps == 1);
    /* A full queue is waited on until the GMU reads it; it fails only after the budget (above). */
    reset(NORMAL); prepare_ib(); q = (uint32_t *)context_queue_view; q[5] = 12; pending_read = 0 + 1;
    assert(submit() == 1 && sleeps == 3 && context_state == CONTEXT_REGISTERED);
    reset(NORMAL); gpu_ready = false;
    assert(!adreno8_context_test() && allocation_count == 0 && dispatches == 0);
    reset(NORMAL); idle_ok = false;
    assert(!adreno8_context_test() && dispatches == 1 && context_state == CONTEXT_FAILED);
    reset(NORMAL);
    puts("HWSCHED context: wire sizes, permissions, cache views, wrap/full queue, submissions in flight, reopen, ACK/EOP faults and retention: PASS");
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix="adreno8-context-") as directory:
    target = Path(directory)
    (target / "check.c").write_text(source)
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Wno-unused-const-variable",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(DEV),
                    str(target / "check.c"), "-o", str(target / "check")], check=True)
    subprocess.run([str(target / "check")], check=True)
