#!/usr/bin/env python3
"""Validate IOSurface wire layouts and the driver's mapping ownership on the host."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "xnu/files/iokit/Kernel/MiniIOSurfaceABI.h"
DRIVER = ROOT / "xnu/files/iokit/Kernel/MiniIOSurface.cpp"

ABI_SOURCE = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "MiniIOSurfaceABI.h"

static uint64_t read(const uint8_t *p, unsigned offset, unsigned width)
{
    uint64_t result = 0;
    memcpy(&result, p + offset, width);
    return result;
}

static void test_init(void)
{
    for (unsigned size = 0; size <= 0x29; ++size) {
        uint8_t output[0x30];
        memset(output, 0xa5, sizeof(output));
        bool valid = size == 0x18 || size == 0x28;
        assert(minisurfInitReply(output, size, 0x3fff, 63, 16384) == valid);
        if (!valid) {
            for (uint8_t byte : output) assert(byte == 0xa5);
            continue;
        }
        assert(read(output, 0, 4) == 0x3fff);
        assert(read(output, 4, 4) == 63);
        if (size == 0x28) {
            assert(read(output, 8, 8) == 262144);
            assert(read(output, 0x10, 8) == 16384);
            assert(read(output, 0x18, 8) == 16384);
            assert(read(output, 0x22, 2) == 1);
        } else {
            assert(read(output, 8, 4) == 262144);
            assert(read(output, 0xc, 4) == 16384);
            assert(read(output, 0x10, 4) == 16384);
            assert(read(output, 0x16, 2) == 1);
        }
        assert(output[size] == 0xa5);
    }
    assert(!minisurfInitReply(NULL, 0x28, 0, 0, 0));
}

static void test_result(unsigned count)
{
    minisurf_result info = {};
    info.base = UINT64_MAX;
    info.shared = 0xabcdef1234567890ULL;
    info.aux = 0xabcdef1234567c90ULL;
    info.sid = 0x10203040;
    info.allocSize = 0xfedcba98;
    info.width = 1206; info.height = 2622; info.bytesPerRow = 4864; info.offset = 128;
    info.pixelFormat = 0x42475241; info.planeCount = count;
    info.bytesPerElement = 4; info.elementWidth = 1; info.elementHeight = 2;
    info.cacheMode = 3; info.lockSeed = 17; info.subsampling = 2; info.prohibitUseCount = 1;
    info.parentID = 91; info.sliceCount = 2; info.traceID = 0x1020304050607080ULL;
    info.protectionOptions = 0xfedcba9876543210ULL;
    minisurf_plane planes[4] = {};
    for (unsigned i = 0; i < 4; ++i) {
        planes[i].width = 301 + i; planes[i].height = 601 + i;
        planes[i].offset = 0xffff0000 + i; planes[i].bytesPerRow = 2048 + i; planes[i].size = 0xf0000000 + i;
        planes[i].bytesPerElement = 2 + i; planes[i].elementWidth = 1; planes[i].elementHeight = 2;
        planes[i].componentCount = 4; planes[i].addressFormat = 1; planes[i].compression = i;
        for (unsigned j = 0; j < 4; ++j) {
            planes[i].componentBitDepth[j] = 8 + j;
            planes[i].componentBitOffset[j] = 16 + j;
            planes[i].componentName[j] = 24 + j;
            planes[i].componentType[j] = 32 + j;
            planes[i].componentRange[j] = 40 + j;
            planes[i].extendedPixels[j] = 100 * i + j;
        }
    }
    for (minisurf_abi abi : {MINISURF_ABI16, MINISURF_ABI26}) {
        unsigned plane_count = count ? count : 1;
        unsigned expected = abi == MINISURF_ABI26 ? 0x88 + plane_count * 0x98 : 0x68 + plane_count * 0x80;
        uint8_t allocation[0x400];
        uint8_t *output = allocation + 1;
        memset(allocation, 0xa5, sizeof(allocation));
        assert(minisurfWriteResult(output, expected - 1, abi, &info, planes) == 0);
        for (uint8_t byte : allocation) assert(byte == 0xa5);
        assert(minisurfWriteResult(output, expected, abi, &info, planes) == expected);
        assert(allocation[0] == 0xa5 && output[expected] == 0xa5);
        if (abi == MINISURF_ABI16) {
            static_assert(sizeof(minisurf_result) == 0x68, "legacy header");
            static_assert(sizeof(minisurf_plane) == 0x80, "legacy plane");
            assert(!memcmp(output, &info, 0x68));
            assert(!memcmp(output + 0x68, planes, plane_count * 0x80));
            continue;
        }
        struct Field { unsigned offset, width; uint64_t value; } fields[] = {
            {0,8,info.base}, {8,8,info.shared}, {0x10,8,info.aux}, {0x18,4,info.sid},
            {0x20,8,info.allocSize}, {0x28,8,1206}, {0x30,8,2622}, {0x38,8,4864}, {0x40,8,128},
            {0x48,4,0x42475241}, {0x58,4,count}, {0x60,2,4}, {0x62,1,1}, {0x63,1,2},
            {0x64,4,3}, {0x68,4,17}, {0x6c,1,2}, {0x6d,1,1}, {0x70,4,91}, {0x74,4,2},
            {0x78,8,info.traceID}, {0x80,8,info.protectionOptions},
        };
        for (Field field : fields) assert(read(output, field.offset, field.width) == field.value);
        assert(read(output, 0x1c, 4) == 0 && read(output, 0x50, 8) == 0);
        for (unsigned i = 0; i < plane_count; ++i) {
            const uint8_t *p = output + 0x88 + i * 0x98;
            assert(read(p, 0, 8) == 301 + i && read(p, 8, 8) == 601 + i);
            assert(read(p, 0x18, 8) == 0xffff0000 + i);
            assert(read(p, 0x20, 8) == 2048 + i && read(p, 0x28, 8) == 0xf0000000 + i);
            assert(read(p, 0x30, 2) == 2 + i && p[0x32] == 1 && p[0x33] == 2);
            assert(p[0x34] == 4 && p[0x35] == 1 && p[0x6c] == i);
            for (unsigned j = 0; j < 4; ++j) {
                assert(p[0x38+j] == 8+j && p[0x3c+j] == 16+j && p[0x40+j] == 24+j);
                assert(p[0x44+j] == 32+j && p[0x48+j] == 40+j);
                assert(read(p, 0x88+4*j, 4) == 100*i+j);
            }
        }
    }
    info.planeCount = 5;
    uint8_t bytes[1];
    assert(!minisurfWriteResult(bytes, sizeof(bytes), MINISURF_ABI26, &info, planes));
}

static void test_bulk(void)
{
    const unsigned begin[] = {0,0x20,0x30,0x38,0x39,0x3a,0x3b,0x3c,0x3d,0x3e,0x3f,0x40,0x58,0x5c,0x64,0x68,0x70,0x65,0x66,0x74,0x80,0x82};
    const unsigned end[] = {0x20,0x30,0x38,0x39,0x3a,0x3b,0x3c,0x3d,0x3e,0x3f,0x40,0x58,0x5c,0x64,0x65,0x70,0x74,0x66,0x67,0x80,0x82,0x84};
    for (minisurf_abi abi : {MINISURF_ABI16, MINISURF_ABI26}) {
        unsigned suffix = abi == MINISURF_ABI26 ? 0x88 : 0x70;
        uint64_t limit = abi == MINISURF_ABI26 ? 0x84 : 0x70;
        unsigned base = abi == MINISURF_ABI26 ? 0x34 : 0x30;
        unsigned fields = abi == MINISURF_ABI26 ? 22 : 15;
        uint8_t input[0xa0], shared[0xc0];
        memset(input, 0x3c, sizeof(input));
        uint64_t size = 0, mask = 0;
        uint32_t sid = 0, expected_sid = 197;
        memcpy(input + suffix, &limit, 8);
        memcpy(input + suffix + 16, &expected_sid, 4);
        for (unsigned i = 0; i < fields; ++i) {
            uint64_t bit = 1ULL << i;
            memcpy(input + suffix + 8, &bit, 8);
            assert(minisurfBulkInput(abi, input, suffix + 0x18, &size, &mask, &sid));
            assert(size == limit && mask == bit && sid == 197);
            assert(!minisurfBulkInput(abi, input, suffix + 0x17, &size, &mask, &sid));
            memset(shared, 0xa5, sizeof(shared));
            minisurfApplyBulk(shared, abi, input, size, mask);
            for (unsigned j = 0; j < sizeof(shared); ++j)
                assert(shared[j] == (j >= base+begin[i] && j < base+end[i] ? 0x3c : 0xa5));
            memset(shared, 0xa5, sizeof(shared));
            minisurfApplyBulk(shared, abi, input, end[i] - 1, mask);
            for (uint8_t byte : shared) assert(byte == 0xa5);
        }
        ++limit;
        memcpy(input + suffix, &limit, 8);
        assert(!minisurfBulkInput(abi, input, suffix+0x18, &size, &mask, &sid));
        memset(shared, 0xa5, sizeof(shared));
        minisurfSetYCbCrMatrix(shared, abi, 7);
        if (abi == MINISURF_ABI26) {
            assert(shared[0x6e] == 7 && read(shared, 0x24, 4) == 0xa5a5a5a5);
        } else {
            assert(read(shared, 0x24, 4) == 7 && shared[0x6e] == 0xa5);
        }
    }
}

int main(void)
{
    test_init();
    for (unsigned count : {0u, 1u, 4u}) test_result(count);
    test_bulk();
    puts("IOSurface ABI: init, results, planes, bulk fields and bounds passed");
}
'''

MAP_PREFIX = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <new>
#include "MiniIOSurfaceABI.h"
typedef int IOReturn;
enum {kIOReturnSuccess, kIOReturnUnsupported, kIOReturnNoMemory, kIOReturnBadArgument, kIOMapAnywhere};
enum {PAGE_MASK = 0x3fff, MINISURF_ROW_ALIGN = 64, MINISURF_MAX_DIMENSION = 16384};
static int gSurfaceLock;
static void IOLockLock(int) {}
static void IOLockUnlock(int) {}
struct IOExternalMethodArguments { void *structureOutput; uint32_t structureOutputSize; };
static int liveMaps;
struct IOMemoryMap {
    IOMemoryMap() { ++liveMaps; }
    void release() { --liveMaps; delete this; }
};
struct Memory {
    bool fail;
    IOMemoryMap *createMappingInTask(int, int, int) { return fail ? NULL : new IOMemoryMap; }
};
struct MiniIOSurface {
    minisurf_abi sharedABI;
    unsigned sid, holders, useCount[2];
    minisurf_result info;
    Memory *memory, *client;
    int clientTask;
    struct Shared { uint64_t useCount; } state;
    Shared *shared() { return &state; }
};
struct minisurf_client_ref {
    MiniIOSurface *surface;
    IOMemoryMap *map, *pixelMap;
    uint32_t use[2];
    bool detached;
};
struct IOSurfaceRootUserClient {
    int fTask;
    minisurf_client_ref *fRefs;
    unsigned fRefCount, fRefCapacity;
    minisurf_abi fABI;
    minisurf_client_ref *addRef(MiniIOSurface *, IOReturn *);
    void dropRef(minisurf_client_ref *);
    IOReturn methodInit(IOExternalMethodArguments *);
};
#define IONew(type, count) new (std::nothrow) type[count]
#define IODelete(pointer, type, count) delete[] pointer
#define IOLog(...) ((void)0)
#define OSSafeReleaseNULL(pointer) do { if (pointer) { pointer->release(); pointer = NULL; } } while (0)
static void surfaceHold(MiniIOSurface *s) { ++s->holders; }
static void surfaceUnhold(MiniIOSurface *s) { --s->holders; }
'''
MAP_MAIN = r'''
int main(void)
{
    for (minisurf_abi abi : {MINISURF_ABI16, MINISURF_ABI26}) {
        Memory memory = {true}, pixels = {true};
        MiniIOSurface surface = {};
        surface.memory = &memory; surface.client = &pixels; surface.clientTask = 2; surface.holders = 1;
        IOSurfaceRootUserClient client = {};
        client.fTask = 1; client.fABI = abi;
        IOReturn error = -1;
        assert(!client.addRef(&surface, &error));
        assert(error == kIOReturnNoMemory && liveMaps == 0 && client.fRefCount == 0);
        assert(surface.sharedABI == MINISURF_UNCLAIMED && surface.holders == 1);
        memory.fail = false;
        assert(!client.addRef(&surface, &error));
        assert(error == kIOReturnNoMemory && liveMaps == 0 && client.fRefCount == 0);
        assert(surface.sharedABI == MINISURF_UNCLAIMED && surface.holders == 1);
        pixels.fail = false;
        minisurf_client_ref *ref = client.addRef(&surface, &error);
        assert(ref && error == kIOReturnSuccess && surface.sharedABI == abi);
        assert(liveMaps == 2 && client.fRefCount == 1 && surface.holders == 2);
        IOSurfaceRootUserClient other = {};
        other.fTask = 3; other.fABI = abi == MINISURF_ABI16 ? MINISURF_ABI26 : MINISURF_ABI16;
        assert(!other.addRef(&surface, &error));
        assert(error == kIOReturnUnsupported && liveMaps == 2 && other.fRefCount == 0 && !other.fRefs);
        client.dropRef(ref);
        assert(liveMaps == 0 && client.fRefCount == 0 && surface.holders == 1);
        assert(!other.addRef(&surface, &error) && error == kIOReturnUnsupported);
        ref = client.addRef(&surface, &error);
        assert(ref && error == kIOReturnSuccess);
        client.dropRef(ref);
        delete[] client.fRefs;
    }
    IOSurfaceRootUserClient negotiated = {};
    uint8_t output[0x28];
    IOExternalMethodArguments args = {output, 0x27};
    assert(negotiated.methodInit(&args) == kIOReturnBadArgument);
    assert(negotiated.fABI == MINISURF_UNCLAIMED);
    args.structureOutputSize = 0x28;
    assert(negotiated.methodInit(&args) == kIOReturnSuccess && negotiated.fABI == MINISURF_ABI26);
    assert(negotiated.methodInit(&args) == kIOReturnSuccess);
    args.structureOutputSize = 0x18;
    assert(negotiated.methodInit(&args) == kIOReturnBadArgument && negotiated.fABI == MINISURF_ABI26);
    IOSurfaceRootUserClient legacy = {};
    Memory memory = {false};
    MiniIOSurface surface = {};
    surface.memory = &memory; surface.holders = 1;
    IOReturn error;
    minisurf_client_ref *ref = legacy.addRef(&surface, &error);
    assert(ref && legacy.fABI == MINISURF_ABI16 && surface.sharedABI == MINISURF_ABI16);
    legacy.dropRef(ref);
    args.structureOutputSize = 0x28;
    assert(legacy.methodInit(&args) == kIOReturnBadArgument);
    delete[] legacy.fRefs;
    assert(liveMaps == 0);
    puts("IOSurface mapping: failures, ABI negotiation/isolation and cleanup passed");
}
'''


def sources():
    driver = DRIVER.read_text()
    start = driver.index("struct minisurf_client_ref *\nIOSurfaceRootUserClient::addRef(")
    end = driver.index('#include "MiniIOSurfaceABI.inc"', start)
    methods = HEADER.with_suffix(".inc").read_text()
    init_start = methods.index("IOReturn\nIOSurfaceRootUserClient::methodInit(")
    init_end = methods.index("IOReturn\nIOSurfaceRootUserClient::methodSetBulkAttachments", init_start)
    return {"abi": ABI_SOURCE, "mapping": MAP_PREFIX + driver[start:end] + methods[init_start:init_end] + MAP_MAIN}


def main():
    with tempfile.TemporaryDirectory() as directory:
        for name, body in sources().items():
            source = Path(directory) / (name + ".cc")
            executable = Path(directory) / name
            source.write_text("#include <initializer_list>\n" + body)
            subprocess.run([
                "clang++", "-std=c++11", "-Wall", "-Wextra", "-Werror", "-O1",
                "-fsanitize=address,undefined", "-I", str(HEADER.parent),
                str(source), "-o", str(executable),
            ], check=True)
            subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    main()
