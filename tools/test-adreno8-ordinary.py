#!/usr/bin/env python3
"""Exercise ordinary HFI layout and startup against pinned mainline and production C."""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
PIN = "efbda69d92e6a4822c5f2d09910f547872a9498d"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--mainline-dir", required=True, type=Path, help=f"Linux adreno sources at {PIN}")
args = parser.parse_args()
mainline = {}
for name, digest in {
    "a6xx_hfi.c": "e91ef0257331983c7ea1f5531efdfa8a27f2a7b4033a9ded1adad1cfd4026173",
    "a6xx_hfi.h": "1e53d022fccaec3d658a4e939d0572c5711597fc8db5c4dec97ca08ee365c7e2",
}.items():
    data = (args.mainline_dir / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest, f"unexpected mainline input: {name}"
    mainline[name] = data.decode()


def function(text, name, result):
    start = text.index("\n" + name + "(") + 1 if "\n" + name + "(" in text else text.index(name + "(")
    return result + "\n" + text[start:text.index("\n}\n", start) + 2] + "\n"


reference = "\n".join(re.search(r"struct " + name + r" \{.*?\n\} __packed;", mainline["a6xx_hfi.h"], re.S)[0]
                      for name in ("a6xx_hfi_queue_table_header", "a6xx_hfi_queue_header"))
reference += r'''
struct a6xx_hfi_queue {
    struct a6xx_hfi_queue_header *header; int lock; void *data; int seqnum;
    int history[8]; unsigned history_idx;
};
struct a6xx_gmu_bo { void *virt; uint64_t iova; };
struct a6xx_gmu { struct a6xx_gmu_bo hfi; struct a6xx_hfi_queue queues[3]; bool legacy; };
#define spin_lock_init(p) (*(p) = 0)
#define atomic_set(p, v) (*(p) = (v))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define SZ_4K 4096u
#define HFI_MAX_QUEUES 3
#define WARN_ON(c) assert(!(c))
'''
reference += function(mainline["a6xx_hfi.c"], "a6xx_hfi_queue_init", "static void")
reference += function(mainline["a6xx_hfi.c"], "a6xx_hfi_init", "void")
hfi = (DEV / "adreno8_hfi.c").read_text()
transport = hfi[hfi.index("static unsigned adreno8_hfi_seq;"):hfi.index("/* ---- step 12:")]
ordinary = function(hfi, "adreno8_ordinary_start", "bool") + function(hfi, "adreno8_ordinary_ready", "bool")
source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include "adreno8_wire_private.h"
typedef uint32_t u32;
typedef uint64_t u64;
typedef uintptr_t vm_offset_t;
#define __packed __attribute__((packed))
''' + reference + r'''
#define GMU_HOST2GMU_SET 0
#define GMU_GMU2HOST_INFO 4
#define GMU_GMU2HOST_CLR 8
#define GMU_FW_INIT_RESULT 12
#define GMU_FW_FAILED 0xe00
#define SECURE_MODE_TRANSLATION_B 4u
static struct { vm_offset_t base; } adreno8_gmu;
static vm_offset_t adreno8_hfi_view_table, adreno8_hfi_view_cmd, adreno8_hfi_view_resp, adreno8_hfi_view_debug;
static uint32_t regs[4], adreno8_gpu8_secure_mode, adreno8_step, adreno8_newest_step;
static bool adreno8_direct_ring, adreno8_gmu_talks, adreno8_gpu8_keep, adreno8_gpu8_return;
static bool adreno8_reset_required, cx_ready, clock_ok, respond, gpu_fault;
static unsigned scenario, fail_at, sent, barriers, published_barrier, memory_failures, postmortems;
static unsigned request_calls, clock_calls, sleeps, checks, io_reads, io_writes;
static uint64_t time_ns;
static uint32_t published_header, last_id, sent_ids[8], sent_words[8];
static uint32_t *table, *command, *response, *debug;
static int adreno8_say(const char *format, ...) { return 0; }
#define printf adreno8_say
static void adreno8_announce(const char *format, ...) {}
static bool adreno8_cx_ready(void) { return cx_ready; }
static bool adreno8_gpu_faulted(void) { return gpu_fault; }
static void adreno8_hwsched_memory_failed(void) { memory_failures++; }
static void adreno8_gmu_post_mortem(void) { postmortems++; }
static bool adreno8_hwsched_request(const uint32_t *msg, uint32_t words, uint32_t *reply, uint32_t *count) {
    request_calls++; *count = 1; reply[0] = msg[0]; return true;
}
static bool adreno8_clock_started(void) { assert(sent == 3 && last_id == 10); clock_calls++; return clock_ok; }
static uint32_t adreno8_rd(vm_offset_t address) { io_reads++; return *(uint32_t *)address; }
static void append(unsigned queue, const uint32_t *msg, unsigned words) {
    uint32_t *h = table + ADRENO8_HFI_HEADER(queue), *data = queue == 1 ? response : debug;
    for (unsigned i = 0; i < words; i++) data[(h[11] + i) % 1024] = msg[i];
    h[11] = (h[11] + ((words + 3) & ~3u)) % 1024;
}
static void emit_ack(unsigned queue, uint32_t echo, unsigned words, uint32_t status, uint32_t error) {
    uint32_t msg[4] = {(1u << 16) | (words << 8) | 126, echo, status, error};
    append(queue, msg, words);
}
static void firmware_reply(void) {
    unsigned id = published_header & 255, words = id == 11 ? 4 : 3;
    bool fail = fail_at == sent;
    if (fail && scenario == 1) return;
    if (fail && scenario == 8) { regs[1] = 1u << 15; return; }
    if (fail && scenario == 9) { regs[3] = GMU_FW_FAILED; return; }
    if (fail && scenario == 10) { table[ADRENO8_HFI_HEADER(1) + 11] = 1; return; }
    if (fail && (scenario == 4 || scenario == 5)) {
        uint32_t msg[8] = {(8u << 8) | (scenario == 4 ? 20 : 110), 1, 2, 3, 4, 5, 6, 7};
        append(1, msg, 8);
    }
    if (fail && (scenario == 6 || scenario == 7)) {
        uint32_t msg[2] = {(2u << 8) | (scenario == 6 ? 100 : 150), 1}; append(2, msg, 2);
    }
    if (fail && scenario == 11) words = 2;
    unsigned queue = fail && scenario == 13 ? 2 : 1;
    uint32_t echo = published_header ^ (fail && scenario == 12 ? 1u << 20 : 0);
    emit_ack(queue, echo, words, fail && scenario == 2 ? 7 : 0,
             fail && scenario == 3 ? 6 : 0);
}
static void adreno8_wr(vm_offset_t address, uint32_t value) {
    io_writes++;
    if (address == adreno8_hfi_view_table + (ADRENO8_HFI_HEADER(0) + 11) * 4) {
        unsigned start = table[ADRENO8_HFI_HEADER(0) + 11];
        published_header = command[start]; unsigned words = (published_header >> 8) & 255;
        assert(barriers > 0 && value == (start + ((words + 3) & ~3u)) % 1024);
        for (unsigned i = words; i < ((words + 3) & ~3u); i++)
            assert(command[(start + i) % 1024] == 0xfafafafa);
        published_barrier = barriers;
        if (respond) {
            assert(sent < 8); sent_ids[sent] = published_header & 255; sent_words[sent++] = words;
            last_id = published_header & 255;
            if (last_id == 11) {
                assert(words == 4 && command[(start + 1) % 1024] == 9);
                assert(!command[(start + 2) % 1024] && !command[(start + 3) % 1024]);
            } else if (last_id == 14) assert(words == 2 && !command[(start + 1) % 1024]);
            else if (last_id == 30) {
                assert(words == 4 && command[(start + 1) % 1024] == 1);
                assert(command[(start + 2) % 1024] == 1 && command[(start + 3) % 1024] == 2);
            } else assert(last_id == 10 && words == 1);
        }
    }
    *(uint32_t *)address = value;
    if (address == adreno8_gmu.base + GMU_HOST2GMU_SET) {
        assert(value == 1 && barriers > published_barrier);
        table[ADRENO8_HFI_HEADER(0) + 10] = table[ADRENO8_HFI_HEADER(0) + 11];
        if (respond) firmware_reply();
    }
}
static void adreno8_settle(void) { barriers++; }
static uint64_t mach_absolute_time(void) { return time_ns; }
static void absolutetime_to_nanoseconds(uint64_t t, uint64_t *ns) { *ns = t; }
static void adreno8_sleep_us(uint32_t us) {
    time_ns += (uint64_t)us * 1000; sleeps++;
    if ((scenario == 12 || scenario == 13) && sleeps == 1)
        emit_ack(1, published_header, last_id == 11 ? 4 : 3, 0, 0);
}
''' + transport + ordinary + r'''
static void reset(unsigned test, unsigned at) {
    memset(table, 0, 0x4000); memset(regs, 0, sizeof(regs));
    adreno8_hfi_init_table(table, 0x60006000, true);
    command = table + 1024; response = table + 2048; debug = table + 3072;
    adreno8_hfi_view_table = (uintptr_t)table; adreno8_hfi_view_cmd = (uintptr_t)command;
    adreno8_hfi_view_resp = (uintptr_t)response; adreno8_hfi_view_debug = (uintptr_t)debug;
    adreno8_gmu.base = (uintptr_t)regs;
    adreno8_direct_ring = adreno8_gmu_talks = adreno8_gpu8_keep = true;
    cx_ready = clock_ok = respond = adreno8_hfi_tables_sent = true;
    adreno8_gpu8_return = adreno8_reset_required = gpu_fault = false;
    adreno8_gpu8_secure_mode = 0; adreno8_step = 14; adreno8_newest_step = 17;
    adreno8_ordinary_attempted = adreno8_ordinary_started = false; adreno8_hfi_seq = 4094;
    scenario = test; fail_at = at; sent = barriers = published_barrier = memory_failures = postmortems = 0;
    request_calls = clock_calls = sleeps = io_reads = io_writes = 0; time_ns = 0;
}
static void success(void) {
    assert(adreno8_ordinary_start() && adreno8_ordinary_ready()); checks++;
    assert(sent == 3 && sent_ids[0] == 11 && sent_ids[1] == 14 && sent_ids[2] == 10);
    assert(sent_words[0] == 4 && sent_words[1] == 2 && sent_words[2] == 1);
    assert(clock_calls == 1 && adreno8_hfi_seq == 1 && adreno8_reset_required);
    assert(!request_calls && !memory_failures && !postmortems);
    assert(adreno8_hfi_poll() && adreno8_ordinary_ready());
}
static void closed_transport(void) {
    memset(regs, 0, sizeof(regs)); adreno8_hfi_init_table(table, 0x60006000, true);
    unsigned reads = io_reads, writes = io_writes, before = sent;
    const uint32_t handle = 0;
    assert(!adreno8_hfi_send(14, &handle, 1, "after permanent failure"));
    assert(!adreno8_hfi_poll() && !adreno8_ordinary_ready());
    assert(io_reads == reads && io_writes == writes && sent == before && !request_calls); checks++;
}
int main(void) {
    table = aligned_alloc(PAGE_SIZE, 0x4000); assert(table);
    uint32_t *expected = calloc(1, 0x4000); assert(expected);
    struct a6xx_gmu mainline_gmu = {.hfi = {expected, 0x60006000}};
    a6xx_hfi_init(&mainline_gmu); reset(0, 0);
    assert(!memcmp(table, expected, 0x4000));
    assert(table[1] == 168 && table[2] == 6 && table[3] == 12 && table[4] == 3 && table[5] == 3);
    for (unsigned q = 0; q < 3; q++) {
        uint32_t *h = table + 6 + 12 * q;
        assert(h[1] == 0x60007000 + q * 4096 && h[2] == (0xa00u | q));
        assert(h[6] == 1 && h[7] == 1 && h[8] == 1);
    }
    success();
    const uint32_t vote[] = {1, 1, 2};
    assert(adreno8_hfi_send(30, vote, 3, "READY DCVS") && adreno8_ordinary_ready() && sent == 4);
    assert(adreno8_hfi_poll() && adreno8_ordinary_ready()); checks++;
    unsigned before = sent; assert(!adreno8_ordinary_start() && !adreno8_ordinary_ready() && sent == before);
    closed_transport();
    for (unsigned bad = 0; bad < 9; bad++) {
        reset(0, 0);
        if (bad == 0) adreno8_direct_ring = false;
        if (bad == 1) adreno8_step = 13;
        if (bad == 2) adreno8_newest_step = 23;
        if (bad == 3) cx_ready = false;
        if (bad == 4) adreno8_gmu_talks = false;
        if (bad == 5) adreno8_hfi_tables_sent = false;
        if (bad == 6) adreno8_gpu8_keep = false;
        if (bad == 7) adreno8_gpu8_return = true;
        if (bad == 8) adreno8_ordinary_attempted = true;
        assert(!adreno8_ordinary_start() && !adreno8_ordinary_ready() && !sent && !clock_calls); checks++;
    }
    for (unsigned at = 1; at <= 3; at++) for (unsigned test = 1; test <= 13; test++) {
        if ((test == 3 && at != 1) || ((test == 4 || test == 5) && at != 3)) continue;
        reset(test, at);
        if (test == 12 || test == 13) { success(); assert(sleeps == 1); continue; }
        assert(!adreno8_ordinary_start() && !adreno8_ordinary_ready()); checks++;
        assert(sent == at && !clock_calls && !request_calls && memory_failures && adreno8_reset_required);
        assert(!adreno8_ordinary_start() && !adreno8_ordinary_ready() && sent == at);
        closed_transport();
    }
    reset(0, 0); clock_ok = false;
    assert(!adreno8_ordinary_start() && !adreno8_ordinary_ready() && sent == 3 && clock_calls == 1); checks++;
    closed_transport();
    for (unsigned bad = 0; bad < 9; bad++) {
        reset(0, 0); success();
        if (bad == 0 || bad == 1 || bad == 2 || bad == 3) {
            uint32_t msg[8] = {(8u << 8) | (bad == 0 ? 100 : bad == 1 ? 150 : bad == 2 ? 20 : 110)};
            append(1, msg, 8);
        }
        if (bad == 4) regs[1] = 1u << 15;
        if (bad == 5) regs[3] = GMU_FW_FAILED;
        if (bad == 6) table[ADRENO8_HFI_HEADER(1) + 11] = 1;
        if (bad == 7) adreno8_hfi_view_debug = 0;
        if (bad == 8) gpu_fault = true;
        assert(!adreno8_hfi_poll() && !adreno8_ordinary_ready() && memory_failures && !request_calls); checks++;
        before = sent; assert(!adreno8_ordinary_start() && sent == before);
        closed_transport();
    }
    for (unsigned bad = 0; bad < 3; bad++) {
        reset(0, 0);
        if (bad == 0) regs[1] = 1u << 15;
        if (bad == 1) table[ADRENO8_HFI_HEADER(1) + 11] = 1;
        if (bad == 2) { uint32_t msg[2] = {(2u << 8) | 20}; append(1, msg, 2); }
        assert(!adreno8_hfi_poll() && !adreno8_ordinary_ready()); checks++;
        memset(regs, 0, sizeof(regs)); adreno8_hfi_init_table(table, 0x60006000, true);
        assert(!adreno8_ordinary_start() && !sent && !clock_calls && !request_calls);
        closed_transport();
    }
    reset(0, 0); respond = false;
    uint32_t msg[255], output[255]; for (unsigned i = 0; i < 255; i++) msg[i] = i + 1;
    for (unsigned words = 1; words <= 255; words += 127) {
        uint32_t *h = table + ADRENO8_HFI_HEADER(0); h[10] = 512; h[11] = 1020;
        msg[0] = (words << 8) | 14; assert(adreno8_queue_write(msg, words)); checks++;
        for (unsigned q = 1; q < 3; q++) {
            h = table + ADRENO8_HFI_HEADER(q); h[10] = h[11] = 1020; append(q, msg, words);
            assert(adreno8_queue_read(q, output) == (int)words && !memcmp(output, msg, words * 4));
        }
    }
    uint32_t *h = table + ADRENO8_HFI_HEADER(0); h[10] = 4; h[11] = 0;
    assert(!adreno8_queue_write(msg, 4) && h[11] == 0);
    assert(!adreno8_queue_write(msg, 0) && !adreno8_queue_write(msg, 256));
    free(expected); free(table);
    fprintf(stdout, "Ordinary mainline layout, START/clock, ACK identity, errors, retained failure and rings: %u cases PASS\n", checks);
}
'''
header = (DEV / "adreno8_wire_private.h").read_text()
terminal = "if (adreno8_direct_ring && adreno8_ordinary_attempted && !adreno8_ordinary_started)"
send_at = source.index("bool\nadreno8_hfi_send(")
mutations = {
    "ordinary-byte-size": (source, header.replace("(6u + 12u * ADRENO8_HFI_QUEUE_COUNT) * 4u", "6u + 12u * ADRENO8_HFI_QUEUE_COUNT")),
    "ordinary-queue-offset": (source, header.replace("(queue + 1u) * 4096u", "ADRENO8_HFI_QUEUE_OFFSET(queue)")),
    "ordinary-type": (source, header.replace("0xa00u | q", "q")),
    "ordinary-watermarks": (source, header.replace("if (ordinary) h[6] = h[7] = h[8] = 1;", "")),
    "ordinary-address-fault": (source.replace("adreno8_direct_ring || adreno8_gpu8_secure_mode", "adreno8_gpu8_secure_mode"), header),
    "resource-admission": (source.replace("if (adreno8_direct_ring || id != 10", "if (id != 10"), header),
    "normal-start-status": (source.replace("bool hwsched = !adreno8_direct_ring &&", "bool hwsched ="), header),
    "terminal-poll": (source.replace(terminal, "if (false)", 1), header),
    "terminal-send": (source[:send_at] + source[send_at:].replace(terminal, "if (false)", 1), header),
    "early-attempted": (source.replace("adreno8_reset_required = true;", "adreno8_ordinary_attempted = true; adreno8_reset_required = true;", 1), header),
    "failed-ready": (source.replace("adreno8_ordinary_started = false;", "/* omitted */", 1), header),
    "failure-before-start": (source.replace("if (adreno8_direct_ring) adreno8_ordinary_attempted = true;", ""), header),
    "retry-after-attempt": (source.replace("!adreno8_direct_ring || adreno8_ordinary_attempted ||", "!adreno8_direct_ring ||"), header),
    "start-stage": (source.replace("adreno8_step != 14 ||", ""), header),
    "start-newest": (source.replace("adreno8_newest_step != 17 ||", ""), header),
    "clock-hook": (source.replace("!adreno8_clock_started()", "false"), header),
    "debug-ack": (source.replace("!request || queue != 1 ||", "!request ||"), header),
    "padding": (source.replace("i < aligned; i++", "i < dwords; i++"), header),
    "publish-barrier": (source.replace("adreno8_wr(header + 44, write);\n\tadreno8_settle();", "adreno8_wr(header + 44, write);"), header),
}
with tempfile.TemporaryDirectory(prefix="adreno8-ordinary-") as directory:
    path = Path(directory)
    for label, (body, wire) in {"baseline": (source, header), **mutations}.items():
        assert label == "baseline" or body != source or wire != header, label
        (path / "check.c").write_text(body); (path / "adreno8_wire_private.h").write_text(wire)
        for page in (4096, 16384):
            binary = path / f"check-{page}"
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Wno-unused-parameter", "-Wno-unused-function", "-fsanitize=address,undefined",
                            f"-DPAGE_SIZE={page}u", "-I", str(path), str(path / "check.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            if label == "baseline":
                assert result.returncode == 0, result.stdout + result.stderr
                print(f"PAGE_SIZE={page}: {result.stdout.strip()}")
            else:
                assert result.returncode != 0, f"undetected mutation: {label}, page={page}"
    print(f"Ordinary HFI mutations rejected: {len(mutations)} x 2 page sizes")
