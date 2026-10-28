#!/usr/bin/env python3
"""Exercise ordinary SQE zero-value control through the production GPU C fixture."""
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

fixture = runpy.run_path(str(Path(__file__).with_name("test-adreno8-gpu.py")))
dev, args = fixture["DEV"], fixture["args"]
source = fixture["source"].replace("static unsigned ring_completed, ring_setup_completed;", r'''
static unsigned ring_completed, ring_setup_completed;
static bool sqe_watch;
static unsigned sqe_reads, sqe_writes, sqe_write_barrier, sqe_fault_read;
static uint32_t sqe_read_answer;
''', 1)
source = source.replace("    assert(ring_input_mask==7 && ring_setup_read_mask==127);", r'''
    assert(ring_input_mask==7 && ring_setup_read_mask==127);
    if (sqe_watch) {
        assert(sqe_reads==2 && sqe_writes==1 && gpu_regs[0][0x815]==0);
        assert(!ring_wptr_writes && !ring_payloads && !raw_calls);
        for (unsigned b=2;b<5;b++) for (unsigned i=0;i<allocations[b].size;i++)
            assert(!device_memory[b][i] && !((uint8_t *)allocations[b].memory)[i]);
    }
''', 1)
read = re.search(r"uint32_t adreno8_rd\(vm_offset_t address\)\n\{.*?\n}", source, re.S)[0]
read_spy = r'''
    if (sqe_watch && reg==0x815) {
        assert(ring_input_mask==7 && ring_setup_read_mask==127 && current_pipe==0);
        assert(gpu_regs[0][0x816]==sqe->gpu_address && !gpu_regs[0][0x817]);
        assert(gpu_regs[0][0x818]==aqe->gpu_address && !gpu_regs[0][0x819]);
        sqe_reads++;
        if (sqe_reads==1) assert(!sqe_writes);
        else assert(sqe_reads==2 && sqe_writes==1 && barriers>sqe_write_barrier);
        if (sqe_reads==sqe_fault_read) return sqe_read_answer;
    }
'''
at = "    if (reg == GEN8_CP_APERTURE_CNTL_HOST)"
assert at in read
source = source.replace(read, read.replace(at, read_spy + at, 1), 1)
write = re.search(r"void adreno8_wr\(vm_offset_t address, uint32_t value\)\n\{.*?\n}", source, re.S)[0]
write_spy = r'''
    if (sqe_watch && reg==0x815) {
        assert(raw_nop_asked==2 && sqe_reads==1 && !sqe_writes && value==0);
        assert(ring_input_mask==7 && ring_setup_read_mask==127 && current_pipe==0);
        assert(oob_requested && oob_acked && adreno8_reset_required && !raw_calls);
        assert(!setup_waits && !ring_wptr_writes && !ring_payloads);
        sqe_writes++; sqe_write_barrier=barriers;
    }
'''
at = "    if (adreno8_direct_ring) {"
assert at in write
source = source.replace(write, write.replace(at, write_spy + at, 1), 1)
source = source.replace("    setup_waits = 0;", r'''
    setup_waits = 0;
    sqe_watch=false; sqe_reads=sqe_writes=sqe_write_barrier=sqe_fault_read=0;
    sqe_read_answer=0;
''', 1)
cases = r'''
static void prepare_sqe_control(uint32_t inherited)
{
    prepare_ring(); raw_nop_asked=2; sqe_watch=true;
    gpu_regs[0][0x815]=inherited;
}
static void check_sqe_control(void)
{
    prepare_sqe_control(0);
    assert(!adreno8_gpu_cp_init()); ring_retained(0);
    assert(sqe_reads==2 && sqe_writes==1 && ring_sqe_writes==1);
    assert(setup_waits==100 && ring_setup_completed==1 && ring_halt_reads==4);
    assert(!ring_wptr_writes && !ring_payloads && gpu_regs[0][0x815]==0);
    const uint32_t inherited[]={1,2,3,0xa5,0x80000000,0xffffffff};
    for (unsigned i=0;i<sizeof(inherited)/sizeof(*inherited);i++) {
        prepare_sqe_control(inherited[i]);
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(sqe_reads==1 && !sqe_writes && !ring_sqe_writes && !setup_waits);
    }
    const uint32_t bad[]={1,2,3,7,0x80000000,0xffffffff};
    for (unsigned i=0;i<sizeof(bad)/sizeof(*bad);i++) {
        prepare_sqe_control(0); sqe_fault_read=2; sqe_read_answer=bad[i];
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(sqe_reads==2 && sqe_writes==1 && !setup_waits && !ring_halt_reads);
    }
    /* An ignored zero write has the same readback as a delivered zero write. */
    prepare_sqe_control(0); ring_drop_write=0x815;
    assert(!adreno8_gpu_cp_init()); ring_retained(0);
    assert(sqe_reads==2 && sqe_writes==1 && setup_waits==100 && ring_setup_completed==1);
    assert(gpu_regs[0][0x815]==0 && !ring_wptr_writes && !ring_payloads);
    for (unsigned fault=1;fault<=4;fault++) for (unsigned at=1;at<=100;at+=99) {
        prepare_sqe_control(0); ring_setup_fault=fault; ring_setup_fault_at=at;
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(sqe_reads==2 && sqe_writes==1 && setup_waits==at && !ring_setup_completed);
        assert(!ring_wptr_writes && !ring_payloads);
    }
    for (unsigned phase=1;phase<=3;phase++) {
        prepare_sqe_control(0); ring_publish_fail=phase;
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(!sqe_reads && !sqe_writes && !setup_waits);
    }
    for (unsigned i=0;i<7;i++) {
        prepare_sqe_control(0); ring_read_fault=ring_setup_regs[i];
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(!sqe_reads && !sqe_writes && !setup_waits);
    }
    for (unsigned bad=0;bad<8;bad++) {
        prepare_sqe_control(0);
        switch (bad) {
        case 0: raw_nop_asked=1; break;
        case 1: raw_nop_asked=4; break;
        case 2: adreno8_step=16; break;
        case 3: adreno8_newest_step=18; break;
        case 4: adreno8_gpu8_keep=false; break;
        case 5: adreno8_gpu8_return=true; break;
        case 6: adreno8_skip_steps=1; break;
        case 7: empty_powerup_asked=1; break;
        }
        assert(!adreno8_gpu_cp_init() && gpu_state==GPU_FAILED);
        assert(!oob_requested && !gx_reads && !gx_writes && !gmu_reads && !gmu_writes);
        assert(!sqe_reads && !sqe_writes && !setup_waits);
    }
    reset(); prepare(); raw_nop_asked=2;
    assert(!adreno8_gpu_cp_init() && gpu_state==GPU_FAILED && raw_calls==1);
    assert(!ring_sqe_writes && !sqe_reads && !sqe_writes && !adreno8_gpu_cp_ready());
    puts("ordinary SQE zero-value control: inherited zero required, zero write/readback, ignored zero write remains indistinguishable, health, retention and HWSCHED NOP2 passed");
}
'''
source = source.replace("int main(int argc, char **argv)", cases + "\nint main(int argc, char **argv)", 1)
source = source.replace("    check_ring_setup_only();", "    check_ring_setup_only();\n    check_sqe_control();", 1)
production = (dev / "adreno8_gpu.c").read_text()
control = re.search(r"\t\tif \(raw_mode == 2\) \{.*?\n\t\t}", production, re.S)[0]
variants = {}
for label, before, after in (
    ("old-admission", "(raw_nop != 0 && raw_nop != 2 && raw_nop != 3)", "(raw_nop != 0 && raw_nop != 3)"),
    ("lost-mode", "gpu_direct_ring_init(raw_nop)", "gpu_direct_ring_init(raw_nop == 3)"),
    ("missing-control", control, ""),
    ("missing-inherited-read", "uint32_t inherited = gpu_read(GEN8_CP_SQE_CNTL);", "uint32_t inherited = 0;"),
    ("unchecked-inherited", "if (inherited != 0)", "if (false)"),
    ("only-all1-inherited-rejected", "if (inherited != 0)", "if (inherited == 0xffffffffu)"),
    ("missing-write", "\t\t\tgpu_write(GEN8_CP_SQE_CNTL, 0);", ""),
    ("wrong-write", "gpu_write(GEN8_CP_SQE_CNTL, 0)", "gpu_write(GEN8_CP_SQE_CNTL, 1)"),
    ("missing-barrier", "\t\t\tadreno8_settle();", ""),
    ("unchecked-readback", "if (gpu_read(GEN8_CP_SQE_CNTL) != 0)", "if (false)"),
    ("partial-readback", "if (gpu_read(GEN8_CP_SQE_CNTL) != 0)", "if ((gpu_read(GEN8_CP_SQE_CNTL) & 3) != 0)"),
):
    owner = control if before in control and label != "missing-control" else production
    assert before in owner, label
    variants[label] = production.replace(owner, owner.replace(before, after, 1), 1)
ring_body = re.search(r"static bool\ngpu_direct_ring_init\(.*?\n}", production, re.S)[0]
for label, marker in (("before-publication", "\tmemset((void *)ring->memory"),
                      ("before-register-setup", "\tfor (unsigned i = 0; i < A8_COUNT(setup); i++)")):
    moved = ring_body.replace(control + "\n", "", 1)
    at = moved.index(marker)
    moved_control = "\n".join(line[1:] if line.startswith("\t") else line for line in control.splitlines())
    moved = moved[:at] + moved_control + "\n" + moved[at:]
    variants[label] = production.replace(ring_body, moved, 1)
with tempfile.TemporaryDirectory(prefix="adreno8-sqe-control-") as directory:
    target = Path(directory)
    (target / "check.c").write_text(source)
    for label, content in [("production", production)] + list(variants.items()):
        if label != "production": assert content != production, label
        (target / "adreno8_gpu.c").write_text(content)
        for page in (4096, 16384):
            binary = target / f"sqe-control-{label}-{page}"
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                            f"-DPAGE_SIZE={page}", "-I", str(dev), str(target / "check.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary), str(args.firmware_dir / "gen80000_sqe.fw"),
                                     str(args.firmware_dir / "gen80000_aqe.fw")], capture_output=True, text=True)
            if label == "production":
                assert result.returncode == 0, result.stdout + result.stderr
                print(f"SQE zero-value control PAGE_SIZE={page}: PASS")
            else:
                assert result.returncode != 0 and "assert" in result.stderr.lower(), (label, page, result.stderr)
print(f"Ordinary SQE zero-value control mutations rejected: {len(variants)} variants x 2 page sizes")
