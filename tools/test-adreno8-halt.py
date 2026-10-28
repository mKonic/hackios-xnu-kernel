#!/usr/bin/env python3
"""Exercise ordinary HALT release failures with the production GPU C fixture."""
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

# The shared fixture validates vendor inputs and supplies the same CLI arguments.
fixture = runpy.run_path(str(Path(__file__).with_name("test-adreno8-gpu.py")))
dev, args = fixture["DEV"], fixture["args"]
cases = r'''
static void check_halt_release(void)
{
    for(unsigned target=0;target<2;target++) for(unsigned fault=0;fault<2;fault++) {
        prepare_ring();
        unsigned reg=target?0x00a:0x3c45;
        if(fault) ring_read_fault=reg; else ring_halt_drop=reg;
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(ring_halt_clears==(target?3u:1u));
        assert(gx_writes==target+2 && gmu_writes==2);
        assert(!ring_input_mask && !ring_sqe_writes && !ring_wptr_writes && !ring_payloads);
        assert(gpu_regs[0][0x3c46]==3 && gpu_regs[0][0x00b]==1);
        if(!target) assert(gpu_regs[0][0x00a]==1);
    }
    reset(); prepare();
    gpu_regs[0][0x3c45]=8; gpu_regs[0][0x00a]=1;
    gpu_regs[0][0x3c46]=3; gpu_regs[0][0x00b]=1;
    assert(adreno8_gpu_cp_init() && adreno8_gpu_cp_ready());
    assert(gpu_regs[0][0x3c45]==8 && gpu_regs[0][0x00a]==1);
    assert(gpu_regs[0][0x3c46]==3 && gpu_regs[0][0x00b]==1);
    puts("ordinary HALT release: ordered zero/readback, nonzero ACK retention, failures stop writes, HWSCHED unchanged passed");
}
'''
source = fixture["source"].replace("int main(int argc, char **argv)", cases + "\nint main(int argc, char **argv)", 1)
source = source.replace("    check_ring_setup_only();", "    check_ring_setup_only();\n    check_halt_release();", 1)
production = (dev / "adreno8_gpu.c").read_text()
config = re.search(r"static bool\ngpu_register_config\(.*?\n}", production, re.S)[0]
mutations = [
    ("all-modes", "if (adreno8_direct_ring)", "if (true)"),
    ("wrong-order", "{ GEN8_GBIF_HALT, GEN8_RBBM_GBIF_HALT }", "{ GEN8_RBBM_GBIF_HALT, GEN8_GBIF_HALT }"),
    ("missing-write", "gpu_write(release[i], 0);", ""),
    ("missing-barrier", "gpu_write(release[i], 0);\n\t\t\tadreno8_settle();", "gpu_write(release[i], 0);"),
    ("missing-readback", "if (gpu_read(release[i]) != 0)", "if (false)"),
    ("only-all1-rejected", "if (gpu_read(release[i]) != 0)", "if (gpu_read(release[i]) == 0xffffffffu)"),
]
with tempfile.TemporaryDirectory(prefix="adreno8-halt-test-") as temp:
    for label, content in [("production", production)] + [
            (name, production.replace(config, config.replace(before, after, 1), 1))
            for name, before, after in mutations]:
        if label != "production":
            assert content != production, label
        path = Path(temp) / label
        path.mkdir()
        (path / "gpu.c").write_text(source)
        (path / "adreno8_gpu.c").write_text(content)
        for page in (4096, 16384):
            binary = path / f"halt-{page}"
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                            f"-DPAGE_SIZE={page}", "-I", str(dev), str(path / "gpu.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary), str(args.firmware_dir / "gen80000_sqe.fw"),
                                     str(args.firmware_dir / "gen80000_aqe.fw")], capture_output=True, text=True)
            if label == "production":
                assert result.returncode == 0, result.stdout + result.stderr
                print(result.stdout, end="")
            else:
                assert result.returncode != 0 and "assert" in result.stderr.lower(), (label, page, result.stderr)
print(f"Ordinary HALT release mutations rejected: {len(mutations)} variants x 2 page sizes")
