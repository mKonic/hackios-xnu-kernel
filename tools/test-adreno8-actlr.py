#!/usr/bin/env python3
"""Exercise ordinary CB0 ACTLR programming and verification with the production SMMU."""
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
fixture = runpy.run_path(str(ROOT / "tools/test-adreno8-smmu.py"))
stub = fixture["stub"].replace("static void adreno8_settle(void) {}",
    "static unsigned barriers;\nstatic void adreno8_settle(void) { barriers++; }", 1)
stub = stub.replace("static unsigned writes, translations, ats_requests;", r'''
static unsigned writes, translations, ats_requests;
static unsigned actlr_writes, actlr_reads, enabled, routed, actlr_barrier, invalidated, synced;
static bool actlr_allowed, actlr_bad_read, cleanup_fail;
static uint32_t actlr_answer;
''', 1)
stub = stub.replace("    reads[offset / 4]++;", r'''
    reads[offset / 4]++;
    if (offset == 0x20004) {
        actlr_reads++;
        if (actlr_allowed && actlr_writes) assert(barriers > actlr_barrier);
        if (actlr_bad_read) {
            if (cleanup_fail) sync_ok = false;
            return actlr_answer;
        }
    }
''', 1)
stub = stub.replace("    writes++;", r'''
    writes++;
    if (offset >= 0x20000 && (offset & 0xfff) == 4) {
        assert(actlr_allowed && adreno8_direct_ring && offset == 0x20004);
        assert(regs[0xc00 / 4] == 0x20300 && regs[0x800 / 4] == 0x80000000);
        assert(regs[0x20000 / 4] == 0 && value == 0x303);
        unsigned disabled = 0;
        for (unsigned i = 0; i + 1 < write_log_count; i++)
            disabled += write_log[i].offset == 0x20000 && write_log[i].value == 0;
        assert(disabled);
        actlr_barrier = barriers;
        actlr_writes++;
    }
    if (actlr_allowed && regs[0x20030 / 4] == 0x803519) {
        if (offset == 0x20610) { assert(actlr_writes == 1 && actlr_reads); invalidated++; }
        if (offset == 0x207f0) { assert(invalidated); synced++; }
    }
    if (actlr_allowed && offset == 0x20000 && (value & 1)) {
        assert(actlr_writes == 1 && actlr_reads && barriers > actlr_barrier);
        assert(invalidated == 1 && synced == 1);
        assert(regs[0x20004 / 4] == 0x303 && !actlr_bad_read);
        enabled++;
    }
    if (actlr_allowed && offset == 0xc00 && value == 0x300) {
        assert(enabled == 1 && actlr_reads >= 2);
        routed++;
    }
''', 1)
setup = fixture["tests"].split("int main(void) {")[0]
cases = r'''
static void prepare_actlr(struct adreno8_gpu_space *space, uint32_t context, bool direct)
{
    reset(space, false);
    space->context = context;
    adreno8_direct_ring = direct;
    actlr_allowed = space == &adreno8_gpu_space && context == 0 && direct;
    actlr_writes = actlr_reads = enabled = routed = barriers = invalidated = synced = 0;
    actlr_bad_read = cleanup_fail = false;
    regs[0x20004 / 4] = 3;
    regs[0x22004 / 4] = 0x123;
    regs[0x23004 / 4] = 0x456;
    map_one(space);
}
static void retained_failure(void)
{
    assert(!enabled && !routed);
    assert(regs[0xc00 / 4] == 0x20300 && regs[0x20000 / 4] == 0);
    assert(adreno8_gpu_space.table_count == 3 && allocation_count == 3);
    for (unsigned i = 0; i < allocation_count; i++) assert(allocations[i].va);
}
int main(void)
{
    struct adreno8_gpu_space other;
    for (unsigned stall = 0; stall <= 1; stall++) {
        prepare_actlr(&adreno8_gpu_space, 0, true);
        adreno8_gpu_stall = stall;
        regs[0x20004 / 4] = 0x32b;
        assert(adreno8_space_on(&adreno8_gpu_space, 0, 0));
        assert(actlr_writes == 1 && enabled == 1 && routed == 1);
        assert(regs[0x20004 / 4] == 0x303);
        assert(adreno8_space_check(&adreno8_gpu_space, 0, 0));
        const uint32_t invalid[] = {3, 0x103, 0x301, 0x323, 0xffffffff};
        for (unsigned i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
            regs[0x20004 / 4] = invalid[i];
            unsigned before = writes;
            assert(!adreno8_space_check(&adreno8_gpu_space, 0, 0));
            assert(writes == before && adreno8_gpu_space.is_on);
            assert(adreno8_gpu_space.table_count == 3 && allocation_count == 3);
        }
    }
    const uint32_t bad[] = {0, 3, 0x301, 0x323, 0xffffffff};
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        prepare_actlr(&adreno8_gpu_space, 0, true);
        actlr_bad_read = true; actlr_answer = bad[i];
        assert(!adreno8_space_on(&adreno8_gpu_space, 0, 0));
        retained_failure();
    }
    prepare_actlr(&adreno8_gpu_space, 0, true);
    reject_offset = 0x20004; reject_value = 0x303;
    assert(!adreno8_space_on(&adreno8_gpu_space, 0, 0));
    retained_failure();
    prepare_actlr(&adreno8_gpu_space, 0, true);
    actlr_bad_read = cleanup_fail = true; actlr_answer = 3;
    assert(!adreno8_space_on(&adreno8_gpu_space, 0, 0));
    retained_failure();
    assert(adreno8_gpu_space.is_on);
    /* Unrelated banks and the HWSCHED path keep their inherited values. */
    const uint32_t contexts[] = {0, 2, 3};
    for (unsigned direct = 0; direct <= 1; direct++) {
        for (unsigned i = 0; i < sizeof(contexts) / sizeof(contexts[0]); i++) {
            uint32_t context = contexts[i];
            struct adreno8_gpu_space *space = context == 0 && !direct ? &adreno8_gpu_space : &other;
            prepare_actlr(space, context, direct);
            uint32_t stream = context == 2 ? 5 : context;
            uint32_t match = context == 2 ? 1 : context == 3 ? 2 : context;
            assert(adreno8_space_on(space, stream, match));
            assert(!actlr_writes && regs[0x20004 / 4] == 3);
            assert(regs[0x22004 / 4] == 0x123 && regs[0x23004 / 4] == 0x456);
        }
    }
    prepare_actlr(&adreno8_gpu_space, 2, true);
    assert(adreno8_space_on(&adreno8_gpu_space, 5, 1));
    assert(!actlr_writes && regs[0x22004 / 4] == 0x123);
    release_all();
    puts("ordinary CB0 ACTLR: full 303, fault route/SCTLR-off ordering, strict readback/check, retained failures and other paths unchanged passed");
}
'''
production = fixture["driver"]
enable = re.search(r"bool\nadreno8_space_on\(.*?\n}", production, re.S)[0]
write = re.search(r'\tif \([^\n]+\) \{\n\t\tadreno8_put\(&adreno8_smmu, "cb actlr".*?\n\t}',
                  enable, re.S)[0]
active = re.search(r'\tif \([^\n]+\)\n\t\tok &= adreno8_get\(&adreno8_smmu, "cb actlr"[^\n]+;', production)[0]
variants = {}
for label, before, after in (
    ("all-modes", "adreno8_direct_ring && ", ""),
    ("all-spaces", "space == &adreno8_gpu_space && ", ""),
    ("all-banks", " && space->context == 0", ""),
    ("omitted-write", '\t\tadreno8_put(&adreno8_smmu, "cb actlr", cb + SMMU_CB_ACTLR, SMMU_GPU_ACTLR);\n', ""),
    ("missing-barrier", '\t\tadreno8_settle();\n', ""),
    ("unchecked-write", 'if (adreno8_get(&adreno8_smmu, "cb actlr readback", cb + SMMU_CB_ACTLR) != SMMU_GPU_ACTLR)', 'if (false)'),
    ("partial-readback", 'if (adreno8_get(&adreno8_smmu, "cb actlr readback", cb + SMMU_CB_ACTLR) != SMMU_GPU_ACTLR)',
     'if ((adreno8_get(&adreno8_smmu, "cb actlr readback", cb + SMMU_CB_ACTLR) & 3) != 3)'),
    ("preserved-extra-bits", 'cb + SMMU_CB_ACTLR, SMMU_GPU_ACTLR);',
     'cb + SMMU_CB_ACTLR, SMMU_GPU_ACTLR | 0x28);'),
):
    assert before in write, label
    variants[label] = production.replace(write, write.replace(before, after, 1), 1)
variants["missing-active-check"] = production.replace(active, "", 1)
for label, marker in (
    ("before-fault-route", '\tadreno8_put(&adreno8_smmu, "s2cr fault", SMMU_S2CR(match),'),
    ("before-disabled", '\tadreno8_put(&adreno8_smmu, "cb sctlr", cb + SMMU_CB_SCTLR, 0);'),
    ("after-enabled", '\tadreno8_put(&adreno8_smmu, "cb sctlr", cb + SMMU_CB_SCTLR, sctlr);'),
):
    moved = enable.replace(write + "\n", "", 1)
    at = moved.index(marker)
    if label == "after-enabled": at = moved.index("\n", at) + 1
    moved = moved[:at] + write + "\n" + moved[at:]
    variants[label] = production.replace(enable, moved, 1)
variants["omitted-TLBI"] = production.replace(enable,
    enable.replace("if (!adreno8_space_sync(space))", "if (false)", 1), 1)
with tempfile.TemporaryDirectory(prefix="adreno8-actlr-") as directory:
    target = Path(directory)
    (target / "adreno8_private.h").write_text(stub)
    for name in ("adreno8_smmu_private.h", "adreno8_memory_private.h"):
        (target / name).write_text((DEV / name).read_text())
    (target / "check.c").write_text('#include "adreno8_smmu.c"\n#undef printf\n' + setup + cases)
    for label, content in [("production", production)] + list(variants.items()):
        if label != "production": assert content != production, label
        (target / "adreno8_smmu.c").write_text(content)
        for page in (4096, 16384):
            binary = target / f"actlr-{label}-{page}"
            subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page}", "-fsanitize=address,undefined",
                            "-fno-omit-frame-pointer", str(target / "check.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            if label == "production":
                assert result.returncode == 0, result.stdout + result.stderr
                print(f"ACTLR PAGE_SIZE={page}: PASS")
            else:
                assert result.returncode != 0 and "assert" in result.stderr.lower(), (label, page, result.stderr)
    print(f"Ordinary ACTLR mutations rejected: {len(variants)} variants x 2 page sizes")
