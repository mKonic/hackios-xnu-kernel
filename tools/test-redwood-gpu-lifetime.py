#!/usr/bin/env python3
"""Execute RedwoodGPU's production BO methods with checked descriptor ownership."""

from pathlib import Path
import argparse
import re
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "xnu/files/iokit/Kernel/RedwoodGPU.cpp"
MEMORY = SOURCE.with_name("RedwoodGPUMemory.inc")
SUBMIT = SOURCE.with_name("RedwoodGPUSubmit.inc")
LEGACY = ROOT / "xnu/files/bsd/dev/redwood_gpu_service.inc"


def function(source, name):
    position = source.index("\n" + name + "(")
    start = source.rfind("\n", 0, position - 1) + 1
    return source[start : source.index("\n}\n", position) + 3]


PRELUDE = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <algorithm>
using IOByteCount = uint64_t;
using addr64_t = uint64_t;
using user_addr_t = uint64_t;
using vm_size_t = uint64_t;
using task_t = void *;
using IOReturn = int;
enum { kIOReturnSuccess, kIOReturnBadArgument, kIOReturnNoMemory,
       kIOReturnNotReady, kIOReturnNotFound, kIOReturnUnsupported,
       kIOReturnVMError, kIOReturnTimeout };
enum { kIOMemoryMapperNone, kIODirectionInOut = 1,
       kIOMemoryKernelUserShared = 2, kIOMapWriteCombineCache = 4 };
#ifndef HOST_PAGE_SIZE
#define HOST_PAGE_SIZE 16384
#endif
constexpr uint64_t PAGE_SIZE = HOST_PAGE_SIZE;
constexpr uint64_t NSEC_PER_USEC = 1000, NSEC_PER_MSEC = 1000000,
                   NSEC_PER_SEC = 1000000000;
static void *kernel_task;
static uint64_t now;
static uint64_t mach_absolute_time() { return now; }
static void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *t) { *t = ns; }
static void absolutetime_to_nanoseconds(uint64_t t, uint64_t *ns) { *ns = t; }
static void delay_for_interval(uint64_t n, uint64_t unit) { now += n * unit; }
static uint64_t round_page_64(uint64_t n) { return (n + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1); }
static void IOLog(const char *, ...) {}
static void redwood_tl(const char *, ...) {}
static int copyin(user_addr_t from, void *to, size_t size) {
    memcpy(to, reinterpret_cast<void *>(from), size); return 0;
}
#define TUNABLE(type, name, key, value) type name = value
#define IONew(type, count) new type[count]
#define IODelete(ptr, type, count) delete[] ptr
#define OSDynamicCast(type, ptr) dynamic_cast<type *>(ptr)
#define OSSafeReleaseNULL(ptr) do { if (ptr) { (ptr)->release(); (ptr) = nullptr; } } while (0)
struct IOLock { unsigned depth = 0; };
static void IOLockLock(IOLock *l) { assert(l && l->depth++ == 0); }
static void IOLockUnlock(IOLock *l) { assert(l && l->depth-- == 1); }

class IOMemoryDescriptor {
public:
    unsigned refs = 1;
    bool freed = false;
    uint64_t size, segment_limit = UINT64_MAX;
    uint8_t *bytes;
    IOMemoryDescriptor *parent = nullptr;
    static std::vector<IOMemoryDescriptor *> allocated;
    explicit IOMemoryDescriptor(uint64_t n) : size(n), bytes((uint8_t *)calloc(1, n)) {
        assert(bytes); allocated.push_back(this);
    }
    virtual ~IOMemoryDescriptor() { free(bytes); }
    void retain() { assert(!freed && refs); ++refs; }
    void release() {
        assert(!freed && refs);
        if (--refs == 0) { freed = true; if (parent) parent->release(); }
    }
    uint64_t getLength() const { assert(!freed); return size; }
    void *getBytesNoCopy() { assert(!freed); return bytes; }
    addr64_t getPhysicalSegment(uint64_t from, IOByteCount *length, int) {
        assert(!freed && from < size);
        *length = std::min(size - from, segment_limit);
        return 0x100000000ull + from;
    }
};
std::vector<IOMemoryDescriptor *> IOMemoryDescriptor::allocated;
class IOBufferMemoryDescriptor : public IOMemoryDescriptor {
public:
    using IOMemoryDescriptor::IOMemoryDescriptor;
    static uint64_t next_segment_limit;
    static IOBufferMemoryDescriptor *inTaskWithOptions(void *, unsigned, uint64_t size, uint64_t) {
        auto *m = new IOBufferMemoryDescriptor(size);
        m->segment_limit = next_segment_limit; return m;
    }
};
uint64_t IOBufferMemoryDescriptor::next_segment_limit = UINT64_MAX;
class IOSubMemoryDescriptor : public IOMemoryDescriptor {
public:
    using IOMemoryDescriptor::IOMemoryDescriptor;
    static IOSubMemoryDescriptor *withSubRange(IOMemoryDescriptor *p, uint64_t from, uint64_t n, int) {
        if (from > p->size || n > p->size - from) return nullptr;
        auto *m = new IOSubMemoryDescriptor(n); p->retain(); m->parent = p; return m;
    }
};
struct MiniIOSurface {
    IOMemoryDescriptor *memory;
    uint8_t *kbase;
    unsigned held = 1;
    uint32_t drawn = 0;
    void *client = nullptr;
    struct { uint64_t allocSize; uint32_t offset, bytesPerRow, width, height, pixelFormat, planeCount; } info{};
    struct Shared { uint32_t seed = 0; } state;
    Shared *shared() { return &state; }
};
static MiniIOSurface *surface_lookup;
static MiniIOSurface *MiniIOSurfaceCopy(uint32_t) {
    if (surface_lookup) ++surface_lookup->held;
    return surface_lookup;
}
static void MiniIOSurfaceDrop(MiniIOSurface *s) {
    assert(s->held); if (--s->held == 0) s->memory->release();
}
struct IOUserClient {
    bool inactive = false;
    bool isInactive() { return inactive; }
    void terminate() { inactive = true; }
    void free() {}
};
static bool a830;
static bool redwood_platform_is_annibale() { return a830; }
static bool ready, mapped_ok, unmap_ok, fence_done, recover_ok;
static unsigned map_calls, unmap_calls, freed_addresses, done_calls, submit_calls, map_fail_at;
static uint32_t submit_answer;
static uint64_t next_address, last_base, base_phys, wrong_pages;
static uint32_t gpu_log, gpu_va;
static bool redwood_gpu_ready() { return ready; }
static uint64_t redwood_gpu_address(uint64_t size) {
    uint64_t result = next_address; next_address += size; last_base = result; return result;
}
static void redwood_gpu_address_free(uint64_t, uint64_t) { ++freed_addresses; }
static bool redwood_gpu_map(uint64_t address, uint64_t phys, bool) {
    /* every 4 KB of a buffer is entered at its own physical page */
    if (address == last_base) base_phys = phys;
    else wrong_pages += phys - base_phys != address - last_base;
    return ++map_calls != map_fail_at;
}
static bool redwood_gpu_mapped() { return mapped_ok; }
static bool redwood_gpu_unmap(uint64_t, uint64_t) { ++unmap_calls; return unmap_ok; }
static unsigned busy_for;                       /* redwood_gpu_done() says no so many times first */
static bool redwood_gpu_done(uint32_t) { ++done_calls; return fence_done && done_calls > busy_for; }
static uint32_t redwood_gpu_last() { return 7; }
static uint32_t context_depth = 4;
static uint32_t adreno8_context_depth() { return context_depth; }
static uint32_t redwood_gpu_submit(const uint64_t *, const uint32_t *, uint32_t count) {
    assert(count); ++submit_calls; return submit_answer;
}
static void redwood_gpu_to_memory(void *, uint64_t) {}
static void redwood_gpu_report(const char *) {}
static bool redwood_gpu_recover() { return recover_ok; }
static void redwood_gpu_worked(uint64_t *time, uint32_t *count, uint32_t by[7]) {
    *time = *count = 0; memset(by, 0, 7 * sizeof(*by));
}
static void redwood_gpu_space(uint64_t *, uint32_t *, uint64_t *, unsigned *, unsigned *, uint64_t *, uint32_t *) {}
static void redwood_gpu_speed(uint32_t *mhz, uint32_t *changes) { *mhz = *changes = 0; }
'''


CASES = r'''
static void reset() {
    assert(gGPULock->depth == 0);
    for (auto *m : IOMemoryDescriptor::allocated) delete m;
    IOMemoryDescriptor::allocated.clear();
    a830 = false; ready = mapped_ok = unmap_ok = fence_done = true;
    recover_ok = false;
    map_calls = unmap_calls = freed_addresses = done_calls = submit_calls = map_fail_at = 0;
    wrong_pages = busy_for = 0; context_depth = 4;
    submit_answer = 17; next_address = 0x40000000; now = 0;
    gGPUReported = gGPUCached = false; gGPUKeep = 0;
    IOBufferMemoryDescriptor::next_segment_limit = UINT64_MAX;
    surface_lookup = nullptr; memset(&gGPUStat, 0, sizeof(gGPUStat));
}
static RedwoodGPUUserClient client() {
    RedwoodGPUUserClient c{}; c.fBuffers = new gpu_buffer[GPU_BUFFERS]{}; return c;
}
static gpu_buffer *allocate(RedwoodGPUUserClient &c, uint64_t size = PAGE_SIZE) {
    msm_gem_new request{size, 0, 0};
    assert(c.gemNew(&request) == kIOReturnSuccess);
    return &c.fBuffers[request.handle - 1];
}
static void healthy() {
    reset(); auto c = client(); auto *b = allocate(c); auto *m = b->memory;
    c.freeBuffer(b); assert(m->freed && unmap_calls == 1 && freed_addresses == 1); c.free();
}
static void stopped() {
    reset(); auto c = client(); auto *b = allocate(c); auto *m = b->memory;
    b->fence = 7; ready = false; c.freeBuffer(b);
    assert(c.fLeftCount == 1 && !m->freed && done_calls == 0);
    c.reap(); c.clientClose(); assert(!m->freed && done_calls == 0 && freed_addresses == 0); c.free();
}
static void failed_reap() {
    reset(); auto c = client(); auto *b = allocate(c); auto *m = b->memory;
    b->fence = 7; fence_done = false; c.freeBuffer(b); assert(c.fLeftCount == 1);
    fence_done = true; unmap_ok = false; c.reap();
    assert(c.fLeftCount == 1 && !m->freed && gGPUReported && freed_addresses == 0);
    c.reap(); assert(unmap_calls == 1); c.clientClose(); assert(!m->freed); c.free();
}
static void completed_reap() {
    for (bool cached : {false, true}) {
        reset(); auto c = client(); gGPUKeep = cached; auto *b = allocate(c, GPU_KEEP_FROM); auto *m = b->memory;
        b->fence = 7; fence_done = false; c.freeBuffer(b); assert(c.fLeftCount == 1);
        fence_done = true; c.reap(); assert(c.fLeftCount == 0);
        if (cached) {
            assert(!m->freed && c.fKeptBytes == GPU_KEEP_FROM && unmap_calls == 0);
            assert(c.dropKept(&c.fKept[0]) && m->freed);
        } else assert(m->freed && unmap_calls == 1 && freed_addresses == 1);
        c.free();
    }
}
static void failed_free() {
    reset(); auto c = client(); auto *b = allocate(c); auto *m = b->memory;
    unmap_ok = false; c.freeBuffer(b);
    assert(!m->freed && m->refs == 1 && b->memory == m && c.fBufferCount == 1);
    assert(gGPUReported && freed_addresses == 0);
    msm_gem_new request{PAGE_SIZE, 0, 0}; assert(c.gemNew(&request) == kIOReturnNotReady); c.free();
}
static void partial_map() {
    for (bool commit_failure : {false, true}) for (bool cleanup : {false, true}) {
        reset(); auto c = client(); map_fail_at = commit_failure ? 0 : 2;
        mapped_ok = !commit_failure; unmap_ok = cleanup;
        msm_gem_new request{2 * PAGE_SIZE, 0, 0};
        assert(c.gemNew(&request) == kIOReturnNoMemory);
        auto *m = IOMemoryDescriptor::allocated.back();
        assert(m->freed == cleanup && freed_addresses == unsigned(cleanup));
        assert(unmap_calls == 1 && (cleanup || (m->refs == 1 && gGPUReported))); c.free();
    }
    reset(); auto c = client(); IOBufferMemoryDescriptor::next_segment_limit = 1024;
    msm_gem_new request{PAGE_SIZE, 0, 0}; assert(c.gemNew(&request) == kIOReturnNoMemory);
    assert(map_calls == 0 && IOMemoryDescriptor::allocated.back()->freed); c.free();
}
static void segments() {
    for (uint64_t limit : {uint64_t(4096), uint64_t(8192), uint64_t(PAGE_SIZE), UINT64_MAX}) {
        reset(); auto c = client(); IOBufferMemoryDescriptor::next_segment_limit = limit;
        allocate(c, 3 * PAGE_SIZE);
        assert(map_calls == 3 * PAGE_SIZE / 4096 && wrong_pages == 0); c.free();
    }
}
static void kept() {
    reset(); auto c = client(); gGPUKeep = 1; auto *b = allocate(c, GPU_KEEP_FROM); auto *m = b->memory;
    c.freeBuffer(b); assert(!m->freed && c.fKeptBytes == GPU_KEEP_FROM);
    unmap_ok = false; assert(!c.dropKept(&c.fKept[0]));
    assert(!m->freed && c.fKept[0].memory == m && c.fKeptBytes == GPU_KEEP_FROM);
    c.clientClose(); assert(!m->freed); c.free();
    reset(); c = client(); gGPUKeep = 1;
    for (unsigned i = 0; i < GPU_KEPT; ++i) {
        auto *memory = new IOBufferMemoryDescriptor(GPU_KEEP_FROM);
        assert(c.keep(memory, memory->bytes, i * GPU_KEEP_FROM, GPU_KEEP_FROM, 0));
    }
    auto *extra = new IOBufferMemoryDescriptor(GPU_KEEP_FROM); unmap_ok = false;
    assert(!c.keep(extra, extra->bytes, 0xf0000000, GPU_KEEP_FROM, 0));
    assert(unmap_calls == 1 && !extra->freed); extra->release(); c.free();
    reset(); c = client(); gGPUKeep = 1; b = allocate(c, GPU_KEEP_FROM); m = b->memory;
    c.freeBuffer(b); b = allocate(c, GPU_KEEP_FROM);
    assert(b->memory == m && c.fKeptBytes == 0 && unmap_calls == 0);
    c.freeBuffer(b); assert(c.dropKept(&c.fKept[0]) && m->freed); c.free();
    reset(); c = client(); gGPUKeep = 1; b = allocate(c, GPU_KEEP_FROM); m = b->memory;
    c.freeBuffer(b); unmap_ok = false; now = (GPU_KEPT_SECONDS + 1ull) * NSEC_PER_SEC;
    msm_gem_new request{GPU_KEEP_FROM, 0, 0};
    assert(c.gemNew(&request) == kIOReturnNotReady && !m->freed && c.fKept[0].memory == m); c.free();
}
static void failed_surface() {
    reset(); auto c = client(); auto *root = new IOBufferMemoryDescriptor(3 * PAGE_SIZE);
    MiniIOSurface s{}; s.memory = root; s.kbase = root->bytes; s.info.allocSize = 2 * PAGE_SIZE;
    surface_lookup = &s; map_fail_at = 2; unmap_ok = false;
    redwood_surface request{}; assert(c.surfaceBuffer(&request) == kIOReturnNoMemory);
    assert(c.fBufferCount == 1 && c.fBuffers[request.handle - 1].memory == root);
    MiniIOSurfaceDrop(&s); assert(!root->freed && root->refs == 1); c.free();
}
static void surface_ownership() {
    for (bool cleanup : {false, true}) {
        reset(); auto c = client(); auto *root = new IOBufferMemoryDescriptor(2 * PAGE_SIZE);
        MiniIOSurface s{}; s.memory = root; s.kbase = root->bytes; s.info.allocSize = PAGE_SIZE;
        surface_lookup = &s; redwood_surface request{};
        assert(c.surfaceBuffer(&request) == kIOReturnSuccess && s.held == 2);
        auto *b = &c.fBuffers[request.handle - 1]; auto *sub = b->memory;
        unmap_ok = cleanup; c.freeBuffer(b); MiniIOSurfaceDrop(&s);
        assert(root->freed == cleanup && sub->freed == cleanup);
        if (!cleanup) {
            assert(b->memory == sub && b->surface == &s && s.held == 1);
            c.clientClose(); assert(!root->freed && !sub->freed);
        }
        c.free();
    }
}
static void full_pending() {
    reset(); auto c = client(); std::vector<IOMemoryDescriptor *> backing;
    for (unsigned i = 0; i <= GPU_LEFT; ++i) {
        auto *b = allocate(c); b->fence = 7; backing.push_back(b->memory);
    }
    gGPUReported = true;
    for (unsigned i = 0; i <= GPU_LEFT; ++i) c.freeBuffer(&c.fBuffers[i]);
    assert(c.fLeftCount == GPU_LEFT && c.fBufferCount == 1 && c.fBuffers[GPU_LEFT].memory == backing.back());
    c.clientClose(); c.free();
    for (auto *m : backing) assert(!m->freed && m->refs == 1);
    assert(done_calls == 0 && freed_addresses == 0);
}
static void failed_submit() {
    for (bool recovered : {false, true}) {
        reset(); auto c = client(); auto *b = allocate(c); auto *m = b->memory;
        msm_gem_submit_bo bo{0, 1, 0}; msm_gem_submit_cmd cmd{};
        cmd.type = MSM_SUBMIT_CMD_BUF; cmd.size = 4;
        msm_gem_submit request{}; request.nr_cmds = request.nr_bos = 1;
        request.bos = reinterpret_cast<uint64_t>(&bo); request.cmds = reinterpret_cast<uint64_t>(&cmd);
        submit_answer = 0; recover_ok = recovered;
        assert(c.submit(&request) == kIOReturnTimeout && gGPUReported && b->fence == 0);
        assert(submit_calls == (recovered ? 2u : 1u)); c.freeBuffer(b);
        assert(!m->freed && freed_addresses == 0);
        MiniIOSurface surface{}; assert(!MiniIOSurfaceDrawn(&surface, 0)); c.free();
    }
}
static void queue_room() {
    /* A830: with the queue full, submit waits for the oldest through gpuWait() before the backend is called */
    for (bool a830_queue : {false, true}) {
        reset(); a830 = a830_queue; auto c = client(); allocate(c);
        msm_gem_submit_bo bo{0, 1, 0}; msm_gem_submit_cmd cmd{};
        cmd.type = MSM_SUBMIT_CMD_BUF; cmd.size = 4;
        msm_gem_submit request{}; request.nr_cmds = request.nr_bos = 1; request.flags = MSM_PIPE_3D0;
        request.bos = reinterpret_cast<uint64_t>(&bo); request.cmds = reinterpret_cast<uint64_t>(&cmd);
        busy_for = 3; uint64_t before = now;
        assert(c.submit(&request) == kIOReturnSuccess && submit_calls == 1);
        assert(a830_queue ? done_calls >= 4 && now > before && gGPUStat.waits == 1 : done_calls == 0 && now == before);
        c.free();
    }
    reset(); a830 = true; context_depth = 8; auto c = client(); allocate(c);   /* last 7 < depth 8: room, no wait */
    msm_gem_submit_bo bo{0, 1, 0}; msm_gem_submit_cmd cmd{};
    cmd.type = MSM_SUBMIT_CMD_BUF; cmd.size = 4;
    msm_gem_submit request{}; request.nr_cmds = request.nr_bos = 1; request.flags = MSM_PIPE_3D0;
    request.bos = reinterpret_cast<uint64_t>(&bo); request.cmds = reinterpret_cast<uint64_t>(&cmd);
    busy_for = 3; assert(c.submit(&request) == kIOReturnSuccess && done_calls == 0); c.free();
}
static void display() {
    reset(); MiniIOSurface s{}; s.drawn = 7; ready = false;
    assert(!MiniIOSurfaceDrawn(&s, 0) && done_calls == 0);
    ready = true; assert(MiniIOSurfaceDrawn(&s, 0));
}
int main() {
    healthy(); stopped(); failed_reap(); completed_reap(); failed_free(); partial_map(); segments(); kept();
    failed_surface(); surface_ownership(); full_pending(); failed_submit(); queue_room(); display(); reset();
    printf("RedwoodGPU production lifecycle: 30 scenarios at %llu-byte pages passed\n", (unsigned long long)PAGE_SIZE);
}
'''


def unit(source, memory):
    uapi = source[source.index("/* Linux's include/uapi") : source.index("static IOLock * gGPULock;")]
    stats = re.search(r"static struct \{.*?\} gGPUStat;", source, re.S).group()
    records = source[source.index("struct gpu_buffer {") : source.index("class RedwoodGPUUserClient :")]
    fields = source[source.index("\ttask_t                  fTask;") : source.index("\npublic:\n\tvirtual bool initWithTask")]
    return (PRELUDE + uapi + "\nstatic IOLock lock; static IOLock *gGPULock = &lock;\n"
            "static bool gGPUReported, gGPUCached;\n" + stats + records
            + "\nstruct RedwoodGPUUserClient : IOUserClient {\n" + fields
            + "\nIOReturn clientClose(); void free();\n};\n"
            + function(source, "gpuWait") + function(source, "MiniIOSurfaceDrawn")
            + memory + function(source, "RedwoodGPUUserClient::submit") + CASES)


LEGACY_MODEL = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
struct { unsigned context = 3; } core_space;
struct { uint64_t base = 0x100000; } smmu;
enum { SMMU_CB_TLBIASID = 1, SMMU_CB_TLBSYNC = 2, SMMU_CB_TLBSTATUS = 3 };
static bool pte_ok = true, tlb_busy;
static unsigned commits, maps, status_reads;
static uint32_t smmu_context(void *) { return 0x1000; }
static void space_commit(void *) { ++commits; }
static bool space_map(void *, uint64_t, uint64_t phys, unsigned prot) {
    assert(phys == 0 && prot == 0); ++maps; return pte_ok;
}
static void settle() {}
static void wr(uint64_t, uint64_t) {}
static unsigned rd(uint64_t address) {
    assert(address == smmu.base + 0x1000 + SMMU_CB_TLBSTATUS);
    ++status_reads; return tlb_busy;
}
'''
LEGACY_CASES = r'''
int main() {
    assert(redwood_gpu_mapped() && commits == 1);
    assert(redwood_gpu_unmap(0x20000000, 8192) && maps == 2 && commits == 2);
    pte_ok = false; assert(!redwood_gpu_unmap(0x20000000, 8192));
    pte_ok = true; tlb_busy = true; status_reads = 0;
    assert(!redwood_gpu_unmap(0x20000000, 8192) && status_reads == 1001);
    status_reads = 0;
    assert(!redwood_gpu_mapped() && status_reads == 1001);
    puts("legacy production unmap: PTE and TLB failure checks passed");
}
'''


def execute(text, folder, label, expect_success=True, page_size=16384):
    source = folder / (label + ".cpp")
    binary = folder / label
    source.write_text(text)
    compile_result = subprocess.run(["c++", "-std=c++17", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-DHOST_PAGE_SIZE=" + str(page_size),
                                     "-Werror=return-type", str(source), "-o", str(binary)], capture_output=True, text=True)
    if compile_result.returncode:
        raise RuntimeError(label + " compilation failed:\n" + compile_result.stderr)
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    if expect_success:
        if run.returncode:
            raise RuntimeError(label + " failed:\n" + run.stdout + run.stderr)
        print(run.stdout, end="")
    elif run.returncode == 0:
        raise RuntimeError("mutation survived: " + label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()
    source = SOURCE.read_text().replace('#include "RedwoodGPUSubmit.inc"', SUBMIT.read_text())
    memory, legacy = MEMORY.read_text(), LEGACY.read_text()
    legacy_functions = function(legacy, "legacy_gpu_mapped") + function(legacy, "legacy_gpu_unmap")
    with tempfile.TemporaryDirectory(prefix="redwood-gpu-lifetime-") as temporary:
        folder = Path(temporary)
        execute(unit(source, memory), folder, "lifecycle_4k", page_size=4096)
        execute(unit(source, memory), folder, "lifecycle_16k")
        execute(LEGACY_MODEL + legacy_functions + LEGACY_CASES.replace("redwood_gpu_", "legacy_gpu_"), folder, "legacy")
        if args.mutations:
            changes = [
                ("release_stopped", memory, "gGPUReported || !redwood_gpu_ready() || (b->fence", "gGPUReported || (b->fence", False),
                ("ignore_unmap", memory, "!redwood_gpu_unmap(address, size)", "(redwood_gpu_unmap(address, size), false)", False),
                ("partial_release", memory, "\n\t\t\tmemory->retain();", "\n\t\t\t/* removed retain */", False),
                ("reuse_failed_kept", memory, "if (!gpuReleaseMapping(k->address, k->size))", "if ((gpuReleaseMapping(k->address, k->size), false))", False),
                ("short_segment", memory, " && length >= 4096;", ";", False),
                ("one_lookup", memory, "phys += 4096, length -= 4096", "length -= 4096", False),
                ("queue_wait_held", source, "if (!gpuWait(last - depth + 1, GPU_PATIENCE_MS)) {", "if (true) {", True),
                ("submit_zero", source, "gGPUReported = true;\n\t}\n\tif (gpu_log", "gGPUReported = false;\n\t}\n\tif (gpu_log", True),
                ("display_stopped", source, "(redwood_gpu_ready() && gpuWait(fence, milliseconds))", "(!redwood_gpu_ready() || gpuWait(fence, milliseconds))", True),
            ]
            for name, original, old, new, in_source in changes:
                assert old in original, name
                changed = original.replace(old, new, 1)
                execute(unit(changed if in_source else source, memory if in_source else changed), folder, name, False)
            changed = legacy_functions.replace("return cleared && synced;", "return true;")
            assert changed != legacy_functions
            execute(LEGACY_MODEL + changed + LEGACY_CASES.replace("redwood_gpu_", "legacy_gpu_"), folder, "ignore_tlb", False)
            print(f"{len(changes) + 1} lifecycle mutations detected")


if __name__ == "__main__":
    main()
