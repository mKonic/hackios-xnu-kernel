#!/usr/bin/env python3
"""Exercise the production MBN parser and PAS call sequence with the real gen80000 file."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "xnu/files/bsd/dev/adreno8_zap.c"
FIRMWARE = ROOT.parent / "hackios-xnu-userland/firmware-gen8/gen80000_zap.mbn"
production = DRIVER.read_text().replace('#include "adreno8_private.h"', "")
stub = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include "adreno8_zap_private.h"
typedef uintptr_t vm_offset_t;
#define KMA_KOBJECT 0
#define KMA_ZERO 0
#define KMA_DATA 0
#define VM_KERN_MEMORY_IOKIT 0
#define KERN_SUCCESS 0
static int kernel_map;
static bool adreno8_reset_required, allocation_fail, service_fail;
static unsigned calls, fail_call, allocations, barriers;
static uint8_t original[16384], file[16384], metadata[PAGE_SIZE], target[ADRENO8_ZAP_SIZE];
static size_t file_size;
static int adreno8_say(const char *format, ...) { return 0; }
static const uint8_t *redwood_image_file(const char *dir, const char *name, uint64_t *size, int *disk) {
    assert(!strcmp(dir, "fw") && !strcmp(name, "gen80000_zap.mbn"));
    *size = file_size; return file;
}
static int kmem_alloc(int map, vm_offset_t *address, size_t size, unsigned flags, unsigned tag) {
    assert(size == PAGE_SIZE); allocations++;
    if (allocation_fail) return -1;
    *address = (uintptr_t)metadata; return 0;
}
static uint64_t kvtophys(vm_offset_t address) {
    assert(address >= (uintptr_t)metadata && address < (uintptr_t)(metadata + PAGE_SIZE));
    return 0x220000000ULL + address - (uintptr_t)metadata;
}
static void adreno8_to_memory(vm_offset_t address, size_t size) { assert(address == (uintptr_t)metadata && size == PAGE_SIZE); }
static void adreno8_settle(void) { barriers++; }
static vm_offset_t ml_io_map_wcomb(uint64_t physical, size_t size) {
    if (physical == 0x220000000ULL) { assert(size == 4096); return (uintptr_t)metadata; }
    assert(physical == ADRENO8_ZAP_BASE && size == 4096); return (uintptr_t)target;
}
static int32_t adreno8_scm_call(uint64_t function, uint64_t arginfo, uint64_t pas,
                              uint64_t a1, uint64_t a2, uint64_t a3, uint64_t *answer) {
    assert(a3 == 0); calls++; assert(pas == 13 && calls <= 3);
    if (calls == 1) {
        assert(function == 0x42000201 && arginfo == 0x82 && a1 == 0x220000000ULL && a2 == 0);
        assert(barriers >= 1);
        assert(!memcmp(metadata, file, 0x94));
        assert(!memcmp(metadata + 0x94, file + 0x2000, 0xf38));
        for (unsigned i = 0; i < sizeof(target); i++) assert(target[i] == 0xcc);
    } else if (calls == 2) {
        assert(function == 0x42000202 && arginfo == 3 && a1 == ADRENO8_ZAP_BASE && a2 == 4096);
    } else {
        assert(function == 0x42000205 && arginfo == 1 && a1 == 0 && a2 == 0);
        assert(barriers >= 2 && !memcmp(target, file + 0x1000, 0x3b4));
        for (unsigned i = 0x3b4; i < sizeof(target); i++) assert(target[i] == 0xcc);
    }
    *answer = calls == fail_call && service_fail ? 7 : 0;
    return calls == fail_call && !service_fail ? -1 : 0;
}
'''
tests = r'''
#undef printf
static void reset(void) {
    memcpy(file, original, file_size);
    memset(metadata, 0, sizeof(metadata)); memset(target, 0xcc, sizeof(target));
    zap_metadata = 0; zap_attempted = zap_authenticated = false;
    calls = allocations = barriers = fail_call = 0;
    allocation_fail = service_fail = false; adreno8_reset_required = true;
}
static void word(unsigned at, uint32_t value) {
    for (unsigned i = 0; i < 4; i++) file[at + i] = value >> (i * 8);
}
int main(int argc, char **argv) {
    struct adreno8_zap_image image;
    assert(argc == 2);
    FILE *input = fopen(argv[1], "rb"); assert(input);
    file_size = fread(original, 1, sizeof(original), input); fclose(input);
    assert(file_size == 12088);
    reset(); assert(adreno8_zap_parse(file, file_size, &image));
    assert(image.header_size == 0x94 && image.hash_offset == 0x2000 && image.hash_size == 0xf38);
    assert(image.minimum == 0x1000 && image.memory_size == 4096 && image.count == 1);
    assert(image.segments[0].file_offset == 0x1000 && image.segments[0].file_size == 0x3b4);
    for (size_t size = 0; size < file_size; size++) assert(!adreno8_zap_parse(file, size, &image));
    file[4] = 2; assert(!adreno8_zap_parse(file, file_size, &image));
    reset(); word(28, UINT32_MAX); assert(!adreno8_zap_parse(file, file_size, &image));
    reset(); word(52 + 16, 0x90); assert(!adreno8_zap_parse(file, file_size, &image));
    reset(); word(52 + 32 + 16, 0x3b5); assert(!adreno8_zap_parse(file, file_size, &image));
    reset(); word(52 + 32 + 12, 0xfffff000); assert(!adreno8_zap_parse(file, file_size, &image));
    reset(); word(52 + 32 + 20, 0x2001); assert(!adreno8_zap_parse(file, file_size, &image));
    reset(); word(52 + 32 + 24, 7); assert(!adreno8_zap_parse(file, file_size, &image));
    reset(); word(52 + 64 + 24, 0); assert(!adreno8_zap_parse(file, file_size, &image));
    reset(); word(52 + 64 + 4, UINT32_MAX); assert(!adreno8_zap_parse(file, file_size, &image));
    reset(); word(52 + 16, 4096); assert(!adreno8_zap_parse(file, file_size, &image));
    reset(); adreno8_reset_required = false; assert(!adreno8_zap_authenticate() && allocations == 0 && calls == 0);
    reset(); allocation_fail = true; assert(!adreno8_zap_authenticate() && calls == 0);
    reset(); assert(adreno8_zap_authenticate() && zap_authenticated && calls == 3);
    assert(adreno8_zap_authenticate() && calls == 3 && allocations == 1);
    for (unsigned service = 0; service < 2; service++) {
        for (unsigned fail = 1; fail <= 3; fail++) {
            reset(); fail_call = fail; service_fail = service;
            assert(!adreno8_zap_authenticate() && !zap_authenticated && zap_attempted && calls == fail);
            assert(!adreno8_zap_authenticate() && calls == fail && allocations == 1);
        }
    }
    puts("Real MBN bounds, relocation, PAS ordering and failure retention: PASS");
}
'''
with tempfile.TemporaryDirectory(prefix="adreno8-zap-") as directory:
    target = Path(directory)
    source = target / "check.c"
    source.write_text(stub + production + tests)
    for page_size in (4096, 16384):
        binary = target / f"check-{page_size}"
        subprocess.run(["cc", "-std=c11", f"-DPAGE_SIZE={page_size}", "-fsanitize=address,undefined",
                        "-I", str(DRIVER.parent), str(source), "-o", str(binary)], check=True)
        subprocess.run([str(binary), str(FIRMWARE)], check=True)
