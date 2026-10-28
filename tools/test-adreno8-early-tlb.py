#!/usr/bin/env python3
"""Exercise the production ordinary-mode early NS TLB operation and its preconditions."""
import argparse
import hashlib
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--micode-dir", required=True, type=Path)
args = parser.parse_args()
# MiCode/Xiaomi_Kernel_OpenSource, annibale-w-oss, f66f8c8bcbe38044fab61d44ea7f7c224f82b3ea.
sources = {}
for name, digest in {
    "arm-smmu.h": "b4586e70f3d22f618cf4550cdac52b450f05f86edb0cc46e84db965c5ccab879",
    "arm-smmu.c": "0a90ed32693e6c653146f865daa046081ca6ceccdf7c0e44a00cb9564f920bf1",
}.items():
    data = (args.micode_dir / "drivers/iommu/arm/arm-smmu" / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest, name
    sources[name] = data.decode()
for name, offset in (("TLBIALLNSNH", "68"), ("sTLBGSYNC", "70"), ("sTLBGSTATUS", "74")):
    assert re.search(r"#define\s+ARM_SMMU_GR0_" + name + r"\s+0x" + offset + r"\b", sources["arm-smmu.h"])
for name, bit in (("GFIE", 2), ("GFRE", 1), ("CLIENTPD", 0)):
    assert re.search(r"#define\s+ARM_SMMU_sCR0_" + name + r"\s+BIT\(" + str(bit) + r"\)", sources["arm-smmu.h"])
assert "#define QCOM_DUMMY_VAL -1" in sources["arm-smmu.c"]
assert "ARM_SMMU_GR0_TLBIALLNSNH, QCOM_DUMMY_VAL" in sources["arm-smmu.c"]
assert "ARM_SMMU_GR0_sTLBGSYNC," in sources["arm-smmu.c"]
init = (DEV / "adreno8.c").read_text()
stages = re.search(r"static bool \(\*const each\[\]\)\(void\) = \{(.*?)\};", init, re.S)[1]
stages = re.findall(r"adreno8_\w+", stages)
assert stages[7:9] == ["adreno8_step_smmu_read", "adreno8_step_gmu_space"]
assert re.search(r"if \(!each\[i\]\(\)\) \{[^}]*\bbreak;", init, re.S)

fixture = runpy.run_path(str(ROOT / "tools/test-adreno8-smmu.py"))
stub = fixture["stub"].replace("static void adreno8_settle(void) {}", r'''
static unsigned barriers;
static void adreno8_settle(void) { barriers++; }
''', 1)
stub = stub.replace("static struct adreno8_block adreno8_gmu;",
    "static struct adreno8_block adreno8_gmu;\nextern struct adreno8_block adreno8_smmu;\n"
    "extern struct adreno8_gpu_space adreno8_gpu_space;", 1)
stub = stub.replace("static uint64_t mach_absolute_time(void) { return 0x1234abcdull; }", r'''
static uint64_t clock_ns, clock_step;
static uint64_t mach_absolute_time(void) {
    uint64_t now = clock_ns;
    clock_ns += clock_step;
    return now;
}
''', 1)
stub = stub.replace("static unsigned reads[0x40000 / 4];", r'''
static unsigned reads[0x40000 / 4];
static uint32_t cm3, gx, final_status, status_busy_reads;
static unsigned preflight_reads, cm3_reads, gx_reads, invalidations, syncs, status_reads;
static unsigned invalidate_barrier, sync_barrier, scr_reads, scr_writes, scr_barrier, scr_fault;
static uint32_t inherited_scr0;
static bool prerequisites_started, cx_ready = true;
''', 1)
stub = stub.replace("static bool adreno8_cx_ready(void) { return true; }",
                    "static bool cx_ready;\nstatic bool adreno8_cx_ready(void) { return cx_ready; }", 1)
# Keep the shared mapper/register model; specialize only the added GMU and GR0 accesses.
stub = stub.replace("    assert(address < sizeof(regs));", r'''
    assert(address >= 0x100000 && address < 0x100000 + sizeof(regs));
    address -= 0x100000;
    assert(address < sizeof(regs));
    assert(address != 0x68 && address != 0x70); /* Both command registers are WO. */
    if (address == 0x74) {
        assert(invalidations == 1 && syncs == 1 && adreno8_reset_required);
        status_reads++;
        assert(status_reads <= 101 && clock_ns <= 101000000ull);
        return status_reads <= status_busy_reads ? 1u : final_status;
    }
''', 1)
stub = stub.replace("return adreno8_rd(offset);", "return adreno8_rd(b->base + offset);", 1)
stub = stub.replace("    reads[offset / 4]++;", r'''
    if (b == &adreno8_gmu) {
        assert(!writes);
        prerequisites_started = true;
        if (offset == 0x47000) { cm3_reads++; return cm3; }
        assert(offset == 0x46fa0 && cm3_reads == 1);
        gx_reads++; return gx;
    }
    assert(b == &adreno8_smmu);
    if (prerequisites_started && offset >= 0x800 && offset < 0xc00) preflight_reads++;
    if (prerequisites_started && offset == 0) {
        assert(preflight_reads == (regs[0x20 / 4] & 255));
        scr_reads++;
        if (scr_reads == 2) {
            assert(scr_writes == 1 && barriers > scr_barrier);
            if (scr_fault == 2) return regs[0] ^ 0x100;
            if (scr_fault == 3) return ~0u;
        }
    }
    reads[offset / 4]++;
''', 1)
stub = stub.replace("    writes++;", r'''
    writes++;
    assert(b == &adreno8_smmu && adreno8_direct_ring && adreno8_step == 8);
    assert(adreno8_reset_required && !adreno8_gmu_space.is_on &&
           !adreno8_gpu_space.is_on && !adreno8_secure_space.is_on);
    assert(cm3_reads == 1 && gx_reads == 1 && cm3 != ~0u && (cm3 & 1) && gx != ~0u && (gx & 3) == 3);
    assert(preflight_reads == (regs[0x20 / 4] & 255));
    if (offset == 0) {
        assert(writes == 1 && !scr_writes && scr_reads == 1);
        assert(inherited_scr0 != ~0u && !(inherited_scr0 & 1) && (inherited_scr0 & 6) == 6);
        assert(value == (inherited_scr0 & ~4u));
        scr_writes++; scr_barrier = barriers;
        if (scr_fault != 1) regs[0] = value;
        return;
    }
    assert(regs[0] == (inherited_scr0 & ~4u) && scr_reads == (scr_writes ? 2u : 1u));
    assert(value == 0xffffffff);
    for (unsigned i = 0; i < preflight_reads; i++) assert(!(regs[(0x800 + 4 * i) / 4] & 0x80000000));
    if (offset == 0x68) {
        assert(writes == 1 + scr_writes && !invalidations && !syncs && barriers > 0);
        invalidations++; invalidate_barrier = barriers;
    } else {
        assert(offset == 0x70 && writes == 2 + scr_writes && invalidations == 1 && !syncs);
        assert(barriers > invalidate_barrier);
        syncs++; sync_barrier = barriers;
    }
    return;
''', 1)
setup = fixture["tests"].split("int main(void) {")[0]
cases = r'''
static void fresh(bool direct)
{
    struct adreno8_gpu_space unused;
    reset(&unused, false);
    memset(&adreno8_gmu_space, 0, sizeof(adreno8_gmu_space));
    memset(&adreno8_gpu_space, 0, sizeof(adreno8_gpu_space));
    memset(&adreno8_secure_space, 0, sizeof(adreno8_secure_space));
    adreno8_smmu.size = 0x40000;
    adreno8_smmu.base = 0x100000;
    adreno8_smmu_page = adreno8_smmu_pages = adreno8_smmu_contexts = adreno8_smmu_matches = 0;
    adreno8_step = 8;
    adreno8_direct_ring = direct;
    adreno8_reset_required = false;
    barriers = cm3_reads = gx_reads = preflight_reads = invalidations = syncs = status_reads = 0;
    invalidate_barrier = sync_barrier = scr_reads = scr_writes = scr_barrier = scr_fault = 0;
    inherited_scr0 = regs[0] = 0x2e0406;
    prerequisites_started = false;
    cm3 = 1; gx = 3; final_status = status_busy_reads = 0;
    clock_ns = 0; clock_step = 1000000;
    cx_ready = true;
}
static void unchanged(void)
{
    assert(writes == 0 && !invalidations && !syncs && !status_reads && !adreno8_reset_required);
    assert(allocation_count == 0 && !clean_count);
}
static void completed(void)
{
    assert(writes == 2 + scr_writes && invalidations == 1 && syncs == 1 && adreno8_reset_required);
    assert(regs[0] == (inherited_scr0 & ~4u) && scr_reads == (scr_writes ? 2u : 1u));
    assert(barriers > sync_barrier && preflight_reads == (regs[0x20 / 4] & 255));
    assert(allocation_count == 0 && !clean_count);
    assert(regs[0x68 / 4] == 0 && regs[0x70 / 4] == 0);
    /* No route, context, fault or Hyp register was written. */
    for (unsigned i = 0; i < sizeof(regs) / sizeof(regs[0]); i++)
        if (i != 0x20 / 4 && i != 0x24 / 4 && i != 0x28 / 4 && i != 0)
            assert(regs[i] == 0);
}
int main(void)
{
    assert(SMMU_TLBIALLNSNH == 0x68 && SMMU_TLBGSYNC == 0x70 && SMMU_TLBGSTATUS == 0x74);
    assert(GMU_CM3_SYSRESET == 0x47000 && GMU_PWR_CLK_STATUS == 0x46fa0);
    fresh(true);
    assert(adreno8_step_smmu_read()); completed(); assert(status_reads == 1);
    fresh(true); status_busy_reads = 99; final_status = 2;
    assert(adreno8_step_smmu_read()); completed(); assert(status_reads == 100);
    /* Scan all visible entries, including entries beyond the old diagnostic cap. */
    fresh(true); regs[0x20 / 4] = (regs[0x20 / 4] & ~255u) | 128;
    assert(adreno8_step_smmu_read()); completed(); assert(preflight_reads == 128);
    for (unsigned slot = 0; slot < 24; slot++) {
        const uint32_t bad[] = {0x80000000u, 0xffffffffu};
        for (unsigned i = 0; i < 2; i++) {
            fresh(true); regs[(0x800 + 4 * slot) / 4] = bad[i];
            assert(!adreno8_step_smmu_read()); unchanged(); assert(preflight_reads == slot + 1);
        }
    }
    for (unsigned bank = 0; bank < 3; bank++) {
        fresh(true);
        (bank == 0 ? &adreno8_gmu_space : bank == 1 ? &adreno8_gpu_space : &adreno8_secure_space)->is_on = true;
        assert(!adreno8_step_smmu_read()); unchanged(); assert(!cm3_reads && !gx_reads);
    }
    const uint32_t bad_cm3[] = {0, 2, 0xffffffffu};
    for (unsigned i = 0; i < 3; i++) {
        fresh(true); cm3 = bad_cm3[i];
        assert(!adreno8_step_smmu_read()); unchanged(); assert(!preflight_reads);
    }
    const uint32_t bad_gx[] = {0, 1, 2, 0xffffffffu};
    for (unsigned i = 0; i < 4; i++) {
        fresh(true); gx = bad_gx[i];
        assert(!adreno8_step_smmu_read()); unchanged(); assert(!preflight_reads);
    }
    const uint32_t bad_scr0[] = {0xffffffffu, 0, 1, 3, 4, 5, 0x2e0407};
    for (unsigned i = 0; i < sizeof(bad_scr0) / sizeof(*bad_scr0); i++) {
        fresh(true); inherited_scr0 = regs[0] = bad_scr0[i];
        assert(!adreno8_step_smmu_read()); unchanged(); assert(scr_reads == 1);
    }
    const uint32_t valid_scr0[] = {2, 6, 0xfffffffeu, 0x2e0406, 0xdeadbeee};
    for (unsigned i = 0; i < sizeof(valid_scr0) / sizeof(*valid_scr0); i++) {
        fresh(true); inherited_scr0 = regs[0] = valid_scr0[i];
        assert(adreno8_step_smmu_read()); completed();
        assert(scr_writes == !!(inherited_scr0 & 4));
    }
    for (unsigned fault = 1; fault <= 3; fault++) {
        fresh(true); scr_fault = fault;
        assert(!adreno8_step_smmu_read());
        assert(writes == 1 && scr_writes == 1 && scr_reads == 2 && adreno8_reset_required);
        assert(!invalidations && !syncs && !status_reads && !allocation_count);
    }
    fresh(true); cm3 = 3; gx = 7; /* Only the reset bit and two GX-off bits are constrained. */
    assert(adreno8_step_smmu_read()); completed();
    for (unsigned busy = 0; busy <= 2; busy++) {
        fresh(true); status_busy_reads = busy; final_status = 0xffffffffu;
        assert(!adreno8_step_smmu_read());
        assert(adreno8_reset_required && writes == 3 && invalidations == 1 && syncs == 1);
        assert(status_reads == busy + 1 && barriers == sync_barrier && allocation_count == 0);
    }
    fresh(true); status_busy_reads = 1000;
    assert(!adreno8_step_smmu_read());
    assert(status_reads == 100 && clock_ns == 101000000ull && writes == 3);
    assert(adreno8_reset_required && barriers == sync_barrier && !allocation_count);
    fresh(true); cx_ready = false;
    assert(!adreno8_step_smmu_read()); unchanged(); assert(!reads[0x20 / 4]);
    fresh(true); regs[0x24 / 4] = 0;
    assert(!adreno8_step_smmu_read()); unchanged(); assert(!cm3_reads);
    const uint32_t ids[] = {0x20, 0x24};
    for (unsigned i = 0; i < 2; i++) {
        fresh(true); regs[ids[i] / 4] = 0xffffffffu;
        assert(!adreno8_step_smmu_read()); unchanged(); assert(!cm3_reads && !gx_reads);
        assert(!adreno8_smmu_page && !adreno8_smmu_pages && !adreno8_smmu_contexts && !adreno8_smmu_matches);
    }
    fresh(false); regs[0x20 / 4] = 0xffffffffu;
    assert(adreno8_step_smmu_read()); unchanged(); assert(!cm3_reads && !gx_reads);
    fresh(false); regs[0x24 / 4] = 0xffffffffu;
    assert(!adreno8_step_smmu_read()); unchanged(); assert(!cm3_reads && !gx_reads);
    fresh(false); cm3 = gx = 0xffffffffu; regs[0x800 / 4] = 0xffffffffu;
    adreno8_gmu_space.is_on = adreno8_gpu_space.is_on = adreno8_secure_space.is_on = true;
    assert(adreno8_step_smmu_read()); unchanged(); assert(!cm3_reads && !gx_reads && !preflight_reads && !scr_reads && !scr_writes);
    release_all();
    puts("early ordinary NS TLB: preflight, WO sequence, timeout, retained failure and HWSCHED zero writes passed");
}
'''
production = fixture["driver"]
variants = {}
for label, before, after in (
    ("all-modes", "!adreno8_direct_ring || ", ""),
    ("omitted-call", "return !adreno8_direct_ring || adreno8_smmu_early_ns_tlb();", "return true;"),
    ("ignored-failure", "return !adreno8_direct_ring || adreno8_smmu_early_ns_tlb();",
     "if (adreno8_direct_ring) (void)adreno8_smmu_early_ns_tlb(); return true;"),
    ("gmu-on", "adreno8_gmu_space.is_on || ", ""),
    ("gpu-on", "adreno8_gpu_space.is_on || ", ""),
    ("secure-on", " || adreno8_secure_space.is_on", ""),
    ("idr0-all1", "idr0 == ~0u || ", ""),
    ("idr1-all1", "idr1 == ~0u", "false"),
    ("cm3-not-reset", "!(reset & 1)", "false"),
    ("cm3-all1", "reset == ~0u || ", ""),
    ("gx-state", "(gx & 3) != 3", "false"),
    ("gx-all1", "gx == ~0u || ", ""),
    ("smr-cap16", "i < adreno8_smmu_matches; i++", "i < adreno8_smmu_matches && i < 16; i++"),
    ("ignored-valid-smr", "SMMU_SMR(i)) & SMR_VALID", "SMMU_SMR(i)) & 0"),
    ("scr0-clientpd", "(scr0 & 1)", "false"),
    ("scr0-gfre", "!(scr0 & 2)", "false"),
    ("scr0-unconditional-write", "if (scr0 & 4)", "if (true)"),
    ("scr0-wrong-bit", "scr0 & ~4u", "scr0 & ~2u"),
    ("scr0-readback-ignored", 'if (adreno8_get(&adreno8_smmu, "early NS global fault IRQ readback", SMMU_SCR0) != (scr0 & ~4u))',
     'if (adreno8_get(&adreno8_smmu, "early NS global fault IRQ readback", SMMU_SCR0) == 0)'),
    ("scr0-retention", "adreno8_reset_required = true;", "adreno8_reset_required = false;"),
    ("scr0-post-write-barrier", '\t\tadreno8_settle();\n\t\tif (adreno8_get(&adreno8_smmu, "early NS global fault IRQ readback"',
     '\t\tif (adreno8_get(&adreno8_smmu, "early NS global fault IRQ readback"'),
    ("no-invalidate", '\tadreno8_put(&adreno8_smmu, "early NS TLB invalidate", SMMU_TLBIALLNSNH, ~0u);', ""),
    ("wrong-invalidate-value", 'SMMU_TLBIALLNSNH, ~0u);', 'SMMU_TLBIALLNSNH, 0);'),
    ("no-sync", '\tadreno8_put(&adreno8_smmu, "early NS TLB sync", SMMU_TLBGSYNC, ~0u);', ""),
    ("wrong-sync-value", 'SMMU_TLBGSYNC, ~0u);', 'SMMU_TLBGSYNC, 0);'),
    ("no-pre-invalidate-barrier", '\tadreno8_settle();\n\tadreno8_put(&adreno8_smmu, "early NS TLB invalidate"',
     '\tadreno8_put(&adreno8_smmu, "early NS TLB invalidate"'),
    ("no-command-barrier", '\tadreno8_settle();\n\tadreno8_put(&adreno8_smmu, "early NS TLB sync"',
     '\tadreno8_put(&adreno8_smmu, "early NS TLB sync"'),
    ("all1-status", "if (status == ~0u) break;", "if (status == ~0u) return true;"),
    ("missing-status-base", "adreno8_smmu.base + SMMU_TLBGSTATUS", "SMMU_TLBGSTATUS"),
    ("busy-as-complete", "if (!(status & 1))", "if (true)"),
    ("timeout-too-long", "elapsed >= 100000000ull", "elapsed >= 200000000ull"),
    ("timeout-too-short", "elapsed >= 100000000ull", "elapsed >= 1000000ull"),
    ("no-final-barrier", '\t\t\tadreno8_settle();', ""),
):
    assert before in production, label
    variants[label] = production.replace(before, after, 1)
variants["tlb-retention"] = production.replace(
    '\tadreno8_reset_required = true;\n\tadreno8_settle();\n\tadreno8_put(&adreno8_smmu, "early NS TLB invalidate"',
    '\tadreno8_reset_required = false;\n\tadreno8_settle();\n\tadreno8_put(&adreno8_smmu, "early NS TLB invalidate"', 1)
variants["before-layout"] = production.replace("\tif (!adreno8_smmu_layout()) {",
    "\tif (adreno8_direct_ring && !adreno8_smmu_early_ns_tlb()) return false;\n\tif (!adreno8_smmu_layout()) {", 1)
with tempfile.TemporaryDirectory(prefix="adreno8-early-tlb-") as directory:
    target = Path(directory)
    (target / "adreno8_private.h").write_text(stub)
    for name in ("adreno8_memory_private.h", "adreno8_smmu_private.h"):
        (target / name).write_text((DEV / name).read_text())
    (target / "check.c").write_text('#include "adreno8_smmu.c"\n#undef printf\n' + setup + cases)
    for label, content in [("production", production)] + list(variants.items()):
        if label != "production": assert content != production, label
        (target / "adreno8_smmu.c").write_text(content)
        for page in (4096, 16384):
            binary = target / f"tlb-{label}-{page}"
            subprocess.run(["cc", "-std=c11", "-Werror", f"-DPAGE_SIZE={page}",
                            "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                            str(target / "check.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            if label == "production":
                assert result.returncode == 0, result.stdout + result.stderr
                print(f"Early NS TLB PAGE_SIZE={page}: PASS")
            else:
                assert result.returncode != 0 and "assert" in result.stderr.lower(), (label, page, result.stderr)
    print(f"Early NS TLB mutations rejected: {len(variants)} variants x 2 page sizes")
