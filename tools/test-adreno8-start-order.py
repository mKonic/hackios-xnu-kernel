#!/usr/bin/env python3
"""Exercise CB0-before-START ordering, active-CB0 handover and read-only stage 16."""
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
ORDER = "return adreno8_gpu_prepare() && adreno8_gpu_memory_enable() &&\n\t    (adreno8_direct_ring ? adreno8_ordinary_start() : adreno8_hwsched_start());"

WRAPPER_TEST = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
static bool adreno8_gpu8_keep, adreno8_gpu8_return, adreno8_direct_ring;
static unsigned failure, count, retained;
static char calls[4];
static void record(char value) { assert(count < 3); calls[count++] = value; }
static int adreno8_say(const char *format, ...) { return 0; }
static bool adreno8_gpu_prepare(void) {
    assert(count == 0); record('P'); retained = 1;
    return failure != 1;
}
static bool adreno8_gpu_memory_enable(void) {
    assert(count == 1 && calls[0] == 'P' && failure != 1); record('M');
    return failure != 2;
}
static bool adreno8_hwsched_start(void) {
    assert(!adreno8_direct_ring && count == 2 && calls[1] == 'M' && failure != 2); record('H');
    return failure != 3;
}
static bool adreno8_ordinary_start(void) {
    assert(adreno8_direct_ring && count == 2 && calls[1] == 'M' && failure != 2); record('O');
    return failure != 3;
}
#define printf adreno8_say
@@WRAPPER@@
#undef printf
int main(void) {
    for (unsigned direct = 0; direct <= 1; direct++) for (unsigned keep = 0; keep <= 1; keep++) {
        for (unsigned back = 0; back <= 1; back++) {
            for (failure = 0; failure <= 3; failure++) {
                adreno8_gpu8_keep = keep; adreno8_gpu8_return = back; adreno8_direct_ring = direct;
                count = retained = 0; memset(calls, 0, sizeof(calls));
                assert(adreno8_step_core_start() == (keep && !back && !failure));
                if (!keep || back) { assert(!count && !retained); continue; }
                const char *expected = failure == 1 ? "P" : failure == 2 ? "PM" : direct ? "PMO" : "PMH";
                assert(!strcmp(calls, expected) && retained == 1);
            }
        }
    }
    puts("START wrapper: admission, CB0-before-START and short circuit: PASS");
}
'''

ASSIGN_CHECK = r'''
static void check_cb0_before_assign(void) {
    assert(adreno8_gpu_memory_ready() && aperture_calls == 1);
    assert(regs[SMMU_SMR(0) / 4] == SMR_VALID);
    assert(regs[SMMU_S2CR(0) / 4] == SMMU_S2CR_SHCFG_NSH);
    assert(regs[(0x20000 + SMMU_CB_SCTLR) / 4] & SCTLR_M);
    /* The ordinary page table must contain no secure data or table alias. */
    for (unsigned i = 0; i < adreno8_gpu_space.table_count; i++) {
        const struct adreno8_space_table *table = &adreno8_gpu_space.tables[i];
        if (table->level != 2) continue;
        for (unsigned j = 0; j < 512; j++) {
            uint64_t pa = ((uint64_t *)table->table)[j] & PTE_ADDRESS;
            if (!pa) continue;
            for (unsigned k = 0; k < adreno8_memory_count; k++) {
                const struct adreno8_memory_entry *entry = &adreno8_memory_entries[k];
                if (!entry->secure) continue;
                for (unsigned at = 0; at < entry->buffer.size; at += 4096)
                    assert(pa != kvtophys(entry->buffer.memory + at));
            }
            for (unsigned k = 0; k < adreno8_secure_space.table_count; k++)
                assert(pa != adreno8_secure_space.tables[k].phys);
        }
    }
}
'''

EARLY_CB = r'''
    assert(adreno8_gpu_memory_enable() && adreno8_gpu_memory_ready() && aperture_calls == 1);
'''

READBACK = r'''
    if (started) {
        unsigned old_writes = writes, old_clean = clean_count, old_ats = ats_requests;
        unsigned old_calls = scm_calls;
        regs[(0x20000 + SMMU_CB_FSR) / 4] = 0x400;
        assert(adreno8_gpu_memory_verify());
        assert(writes == old_writes && clean_count == old_clean && ats_requests == old_ats);
        assert(aperture_calls == 1 && scm_calls == old_calls);
        unsigned corrupt = argc > 3 ? (unsigned)atoi(argv[3]) : 0;
        const uint32_t offsets[] = {SMMU_SMR(0), SMMU_S2CR(0),
            0x1000 + SMMU_CBA2R(0), 0x1000 + SMMU_CBAR(0),
            0x20000 + SMMU_CB_SCTLR, 0x20000 + SMMU_CB_TCR,
            0x20000 + SMMU_CB_TCR2, 0x20000 + SMMU_CB_TTBR0,
            0x20000 + SMMU_CB_TTBR0 + 4, 0x20000 + SMMU_CB_TTBR1,
            0x20000 + SMMU_CB_TTBR1 + 4, 0x20000 + SMMU_CB_MAIR0,
            0x20000 + SMMU_CB_MAIR1};
        if (corrupt && corrupt < 18) {
            if (corrupt <= 13) regs[offsets[corrupt - 1] / 4] ^= 1;
            else if (corrupt == 14) *adreno8_space_entry(&adreno8_gpu_space, before->gpu_address) ^= 1;
            else if (corrupt == 15) regs[(0x20000 + SMMU_CB_FSR) / 4] = 0x408;
            else if (corrupt == 16) adreno8_gpu_space.tables[0].level = 1;
            else adreno8_gpu_space.is_on = false;
            assert(!adreno8_gpu_memory_verify() && adreno8_memory_failed && adreno8_memory_retained());
            assert(writes == old_writes && clean_count == old_clean && ats_requests == old_ats);
            assert(aperture_calls == 1 && scm_calls == old_calls);
            check_closed(scm_calls, adreno8_memory_count);
            release_all();
            puts("stage 16: changed route/register/PTE/FSR rejected without writes or SCM: PASS");
            return 0;
        }
    }
'''

START_FAILURE = r'''
    if (start_tlb_fail) {
        assert(!started && adreno8_memory_count == 3 && resource_replies == 1);
        const struct adreno8_memory_entry *failed = &adreno8_memory_entries[2];
        assert(!failed->complete && failed->buffer.memory && !failed->secure);
        assert(walk(adreno8_gpu_space.tables[0].phys, failed->buffer.gpu_address) ==
            kvtophys(failed->buffer.memory));
        assert(adreno8_gpu_space.is_on && adreno8_memory_failed && adreno8_memory_retained());
        check_closed(scm_calls, adreno8_memory_count);
        release_all();
        puts("START ordinary allocation: live CB0 TLB timeout closes HFI and retains memory: PASS");
        return 0;
    }
'''


def replace_once(text, old, new):
    assert text.count(old) == 1, old
    return text.replace(old, new, 1)


def compile_run(directory, source, page_size, args=(), success=True):
    path, binary = directory / "check.c", directory / "check"
    path.write_text(source)
    compiled = subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", str(path), "-o", str(binary)],
        capture_output=True, text=True)
    if compiled.returncode:
        raise RuntimeError(compiled.stderr)
    result = subprocess.run([str(binary), *map(str, args)], capture_output=True, text=True)
    if success and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    if not success and result.returncode == 0:
        raise RuntimeError("ordering mutation survived")
    return result


def main():
    production = (DEV / "adreno8.c").read_text()
    wrapper = re.search(r"static bool\nadreno8_step_core_start\(void\)\n\{.*?\n\}", production, re.S).group()
    assert "adreno8_step_gx_power, adreno8_gpu_memory_verify, adreno8_gpu_cp_init" in production
    with tempfile.TemporaryDirectory(prefix="adreno8-start-order-") as directory:
        target = Path(directory)
        print(compile_run(target, WRAPPER_TEST.replace("@@WRAPPER@@", wrapper), 4096).stdout.strip())
        selector = "(adreno8_direct_ring ? adreno8_ordinary_start() : adreno8_hwsched_start())"
        replacements = [
            f"return adreno8_gpu_prepare() && {selector};",
            f"return adreno8_gpu_memory_enable() && adreno8_gpu_prepare() && {selector};",
            f"return adreno8_gpu_prepare() && {selector} && adreno8_gpu_memory_enable();",
            f"return adreno8_gpu_prepare() && (adreno8_gpu_memory_enable(), {selector});",
            ORDER.replace("adreno8_direct_ring ?", "false ?"),
            ORDER.replace("adreno8_direct_ring ?", "true ?"),
        ]
        for replacement in replacements:
            mutant = replace_once(wrapper, ORDER, replacement)
            compile_run(target, WRAPPER_TEST.replace("@@WRAPPER@@", mutant), 4096, success=False)
        print("START order/mode mutations detected:", len(replacements))
        service = runpy.run_path(str(ROOT / "tools/test-adreno8-service.py"))
        base = service["base"]
        stub = replace_once(service["stub"], "    scm_calls++;", "    check_cb0_before_assign();\n    scm_calls++;")
        stub = replace_once(stub, "static bool adreno8_scm_gpu_aperture(void) { return true; }",
            "static unsigned aperture_calls;\nstatic bool adreno8_scm_gpu_aperture(void) { aperture_calls++; return true; }")
        stub = "static void check_cb0_before_assign(void);\n" + stub
        tests = replace_once(service["tests"], "    assert(before && adreno8_memory_publish(before, 0, 4096));",
            "    assert(before && adreno8_memory_publish(before, 0, 4096));\n" + EARLY_CB)
        tests = replace_once(tests, "    hmode = scenario;",
            "    hmode = scenario;\n    start_tlb_fail = argc > 3 && atoi(argv[3]) == 18;")
        tests = replace_once(tests, "    bool started = adreno8_hwsched_start();",
            "    bool started = adreno8_hwsched_start();\n" + START_FAILURE + READBACK)
        world = "static bool start_tlb_fail;\n" + replace_once(service["world"],
            "            emit_memory(0x007, 4, 12, 0x2000, 0xc10);",
            "            if (start_tlb_fail) sync_ok = false;\n"
            "            emit_memory(0x007, 4, 12, 0x2000, 0xc10);")
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
            '#include "adreno8_hwsched_private.h"\n' + ASSIGN_CHECK + world +
            base["transport"] + "\n" + base["protocol"] + "\n#undef printf\n" +
            base["setup"] + service["setup"] + tests)
        for page_size in (4096, 16384):
            for stall in (0, 1):
                compile_run(target, source, page_size, (0, stall, 0))
                binary = target / "check"
                cases = [(scenario, stall, 0) for scenario in range(1, 21)]
                cases += [(0, stall, corrupt) for corrupt in range(1, 19)]
                for case in cases:
                    result = subprocess.run([str(binary), *map(str, case)], capture_output=True, text=True)
                    if result.returncode:
                        raise RuntimeError(f"PAGE_SIZE={page_size}, case={case}:\n" + result.stdout + result.stderr)
                print(f"Active CB0: 21 mode-4 scenarios, 17 readback failures and live TLB failure, stall={stall}, PAGE_SIZE={page_size}: PASS")


if __name__ == "__main__":
    main()
