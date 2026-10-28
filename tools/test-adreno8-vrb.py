#!/usr/bin/env python3
"""Exercise production VRB allocation/publication/startup against pinned trace ABI."""
import argparse
import contextlib
import hashlib
import io
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--vendor-dir", required=True, type=Path)
args = parser.parse_args()
vendor = {}
for name, digest in {
    "kgsl_gmu_core.c": "fa96591c4ee5017a436f33ddd20245b9466e77f8250eb234214668156ff92c9e",
    "kgsl_gmu_core.h": "ca5e8d4d6bff0b978ffcac5e933ac2bf7d31e495b9f7f67e35c557b338342b4d",
}.items():
    data = (args.vendor_dir / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest, f"unexpected vendor input: {name}"
    vendor[name] = data.decode()


def function(text, name, result):
    start = text.index(name + "(")
    return result + "\n" + text[start:text.index("\n}\n", start) + 2] + "\n"


with contextlib.redirect_stdout(io.StringIO()):
    fixture = runpy.run_path(str(ROOT / "tools/test-adreno8-smmu.py"))
stub = fixture["stub"].replace("assert(size == 4096);", "assert(size != 0);")
stub = stub.replace("0x40000 / 4", "0x68000 / 4")
private = (DEV / "adreno8_private.h").read_text()
constants = "\n".join(line for line in private.splitlines() if line.startswith(("#define GMU", "#define GPU_GBIF_", "#define RSCC")))
stub = stub.replace("static int kernel_map;", constants + "\n" + r'''
static unsigned releases, clean_at_general11;
static uint32_t expected_vrb;
static int kernel_map;''')
stub = stub.replace("    assert(offset < sizeof(regs));", r'''
    assert(offset < sizeof(regs));
    if (offset == GMU_GENERAL_11) clean_at_general11 = clean_count;
    if (offset == GMU_CM3_SYSRESET && value == 0) {
        assert(expected_vrb && regs[GMU_GENERAL_11 / 4] == expected_vrb);
        assert(reads[GMU_GENERAL_11 / 4] > 0 && clean_at_general11 > 0);
        releases++; regs[GMU_FW_INIT_RESULT / 4] = GMU_FW_STARTED;
    }''', 1)
stub = stub.replace("    if (offset == 0x22000 + 0x8f0)",
    "    if (offset == GMU_FW_INIT_RESULT) return (regs[offset / 4] & mask) == value;\n"
    "    if (offset == 0x22000 + 0x8f0)", 1)

memory = (DEV / "adreno8_memory.c").read_text()
declarations = memory[memory.index("#define MEMORY_GRANULE"):memory.index("bool\nadreno8_memory_retained")]
memory_code = declarations + "\n".join(function(memory, name, result) for name, result in (
    ("adreno8_memory_retained", "bool"),
    ("adreno8_memory_alloc", "static struct adreno8_memory *"),
    ("adreno8_gmu_alloc", "struct adreno8_memory *"),
    ("adreno8_memory_entry", "static struct adreno8_memory_entry *"),
    ("adreno8_memory_range", "static bool"),
    ("adreno8_memory_publish", "bool"),
))
gmu = (DEV / "adreno8_gmu.c").read_text()
gmu_code = function(gmu, "adreno8_gmu_hwsched_memory", "static bool") + function(gmu, "adreno8_step_gmu_start", "bool")
vendor_code = "\n".join(re.search(r"^#define\s+" + name + r"\s+.*$", vendor["kgsl_gmu_core.h"], re.M)[0] for name in (
    "TRACE_MODE_DROP", "TRACE_HEADER_VERSION_1", "TRACE_BUFFER_THRESHOLD", "TRACE_TIMEOUT_MSEC", "TRACE_LOGTYPE_HWSCHED",
)) + "\n" + re.search(r"struct gmu_trace_header \{.*?\n};", vendor["kgsl_gmu_core.h"], re.S)[0]
vendor_code += function(vendor["kgsl_gmu_core.c"], "gmu_core_trace_header_init", "void")
setup = fixture["tests"].split("int main(void) {")[0]
prefix = r'''
#include "adreno8_smmu.c"
#include "adreno8_hwsched_private.h"
#include "adreno8_secure_private.h"
uint32_t adreno8_gpu8_secure_mode;
uint32_t adreno8_secure_xfer_state(void) { return SECURE_XFER_NOT_STARTED; }
bool adreno8_secure_lost(void) { return false; }
''' + memory_code + r'''
static const struct adreno8_memory *adreno8_gmu_vrb;
static struct adreno8_block adreno8_gmu = {"gmu", 0, 0x68000, 0}, adreno8_gpu, adreno8_rscc;
static struct adreno8_gpu_block adreno8_gmu_log, adreno8_gmu_hfi;
static bool adreno8_direct_ring;
static bool adreno8_fw_loaded, adreno8_gmu_runs, adreno8_rsc_asleep_at_first, adreno8_rsc_woken_by_us;
static bool adreno8_scm_gpu_init(void) { return true; }
static void adreno8_gmu_post_mortem(void) {}
'''
suffix = r'''
#undef printf
typedef uint32_t u32;
typedef uint64_t u64;
#define __packed __attribute__((packed))
#define GENMASK(h, l) ((~0u << (l)) & (~0u >> (31 - (h))))
#define FIELD_PREP(mask, val) (((uint32_t)(val) << __builtin_ctz(mask)) & (mask))
struct kgsl_memdesc { void *hostptr; uint32_t gmuaddr, size; };
struct kgsl_gmu_trace { struct kgsl_memdesc *md; };
''' + vendor_code + setup + r'''
static void prepare(void) {
    reset(&adreno8_gmu_space, false);
    memset(adreno8_memory_entries, 0, sizeof(adreno8_memory_entries));
    memset(&adreno8_gpu_space, 0, sizeof(adreno8_gpu_space));
    adreno8_memory_count = 0; adreno8_memory_bytes = 0; adreno8_memory_failed = false;
    adreno8_gmu_vrb = NULL; adreno8_gmu_runs = false; releases = clean_at_general11 = 0;
    assert(adreno8_space_new_table(&adreno8_gmu_space, 0, 0));
    for (unsigned offset = 0; offset < 0xa000; offset += 4096)
        assert(adreno8_space_map(&adreno8_gmu_space, 0x60000000 + offset, 0x90000000 + offset));
    /* A second reservation makes trace allocation skip the first candidate. */
    assert(adreno8_space_map(&adreno8_gmu_space, 0x6000b000, 0x9000b000));
    adreno8_gmu_log = (struct adreno8_gpu_block){"log", 0x60002000, 0x4000, 1};
    adreno8_gmu_hfi = (struct adreno8_gpu_block){"hfi", 0x60006000, 0x4000, 1};
    adreno8_fw_loaded = true; regs[GMU_CM3_SYSRESET / 4] = 1;
}
static void check_published(const struct adreno8_memory *vrb, const struct adreno8_memory *trace) {
    bool bank_clean = false, trace_clean = false;
    for (unsigned i = 0; i < clean_count; i++) {
        if (cleaned[i].start == trace->memory && cleaned[i].size == 48 && cleaned[i].snap_len == 48) {
            struct gmu_trace_header expected = {0};
            struct kgsl_memdesc md = {&expected, trace->gmu_address, trace->size};
            struct kgsl_gmu_trace vendor_trace = {&md};
            gmu_core_trace_header_init(&vendor_trace);
            assert(!memcmp(cleaned[i].snap, &expected, sizeof(expected)));
            trace_clean = true;
        }
        if (cleaned[i].start == vrb->memory && cleaned[i].size == vrb->size && trace_clean)
            bank_clean = true;
    }
    assert(trace_clean && bank_clean);
}
int main(void) {
    prepare();
    assert(adreno8_space_on(&adreno8_gmu_space, 5, 1));
    unsigned before = writes;
    assert(!adreno8_step_gmu_start() && !releases && writes == before);
    for (int failure = 0; failure < 2; failure++) {
        prepare(); alloc_fail_after = failure;
        assert(!adreno8_gmu_hwsched_memory() && !adreno8_gmu_vrb);
        assert(adreno8_memory_count == (unsigned)failure && !adreno8_gmu_space.is_on);
        assert(adreno8_memory_retained() == (failure == 1));
        assert(!adreno8_step_gmu_start() && !releases);
    }
    prepare(); next_pa = SMMU_PHYS_LIMIT;
    assert(!adreno8_gmu_hwsched_memory() && !adreno8_gmu_vrb);
    assert(adreno8_memory_count == 1 && adreno8_memory_failed);
    assert(adreno8_memory_retained());
    assert(adreno8_memory_entries[0].buffer.memory && !adreno8_memory_entries[0].complete);
    assert(!adreno8_step_gmu_start() && !releases);
    prepare();
    assert(adreno8_gmu_hwsched_memory() && adreno8_memory_count == 2);
    const struct adreno8_memory *vrb = adreno8_gmu_vrb, *trace = &adreno8_memory_entries[1].buffer;
    assert(vrb == &adreno8_memory_entries[0].buffer && vrb->size == 4096 && trace->size == 16384);
    assert(vrb->gpu_address == 0 && trace->gpu_address == 0);
    assert(vrb->gmu_address == 0x6000a000 && trace->gmu_address == 0x6000c000);
    uint32_t *bank = (uint32_t *)vrb->memory;
    for (unsigned i = 0; i < vrb->size / 4; i++)
        assert(bank[i] == (i == 0 ? 1024 : i == 2 ? trace->gmu_address : 0));
    assert(sizeof(struct gmu_trace_header) == 48);
    for (unsigned i = 48; i < trace->size; i++) assert(!((uint8_t *)trace->memory)[i]);
    check_published(vrb, trace);
    for (unsigned i = 0; i < 2; i++) {
        const struct adreno8_memory *buffer = &adreno8_memory_entries[i].buffer;
        for (unsigned offset = 0; offset < buffer->size; offset += 4096) {
            uint64_t pte = *adreno8_space_entry(&adreno8_gmu_space, buffer->gmu_address + offset);
            assert((pte & ~PTE_ADDRESS) == (0xe03 | (3ULL << 53)) &&
                   (pte & PTE_ADDRESS) == kvtophys(buffer->memory + offset));
        }
    }
    assert(adreno8_space_on(&adreno8_gmu_space, 5, 1));
    expected_vrb = vrb->gmu_address;
    reject_offset = GMU_GENERAL_11; reject_value = expected_vrb;
    assert(!adreno8_step_gmu_start() && !adreno8_gmu_runs && !releases);
    assert(adreno8_memory_count == 2 && vrb->memory && trace->memory);
    assert(adreno8_memory_retained());
    reject_offset = ~0u;
    assert(adreno8_step_gmu_start() && adreno8_gmu_runs && releases == 1);
    assert(regs[GMU_GENERAL_11 / 4] == expected_vrb && reads[GMU_GENERAL_11 / 4] >= 2);
    check_published(vrb, trace);
    release_all();
    puts("VRB/trace vendor header, NC mappings, collision avoidance, retained failures and CM3 ordering: PASS");
}
'''
variants = {
    "baseline": gmu_code,
    "wrong-metadata": gmu_code.replace("header[1] = 0x40000001", "header[1] = 1"),
    "wrong-vrb-index": gmu_code.replace("bank[2] = trace->gmu_address", "bank[1] = trace->gmu_address"),
    "missing-trace-publish": gmu_code.replace("!adreno8_memory_publish(trace, 0, 48) || ", ""),
    "missing-bank-publish": gmu_code.replace(" || !adreno8_memory_publish(vrb, 0, vrb->size)", ""),
    "unprivileged-mapping": gmu_code.replace(" | ADRENO8_MEM_PRIV;", ";"),
    "ignore-readback": gmu_code.replace("!= adreno8_gmu_vrb->gmu_address) {", "== UINT32_MAX) {"),
    "missing-vrb-admission": gmu_code.replace("(!adreno8_direct_ring && !adreno8_gmu_vrb) || ", ""),
}
with tempfile.TemporaryDirectory(prefix="adreno8-vrb-") as directory:
    target = Path(directory)
    (target / "adreno8_private.h").write_text(stub)
    for name in ("adreno8_smmu_private.h", "adreno8_memory_private.h", "adreno8_hwsched_private.h", "adreno8_secure_private.h"):
        (target / name).write_text((DEV / name).read_text())
    (target / "adreno8_smmu.c").write_text(fixture["driver"])
    for name, body in variants.items():
        assert name == "baseline" or body != gmu_code, name
        (target / "check.c").write_text(prefix + body + suffix)
        for page in (4096, 16384):
            binary = target / f"check-{page}"
            subprocess.run(["cc", "-std=c11", "-fsanitize=address,undefined", f"-DPAGE_SIZE={page}",
                            str(target / "check.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            if name == "baseline":
                if result.returncode:
                    print(result.stdout, result.stderr)
                    result.check_returncode()
                print(f"XNU PAGE_SIZE={page}: {result.stdout.splitlines()[-1]}")
            else:
                assert result.returncode, f"undetected VRB mutation: {name}, page size {page}"
    print(f"VRB mutations rejected: {len(variants) - 1} variants x 2 page sizes")
