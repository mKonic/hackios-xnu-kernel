#!/usr/bin/env python3
"""Check that both ordinary microcode objects pass validation before OOB."""
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

fixture = runpy.run_path(str(Path(__file__).with_name("test-adreno8-gpu.py")))
dev, args = fixture["DEV"], fixture["args"]
production = (dev / "adreno8_gpu.c").read_text()
check = re.search(r'\tif \(adreno8_direct_ring &&\n\t    \(!adreno8_translation_walk.*?'
                  r'\t\treturn gpu_fail\("microcode object mapping"\);\n', production, re.S)[0]
variants = {"missing-validation": production.replace(check, "", 1)}
for label, before, after in (
    ("ignored-failure", 'return gpu_fail("microcode object mapping");', '(void)0;'),
    ("wrong-protection", "ADRENO8_MEM_READ | ADRENO8_MEM_EXEC", "ADRENO8_MEM_READ | ADRENO8_MEM_WRITE"),
    ("wrong-size", "sqe->memory, sqe->size", "sqe->memory, 4096"),
    ("all-modes", "if (adreno8_direct_ring &&", "if (true &&"),
):
    assert before in check, label
    variants[label] = production.replace(check, check.replace(before, after, 1), 1)
calls = re.findall(r"!adreno8_translation_walk\(.*?\)", check, re.S)
assert len(calls) == 2
for name, call in zip(("SQE", "AQE"), calls):
    variants[f"missing-{name}"] = production.replace(check, check.replace(call, "false", 1), 1)
body = re.search(r"bool\nadreno8_gpu_cp_init\(void\).*?\n}", production, re.S)[0]
for label, marker in (("before-admission", '\tif (adreno8_direct_ring && (adreno8_step'),
                      ("after-OOB", '\tif (adreno8_direct_ring)\n\t\treturn gpu_direct_ring_init')):
    moved = body.replace(check, "", 1)
    at = moved.index(marker)
    moved = moved[:at] + check + moved[at:]
    variants[label] = production.replace(body, moved, 1)
with tempfile.TemporaryDirectory(prefix="adreno8-ucode-mapping-") as directory:
    target = Path(directory)
    (target / "check.c").write_text(fixture["source"])
    for label, content in [("production", production)] + list(variants.items()):
        if label != "production": assert content != production, label
        (target / "adreno8_gpu.c").write_text(content)
        for page in (4096, 16384):
            binary = target / f"ucode-{label}-{page}"
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                            f"-DPAGE_SIZE={page}", "-I", str(dev), str(target / "check.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary), str(args.firmware_dir / "gen80000_sqe.fw"),
                                     str(args.firmware_dir / "gen80000_aqe.fw")], capture_output=True, text=True)
            if label == "production":
                assert result.returncode == 0, result.stdout + result.stderr
                print(f"Microcode validation PAGE_SIZE={page}: PASS")
            else:
                assert result.returncode != 0 and "assert" in result.stderr.lower(), (label, page, result.stderr)
print(f"Microcode validation mutations rejected: {len(variants)} variants x 2 page sizes")
