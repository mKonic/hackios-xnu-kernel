#!/usr/bin/env python3
"""Execute ordinary SQE global-fault sampling in the production GPU C path."""
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

fixture = runpy.run_path(str(Path(__file__).with_name("test-adreno8-gpu.py")))
dev, args = fixture["DEV"], fixture["args"]
production = (dev / "adreno8_gpu.c").read_text()
source = fixture["source"].split("int main(int argc, char **argv)", 1)[0]


def replace_once(code, before, after):
    assert code.count(before) == 1, before
    return code.replace(before, after, 1)


source = replace_once(source, "void loader_log_line(const char *line) { (void)line; }",
                      "void loader_log_line(const char *line);")
source = replace_once(source, "static unsigned gx_reads, gx_writes, raw_calls, allocation_count, publish_count;", r'''
static unsigned gx_reads, gx_writes, raw_calls, allocation_count, publish_count;
static unsigned gf_reads, gf_lines, gf_sqe_reads, gf_write_barrier, gf_poll_calls, gf_console_calls;
static unsigned gf_saved_reads, gf_saved_writes, gf_saved_poll, gf_saved_console, gf_saved_barriers;
static bool gf_dsb_marker;
static uint32_t gf_values[3][6];
''')
source = replace_once(source, "int adreno8_say(const char *fmt, ...) {", "static unsigned gf_console_calls;\nint adreno8_say(const char *fmt, ...) {\n    gf_console_calls++;")
source = replace_once(source, "bool adreno8_hfi_poll(void) {", "bool adreno8_hfi_poll(void) {\n    gf_poll_calls++;")
source = replace_once(source, "void adreno8_sleep_us(uint32_t us) {", "static unsigned gf_delays;\nvoid adreno8_sleep_us(uint32_t us) { if (us == 100) gf_delays++;")
source = replace_once(source, "    if (address >= adreno8_smmu.base && address < adreno8_smmu.base + adreno8_smmu.size) return 0;", r'''
    if (address >= adreno8_smmu.base && address < adreno8_smmu.base + adreno8_smmu.size) {
        const uint32_t offsets[] = {0x48,0x40,0x44,0x50,0x54,0x48};
        unsigned phase=gf_reads/6, position=gf_reads%6;
        assert(phase<3 && gf_lines==phase && adreno8_direct_ring && !raw_nop_asked);
        assert(address-adreno8_smmu.base==offsets[position]);
        assert(ring_input_mask==7 && ring_setup_read_mask==127);
        if (phase<2) assert(!ring_payloads && !ring_wptr_writes);
        if (!phase) assert(!ring_sqe_writes && !gf_dsb_marker);
        else {
            assert(ring_sqe_writes==1 && gf_dsb_marker && barriers>gf_write_barrier);
            assert(gf_sqe_reads==(phase==2));
            if (phase==2) assert(ring_payloads==1 && ring_wptr_writes==1 && gf_delays==2000);
        }
        if (!position) {
            gf_saved_reads=gx_reads+gmu_reads; gf_saved_writes=gx_writes+gmu_writes;
            gf_saved_poll=gf_poll_calls; gf_saved_console=gf_console_calls; gf_saved_barriers=barriers;
        }
        assert(gf_saved_reads==gx_reads+gmu_reads && gf_saved_writes==gx_writes+gmu_writes);
        assert(gf_saved_poll==gf_poll_calls && gf_saved_console==gf_console_calls && gf_saved_barriers==barriers);
        gf_reads++;
        return gf_values[phase][position];
    }
''')
read = re.search(r"uint32_t adreno8_rd\(vm_offset_t address\)\n\{.*?\n}", source, re.S)[0]
source = replace_once(source, read, replace_once(read, "    if (reg == GEN8_CP_APERTURE_CNTL_HOST)", r'''
    if (adreno8_direct_ring && !raw_nop_asked && reg==0x815) {
        assert(gf_lines==2 && gf_reads==12 && gf_dsb_marker);
        gf_sqe_reads++;
    }
    if (reg == GEN8_CP_APERTURE_CNTL_HOST)'''))
write = re.search(r"void adreno8_wr\(vm_offset_t address, uint32_t value\)\n\{.*?\n}", source, re.S)[0]
source = replace_once(source, write, replace_once(write, "    if (adreno8_direct_ring) {", r'''
    if (adreno8_direct_ring && !raw_nop_asked && reg==0x815) {
        assert(gf_lines==1 && gf_reads==6 && !gf_dsb_marker && !gf_sqe_reads);
        gf_write_barrier=barriers;
    }
    if (adreno8_direct_ring) {'''))
source += r'''
void loader_log_line(const char *line) {
    if (!strncmp(line,"adreno8: ordinary SQE global fault ",34)) {
        assert(gf_lines<3 && gf_reads==6*(gf_lines+1));
        assert(gf_saved_reads==gx_reads+gmu_reads && gf_saved_writes==gx_writes+gmu_writes);
        assert(gf_saved_poll==gf_poll_calls && gf_saved_console==gf_console_calls && gf_saved_barriers==barriers);
        char expected[192];
        const uint32_t *v=gf_values[gf_lines];
        snprintf(expected,sizeof(expected),"adreno8: ordinary SQE global fault %s: fsr=%#x/%#x far=%#x:%08x synr=%#x/%#x\n",
                 gf_lines==2?"completion timeout":gf_lines?"after DSB":"before store",v[0],v[5],v[2],v[1],v[3],v[4]);
        assert(!strcmp(line,expected)); gf_lines++;
    } else if (!strcmp(line,"adreno8: ordinary SQE start DSB returned\n")) {
        assert(gf_lines==1 && gf_reads==6 && ring_sqe_writes==1 && barriers>gf_write_barrier);
        assert(last_mmio==MMIO_BARRIER && !gf_dsb_marker); gf_dsb_marker=true;
    } else assert(!strcmp(line,"adreno8: ordinary SQE zero-value control DSB returned\n"));
}
static void fresh_global(unsigned mode) {
    prepare_ring(); raw_nop_asked=mode;
    gf_reads=gf_lines=gf_sqe_reads=gf_write_barrier=gf_poll_calls=gf_console_calls=0;
    gf_saved_reads=gf_saved_writes=gf_saved_poll=gf_saved_console=gf_saved_barriers=0;
    gf_dsb_marker=false; gf_delays=0;
    for (unsigned phase=0;phase<3;phase++) {
        gf_values[phase][0]=gf_values[phase][5]=0;
        for (unsigned i=1;i<5;i++) gf_values[phase][i]=0xffffffffu;
    }
    if(mode==2) gpu_regs[0][0x815]=0;
}
int main(int argc, char **argv) {
    assert(argc==3);
    for(unsigned i=0;i<2;i++) {
        FILE *f=fopen(argv[i+1],"rb"); assert(f);
        assert(!fseek(f,0,SEEK_END)); file_sizes[i]=ftell(f); rewind(f);
        files[i]=malloc(file_sizes[i]); assert(files[i]);
        assert(fread(files[i],1,file_sizes[i],f)==file_sizes[i]); fclose(f);
    }
    /* With no fault, FAR/SYNR may contain unknown values, including all ones. */
    for(unsigned unknown=0;unknown<3;unknown++) {
        fresh_global(0);
        if(unknown!=0) for(unsigned phase=0;phase<2;phase++) for(unsigned i=1;i<5;i++)
            gf_values[phase][i]=unknown==1?0:0x12340000u+phase*0x100+i;
        assert(!adreno8_gpu_cp_init()); ring_retained(1);
        assert(gf_reads==12 && gf_lines==2 && gf_sqe_reads==1 && ring_sqe_writes==1);
        assert(ring_payloads==1 && ring_wptr_writes==1);
    }
    const uint32_t faults[]={1,2,4,8,0x80000000u,0xffffffffu};
    for(unsigned phase=0;phase<2;phase++) for(unsigned end=0;end<=5;end+=5)
        for(unsigned i=0;i<sizeof(faults)/sizeof(*faults);i++) {
            fresh_global(0); gf_values[phase][end]=faults[i];
            assert(!adreno8_gpu_cp_init()); ring_retained(0);
            assert(gf_reads==6*(phase+1) && gf_lines==phase+1);
            assert(ring_sqe_writes==phase && !gf_sqe_reads && !ring_payloads && !ring_wptr_writes);
            assert(!setup_waits);
        }
    /* Preserve the two startup samples, then sample once after all completion polls. */
    for(unsigned unknown=0;unknown<3;unknown++) {
        fresh_global(0); ring_rptr_moves=false;
        for(unsigned i=1;i<5;i++) gf_values[2][i]=unknown==0?0:unknown==1?~0u:0x56780000u+i;
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(gf_reads==18 && gf_lines==3 && gf_sqe_reads==1 && gf_delays==2000);
        assert(ring_sqe_writes==1 && ring_payloads==1 && ring_wptr_writes==1);
    }
    for(unsigned end=0;end<=5;end+=5) for(unsigned i=0;i<sizeof(faults)/sizeof(*faults);i++) {
        fresh_global(0); ring_rptr_moves=false; gf_values[2][end]=faults[i];
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(gf_reads==18 && gf_lines==3 && gf_sqe_reads==1 && gf_delays==2000);
        assert(ring_sqe_writes==1 && ring_payloads==1 && ring_wptr_writes==1);
    }
    for(unsigned bad=0;bad<2;bad++) {
        fresh_global(0);
        vm_offset_t base=adreno8_smmu.base; uint32_t size=adreno8_smmu.size;
        if(bad) adreno8_smmu.size=0x57; else adreno8_smmu.base=0;
        unsigned reads=gx_reads+gmu_reads, writes=gx_writes+gmu_writes;
        assert(!gpu_global_fault_sample("invalid mapping"));
        assert(reads==gx_reads+gmu_reads && writes==gx_writes+gmu_writes);
        assert(!gf_reads && !gf_lines && !gf_poll_calls && !gf_console_calls);
        assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(!gf_reads && !gf_lines && !ring_sqe_writes && !ring_payloads && !ring_wptr_writes);
        adreno8_smmu.base=base; adreno8_smmu.size=size;
    }
    for(unsigned mode=2;mode<=3;mode++) {
        fresh_global(mode); assert(!adreno8_gpu_cp_init()); ring_retained(0);
        assert(!gf_reads && !gf_lines && setup_waits==100 && ring_sqe_writes==(mode==2));
    }
    for(unsigned phase=1;phase<=3;phase++) {
        fresh_global(0); ring_publish_fail=phase;
        assert(!adreno8_gpu_cp_init()); ring_retained(0); assert(!gf_reads && !gf_lines);
    }
    for(unsigned i=0;i<7;i++) {
        fresh_global(0); ring_read_fault=ring_setup_regs[i];
        assert(!adreno8_gpu_cp_init()); ring_retained(0); assert(!gf_reads && !gf_lines);
    }
    for(unsigned mode=0;mode<4;mode++) {
        reset(); prepare(); raw_nop_asked=mode; gf_reads=gf_lines=0;
        (void)adreno8_gpu_cp_init(); assert(!gf_reads && !gf_lines);
    }
    reset(); free(files[0]); free(files[1]);
    puts("SQE global fault: six consecutive raw reads, RAM-only log, first/last fault, store/DSB ordering and scope passed");
}
'''
source = replace_once(source, '    assert(argc==3);', '    assert(argc==3);\n' + fixture['table_checks'])
pre = 'if (!gpu_global_fault_sample("before store"))\n\t\treturn gpu_fail("global fault before ordinary SQE store");'
post = 'if (!gpu_global_fault_sample("after DSB"))\n\t\treturn gpu_fail("global fault after ordinary SQE DSB");'
marker = 'loader_log_line("adreno8: ordinary SQE start DSB returned\\n");'
mutations = [
    ("no-timeout-sample", '\t(void)gpu_global_fault_sample("completion timeout");\n', ""),
    ("wrong-timeout-phase", 'gpu_global_fault_sample("completion timeout")', 'gpu_global_fault_sample("after DSB")'),
    ("early-timeout-sample", '\tfor (unsigned i = 0; i < 2000; i++) {\n\t\tif (!adreno8_hfi_poll() || !gpu_status_ok())',
     '\t(void)gpu_global_fault_sample("completion timeout");\n\tfor (unsigned i = 0; i < 2000; i++) {\n\t\tif (!adreno8_hfi_poll() || !gpu_status_ok())'),
    ("no-pre-sample", pre, ""),
    ("no-post-sample", post, ""),
    ("ignore-pre-fault", pre, '(void)gpu_global_fault_sample("before store");'),
    ("ignore-post-fault", post, '(void)gpu_global_fault_sample("after DSB");'),
    ("ignore-first-fsr", "return first == 0 && last == 0;", "return last == 0;"),
    ("ignore-last-fsr", "return first == 0 && last == 0;", "return first == 0;"),
    ("all1-first-accepted", "return first == 0 && last == 0;", "return (first == 0 || first == ~0u) && last == 0;"),
    ("all1-last-accepted", "return first == 0 && last == 0;", "return first == 0 && (last == 0 || last == ~0u);"),
    ("wrong-far-low", "adreno8_smmu.base + 0x40", "adreno8_smmu.base + 0x60"),
    ("wrong-far-high", "adreno8_smmu.base + 0x44", "adreno8_smmu.base + 0x64"),
    ("wrong-synr0", "adreno8_smmu.base + 0x50", "adreno8_smmu.base + 0x58"),
    ("wrong-synr1", "adreno8_smmu.base + 0x54", "adreno8_smmu.base + 0x58"),
    ("no-last-read", "uint32_t last = adreno8_rd(adreno8_smmu.base + SMMU_SGFSR);", "uint32_t last = first;"),
    ("no-ram-log", "loader_log_line(line);", "(void)line;"),
    ("console-log", "loader_log_line(line);", "adreno8_say(line);"),
    ("sample-hfi-poll", "uint32_t low = ", "(void)adreno8_hfi_poll();\n\tuint32_t low = "),
    ("sample-fsr-clear", "uint32_t low = ", "adreno8_wr(adreno8_smmu.base + SMMU_SGFSR, ~0u);\n\tuint32_t low = "),
    ("sample-diagnostic-read", "adreno8_rd(adreno8_smmu.base + 0x40)", 'adreno8_get(&adreno8_smmu, "sample", 0x40)'),
    ("sample-extra-barrier", "uint32_t low = ", "adreno8_settle();\n\tuint32_t low = "),
    ("reject-unknown-far", "return first == 0 && last == 0;", "return first == 0 && last == 0 && low == 0;"),
    ("reject-unknown-synr", "return first == 0 && last == 0;", "return first == 0 && last == 0 && syn0 == 0;"),
    ("missing-base-guard", "!adreno8_smmu.base || ", ""),
    ("missing-size-guard", "adreno8_smmu.size < 0x58", "adreno8_smmu.size < 0x54"),
    ("pre-after-store", '\t'+pre+'\n\tgpu_write(GEN8_CP_SQE_CNTL, 1);', '\tgpu_write(GEN8_CP_SQE_CNTL, 1);\n\t'+pre),
    ("post-before-dsb", '\tadreno8_settle();\n\t'+marker+'\n\t'+post,
     '\t'+post+'\n\tadreno8_settle();\n\t'+marker),
    ("post-after-sqe-read", post+'\n\tif (gpu_read(GEN8_CP_SQE_CNTL) != 1)\n\t\treturn gpu_fail("direct ring SQE enable readback");',
     'if (gpu_read(GEN8_CP_SQE_CNTL) != 1)\n\t\treturn gpu_fail("direct ring SQE enable readback");\n\t'+post),
]
with tempfile.TemporaryDirectory(prefix="adreno8-global-fault-") as temp:
    for label, before, after in [("production", None, None)]+mutations:
        gpu = production if before is None else replace_once(production, before, after)
        path=Path(temp)/label; path.mkdir()
        (path/"check.c").write_text(source); (path/"adreno8_gpu.c").write_text(gpu)
        for page in (4096,16384):
            binary=path/f"global-{page}"
            subprocess.run(["clang","-std=c11","-Wall","-Wextra","-Werror","-Wno-unused-function",
                            "-fsanitize=address,undefined",f"-DPAGE_SIZE={page}","-I",str(dev),
                            str(path/"check.c"),"-o",str(binary)],check=True)
            result=subprocess.run([str(binary),str(args.firmware_dir/"gen80000_sqe.fw"),
                                   str(args.firmware_dir/"gen80000_aqe.fw")],capture_output=True,text=True)
            if label=="production":
                assert result.returncode==0,result.stdout+result.stderr
                print(f"SQE global fault PAGE_SIZE={page}: PASS")
            else:
                assert result.returncode!=0 and "assert" in result.stderr.lower(),(label,page,result.stderr)
print(f"SQE global-fault mutations rejected: {len(mutations)} variants x 2 page sizes")
