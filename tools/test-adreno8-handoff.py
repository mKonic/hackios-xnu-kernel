#!/usr/bin/env python3
"""Drive one secure handover through the real HFI pump, HWSCHED protocol,
memory path, MP_ASSIGN code, and SMMU fixture in a single program image.

The hwsched test injects the kind-5 MEM_ALLOC message with a fake allocator.
This test keeps the same transport and protocol slices but links the real
allocation chain (adreno8_memory.c, adreno8_secure.c, adreno8_smmu.c over the
SMMU fixture), so every mode contract is checked at the queue boundary: the
pump always fails the START, and the GMU receives no MEM_ALLOC reply words
for any mode; only mode 2 sends SCM calls."""
from pathlib import Path
import re
import runpy
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adreno8_logcheck as logcheck
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
fixture = runpy.run_path(str(ROOT / "tools/test-adreno8-smmu.py"))
stub = fixture["stub"].replace("assert(size == 4096);", "assert(size != 0);")
stub = stub.replace("static int adreno8_say", r'''
uint32_t adreno8_gpu8_secure_mode;
static uint64_t *physical_table(uint64_t pa);

@@SNAPHELP@@static unsigned scm_calls, scm_sleeps, scm_fail_at = 999999u, scm_ebusy;
int32_t adreno8_scm_call(uint64_t function, uint64_t arginfo, uint64_t argument0,
    uint64_t argument1, uint64_t argument2, uint64_t argument3, uint64_t *answer) {
    assert(function == 0x42000c16ull && arginfo == 0x1117ull);
    scm_calls++;

@@SNAP@@
    if (scm_ebusy) {
        *answer = 12;
        return -12;
    }
    *answer = scm_calls == scm_fail_at ? 5 : 0;
    return scm_calls == scm_fail_at ? -5 : 0;
}
void adreno8_sleep_ms(uint32_t ms) { scm_sleeps++; (void)ms; }
static int adreno8_say''', 1)
stub = stub.replace("@@SNAPHELP@@", logcheck.SNAP_HELPERS_C).replace(
    "@@SNAP@@", logcheck.snap_c("scm_calls - 1"))
assert "@@SNAP" not in stub
driver = (DEV / "adreno8_memory.c").read_text()
driver, replaced = re.subn(
    r'static void\nadreno8_memory_invalidate\(.*?\n}',
    'static void adreno8_memory_invalidate(vm_offset_t start, uint32_t size) { invalidations++; }',
    driver, count=1, flags=re.S,
)
assert replaced == 1
# The combined image needs one adreno8_rd for two address spaces: the SMMU
# fixture keeps its byte-offset register file, while the HFI transport points
# its queue views and the GMU window at host arrays.
stub = stub.replace(
    "static uint32_t adreno8_rd(vm_offset_t address) {\n    assert(address < sizeof(regs));\n"
    "    return regs[address / 4];\n}",
    "static uint32_t adreno8_rd(vm_offset_t address) {\n"
    "    if (address < sizeof(regs)) return regs[address / 4];\n"
    "    return *(const uint32_t *)address;\n}", 1)
assert "return *(const uint32_t *)address;" in stub
hfi = (DEV / "adreno8_hfi.c").read_text()
transport = hfi[hfi.index("static unsigned adreno8_hfi_seq;"):hfi.index("/* ---- step 12:")]
protocol = (DEV / "adreno8_hwsched.c").read_text()
protocol = protocol[protocol.index("#define printf adreno8_say"):]
setup = fixture["tests"].split("int main(void) {")[0]

world = r'''
/* The queue world from the hwsched test: host arrays for the four HFI
 * buffers, a small GMU register window, and an injected firmware that
 * answers every pre-START message and then sends the kind-5 MEM_ALLOC. */
static uint32_t table[1024], commands[1024], responses[1024], debug[1024], gmu_regs[4];
static bool adreno8_direct_ring;
static vm_offset_t adreno8_hfi_view_table, adreno8_hfi_view_cmd;
static vm_offset_t adreno8_hfi_view_resp, adreno8_hfi_view_debug;
static bool adreno8_gmu_talks, adreno8_gpu8_keep, adreno8_gpu8_return, adreno8_reset_required;
static unsigned hmode, sent_count, fail_at, resource_replies, sleeps;
static uint32_t captured[32][255], lengths[32], start_header;
static bool adreno8_clock_started(void) { return true; }
static uint64_t clock_ns;
#define GMU_HOST2GMU_SET 0
#define GMU_GMU2HOST_INFO 4
#define GMU_GMU2HOST_CLR 8
#define GMU_FW_INIT_RESULT 12
#define GMU_FW_FAILED 0xe00
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
    if (((msg[0] >> 16) & 0xf) == 1) { resource_replies++; return; }
    if (fail_at == sent_count) { ack(msg[0], 7); return; }
    if ((msg[0] & 0xff) != 10) { ack(msg[0], 0); return; }
    /* The firmware answers START with the v1 kind-5 MEM_ALLOC and a counter
     * request, matching the on-device stream that stops this boot today. */
    start_header = msg[0];
    uint32_t mem[] = {header(20, 11, 0, 99), 1, 0, 0, 0x1007, 5, 0, 11, 0, 0x138000,
                      (12u << 8) | 16};
    uint32_t counter[] = {header(110, 3, 0, 100), 5, 9};
    append(1, mem, 11);
    append(1, counter, 3);
}
static void adreno8_sleep_us(uint32_t us) {
    clock_ns += (uint64_t)us * 1000;
    sleeps++;
}
static void adreno8_gmu_post_mortem(void) { failure_snapshots++; }
'''

tests = r'''
static void reset_memory(void) {
    reset(&adreno8_gmu_space, false);
    memset(&adreno8_gpu_space, 0, sizeof(adreno8_gpu_space));
    adreno8_gpu_space.name = "gpu";
    memset(&adreno8_secure_space, 0, sizeof(adreno8_secure_space));
    adreno8_secure_space.name = "secure";
    adreno8_secure_space.context = SECURE_CONTEXT;
    memset(adreno8_memory_entries, 0, sizeof(adreno8_memory_entries));
    adreno8_memory_count = 0;
    adreno8_memory_bytes = 0;
    adreno8_memory_failed = false;
    invalidations = 0;
    map_one(&adreno8_gmu_space);
    assert(adreno8_space_on(&adreno8_gmu_space, 5, 1));
}
static void world(void) {
    memset(commands, 0, sizeof(commands)); memset(responses, 0, sizeof(responses));
    memset(debug, 0, sizeof(debug)); memset(gmu_regs, 0, sizeof(gmu_regs));
    memset(captured, 0, sizeof(captured)); memset(lengths, 0, sizeof(lengths));
    adreno8_hfi_init_table(table, 0x60006000, false);
    adreno8_hfi_view_table = (uintptr_t)table; adreno8_hfi_view_cmd = (uintptr_t)commands;
    adreno8_hfi_view_resp = (uintptr_t)responses; adreno8_hfi_view_debug = (uintptr_t)debug;
    adreno8_gmu.base = (uintptr_t)gmu_regs;
    adreno8_gmu_talks = adreno8_gpu8_keep = adreno8_hfi_tables_sent = true;
    adreno8_gpu8_return = adreno8_reset_required = false;
    adreno8_hwsched_attempted = adreno8_hwsched_started = false;
    adreno8_hfi_seq = 4090;
    hmode = sent_count = fail_at = resource_replies = sleeps = 0;
    failure_snapshots = 0; clock_ns = 0;
}
static unsigned writes_before;
static void prepare(void) {
    reset_memory();
    world();
    /* CB registers after the GMU space came up; the handover must add none. */
    writes_before = writes;
}
static void check_start_failed(void) {
    assert(!adreno8_hwsched_ready());
    /* Mode-independent contract: the injected kind-5 request stops the real
     * pump after the seven pre-START messages, the GMU gets no resource
     * reply, and the failure postmortem runs exactly once. */
    assert(sent_count == 7 && resource_replies == 0 && failure_snapshots == 1);
    assert(sleeps == 0);
    assert(!adreno8_hwsched_start());
    assert(sent_count == 7 && resource_replies == 0 && failure_snapshots == 1);
}
static void boot(void) {
    prepare();
    assert(!adreno8_hwsched_start());
    check_start_failed();
}
static unsigned request_refused(uint32_t kind, uint32_t flags) {
    uint32_t msg[11] = {header(20, 11, 0, 200), 1, 0, 0, flags, kind, 0, 12, 0, 4096,
                        (12u << 8) | 16};
    uint32_t reply[16], reply_words = 16;
    unsigned calls_before = scm_calls, allocations_before = adreno8_memory_count;
    assert(!adreno8_hwsched_request(msg, 11, reply, &reply_words));
    assert(reply_words == 0);
    assert(scm_calls == calls_before && adreno8_memory_count == allocations_before);
    return calls_before;
}
int main(int argc, char **argv) {
    const char *which = argc > 1 ? argv[1] : "xfer";
    unsigned pages = 0x138000 / PAGE_SIZE;
    if (strcmp(which, "off") == 0) {
        /* Mode 0: preflight runs, no MP_ASSIGN, no retention, no reply. */
        adreno8_gpu8_secure_mode = SECURE_MODE_OFF;
        boot();
        assert(scm_calls == 0 && scm_sleeps == 0);
        assert(adreno8_memory_count == 0 && !adreno8_memory_retained());
        assert(adreno8_secure_space.table_count == 0);
        assert(adreno8_secure_xfer_state() == SECURE_XFER_NOT_STARTED);
        assert(adreno8_gpu_space.table_count == 0);
        request_refused(5, 0x1007);
        puts("handover off through the real pump: no SCM, no reply, START fails: PASS");
        return 0;
    }
    if (strcmp(which, "preflight") == 0) {
        /* Mode 1: the read-only preflight passes, START still stops, and the
         * one-shot state stays unconsumed. */
        adreno8_gpu8_secure_mode = SECURE_MODE_PREFLIGHT;
        boot();
        assert(scm_calls == 0 && scm_sleeps == 0);
        assert(adreno8_memory_count == 0 && !adreno8_memory_retained());
        assert(adreno8_secure_space.table_count == 0);
        assert(adreno8_secure_xfer_state() == SECURE_XFER_NOT_STARTED);
        request_refused(5, 0x1007);
        puts("preflight-only through the real pump: START stops, zero reply, no SCM: PASS");
        return 0;
    }
    bool translation = strcmp(which, "translation") == 0;
    adreno8_gpu8_secure_mode = translation ? SECURE_MODE_TRANSLATION_A : SECURE_MODE_HANDOVER;
    if (strcmp(which, "batchfail") == 0) {
        /* The table batch lands, the first data batch fails: ownership is
         * unclear, the pump still sends nothing, and START cannot be retried. */
        scm_fail_at = 2;
        boot();
        assert(scm_calls == 2 && scm_sleeps == 0 && adreno8_secure_lost());
        assert(adreno8_memory_failed && adreno8_memory_retained());
        assert(!adreno8_memory_entries[0].complete);
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        request_refused(5, 0x1007);
        puts("data batch failure through the real pump: lost, retained, zero reply: PASS");
        return 0;
    }
    if (strcmp(which, "tablefail") == 0) {
        /* The very first batch, the tables, fails: no data batch runs after
         * it and nothing later appears in the log. */
        scm_fail_at = 1;
        boot();
        assert(scm_calls == 1 && adreno8_secure_lost());
        assert(adreno8_memory_failed && adreno8_memory_retained());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        request_refused(5, 0x1007);
        puts("table batch failure through the real pump: no later batch, zero reply: PASS");
        return 0;
    }
    if (strcmp(which, "data2fail") == 0) {
        /* Tables and the first data batch land; the second data batch fails:
         * the run stops there with no third data batch call. */
        scm_fail_at = 3;
        boot();
        assert(scm_calls == 3 && adreno8_secure_lost());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        request_refused(5, 0x1007);
        puts("second data batch failure through the real pump: run stops at the failed batch: PASS");
        return 0;
    }
    if (strcmp(which, "ebusy") == 0) {
        /* The firmware answers MP_ASSIGN with EBUSY forever: this driver's bound of
         * 20 attempts runs out inside the first batch and the boot is retained. */
        scm_ebusy = 1;
        boot();
        assert(scm_calls == ASSIGN_EBUSY_RETRY && scm_sleeps == ASSIGN_EBUSY_RETRY);
        assert(adreno8_secure_lost() && adreno8_memory_failed && adreno8_memory_retained());
        assert(!adreno8_memory_entries[0].complete);
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        request_refused(5, 0x1007);
        puts("EBUSY exhaustion through the real pump: 20 attempts, retained, zero reply: PASS");
        return 0;
    }
    if (strcmp(which, "argfail") == 0) {
        /* The MP_ASSIGN argument page is missing: no SCM call happens, the
         * experiment goes terminal, and the pump failure stands. */
        prepare();
        alloc_fail_after = 4;
        assert(!adreno8_hwsched_start());
        check_start_failed();
        assert(scm_calls == 0 && !adreno8_secure_lost());
        assert(adreno8_memory_failed && adreno8_memory_retained());
        assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
        request_refused(5, 0x1007);
        puts("missing argument page through the real pump: zero SCM, terminal, zero reply: PASS");
        return 0;
    }
    /* Mode 2 success: one terminal handover inside the pump, SCM calls only,
     * the CB3 route untouched, then every later request is refused. */
    boot();
    assert(scm_calls == (pages + ASSIGN_SECTIONS - 1) / ASSIGN_SECTIONS + 1);
    assert(scm_sleeps == 0 && !adreno8_secure_lost() && !adreno8_memory_failed);
    assert(adreno8_memory_retained() && adreno8_memory_count == 1);
    assert(adreno8_memory_entries[0].complete);
    assert(adreno8_memory_entries[0].secure);
    assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
    assert(adreno8_secure_space.table_count == 3 && adreno8_secure_space.is_on == translation);
    assert(adreno8_secure_space.secure_tables == adreno8_secure_space.table_count);
    assert(adreno8_memory_entries[0].buffer.gpu_address == 0xf0000000u);
    struct adreno8_space_table *parent = adreno8_space_table(&adreno8_secure_space, 1, 3);
    assert(parent && ((uint64_t *)adreno8_secure_space.tables[0].table)[3] == (parent->phys | PTE_TABLE));
    assert(((uint64_t *)adreno8_secure_space.tables[0].table)[0] == 0);
    assert(adreno8_gpu_space.table_count == 0 && (translation ? writes > writes_before : writes == writes_before));
    if (translation) assert(regs[SMMU_S2CR(SECURE_MATCH) / 4] == 0x20300);
    assert(adreno8_gmu_space.is_on && gmu_regs[GMU_GMU2HOST_INFO / 4] == 0);
    request_refused(5, 0x1007);
    request_refused(4, 0x007);
    puts("handover success through the real pump: SCM only, no reply words, START fails: PASS");
    return 0;
}
'''

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="adreno8-handoff-") as directory:
        target = Path(directory)
        (target / "adreno8_private.h").write_text(stub)
        for name in ("adreno8_smmu_private.h", "adreno8_memory_private.h", "adreno8_hwsched_private.h",
                     "adreno8_secure_private.h", "adreno8_wire_private.h"):
            (target / name).write_text((DEV / name).read_text())
        (target / "adreno8_smmu.c").write_text(fixture["driver"])
        (target / "adreno8_memory.c").write_text(driver)
        (target / "adreno8_secure.c").write_text((DEV / "adreno8_secure.c").read_text())
        (target / "check.c").write_text(
            'static unsigned invalidations, failure_snapshots;\n'
            '#include "adreno8_smmu.c"\n#include "adreno8_secure.c"\n#undef printf\n'
            '#include "adreno8_memory.c"\n#include "adreno8_wire_private.h"\n'
            '#include "adreno8_hwsched_private.h"\n' + world + transport + "\n" + protocol +
            "\n#undef printf\n" + setup + tests)
        for page_size in (4096, 16384):
            binary = target / f"check-{page_size}"
            subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}",
                            "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                            str(target / "check.c"), "-o", str(binary)], check=True)
            for mode in ((), ("off",), ("preflight",), ("batchfail",), ("tablefail",),
                         ("data2fail",), ("ebusy",), ("argfail",), ("translation",)):
                result = subprocess.run([str(binary), *mode], check=True,
                                        capture_output=True, text=True)
                out = result.stdout
                assert "START resource request 20 failed (11 words)" in out, (mode, out)
                if mode in ((), ("translation",)):
                    pages = 0x138000 // page_size
                    want = (f"{pages} pages merge into {pages} data regions plus 3 table pages, "
                            f"{(pages + 31) // 32 + 1} logical MP_ASSIGN batches")
                    assert want in out, (want, out)
                    assert "secure handover complete" in out
                    # Every batch header names its role, permissions, shared
                    # argument page, and region count before that batch's call line.
                    assert re.search(
                        r"MP_ASSIGN plan: batch 0 table, permissions HLOS read/write and "
                        r"CP_PIXEL read, argument page 0x[0-9a-f]+, 3 regions", out), out
                    assert "MP_ASSIGN plan: batch 0 table, permissions HLOS read/write" in out
                    assert ("MP_ASSIGN plan: batch 1 data, permissions CP_PIXEL read/write" in out)
                    assert out.find("MP_ASSIGN plan:") < out.find("MP_ASSIGN batch 0 table:")
                    # Shared parser: single-pass stream order plus wire
                    # comparison of every region, permission record, and
                    # argument page. The role sequence comes from this scenario
                    # (3 table pages then 32-region data batches), not the log.
                    expected = (pages + 31) // 32 + 1
                    roles = ["table"] + ["data"] * ((pages + 31) // 32)
                    batches, calls, failed = logcheck.check(out, page_size, roles)
                    assert failed is None
                    assert sorted(batches) == list(range(expected)) and len(calls) == expected
                    assert "no MEM_ALLOC reply is built" in out
                    assert "the secure handover experiment ended" in out
                if mode in (("off",), ("preflight",)):
                    assert "HLOS-side conditions are satisfied at the sample time" in out
                if mode == ("off",):
                    assert "secure handover off" in out
                if mode == ("preflight",):
                    assert "secure preflight-only mode: START stops here" in out
                    assert "secure handover attempted" not in out
                if mode in (("off",), ("preflight",)):
                    assert "scmsnap call" not in out
                if mode == ("batchfail",):
                    assert "secure handover attempted" in out
                    assert "MP_ASSIGN batch 1 data returned -5, answer 0x5, 2 top-level SCM calls" in out
                    assert "secure allocation failed; retain GPU memory until cold reset" in out
                    batches, calls, failed = logcheck.check(out, page_size,
                                                            ["table", "data"])
                    assert failed == 1 and sorted(batches) == [0, 1] and len(calls) == 2
                if mode == ("ebusy",):
                    assert "secure handover attempted" in out
                    assert out.count("returned -12") == 1
                    assert "secure pages retained until cold reset" in out
                    # The 20 EBUSY retries reuse one staged batch: the region log
                    # for batch 0 prints once and all 20 snapshots match it.
                    batches, calls, failed = logcheck.check(out, page_size, ["table"])
                    assert failed == 0 and sorted(batches) == [0] and len(calls) == 20
                    assert len(batches[0]["regions"]) == 3
                if mode in (("tablefail",), ("data2fail",)):
                    # The parser compares the failed batch's printed regions and
                    # permissions with its snapshot item by item; nothing from a
                    # later batch may appear under any prefix.
                    stop = 0 if mode == ("tablefail",) else 2
                    roles = ["table"] + ["data"] * stop
                    batches, calls, failed = logcheck.check(out, page_size, roles)
                    assert failed == stop and sorted(batches) == list(range(stop + 1))
                    assert len(calls) == stop + 1
                    role = "table" if stop == 0 else "data"
                    assert f"MP_ASSIGN batch {stop} {role} returned -5" in out
                    assert f"MP_ASSIGN batch {stop + 1}" not in out
                    assert f"MP_ASSIGN plan: batch {stop + 1}" not in out
                if mode == ("argfail",):
                    assert "ends before any MP_ASSIGN; no page transferred" in out
                    assert "MP_ASSIGN batch" not in out
                    assert "MP_ASSIGN plan" not in out
                    assert "scmsnap call" not in out
                if mode != ("off",) and mode != ("preflight",):
                    assert "the secure handover experiment ended" in out
            print(f"XNU PAGE_SIZE={page_size}: PASS")
