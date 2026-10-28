#!/usr/bin/env python3
"""Check inherited ordinary HWCG state and the existing HWSCHED HWCG sequence."""
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / "tools/test-adreno8-gpu.py"))
DEV, args = fixture["DEV"], fixture["args"]
production = (DEV / "adreno8_gpu.c").read_text()
source = fixture["source"]
# The complete startup model must also reject these writes outside gpu_hwcg.
source = source.replace("void adreno8_wr(vm_offset_t address, uint32_t value)\n{", r'''
void adreno8_wr(vm_offset_t address, uint32_t value)
{
    if (adreno8_direct_ring) {
        const vm_offset_t untouched[] = {
            adreno8_gmu.base + GMU(0x23b09), adreno8_gmu.base + GMU(0x23b0a),
            adreno8_gmu.base + GMU(0x23b0b), adreno8_gpu.base + 4 * 0x9a,
            adreno8_gpu.base + 4 * 0x9b, adreno8_gpu.base + 4 * 0x3c00
        };
        for (unsigned i = 0; i < A8_COUNT(untouched); i++) assert(address != untouched[i]);
    }
''', 1)
assert source != fixture["source"]
check = r'''
static void check_hwcg_modes(void)
{
    reset(); adreno8_direct_ring = true;
    const uint32_t inherited[] = {0x12345678, 0xa5a50001, 0xffffffff};
    for (unsigned i = 0; i < 3; i++) {
        gmu_regs[GMU(0x23b09 + i) / 4] = inherited[i];
        gpu_regs[0][i == 2 ? 0x3c00 : 0x9a + i] = inherited[i];
    }
    assert(gpu_hwcg() && gpu_state == GPU_COLD);
    assert(!gx_reads && !gx_writes && !gmu_reads && !gmu_writes && !barriers);
    for (unsigned i = 0; i < 3; i++) {
        assert(gmu_regs[GMU(0x23b09 + i) / 4] == inherited[i]);
        assert(gpu_regs[0][i == 2 ? 0x3c00 : 0x9a + i] == inherited[i]);
    }
    reset(); assert(gpu_hwcg());
    assert(gx_writes == 5 && gx_reads == 1 && gmu_writes == A8_COUNT(a8_ao_hwcg) && !gmu_reads);
    assert(gpu_regs[0][GEN8_RBBM_CGC_0_PC] == 0x702 && gpu_regs[0][0x9a] == 0);
    assert(gpu_regs[0][0x9b] == 1 && gpu_regs[0][GEN8_RBBM_CGC_P2S_TRIG_CMD] == 1);
    for (unsigned i = 0; i < A8_COUNT(a8_ao_hwcg); i++)
        assert(gmu_regs[GMU(a8_ao_hwcg[i].reg) / 4] == a8_ao_hwcg[i].value);
    reset(); clock_ok = false;
    assert(!gpu_hwcg() && gpu_state == GPU_FAILED && gx_reads == 3 && gx_writes == 4);
    reset();
}
'''
main = "int main(int argc, char **argv)\n{"
assert source.count(main) == 1
source = source.replace(main, check + main + "\n    check_hwcg_modes();", 1)
guard = "if (adreno8_direct_ring)\n\t\treturn true;"
hwcg_start = production.index("static bool\ngpu_hwcg(")
hwcg_end = production.index("\n}\n", hwcg_start) + 3
hwcg = production[hwcg_start:hwcg_end]
variants = {"production": production}
assert hwcg.count(guard) == 1
for label, replacement in (
    ("ordinary-writes", "if (false)\n\t\treturn true;"),
    ("hwsched-skipped", "return true;"),
    ("ordinary-fails", "if (adreno8_direct_ring)\n\t\treturn false;"),
):
    variants[label] = production[:hwcg_start] + hwcg.replace(guard, replacement, 1) + production[hwcg_end:]
for label, write in (
    ("mode", "gpu_gmu_write(GMU(0x23b09), 0)"),
    ("delay", "gpu_gmu_write(GMU(0x23b0a), 0)"),
    ("hysteresis", "gpu_gmu_write(GMU(0x23b0b), 0)"),
    ("global", "gpu_write(0x9a, 1)"),
    ("load", "gpu_write(0x9b, 0)"),
    ("GBIF", "gpu_write(0x3c00, 0)"),
):
    changed = hwcg.replace(guard, f"if (adreno8_direct_ring) {{ {write}; return true; }}", 1)
    variants[label] = production[:hwcg_start] + changed + production[hwcg_end:]
with tempfile.TemporaryDirectory(prefix="adreno8-hwcg-") as temporary:
    target = Path(temporary)
    (target / "gpu.c").write_text(source)
    for label, content in variants.items():
        (target / "adreno8_gpu.c").write_text(content)
        for page in (4096, 16384):
            binary = target / f"{label}-{page}"
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", f"-DPAGE_SIZE={page}", "-I", str(DEV),
                            str(target / "gpu.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary), str(args.firmware_dir / "gen80000_sqe.fw"),
                                     str(args.firmware_dir / "gen80000_aqe.fw")], capture_output=True, text=True)
            if label == "production":
                assert result.returncode == 0, result.stdout + result.stderr
                print(f"HWCG inherited ordinary and HWSCHED sequence PAGE_SIZE={page}: PASS")
            else:
                assert result.returncode != 0 and "assert" in result.stderr.lower(), (label, page, result.stderr)
    print(f"HWCG mutations rejected: {len(variants) - 1} variants x 2 page sizes")
