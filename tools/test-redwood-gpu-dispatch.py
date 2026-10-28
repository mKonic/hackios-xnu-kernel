#!/usr/bin/env python3
"""Exercise fixed-platform C ABI, service publication and A830 submission admission."""
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
IOKIT = ROOT / "xnu/files/iokit/Kernel"

STUB = r'''
#ifndef HOST_ADRENO8_PRIVATE
#define HOST_ADRENO8_PRIVATE
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uintptr_t vm_offset_t;
static bool annibale, adreno8_gpu8_keep, adreno8_gpu8_return, bo_open, self_test, publish_ok;
static uint32_t adreno8_gx_hz[17] = {0, 160000000, 222000000, 342000000}, gpu_va, gpu_log;
static unsigned adreno8_gx_count = 4;
static unsigned legacy_calls, enable_calls, close_calls, stop_calls, cache_calls, submit_calls, publish_calls, ready_checks, speed_ticks;
static bool redwood_platform_is_annibale(void) { return annibale; }
static int adreno8_say(const char *format, ...) { (void)format; return 0; }
static bool PE_parse_boot_argn(const char *key, void *value, unsigned size) {
    assert(!strcmp(key, "gpu_log") && size == 4); *(uint32_t *)value = 1; return true;
}
void adreno8_speed_tick(void);
void adreno8_speed_state(uint32_t *mhz, uint32_t *changes);
static void adreno8_to_memory(vm_offset_t address, uint64_t size) {
    assert((address == 0x1000 && size == 4096) || (!address && !size)); cache_calls++;
}
#endif
'''

BACKENDS = r'''
bool adreno8_bo_enable(void) { enable_calls++; bo_open = self_test; return bo_open; }
bool adreno8_bo_ready(void) { ready_checks++; return bo_open; }
void adreno8_bo_stop(void) { stop_calls++; bo_open = false; }
uint64_t adreno8_bo_reserve(uint64_t size) { assert(size == 4096); return bo_open ? 0x10001000 : 0; }
bool adreno8_bo_map(uint64_t address, uint64_t pa, bool ro) {
    assert(address == 0x10001000 && pa == 0x50000000 && ro); return bo_open;
}
bool adreno8_bo_commit(void) { return bo_open; }
bool adreno8_bo_unmap(uint64_t address, uint64_t size) { assert(address == 0x10001000 && size == 4096); return bo_open; }
bool adreno8_bo_release(uint64_t address, uint64_t size) { assert(address == 0x10001000 && size == 4096); return bo_open; }
uint32_t adreno8_context_submit(const uint64_t *addresses, const uint32_t *words, uint32_t count) {
    assert(bo_open && count == 1 && *addresses == 0x10001000 && *words == 6); submit_calls++; return 17;
}
bool adreno8_context_done(uint32_t fence) { assert(bo_open && fence == 17); return true; }
uint32_t adreno8_context_last(void) { return 17; }
bool adreno8_context_close(void) { assert(bo_open); close_calls++; return true; }
bool adreno8_gpu_faulted(void) { return !bo_open; }
void adreno8_speed_tick(void) { assert(bo_open); speed_ticks++; }
void adreno8_speed_state(uint32_t *mhz, uint32_t *changes) { *mhz = 342; *changes = 2; }
void adreno8_hfi_worked(uint64_t *u, uint32_t *c, uint32_t by[7]) { *u = *c = 3; for (unsigned i=0;i<7;i++) by[i]=3; }
bool RedwoodGPUStart(void) { assert(bo_open); publish_calls++; return publish_ok; }
bool legacy_gpu_ready(void) { legacy_calls++; return true; }
bool legacy_gpu_parameter(uint32_t p, uint64_t *v) { assert(p == 1); legacy_calls++; *v = 660; return true; }
uint64_t legacy_gpu_address(uint64_t size) { assert(size == 4096); legacy_calls++; return 0x20000000; }
void legacy_gpu_address_free(uint64_t a, uint64_t s) { (void)a; (void)s; legacy_calls++; }
void legacy_gpu_space(uint64_t *u, uint32_t *l, uint64_t *g, unsigned *h, unsigned *m, uint64_t *x, uint32_t *r) {
    legacy_calls++; *u = *g = *x = 7; *l = *h = *m = *r = 7;
}
bool legacy_gpu_map(uint64_t a, uint64_t p, bool r) { (void)a; (void)p; (void)r; legacy_calls++; return true; }
bool legacy_gpu_mapped(void) { legacy_calls++; return true; }
bool legacy_gpu_unmap(uint64_t a, uint64_t s) { (void)a; (void)s; legacy_calls++; return true; }
void legacy_gpu_to_memory(void *p, uint64_t s) { (void)p; (void)s; legacy_calls++; }
uint32_t legacy_gpu_submit(const uint64_t *a, const uint32_t *w, uint32_t n) { (void)a; (void)w; (void)n; legacy_calls++; return 17; }
bool legacy_gpu_done(uint32_t f) { assert(f == 17); legacy_calls++; return true; }
uint32_t legacy_gpu_last(void) { legacy_calls++; return 17; }
void legacy_gpu_worked(uint64_t *u, uint32_t *s, uint32_t by[7]) { legacy_calls++; *u = *s = 7; for (unsigned i=0;i<7;i++) by[i]=7; }
void legacy_gpu_report(const char *why) { assert(why); legacy_calls++; }
bool legacy_gpu_recover(void) { legacy_calls++; return true; }
void legacy_gpu_stop(void) { legacy_calls++; }
'''

DISPATCH_CASES = r'''
static void exercise(bool platform) {
    annibale = platform; bo_open = true; legacy_calls = close_calls = stop_calls = cache_calls = submit_calls = 0;
    uint64_t value, used=9, largest=9, lost=9, address=0x10001000;
    uint32_t live=9, refused=9, words=6, by[7], timed; unsigned holes=9, most=9;
    assert(redwood_gpu_ready() && redwood_gpu_parameter(1, &value) && value == (platform ? 830u : 660u));
    assert(redwood_gpu_address(4096) == (platform ? 0x10001000u : 0x20000000u));
    redwood_gpu_address_free(address, 4096);
    redwood_gpu_space(&used, &live, &largest, &holes, &most, &lost, &refused);
    assert(used == (platform ? 0u : 7u) && live == used && largest == used && holes == used && most == used && lost == used && refused == used);
    assert(redwood_gpu_map(address, 0x50000000, true) && redwood_gpu_mapped() && redwood_gpu_unmap(address, 4096));
    redwood_gpu_to_memory((void *)0x1000, 4096);
    speed_ticks = 0;
    assert(redwood_gpu_submit(&address, &words, 1) == 17 && redwood_gpu_done(17) && redwood_gpu_last() == 17);
    assert(speed_ticks == (platform ? 1u : 0u));
    { uint32_t mhz = 9, changes = 9; redwood_gpu_speed(&mhz, &changes);
      assert(mhz == (platform ? 342u : 0u) && changes == (platform ? 2u : 0u)); }
    redwood_gpu_worked(&value, &timed, by);
    assert(value == (platform ? 3u : 7u) && timed == value && by[6] == value);
    assert(redwood_gpu_recover() == !platform);
    redwood_gpu_report("failed execution"); redwood_gpu_stop();
    assert(legacy_calls == (platform ? 0u : 16u));
    if (platform) assert(!bo_open && stop_calls == 2 && !close_calls && submit_calls == 1 && cache_calls == 1);
}
int main(void) {
    exercise(false); exercise(true);
    bo_open = true; redwood_gpu_stop(); assert(!bo_open && close_calls == 1 && !legacy_calls);
    uint64_t value=9, address=0x10001000; uint32_t words=6;
    assert(!redwood_gpu_ready() && !redwood_gpu_parameter(1, &value) && !redwood_gpu_address(4096));
    assert(!redwood_gpu_map(address, 0x50000000, true) && !redwood_gpu_mapped() && !redwood_gpu_unmap(address, 4096));
    assert(!redwood_gpu_submit(&address, &words, 1) && !redwood_gpu_done(17));
    unsigned checked = ready_checks;
    redwood_gpu_to_memory(NULL, 0);
    assert(cache_calls == 2 && ready_checks == checked && !legacy_calls && !bo_open);
    bo_open = true;
    const uint32_t params[] = {1,2,3,4,6,7,8,9,10,16,17,20};
    const uint64_t expected[] = {830,0xc00000,0x44050001,342000000,0x04000000,1,0,0,0,16,0,0x1fffffffff000ULL};
    for (unsigned i=0;i<sizeof(params)/sizeof(*params);i++) assert(redwood_gpu_parameter(params[i],&value) && value == expected[i]);
    const uint32_t unsupported[] = {0,5,11,14,15,18,19,21,UINT32_MAX};
    for (unsigned i=0;i<sizeof(unsupported)/sizeof(*unsupported);i++) assert(!redwood_gpu_parameter(unsupported[i],&value));
    for (unsigned platform=0;platform<2;platform++) for (unsigned keep=0;keep<2;keep++)
    for (unsigned back=0;back<2;back++) for (unsigned tested=0;tested<2;tested++) for (unsigned published=0;published<2;published++) {
        annibale=platform; adreno8_gpu8_keep=keep; adreno8_gpu8_return=back; self_test=tested; publish_ok=published;
        bo_open=false; enable_calls=publish_calls=stop_calls=0; gpu_va=2;
        assert(adreno8_step_service() == (platform && keep && !back && tested && published));
        unsigned admitted=platform && keep && !back;
        assert(enable_calls == admitted && publish_calls == (admitted && tested));
        assert(stop_calls == (admitted && tested && !published));
        if (publish_calls) assert(gpu_va == 0 && gpu_log == 1);
    }
    puts("C ABI: 16 fixed-platform entries, A830 retention, parameters and stage23 admission: PASS");
}
'''

PUBLISHER = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
using IOOptionBits = unsigned;
enum { kIOServiceInactiveState=1, kIOServiceRegisteredState=2, kIOServiceSynchronous=2 };
static const char *kIOUserClientClassKey="IOUserClientClass";
static unsigned mode, allocated, released, attached, detached, registrations, lock_allocations, lock_frees, stopped;
static bool cached_arg;
struct IOLock { unsigned depth=0; };
static IOLock *IOLockAlloc() { if(mode==1) return nullptr; lock_allocations++; return new IOLock; }
static void IOLockFree(IOLock *p) { assert(p && !p->depth); lock_frees++; delete p; }
static void IOLockLock(IOLock *p) { assert(p && p->depth++==0); }
static void IOLockUnlock(IOLock *p) { assert(p && p->depth--==1); }
static IOLock *gGPULock;
static bool gGPUCached;
static void IOLog(const char *, ...) {}
static bool PE_parse_boot_argn(const char *, void *v, unsigned) { *(uint32_t *)v=cached_arg; return true; }
struct IOService {
    unsigned state=0; bool has_parent=false;
    static IOService *getPlatform() { static IOService provider; return mode==2 ? nullptr : &provider; }
    virtual bool start(IOService *) { return mode!=6; }
    bool init() { return mode!=4; }
    bool attach(IOService *) { if(mode==5) return false; assert(!has_parent); has_parent=true; attached++; return true; }
    void detach(IOService *) { assert(has_parent); has_parent=false; detached++; }
    bool setProperty(const char *, const char *) { return mode!=7; }
    void setName(const char *) {}
    void registerService(unsigned options) { assert(options==kIOServiceSynchronous); registrations++; state=mode==8 ? 0 : mode==9 ? 3 : 2; }
    unsigned getState() const { return state; }
    void release() { released++; delete this; }
    virtual ~IOService() = default;
};
struct RedwoodGPU : IOService { bool start(IOService *) override; };
static RedwoodGPU *make_gpu() { if(mode==3) return nullptr; allocated++; return new RedwoodGPU; }
#define OSTypeAlloc(type) make_gpu()
#define OSSafeReleaseNULL(p) do { if(p) { (p)->release(); (p)=nullptr; } } while(0)
static void redwood_gpu_stop() { assert(gGPULock && gGPULock->depth==1); stopped++; }
'''

PUBLISH_CASES = r'''
int main() {
    for (mode=1;mode<=9;mode++) {
        allocated=released=attached=detached=registrations=lock_allocations=lock_frees=stopped=0;
        assert(!RedwoodGPUStart() && !gGPUService && !gGPULock);
        assert(allocated==released && attached==detached && lock_allocations==lock_frees);
        RedwoodGPUStop(); assert(!stopped);
    }
    mode=0; assert(RedwoodGPUStart() && gGPUService && gGPULock);
    unsigned count=allocated, locks=lock_allocations, regs=registrations;
    assert(RedwoodGPUStart() && allocated==count && locks==lock_allocations && regs==registrations);
    RedwoodGPUStop(); assert(stopped==1 && !gGPULock->depth);
    gGPUService->state=3; assert(!RedwoodGPUStart() && allocated==count);
    gGPUService->detach(IOService::getPlatform()); gGPUService->release(); gGPUService=nullptr;
    IOLockFree(gGPULock); gGPULock=nullptr;
    puts("IOKit publication: real registered state, sync registration, cleanup, repeat and stop lock: PASS");
}
'''

BOUNDARIES = r'''
static unsigned cache_pushes;
static void a830_boundaries() {
    for (unsigned mode=0;mode<26;mode++) {
        reset(); a830=true; auto c=client(); auto *b=allocate(c,8u<<20);
        msm_gem_submit_bo bo{0,1,0}; msm_gem_submit_cmd commands[83]{};
        for (auto &cmd:commands) { cmd.type=MSM_SUBMIT_CMD_BUF; cmd.size=4; }
        msm_gem_submit request{}; request.nr_bos=1; request.nr_cmds=1; request.flags=MSM_PIPE_3D0;
        request.bos=reinterpret_cast<uint64_t>(&bo); request.cmds=reinterpret_cast<uint64_t>(commands);
        bool valid=false;
        if(mode==0) { request.nr_cmds=82; valid=true; }
        if(mode==1) request.nr_cmds=83;
        if(mode==2) commands[0].size=0;
        if(mode==3) commands[0].size=0x400000;
        if(mode==4) b->address=1ull<<32;
        if(mode==5) { b->address=0xfffffffc; commands[0].size=8; }
        if(mode==6) { b->address=0xfffffffc; valid=true; }
        if(mode==7) { commands[0].size=0x3ffffc; valid=true; }
        if(mode==8) commands[0].size=3;
        if(mode==9) { b->address=UINT64_MAX-3; commands[0].submit_offset=4; }
        if(mode==10) commands[0].submit_idx=1;
        if(mode==11) request.flags=0;
        if(mode==12) { request.flags|=MSM_SUBMIT_NO_IMPLICIT; valid=true; }
        if(mode>=13 && mode<=18) request.flags|=0x40000000u>>(mode-13);
        if(mode==19) request.in_syncobjs=1;
        if(mode==20) request.out_syncobjs=1;
        if(mode==21) request.nr_in_syncobjs=1;
        if(mode==22) request.nr_out_syncobjs=1;
        if(mode==23) request.syncobj_stride=8;
        if(mode==24) request.flags=1;
        if(mode==25) request.flags|=0x100;
        uint64_t original_address=b->address; cache_pushes=0;
        assert(c.submit(&request)==(valid ? kIOReturnSuccess : kIOReturnBadArgument));
        assert(!gGPUReported && submit_calls==(valid?1u:0u) && cache_pushes==(valid?1u:0u));
        assert(b->address==original_address); c.clientClose(); c.free();
    }
    reset();
    puts("IOKit A830 admission: 26 count, flags, syncobj, length and address cases before cache: PASS");
}
'''


def run(source, directory, label, cpp=True, success=True):
    path=directory/(label+(".cpp" if cpp else ".c")); binary=directory/label
    path.write_text(source)
    cmd=["c++" if cpp else "cc", "-std=c++17" if cpp else "-std=c11", "-fsanitize=address,undefined",
         "-fno-omit-frame-pointer", "-Werror=return-type", "-I",str(directory),str(path),"-o",str(binary)]
    result=subprocess.run(cmd,capture_output=True,text=True)
    if result.returncode: raise RuntimeError(label+" compile:\n"+result.stderr)
    result=subprocess.run([str(binary)],capture_output=True,text=True)
    if success and result.returncode: raise RuntimeError(label+":\n"+result.stdout+result.stderr)
    if not success and not result.returncode: raise RuntimeError("mutation survived: "+label)
    if success: print(result.stdout,end="")


def boundaries(source, fixture):
    unit=fixture["unit"](source,(IOKIT/"RedwoodGPUMemory.inc").read_text())
    unit=unit.replace('static void redwood_gpu_to_memory(void *, uint64_t) {}',
        'static unsigned cache_pushes;\nstatic void redwood_gpu_to_memory(void *, uint64_t) { cache_pushes++; }',1)
    return unit.replace('int main() {',BOUNDARIES.replace('static unsigned cache_pushes;','')
                        +'\nint main() {\n    a830_boundaries();',1)


def main():
    fixture=runpy.run_path(str(ROOT/"tools/test-redwood-gpu-lifetime.py"))
    function=fixture["function"]
    original=(IOKIT/"RedwoodGPU.cpp").read_text()
    source=original.replace('#include "RedwoodGPUSubmit.inc"',(IOKIT/"RedwoodGPUSubmit.inc").read_text())
    dispatch=(DEV/"redwood_gpu_dispatch.c").read_text()
    publisher=(function(source,"RedwoodGPU::start")+"\nstatic RedwoodGPU *gGPUService;\n"
               +function(source,"RedwoodGPUStart")+function(source,"RedwoodGPUStop"))
    with tempfile.TemporaryDirectory(prefix="redwood-gpu-dispatch-") as folder:
        target=Path(folder); (target/"sys").mkdir()
        (target/"adreno8_private.h").write_text(STUB)
        (target/"sys/redwood_input.h").write_text("")
        for name in ("adreno8_bo_private.h","adreno8_memory_private.h","adreno8_context_private.h","adreno8_hwsched_private.h","redwood_gpu_private.h"):
            (target/name).write_text((DEV/name).read_text())
        unit='#include "adreno8_private.h"\n'+dispatch+BACKENDS+DISPATCH_CASES
        run(unit,target,"dispatch",False)
        changed=dispatch.replace('adreno8_to_memory((vm_offset_t)start, size);',
                                 'if (adreno8_bo_ready()) adreno8_to_memory((vm_offset_t)start, size);',1)
        assert changed != dispatch
        run('#include "adreno8_private.h"\n'+changed+BACKENDS+DISPATCH_CASES,
            target,"cache_polls_hfi",False,success=False)
        print("A830 cache-only ABI: no BO/HFI readiness checks after close; polling mutation rejected: PASS")
        run(PUBLISHER+publisher+PUBLISH_CASES,target,"publisher")
        run(boundaries(source,fixture),target,"boundaries")
        queue=source[source.index('\tcase kMsmSubmitQueueNew:'):source.index('\tcase kMsmSubmitQueueQuery:')]
        worked=source[source.index('\tcase kRedwoodGPUWorked:'):source.index('\tcase kRedwoodSurface:')]
        set_param=source[source.index('\tcase kMsmSetParam:'):source.index('\tcase kMsmGemCpuFini:')]
        queue_unit='''#include <cassert>\n#include <cstdint>\nstatic bool a830; static bool redwood_platform_is_annibale(){return a830;}
        enum { kMsmSubmitQueueNew, kRedwoodGPUWorked, kMsmSetParam, kIOReturnSuccess, kIOReturnBadArgument, kIOReturnUnsupported };
        #define MSM_PARAM_COMM 0x0c
        #define MSM_PARAM_CMDLINE 0x0d
        static unsigned depth; static void IOLockLock(void*){depth++;} static void IOLockUnlock(void*){depth--;}
        static void *gGPULock; static void redwood_gpu_worked(uint64_t*,uint32_t*,uint32_t*){assert(!a830);}
        int call(unsigned selector,unsigned flags,unsigned prio) {
        struct { struct{unsigned flags,prio,id;} queue; struct{uint64_t microseconds;uint32_t submissions,by[7];}worked; struct{unsigned param;} param;}r{};
        r.param.param=flags;r.queue.flags=flags;r.queue.prio=prio;unsigned fNextQueue=0;int ret=kIOReturnSuccess;
        switch(selector){'''+queue+worked+set_param+'''} return ret; }
        int main(){for(unsigned platform=0;platform<2;platform++){a830=platform;
        for(unsigned flags=0;flags<4;flags++)for(unsigned prio=0;prio<2;prio++)
        assert(call(kMsmSubmitQueueNew,flags,prio)==((a830&&(flags||prio))?kIOReturnBadArgument:kIOReturnSuccess));
        assert(call(kRedwoodGPUWorked,0,0)==(a830?kIOReturnUnsupported:kIOReturnSuccess));assert(!depth);
        for(unsigned param=0;param<0x18;param++)
        assert(call(kMsmSetParam,param,0)==((param==0x0c||param==0x0d)?kIOReturnSuccess:kIOReturnBadArgument));}}
        '''
        run(queue_unit,target,"queues")
        variants=[("publication_state",publisher.replace('return (getState() & (kIOServiceRegisteredState | kIOServiceInactiveState)) == kIOServiceRegisteredState;','return true;',1)),
                  ("publication_sync",publisher.replace('registerService(kIOServiceSynchronous);','registerService(0);',1))]
        for label,changed in variants: run(PUBLISHER+changed+PUBLISH_CASES,target,label,success=False)
        variants=[("ib_count","GPU_A830_SUBMIT_CMDS    82","GPU_A830_SUBMIT_CMDS    128"),
                  ("ib_zero","cmds[i].size == 0 || ",""),
                  ("ib_words","cmds[i].size / 4 > 0xfffff || ",""),
                  ("ib_32bit","b->address >= (1ull << 32) ||\n\t\t    address >= (1ull << 32) || cmds[i].size > (1ull << 32) - address","false"),
                  ("submit_sync","if (a830 && ((request->flags", "if (false && ((request->flags")]
        for label,old,new in variants:
            assert old in source
            run(boundaries(source.replace(old,new,1),fixture),target,label,success=False)
        legacy=(DEV/"redwood_gpu_service.inc").read_text()
        old='static void redwood_gpu_stop() { assert(gGPULock && gGPULock->depth==1); stopped++; }'
        chain_stubs='''static bool annibale=true, bo_open=true, gpu_kept;
        static bool redwood_platform_is_annibale(){return annibale;}
        static bool adreno8_bo_ready(){return bo_open;}
        static bool adreno8_context_close(){assert(gGPULock->depth==1);return true;}
        static void adreno8_bo_stop(){assert(gGPULock->depth==1);bo_open=false;stopped++;}
        static void legacy_gpu_stop(){assert(gGPULock->depth==1);stopped++;}
        void redwood_gpu_stop();'''
        assert old in PUBLISHER
        chain=(PUBLISHER.replace(old,chain_stubs)+publisher+function(dispatch,"redwood_gpu_stop")
               +function(legacy,"redwood_adreno_stop")
               +PUBLISH_CASES.replace('RedwoodGPUStop(); assert(stopped==1',
                                     'redwood_adreno_stop(); assert(stopped==1',1))
        run(chain,target,"stop_chain")
        ready_block=source[source.index('\tif (selector != kRedwoodShow) {'):source.index('\tbzero(&r, sizeof(r));')]
        param_block=source[source.index('\tcase kMsmGetParam:'):source.index('\tcase kMsmSetParam:')]
        locked=PUBLISHER+r'''
        enum { kRedwoodShow=1,kMsmGetParam=2,kIOReturnSuccess=0,kIOReturnNotReady=3,kIOReturnBadArgument=4 };
        static bool gGPUReported, backend_ready;
        static bool redwood_gpu_ready(){assert(gGPULock && gGPULock->depth==1);return backend_ready;}
        static bool redwood_gpu_parameter(uint32_t,uint64_t *v){assert(gGPULock->depth==1);*v=830;return backend_ready;}
        struct gpu_buffer{}; const unsigned GPU_BUFFERS=3;
        #define IONewZero(type,n) new type[n]{}
        struct IOUserClient:IOService{};
        struct RedwoodGPUUserClient:IOUserClient{gpu_buffer *fBuffers=nullptr;bool start(IOService*) override;};
        '''+function(source,"RedwoodGPUUserClient::start")+'''
        static int entry(unsigned selector){'''+ready_block+'''return kIOReturnSuccess;}
        static int parameter(){int ret=kIOReturnSuccess;struct{struct{uint32_t param;uint64_t value;}param;}r{};
        switch(kMsmGetParam){'''+param_block+'''}return ret;}
        int main(){RedwoodGPUUserClient c;IOService provider;
        assert(!c.start(&provider) && entry(kRedwoodShow)==kIOReturnSuccess && entry(0)==kIOReturnNotReady);
        gGPULock=IOLockAlloc();assert(!c.start(&provider) && entry(0)==kIOReturnNotReady);
        backend_ready=true;assert(c.start(&provider) && entry(0)==kIOReturnSuccess && parameter()==kIOReturnSuccess);
        assert(!gGPULock->depth);delete[] c.fBuffers;c.fBuffers=nullptr;
        gGPUReported=true;assert(!c.start(&provider) && entry(0)==kIOReturnNotReady);
        gGPUReported=false;backend_ready=false;assert(parameter()==kIOReturnBadArgument);
        IOLockFree(gGPULock);gGPULock=nullptr;}
        '''
        run(locked,target,"locked_entries")
        legacy_start=(DEV/"redwood_adreno.c").read_text()
        start_line=re.search(r"^\s*if \(!RedwoodGPUStart\(\)\) legacy_gpu_stop\(\);$",legacy_start,re.M).group()
        cleanup='''#include <cassert>\n#include <cstdint>\n#include <cstdio>
        static bool gpu_kept, quiet, core_stuck, publish_ok;static uint32_t gpu_fence;
        static unsigned stopped;static bool legacy_gpu_done(uint32_t){return true;}
        static void sleep_us(unsigned){}static void gpu_block_stop(){stopped++;}
        static bool RedwoodGPUStart(){return publish_ok;}
        '''+function(legacy,"legacy_gpu_stop")+'\nstatic void publish(){'+start_line+'''}
        int main(){for(unsigned pass=0;pass<2;pass++){gpu_kept=true;publish_ok=pass;stopped=0;
        publish();assert(gpu_kept==publish_ok && stopped==!publish_ok);}}
        '''
        run(cleanup,target,"legacy_publication_cleanup")
        print("IOKit queues/timing, complete stop chain and 7 admission/publication mutations: PASS")
    # This source chain has one lock boundary and no recursive call back to its caller.
    legacy=(DEV/"redwood_gpu_service.inc").read_text()
    stopped=function(legacy,"redwood_adreno_stop")
    assert "RedwoodGPUStop();" in stopped and "redwood_adreno_stop" not in function(source,"RedwoodGPUStop")
    assert "RedwoodGPUStop" not in function(dispatch,"redwood_gpu_stop")
    assert "redwood_adreno_stop" not in function(dispatch,"redwood_gpu_stop")
    header=(DEV/"adreno8_context_private.h").read_text()
    assert re.search(r"GPU_A830_SUBMIT_CMDS\s+82",source) and re.search(r"ADRENO8_CONTEXT_MAX_IBS\s+82",header)
    stage=(DEV/"adreno8.c").read_text()
    assert re.search(r"adreno8_context_test,\s*adreno8_step_service",stage)


if __name__=="__main__":
    main()
