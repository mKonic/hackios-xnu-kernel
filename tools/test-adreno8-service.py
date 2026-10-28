#!/usr/bin/env python3
"""Exercise mode 4 through the real HFI, allocator, ownership and translation code."""
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
base = runpy.run_path(str(ROOT / "tools/test-adreno8-handoff.py"))
stub = base["stub"].replace(
    "static uint64_t mach_absolute_time(void) { return 0x1234abcdull; }",
    "static uint64_t clock_ns;\nstatic uint64_t mach_absolute_time(void) { return clock_ns; }")
stub = stub.replace("aligned_alloc(PAGE_SIZE, size)",
                    "aligned_alloc(16384, (size + 16383) & ~(size_t)16383)")
stub = stub.replace("static uint32_t reject_offset = ~0u, reject_value;",
    "static uint32_t reject_offset = ~0u, reject_value, secure_route_reads;\n"
    "static bool change_secure_route, change_secure_sctlr, fault_secure_route;")
stub = stub.replace("    return adreno8_rd(offset);",
    "    if (offset == SMMU_S2CR(SECURE_MATCH) && regs[offset / 4] == 0x303 &&\n"
    "        change_secure_route && ++secure_route_reads == 2) regs[offset / 4] = 0x20300;\n"
    "    if (change_secure_sctlr && strcmp(name, \"secure sctlr after translate\") == 0) regs[offset / 4] = 0;\n"
    "    return adreno8_rd(offset);")
stub = stub.replace("    regs[offset / 4] = value;",
    "    regs[offset / 4] = value;\n"
    "    if (fault_secure_route && offset == SMMU_S2CR(SECURE_MATCH) && value == 0x303)\n"
    "        regs[(0x23000 + SMMU_CB_FSR) / 4] = 0x408;")
world = base["world"][:base["world"].index("static void adreno8_wr(")]
world += r'''
enum { NORMAL, ACK_FAIL, TIMEOUT, FW_ERROR, NEW_HANDLE, CHANGED_FLAGS,
       CHANGED_SIZE, CHANGED_ALIGN, REPLY_FULL, SECURE_FAULT, ROUTE_WRITE_FAIL,
       ROUTE_CHANGED, BAD_ARGUMENT, BAD_QUEUE, REPEAT_START, BAD_RAW, CB_WRITE_FAIL,
       TABLE_FAIL, DATA_FAIL, SCTLR_CHANGED, ROUTE_FAULT };
static uint32_t secure_address;
static bool protected_pages;
static void protect_secure_pages(bool protect) {
    int prot = protect ? PROT_NONE : PROT_READ | PROT_WRITE;
    for (unsigned i = 0; i < adreno8_memory_count; i++) {
        struct adreno8_memory_entry *entry = &adreno8_memory_entries[i];
        if (entry->secure)
            assert(mprotect((void *)entry->buffer.memory,
                (entry->buffer.size + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1), prot) == 0);
    }
    for (unsigned i = 0; i < adreno8_secure_space.table_count; i++)
        assert(mprotect((void *)adreno8_secure_space.tables[i].table, PAGE_SIZE, prot) == 0);
    protected_pages = protect;
}
static void emit_memory(uint32_t flags, uint32_t kind, uint32_t handle, uint32_t size, uint32_t align) {
    uint32_t mem[] = {header(20, 11, 0, 99 + resource_replies), 1, 0, 0,
        flags, kind, 0, handle, 0, size, align};
    append(1, mem, 11);
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
    if (((msg[0] >> 16) & 0xf) == 1) {
        resource_replies++;
        assert(adreno8_secure_xfer_state() == SECURE_XFER_READY);
        assert(regs[SMMU_S2CR(SECURE_MATCH) / 4] == 0x303);
        assert(regs[(0x23000 + SMMU_CB_SCTLR) / 4] == (adreno8_gpu_stall ? 0xa7 : 0x27));
        if (resource_replies == 1) {
            assert(words == 11 && (msg[0] & 0xff) == 20);
            assert(msg[2] == 0xf0000000u && msg[3] == 0 && msg[4] == 0x1007 && msg[5] == 5);
            assert(msg[6] == 11 && msg[7] == 11 && msg[8] == 0);
            assert(msg[9] == 0x138000 && msg[10] == 0xc10);
            secure_address = msg[2];
            if (hmode == ACK_FAIL) { ack(start_header, UINT32_MAX); return; }
            if (hmode == TIMEOUT) return;
            if (hmode == FW_ERROR) {
                uint32_t error[] = {header(100, 2, 0, 12), 7};
                append(1, error, 2); return;
            }
            if (hmode == SECURE_FAULT) {
                protect_secure_pages(true);
                regs[(0x23000 + SMMU_CB_FSR) / 4] = 0x408;
                regs[(0x23000 + SMMU_CB_FAR_LO) / 4] = secure_address;
                ack(start_header, 0); return;
            }
            if (hmode >= NEW_HANDLE && hmode <= CHANGED_ALIGN) {
                emit_memory(hmode == CHANGED_FLAGS ? 0x1003 : 0x1007, 5,
                    hmode == NEW_HANDLE ? 12 : 11,
                    hmode == CHANGED_SIZE ? 0x137000 : 0x138000,
                    hmode == CHANGED_ALIGN ? 0xc11 : 0xc10);
                return;
            }
            emit_memory(0x007, 4, 12, 0x2000, 0xc10);
        } else if (resource_replies == 2) {
            assert(words == 11 && msg[4] == 0x007 && msg[5] == 4 && msg[2] >= 0x10000000u && msg[2] < 0xe0000000u);
            emit_memory(0x1007, 5, 11, 0x138000, 0xc10);
        } else if (resource_replies == 3) {
            assert(words == 11 && msg[2] == secure_address && msg[4] == 0x1007 && msg[5] == 5);
            const uint32_t counter[] = {header(110, 3, 0, 101), 5, 9};
            append(1, counter, 3);
        } else {
            assert(resource_replies == 4 && (msg[0] & 0xff) == 110);
            ack(start_header, 0);
        }
        return;
    }
    if ((msg[0] & 0xff) != 10) { ack(msg[0], 0); return; }
    start_header = msg[0];
    emit_memory(0x1007, 5, 11, 0x138000, 0xc10);
    if (hmode == REPLY_FULL) h[10] = (h[11] + 4) % 1024;
}
static void adreno8_sleep_us(uint32_t us) { clock_ns += (uint64_t)us * 1000; sleeps++; }
static void adreno8_gmu_post_mortem(void) {
    assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL && adreno8_memory_failed);
    failure_snapshots++;
}
'''

setup = base["tests"][:base["tests"].index("static void check_start_failed(")]
tests = r'''
static void check_closed(unsigned calls_before, unsigned entries_before) {
    struct adreno8_hfi_mem_result result;
    const struct adreno8_hfi_mem_request same = {0x1007, 5, 11, 0x138000, 0xc10};
    const struct adreno8_hfi_mem_request ordinary = {0x007, 4, 81, 4096, 0xc10};
    assert(adreno8_secure_xfer_state() == SECURE_XFER_TERMINAL);
    assert(!adreno8_secure_xfer_ready());
    assert(!adreno8_hwsched_alloc(&same, &result) && result.gpu_address == 0);
    assert(!adreno8_hwsched_alloc(&ordinary, &result) && result.gpu_address == 0);
    assert(adreno8_gpu_alloc("after failure", 4096, 4096, ADRENO8_MEM_READ) == NULL);
    assert(scm_calls == calls_before && adreno8_memory_count == entries_before);
    assert(adreno8_memory_retained() && !adreno8_hwsched_ready());
}
int main(int argc, char **argv) {
    unsigned scenario = argc > 1 ? (unsigned)atoi(argv[1]) : NORMAL;
    prepare();
    adreno8_gpu_stall = argc > 2 && atoi(argv[2]) == 1;
    assert(regs[(0x22000 + SMMU_CB_SCTLR) / 4] == 0x27);
    hmode = scenario;
    adreno8_gpu8_secure_mode = SECURE_MODE_TRANSLATION_B;
    regs[(0x23000 + SMMU_CB_FSR) / 4] = 0x400;
    /* These buffers model GPU preparation before START. Their CPU publication
     * and acquisition contract must continue to work after secure service setup. */
    struct adreno8_memory *before = adreno8_gpu_alloc("before START", 4096, 4096,
        ADRENO8_MEM_READ | ADRENO8_MEM_WRITE | ADRENO8_MEM_PRIV);
    assert(before && adreno8_memory_publish(before, 0, 4096));
    if (scenario == ROUTE_WRITE_FAIL) { reject_offset = SMMU_S2CR(SECURE_MATCH); reject_value = 0x303; }
    if (scenario == ROUTE_CHANGED) change_secure_route = true;
    if (scenario == CB_WRITE_FAIL) {
        regs[(0x23000 + SMMU_CB_SCTLR) / 4] = 0x96cc51a;
        reject_offset = 0x23000 + SMMU_CB_SCTLR; reject_value = 0;
    }
    if (scenario == TABLE_FAIL) scm_fail_at = 1;
    if (scenario == DATA_FAIL) scm_fail_at = 2;
    if (scenario == SCTLR_CHANGED) change_secure_sctlr = true;
    if (scenario == ROUTE_FAULT) fault_secure_route = true;
    bool started = adreno8_hwsched_start();
    unsigned expected_calls = (0x138000 / PAGE_SIZE + 31) / 32 + 1;
    if (scenario == TABLE_FAIL || scenario == DATA_FAIL) {
        assert(scm_calls == (scenario == TABLE_FAIL ? 1 : 2) && adreno8_secure_lost());
    } else assert(scm_calls == expected_calls && !adreno8_secure_lost());
    assert(scm_sleeps == 0);
    if ((scenario >= ACK_FAIL && scenario <= ROUTE_CHANGED) || scenario >= CB_WRITE_FAIL) {
        assert(!started);
        unsigned entries = 2;
        assert(adreno8_memory_count == entries);
        assert(resource_replies == (scenario == ROUTE_WRITE_FAIL || scenario == ROUTE_CHANGED ||
            scenario >= CB_WRITE_FAIL || scenario == REPLY_FULL ? 0 : 1));
        if (scenario == ROUTE_WRITE_FAIL || scenario == ROUTE_CHANGED || scenario >= SCTLR_CHANGED)
            assert(regs[SMMU_S2CR(SECURE_MATCH) / 4] == 0x20300);
        check_closed(scm_calls, entries);
        if (scenario == TIMEOUT) assert(clock_ns >= 5000000000ULL && clock_ns < 5000100000ULL);
        if (protected_pages) protect_secure_pages(false);
        puts("mode 4 failure: no extra SCM, service closed permanently, pages retained: PASS");
        release_all(); return 0;
    }
    assert(started && adreno8_hwsched_ready());
    assert(resource_replies == 4 && adreno8_memory_count == 3);
    assert(adreno8_secure_xfer_state() == SECURE_XFER_READY && !adreno8_memory_failed);
    assert(regs[SMMU_S2CR(SECURE_MATCH) / 4] == 0x303);
    struct adreno8_memory_entry *entry = &adreno8_memory_entries[1];
    assert(entry->secure && entry->complete && entry->from_hfi && entry->buffer.gpu_address == secure_address);
    struct adreno8_space_table *parent = adreno8_space_table(&adreno8_secure_space, 1, 3);
    assert(parent && ((uint64_t *)adreno8_secure_space.tables[0].table)[3] == (parent->phys | PTE_TABLE));
    assert(((uint64_t *)adreno8_secure_space.tables[0].table)[0] == 0);
    assert(adreno8_space_entry(&adreno8_gpu_space, secure_address) == NULL);
    assert(before->gpu_address == 0x10000000u);
    unsigned calls_before = scm_calls, allocations_before = allocation_count;
    struct adreno8_hfi_mem_result result;
    struct adreno8_hfi_mem_request request = {0x1007, 5, 11, 0x138000, 0xc10};
    protect_secure_pages(true);
    assert(adreno8_hwsched_alloc(&request, &result) && result.secure && result.gpu_address == secure_address);
    assert(scm_calls == calls_before && allocation_count == allocations_before);
    assert(!adreno8_memory_publish(&entry->buffer, 0, 4096));
    assert(!adreno8_memory_acquire(&entry->buffer, 0, 4096));
    assert(adreno8_memory_publish(before, 0, 4096) && adreno8_memory_acquire(before, 0, 4096));
    struct adreno8_memory *after = adreno8_gpu_alloc("after START", 4096, 4096,
        ADRENO8_MEM_READ | ADRENO8_MEM_WRITE | ADRENO8_MEM_PRIV);
    assert(after && adreno8_memory_publish(after, 0, 4096) && adreno8_memory_acquire(after, 0, 4096));
    assert(after->gpu_address < 0xe0000000u);
    allocations_before = allocation_count;
    request.handle++;
    assert(!adreno8_hwsched_alloc(&request, &result) && result.gpu_address == 0);
    request.handle--;
    request.size++;
    assert(!adreno8_hwsched_alloc(&request, &result));
    assert(scm_calls == calls_before && allocation_count == allocations_before);
    if (scenario == BAD_ARGUMENT) assert(!adreno8_hfi_send(256, NULL, 0, "invalid HFI argument"));
    else if (scenario == BAD_QUEUE) {
        table[ADRENO8_HFI_HEADER(0)] = 0;
        assert(!adreno8_hfi_send(131, NULL, 0, "invalid queue"));
    } else if (scenario == REPEAT_START) assert(!adreno8_hwsched_start());
    else if (scenario == BAD_RAW) assert(!adreno8_hwsched_raw(NULL, 0, "invalid raw command"));
    else adreno8_hwsched_memory_failed();
    check_closed(calls_before, adreno8_memory_count);
    protect_secure_pages(false);
    release_all();
    puts("mode 4 service: secure reply, non-secure continuation, exact replay and failure closure: PASS");
}
'''

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="adreno8-service-") as directory:
        target = Path(directory)
        (target / "adreno8_private.h").write_text(stub)
        for name in ("adreno8_smmu_private.h", "adreno8_memory_private.h",
                     "adreno8_hwsched_private.h", "adreno8_secure_private.h", "adreno8_wire_private.h"):
            (target / name).write_text((DEV / name).read_text())
        (target / "adreno8_smmu.c").write_text(base["fixture"]["driver"])
        (target / "adreno8_memory.c").write_text(base["driver"])
        (target / "adreno8_secure.c").write_text((DEV / "adreno8_secure.c").read_text())
        source = ('#include <sys/mman.h>\nstatic unsigned invalidations, failure_snapshots;\n'
            '#include "adreno8_smmu.c"\n#include "adreno8_secure.c"\n#undef printf\n'
            '#include "adreno8_memory.c"\n#include "adreno8_wire_private.h"\n'
            '#include "adreno8_hwsched_private.h"\n' + world + base["transport"] + "\n" +
            base["protocol"] + "\n#undef printf\n" + base["setup"] + setup + tests)
        (target / "check.c").write_text(source)
        for page_size in (4096, 16384):
            binary = target / f"check-{page_size}"
            subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}",
                            "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                            str(target / "check.c"), "-o", str(binary)], check=True)
            for stall in (0, 1):
                for scenario in range(21):
                    result = subprocess.run([str(binary), str(scenario), str(stall)], capture_output=True, text=True)
                    if result.returncode:
                        print(result.stdout)
                        print(result.stderr)
                        result.check_returncode()
                print(f"Mode 4 real HFI/SCM/translation: 21 scenarios, stall={stall}, XNU PAGE_SIZE={page_size}: PASS")

        memory = base["driver"]
        secure = (DEV / "adreno8_secure.c").read_text()
        smmu = base["fixture"]["driver"]
        closed = "state != SECURE_XFER_NOT_STARTED && state != SECURE_XFER_READY"
        replay_check = ("if (!candidate->complete || candidate->request.flags != request->flags ||\n"
                        "\t\t\t    candidate->request.size != request->size || candidate->request.align != request->align)")
        settled_check = ('(adreno8_get(&adreno8_smmu, "secure route settled", '
                         'SMMU_S2CR(SECURE_MATCH)) & 0x30303ff) != route ||')
        sctlr_check = ('adreno8_get(&adreno8_smmu, "secure sctlr after translate", '
                       'cb + SMMU_CB_SCTLR) != sctlr ||')
        variants = {
            "secure_iova_in_normal_range": ("adreno8_memory.c", memory.replace(
                "&adreno8_secure_space, ADRENO8_SECURE_IOVA_START, ADRENO8_SECURE_IOVA_END",
                "&adreno8_secure_space, ADRENO8_GPU_IOVA_START, ADRENO8_GPU_IOVA_END", 1), 0),
            "normal_allocation_stays_closed": ("adreno8_memory.c", memory.replace(
                closed, "state != SECURE_XFER_NOT_STARTED", 1), 0),
            "hfi_allocation_stays_closed": ("adreno8_memory.c", memory.replace(
                "if (" + closed + ") {\n\t\tprintf", "if (state != SECURE_XFER_NOT_STARTED) {\n\t\tprintf", 1), 0),
            "replay_ignores_handle": ("adreno8_memory.c", memory.replace(
                "candidate->request.handle == request->handle", "true", 1), 4),
            "replay_ignores_parameters": ("adreno8_memory.c", memory.replace(
                replay_check, "if (!candidate->complete)", 1), 6),
            "skip_secure_route_setup": ("adreno8_memory.c", memory.replace(
                "!adreno8_secure_translation_3b(&adreno8_secure_space)", "false", 1), 10),
            "cpu_access_to_secure_buffer": ("adreno8_memory.c", memory.replace(
                "entry != NULL && !entry->secure", "entry != NULL", 1), 0),
            "ready_survives_abort": ("adreno8_secure.c", secure.replace(
                "adreno8_xfer_state == SECURE_XFER_ASSIGNED || adreno8_xfer_state == SECURE_XFER_READY",
                "adreno8_xfer_state == SECURE_XFER_ASSIGNED", 1), 0),
            "closed_service_can_reopen": ("adreno8_secure.c", secure.replace(
                "adreno8_xfer_state != SECURE_XFER_ASSIGNED || adreno8_assign.lost",
                "(adreno8_xfer_state != SECURE_XFER_ASSIGNED && "
                "adreno8_xfer_state != SECURE_XFER_TERMINAL) || adreno8_assign.lost", 1), 0),
            "hfi_failure_does_not_close": ("check.c", source.replace(
                base["transport"], base["transport"].replace("adreno8_hwsched_memory_failed();", ""), 1), 1),
            "postmortem_before_close": ("check.c", source.replace(
                "adreno8_hfi_failed();\n\tadreno8_gmu_post_mortem();",
                "adreno8_gmu_post_mortem();\n\tadreno8_hfi_failed();", 1), 2),
            "secure_fault_reads_table": ("adreno8_smmu.c", smmu.replace(
                "if (space == &adreno8_secure_space) {", "if (false) {", 1), 9),
            "ignore_settled_route": ("adreno8_smmu.c", smmu.replace(settled_check, "false ||", 1), 11),
            "ignore_post_route_sctlr": ("adreno8_smmu.c", smmu.replace(sctlr_check, "false ||", 1), 19),
        }
        originals = {"adreno8_memory.c": memory, "adreno8_secure.c": secure,
                     "adreno8_smmu.c": smmu, "check.c": source}
        for name, (filename, mutated, scenario) in variants.items():
            assert mutated != originals[filename], name
            for path, original in originals.items():
                (target / path).write_text(original)
            (target / filename).write_text(mutated)
            binary = target / f"mutant-{name}"
            subprocess.run(["cc", "-std=c11", "-DPAGE_SIZE=16384",
                            "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                            str(target / "check.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary), str(scenario)], capture_output=True, text=True)
            assert result.returncode != 0, f"mutation survived: {name}"
        print(f"Mode 4 service mutations rejected: {len(variants)}")
