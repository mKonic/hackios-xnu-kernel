#!/usr/bin/env python3
"""Compare the production CB0 aperture call with fixed MiCode SCM code and exercise SMMU ordering."""
import argparse
import hashlib
from pathlib import Path
import re
import runpy
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
# MiCode/Xiaomi_Kernel_OpenSource, annibale-w-oss, f66f8c8bcbe38044fab61d44ea7f7c224f82b3ea.
HASHES = {
    "drivers/firmware/qcom_scm.c": "619418ae82a4547d2ad2fdd0d2119434b35b77b2964ef3a6d1ebd2d30f11ba9e",
    "drivers/firmware/qcom_scm.h": "83341242793a7e4cd9e24e67cefc7f78c95aabecd8b3519dd2156f9144085811",
    "drivers/firmware/qcom_scm-smc.c": "c7e2a7e1f83ac3c4b6bb193eca422e076286e7ffd73196eedf46c75113c9d627",
    "include/linux/arm-smccc.h": "d31e57f467b906c61dd4b4ae4fd16c9d736c1793c17a5ca046d6ccfce524f5a7",
}


def function(text, name):
    match = re.search(r"(?m)^(?:bool|int)(?:\n| +)" + name + r"\(", text)
    assert match, name
    return text[match.start():text.index("\n}", match.end()) + 3]


def macro(text, name):
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(r"#define\s+" + name + r"(?:\W|$)", line))
    end = start
    while lines[end].endswith("\\"):
        end += 1
    return "\n".join(lines[start:end + 1]) + "\n"


def vendor_reference(vendor):
    header = vendor["drivers/firmware/qcom_scm.h"]
    arm = vendor["include/linux/arm-smccc.h"]
    text = "typedef uint32_t u32; typedef uint64_t u64;\n#define _AC(x, y) x ## y\n"
    for name in ("MAX_QCOM_SCM_ARGS", "QCOM_SCM_ARGS_IMPL", "QCOM_SCM_ARGS", "SCM_SMC_FNID",
                 "QCOM_SCM_SVC_MP", "QCOM_SCM_MP_CP_SMMU_APERTURE_ID", "QCOM_SCM_CP_APERTURE_REG"):
        text += macro(header, name)
    for name in ("ARM_SMCCC_STD_CALL", "ARM_SMCCC_SMC_64", "ARM_SMCCC_TYPE_SHIFT",
                 "ARM_SMCCC_CALL_CONV_SHIFT", "ARM_SMCCC_OWNER_MASK", "ARM_SMCCC_OWNER_SHIFT",
                 "ARM_SMCCC_FUNC_MASK", "ARM_SMCCC_CALL_VAL", "ARM_SMCCC_OWNER_SIP"):
        text += macro(arm, name)
    text += re.search(r"struct qcom_scm_desc \{.*?\n};", header, re.S).group() + "\n"
    encoding = re.search(r"smc.args\[0\] = ARM_SMCCC_CALL_VAL\(.*?;",
                         vendor["drivers/firmware/qcom_scm-smc.c"], re.S).group()
    text += r'''
static struct { void *dev; } reference_device, *__scm = &reference_device;
static struct qcom_scm_desc reference_desc;
static uint64_t reference_function;
static int qcom_scm_call(void *device, const struct qcom_scm_desc *desc, void *result) {
    assert(device == NULL && result == NULL);
    struct { uint64_t args[8]; } smc = {0};
    uint32_t smccc_call_type = ARM_SMCCC_STD_CALL, qcom_smccc_convention = ARM_SMCCC_SMC_64;
'''
    text += encoding + r'''
    reference_function = smc.args[0];
    reference_desc = *desc;
    return 0;
}
'''
    text += function(vendor["drivers/firmware/qcom_scm.c"], "qcom_scm_kgsl_set_smmu_aperture")
    return text


SPY = r'''
static int32_t transport_status;
static uint64_t transport_answer;
static unsigned aperture_calls;
int32_t adreno8_scm_call(uint64_t function, uint64_t arginfo, uint64_t a0,
    uint64_t a1, uint64_t a2, uint64_t a3, uint64_t *answer) {
    assert(adreno8_gpu_space.is_on && adreno8_gpu_space.context == 0);
    assert(regs[(0x20000 + SMMU_CB_SCTLR) / 4] & 1);
    assert(regs[(0x20000 + SMMU_CB_TTBR0) / 4] == (uint32_t)adreno8_gpu_space.tables[0].phys);
    assert(function == reference_function && arginfo == reference_desc.arginfo);
    assert(a0 == reference_desc.args[0] && a1 == reference_desc.args[1]);
    assert(a2 == reference_desc.args[2] && a3 == reference_desc.args[3]);
    assert(aperture_calls++ == 0);
    *answer = transport_answer;
    return transport_status;
}
static bool adreno8_memory_failed;
void adreno8_hwsched_memory_failed(void) { adreno8_memory_failed = true; }
static unsigned adreno8_memory_count;
static bool adreno8_secure_lost(void) { return false; }
'''


CASES = r'''
static void reset_aperture(void) {
    reset(&adreno8_gpu_space, false);
    adreno8_gpu_space.context = 0;
    adreno8_memory_failed = false;
    aperture_calls = 0;
    transport_status = 0;
    transport_answer = 0;
    cx_ready = true;
    assert(qcom_scm_kgsl_set_smmu_aperture(0) == 0);
    map_one(&adreno8_gpu_space);
}
int main(void) {
    const uint64_t answers[] = {0, 1, UINT32_MAX, 0x123456789abcdef0ull};
    for (unsigned i = 0; i < sizeof(answers) / sizeof(answers[0]); i++) {
        reset_aperture(); transport_answer = answers[i];
        unsigned allocations_before = allocation_count;
        assert(!adreno8_gpu_memory_ready());
        assert(adreno8_gpu_memory_enable() && adreno8_gpu_memory_ready() && aperture_calls == 1);
        assert(allocation_count == allocations_before);
        unsigned writes_before = writes;
        assert(adreno8_gpu_memory_enable() && aperture_calls == 1 && writes == writes_before);
    }
    const int32_t failures[] = {-4, -5, -12, 2, 3};
    for (unsigned i = 0; i < sizeof(failures) / sizeof(failures[0]); i++) {
        reset_aperture(); transport_status = failures[i];
        unsigned allocations_before = allocation_count, tables_before = adreno8_gpu_space.table_count;
        assert(!adreno8_gpu_memory_enable() && aperture_calls == 1);
        assert(adreno8_memory_failed && !adreno8_gpu_memory_ready());
        assert(adreno8_gpu_space.is_on && adreno8_memory_retained());
        assert(allocation_count == allocations_before && adreno8_gpu_space.table_count == tables_before);
        unsigned writes_before = writes;
        transport_status = 0;
        assert(!adreno8_gpu_memory_enable() && aperture_calls == 1 && writes == writes_before);
    }
    reset_aperture(); sync_ok = false;
    assert(!adreno8_gpu_memory_enable() && aperture_calls == 0);
    assert(adreno8_memory_failed && adreno8_memory_retained() && !adreno8_gpu_memory_ready());
    reset_aperture(); cx_ready = false;
    unsigned writes_before = writes;
    assert(!adreno8_gpu_memory_enable() && aperture_calls == 0 && writes == writes_before);
    release_all();
    puts("CB0 aperture: fixed-vendor wire, TTBR0/enable ordering, transport-only status, retention and repeat gating: PASS");
    return 0;
}
'''


def run(source, target, page_size, label, success=True):
    path, binary = target / (label + ".c"), target / label
    path.write_text(source)
    compilation = subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}", "-fsanitize=address,undefined",
                                  "-fno-omit-frame-pointer", str(path), "-o", str(binary)], capture_output=True, text=True)
    if compilation.returncode:
        raise RuntimeError(label + " compilation failed:\n" + compilation.stderr)
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    if success:
        if result.returncode:
            raise RuntimeError(label + " failed:\n" + result.stdout + result.stderr)
        print(f"XNU PAGE_SIZE={page_size}: " + result.stdout.splitlines()[-1])
    elif result.returncode == 0:
        raise RuntimeError("mutation survived: " + label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--micode-dir", required=True, type=Path)
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()
    vendor = {}
    for name, expected in HASHES.items():
        data = (args.micode_dir / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected, f"unexpected MiCode input: {name}"
        vendor[name] = data.decode()
    fixture = runpy.run_path(str(ROOT / "tools/test-adreno8-smmu.py"))
    stub = fixture["stub"].replace("static bool adreno8_scm_gpu_aperture(void) { return true; }", "")
    stub = stub.replace("static bool adreno8_cx_ready(void) { return true; }",
                        "static bool cx_ready;\nstatic bool adreno8_cx_ready(void) { return cx_ready; }")
    assert "static bool cx_ready;" in stub and "static bool adreno8_scm_gpu_aperture" not in stub
    cx, memory = (DEV / "adreno8_cx.c").read_text(), (DEV / "adreno8_memory.c").read_text()
    helper = function(cx, "adreno8_scm_gpu_aperture")
    functions = macro(memory, "GPU_MATCH") + "\n".join(function(memory, name) for name in
                          ("adreno8_memory_retained", "adreno8_gpu_memory_ready", "adreno8_gpu_memory_verify",
                           "adreno8_gpu_memory_enable", "adreno8_gpu_faulted"))
    setup = fixture["tests"].split("int main(void) {")[0]
    source = ('#include "adreno8_smmu.c"\n' + setup + vendor_reference(vendor) + SPY + helper
              + functions + "\n#undef printf\n" + CASES)
    with tempfile.TemporaryDirectory(prefix="adreno8-aperture-") as directory:
        target = Path(directory)
        (target / "adreno8_private.h").write_text(stub)
        (target / "adreno8_smmu.c").write_text(fixture["driver"])
        for name in ("adreno8_memory_private.h", "adreno8_smmu_private.h"):
            (target / name).write_text((DEV / name).read_text())
        for page_size in (4096, 16384):
            run(source, target, page_size, f"check-{page_size}")
        if args.mutations:
            changes = {
                "selector": ("0x42000c1b", "0x42000c1a"),
                "arginfo": ("0x42000c1b, 4", "0x42000c1b, 3"),
                "context_bank": ("0xffff0000, 0xffffffff", "0xffff0001, 0xffffffff"),
                "argument": ("0xffff0000, 0xffffffff", "0xffff0000, 0"),
                "ignore_transport": ("&answer) == 0;", "&answer) >= -100;"),
                "require_answer": ("&answer) == 0;", "&answer) == 0 && answer == 0;"),
                "skip_aperture": (" || !adreno8_scm_gpu_aperture()", ""),
                "before_ttbr0": ("!adreno8_space_on(&adreno8_gpu_space, 0, GPU_MATCH) || !adreno8_scm_gpu_aperture()",
                                 "!adreno8_scm_gpu_aperture() || !adreno8_space_on(&adreno8_gpu_space, 0, GPU_MATCH)"),
                "retry_after_failure": ("adreno8_memory_failed = true;\n\t\treturn false;\n\t}\n\treturn adreno8_gpu_memory_verify();",
                                        "adreno8_memory_failed = false;\n\t\treturn false;\n\t}\n\treturn adreno8_gpu_memory_verify();"),
            }
            for name, (old, new) in changes.items():
                # Restrict mutations to the production helper/functions, leaving the vendor reference intact.
                production = helper + functions
                assert old in production, name
                mutated = source.replace(production, production.replace(old, new, 1), 1)
                assert mutated != source, name
                run(mutated, target, 4096, name, False)
            print(f"CB0 aperture mutations detected: {len(changes)}")


if __name__ == "__main__":
    main()
