#!/usr/bin/env python3
"""Compare production HFI allocation, views and queues with pinned vendor layout."""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "xnu/files/bsd/dev"
PIN = "d50b305f7da9e14715a25120a4ac7b1a4b8b97c3"
HASHES = {
    "adreno_hfi.h": "cf44ef07a460d74a24486ed0ac9495acf07d8074e6eaf8bf190c55ac374ac2b1",
    "adreno_gen8_hwsched_hfi.c": "661cf9bb6b61ca97c2fce15d78cb6cb2a3cb84831eeec12c3c62ccc358c0f1ad",
}
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--vendor-dir", required=True, type=Path, help=f"graphics-kernel sources at {PIN}")
args = parser.parse_args()
vendor = {}
for name, expected in HASHES.items():
    data = (args.vendor_dir / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected, f"unexpected vendor input: {name}"
    vendor[name] = data.decode()


def define(text, name):
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(r"#define\s+" + name + r"(?:\s|\()", line))
    end = start
    while lines[end].endswith("\\"):
        end += 1
    return "\n".join(lines[start:end + 1])


def function(text, name, result):
    start = text.index(name + "(")
    return result + "\n" + text[start:text.index("\n}\n", start) + 2] + "\n"


wire = vendor["adreno_hfi.h"]
vendor_code = "\n".join(define(wire, name) for name in (
    "HFI_QUEUE_SIZE", "HFI_CMD_ID", "HFI_MSG_ID", "HFI_DBG_ID",
    "HFI_QUEUE_DEFAULT_CNT", "HFI_QUEUE_DISPATCH_MAX_CNT", "HFI_QUEUE_HDR_MAX",
    "HFI_QUEUE_OFFSET", "GMU_QUEUE_START_ADDR", "QUEUE_HDR_TYPE",
))
vendor_code += "\n" + "\n".join(re.search(r"struct " + name + r" \{.*?\n\} __packed;", wire, re.S)[0] for name in (
    "hfi_queue_table_header", "hfi_queue_header", "hfi_queue_table",
))
host = vendor["adreno_gen8_hwsched_hfi.c"]
vendor_code += "\n" + define(host, "HFI_QUEUE_MAX") + "\n" + define(host, "DEFINE_QHDR") + "\n"
vendor_code += "struct kgsl_memdesc { u32 gmuaddr; void *hostptr; };\nstruct gen8_hfi { struct kgsl_memdesc *hfi_mem; };\n"
vendor_code += function(host, "init_queues", "static void").replace("init_queues(", "init_vendor_queues(")

gmu = (DEV / "adreno8_gmu.c").read_text()
hfi = (DEV / "adreno8_hfi.c").read_text()
private = (DEV / "adreno8_private.h").read_text()
constants = "\n".join(line for line in private.splitlines() if line.startswith(("#define GMU_BLOCK", "#define GMU_STREAM", "#define GMU_MATCH")))
fixed = re.search(r"static const struct \{\n\tconst char \*name;\n\tuint32_t address, size;\n\} adreno8_gmu_fixed\[\] = \{.*?\n\};", gmu, re.S)[0]
production = "\n".join(function(gmu, name, "bool") for name in ("adreno8_block_new", "adreno8_covered", "adreno8_step_gmu_space"))
production += hfi[hfi.index("static vm_offset_t\nadreno8_queue_header"):hfi.index("static bool\nadreno8_hfi_reply")]
source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include "adreno8_wire_private.h"
typedef uintptr_t vm_offset_t;
typedef size_t vm_size_t;
typedef int kern_return_t;
typedef uint32_t u32;
#define __packed __attribute__((packed))
#define SZ_4K 4096u
#define SZ_16 16u
#define ALIGN(n, a) (((n) + (a) - 1) & ~((a) - 1))
''' + vendor_code + "\n" + constants + r'''
#define PAGE_MASK (PAGE_SIZE - 1u)
#define KMA_KOBJECT 1
#define KMA_ZERO 2
#define KMA_DATA 4
#define VM_KERN_MEMORY_IOKIT 0
#define KERN_SUCCESS 0
#define kernel_map 0
#define GMU_HOST2GMU_SET 0
struct adreno8_gpu_block { const char *name; uint32_t address, size; vm_offset_t memory; };
struct adreno8_block { vm_offset_t base; };
struct adreno8_gpu_block adreno8_gmu_demands[GMU_BLOCKS], adreno8_gmu_log, adreno8_gmu_hfi;
static struct adreno8_block adreno8_gmu;
static struct { bool is_on; } adreno8_gmu_space;
static unsigned adreno8_gmu_demand_count, adreno8_step, contiguous_calls, maps, smmu_calls, hwsched_memory_calls;
static unsigned barriers, publications, doorbells;
static bool adreno8_direct_ring;
static bool fail_contiguous, fail_mapping;
static vm_offset_t adreno8_log_view[4], adreno8_hfi_view_table, adreno8_hfi_view_cmd;
static vm_offset_t adreno8_hfi_view_resp, adreno8_hfi_view_debug;
static uint32_t registers[1], image[] = {0, 4, 0, 0, 0};
static const uint8_t *adreno8_gmu_firmware = (const uint8_t *)image;
static uint32_t adreno8_gmu_firmware_size = sizeof(image);
static const uint32_t *pending;
static unsigned pending_words, pending_start;
static int adreno8_say(const char *format, ...) { return 0; }
#define printf adreno8_say
static void adreno8_announce(const char *format, ...) {}
static bool adreno8_cx_ready(void) { return true; }
static bool adreno8_read_firmware(void) { return true; }
static bool adreno8_gmu_hwsched_memory(void) {
    assert(!adreno8_gmu_space.is_on && smmu_calls >= 3); hwsched_memory_calls++; return true;
}
static uint64_t kvtophys(vm_offset_t memory) { return memory; }
static int kmem_alloc(int map, vm_offset_t *memory, vm_size_t size, int flags, int tag) {
    assert(!(size & PAGE_MASK));
    *memory = (uintptr_t)aligned_alloc(PAGE_SIZE, size);
    if (*memory) memset((void *)*memory, 0, size);
    return *memory ? 0 : 1;
}
static int kmem_alloc_contig(int map, vm_offset_t *memory, vm_size_t size,
    vm_offset_t mask, unsigned pnum_mask, unsigned pnum_end, int flags, int tag) {
    assert(mask == PAGE_MASK && !pnum_mask && !pnum_end);
    contiguous_calls++;
    if (fail_contiguous) return 1;
    return kmem_alloc(map, memory, size, flags, tag);
}
static void adreno8_to_memory(vm_offset_t start, vm_size_t size) {}
static vm_offset_t ml_io_map_wcomb(vm_offset_t physical, vm_size_t size) {
    assert(!(physical & 4095)); maps++;
    if (physical == adreno8_gmu_hfi.memory) {
        assert(contiguous_calls == 1 && size == 0x4000);
        return fail_mapping ? 0 : physical;
    }
    assert(size == 4096 && physical >= adreno8_gmu_log.memory && physical < adreno8_gmu_log.memory + 0x4000);
    return physical;
}
static vm_offset_t adreno8_space_new_table(void *space, unsigned a, unsigned b) { smmu_calls++; return 1; }
static bool adreno8_space_map_block(void *space, struct adreno8_gpu_block *block) { smmu_calls++; return true; }
static bool adreno8_space_on(void *space, unsigned stream, unsigned match) {
    assert(stream == GMU_STREAM && match == GMU_MATCH);
    smmu_calls++; adreno8_gmu_space.is_on = true; return true;
}
static void adreno8_smmu_cb_scan_after_step9(void) {}
static uint32_t adreno8_rd(vm_offset_t address) { return *(uint32_t *)address; }
static void adreno8_settle(void) { barriers++; }
static void adreno8_wr(vm_offset_t address, uint32_t value) {
    if (address == adreno8_hfi_view_table + (ADRENO8_HFI_HEADER(0) + 11) * 4) {
        assert(pending && barriers >= 1 && !doorbells);
        uint32_t *queue = (uint32_t *)adreno8_hfi_view_cmd;
        for (unsigned i = 0; i < ALIGN(pending_words, 4u); i++)
            assert(queue[(pending_start + i) % 1024] == (i < pending_words ? pending[i] : 0xfafafafa));
        assert(value == (pending_start + ALIGN(pending_words, 4u)) % 1024);
        publications++;
    }
    if (address == adreno8_gmu.base + GMU_HOST2GMU_SET) {
        assert(publications == 1 && barriers >= 2 && value == 1); doorbells++;
    }
    *(uint32_t *)address = value;
}
''' + fixed + "\n" + production + r'''
static void release(void) {
    free((void *)adreno8_gmu_log.memory); free((void *)adreno8_gmu_hfi.memory);
    memset(&adreno8_gmu_log, 0, sizeof(adreno8_gmu_log));
    memset(&adreno8_gmu_hfi, 0, sizeof(adreno8_gmu_hfi));
    adreno8_hfi_view_table = adreno8_hfi_view_cmd = adreno8_hfi_view_resp = adreno8_hfi_view_debug = 0;
    contiguous_calls = maps = smmu_calls = hwsched_memory_calls = 0; adreno8_gmu_space.is_on = false;
}
int main(void) {
    _Alignas(16) uint8_t table[4096], expected[4096];
    memset(table, 0xa5, sizeof(table)); memset(expected, 0xa5, sizeof(expected));
    struct kgsl_memdesc md = {0x60006000, expected}; struct gen8_hfi vendor_hfi = {&md};
    init_vendor_queues(&vendor_hfi);
    adreno8_hfi_init_table((uint32_t *)table, md.gmuaddr, false);
    assert(sizeof(struct hfi_queue_table) == 840);
    assert(!memcmp(table, expected, sizeof(table)));
    assert(ADRENO8_HFI_ALLOC_BYTES == 0x4000);
    for (unsigned q = 0; q < 3; q++) {
        unsigned start = HFI_QUEUE_OFFSET(q), end = start + 4096;
        assert(start == ADRENO8_HFI_QUEUE_OFFSET(q) && start == 0x350 + 4096 * q);
        assert(!(start & 15) && start >= sizeof(struct hfi_queue_table) && end <= 0x4000);
        assert((start & 4095) + 4096 > 4096);
        if (q < 2) assert(end <= HFI_QUEUE_OFFSET(q + 1));
    }
    fail_contiguous = true;
    assert(!adreno8_step_gmu_space() && contiguous_calls == 1 && !maps && !smmu_calls);
    assert(!adreno8_gmu_hfi.memory && adreno8_gmu_log.memory); release();
    fail_contiguous = false; fail_mapping = true;
    assert(!adreno8_step_gmu_space() && contiguous_calls == 1 && maps == 1 && !smmu_calls);
    assert(adreno8_gmu_hfi.memory && adreno8_gmu_log.memory && !adreno8_hfi_view_cmd); release();
    fail_mapping = false;
    assert(adreno8_step_gmu_space() && contiguous_calls == 1 && maps == 5 && adreno8_gmu_space.is_on);
    assert(hwsched_memory_calls == 1);
    assert(adreno8_hfi_view_table == adreno8_gmu_hfi.memory);
    assert(adreno8_hfi_view_cmd == adreno8_hfi_view_table + HFI_QUEUE_OFFSET(0));
    assert(adreno8_hfi_view_resp == adreno8_hfi_view_table + HFI_QUEUE_OFFSET(1));
    assert(adreno8_hfi_view_debug == adreno8_hfi_view_table + HFI_QUEUE_OFFSET(2));
    assert(!memcmp((void *)adreno8_hfi_view_table, expected, sizeof(struct hfi_queue_table)));
    adreno8_gmu.base = (uintptr_t)registers;
    uint32_t message[255], output[255];
    for (unsigned i = 0; i < 255; i++) message[i] = 0x43210000 + i;
    for (unsigned test = 0; test < 3; test++) {
        unsigned words = test == 0 ? 3 : test == 1 ? 11 : 255;
        uint32_t saved[2048];
        memset((void *)adreno8_hfi_view_cmd, 0x11, 4096);
        memset((void *)adreno8_hfi_view_resp, 0x22, 4096);
        memset((void *)adreno8_hfi_view_debug, 0x33, 4096);
        memcpy(saved, (void *)adreno8_hfi_view_resp, sizeof(saved));
        uint32_t *header = (uint32_t *)adreno8_queue_header(0);
        header[10] = 512; header[11] = 1020;
        message[0] = (words << 8) | 131;
        pending = message; pending_words = words; pending_start = 1020;
        barriers = publications = doorbells = 0;
        assert(adreno8_queue_write(message, words) && publications == 1 && doorbells == 1);
        assert(!memcmp(saved, (void *)adreno8_hfi_view_resp, sizeof(saved)));
        for (unsigned q = 1; q < 3; q++) {
            uint32_t *data = (uint32_t *)(adreno8_hfi_view_table + HFI_QUEUE_OFFSET(q));
            header = (uint32_t *)adreno8_queue_header(q);
            header[10] = 1020; header[11] = (1020 + ALIGN(words, 4u)) % 1024;
            for (unsigned i = 0; i < words; i++) data[(1020 + i) % 1024] = message[i];
            assert(adreno8_queue_read(q, output) == (int)words && !memcmp(message, output, words * 4));
            assert(header[10] == header[11]);
        }
    }
    release(); adreno8_direct_ring = true;
    assert(adreno8_step_gmu_space() && contiguous_calls == 1 && maps == 5 && adreno8_gmu_space.is_on);
    assert(!hwsched_memory_calls);
    uint32_t *normal = (uint32_t *)adreno8_hfi_view_table;
    assert(normal[0] == 0 && normal[1] == 168 && normal[2] == 6 && normal[3] == 12);
    assert(normal[4] == 3 && normal[5] == 3);
    for (unsigned q = 0; q < 3; q++) {
        uint32_t *hdr = normal + 6 + q * 12;
        assert(hdr[0] == 1 && hdr[1] == 0x60007000 + q * 4096);
        assert(hdr[2] == (0xa00u | q) && hdr[3] == 1024);
        for (unsigned word = 4; word < 12; word++)
            assert(hdr[word] == (word >= 6 && word <= 8 ? 1 : 0));
    }
    assert(adreno8_hfi_view_cmd == adreno8_hfi_view_table + 4096);
    assert(adreno8_hfi_view_resp == adreno8_hfi_view_table + 8192);
    assert(adreno8_hfi_view_debug == adreno8_hfi_view_table + 12288);
    release();
    puts("Pinned vendor table, contiguous allocation, uncached views and cross-page ring publication/wrap: PASS");
}
'''

header = (DEV / "adreno8_wire_private.h").read_text()
variants = {
    "baseline": (source, header),
    "old-gmu-address": (source, header.replace("address + adreno8_hfi_queue_offset(q, ordinary)", "address + 4096 * (q + 1)")),
    "table-overwrite": (source, header.replace("memset(table, 0, ADRENO8_HFI_TABLE_DWORDS * 4u)", "memset(table, 0, 4096)")),
    "noncontiguous-allocation": (source.replace("if (b == &adreno8_gmu_hfi)", "if (false)"), header),
    "short-cpu-view": (source.replace("kvtophys(adreno8_gmu_hfi.memory), adreno8_gmu_hfi.size", "kvtophys(adreno8_gmu_hfi.memory), 4096"), header),
    "wrong-cpu-offset": (source.replace("adreno8_hfi_view_table + adreno8_hfi_queue_offset(0, adreno8_direct_ring);", "adreno8_hfi_view_table + 4096;"), header),
    "ordinary-vrb-allocation": (source.replace("if (!adreno8_direct_ring && !adreno8_gmu_hwsched_memory())", "if (!adreno8_gmu_hwsched_memory())"), header),
    "ordinary-cpu-offset": (source.replace("adreno8_hfi_queue_offset(0, adreno8_direct_ring);", "adreno8_hfi_queue_offset(0, false);"), header),
    "missing-padding": (source.replace("i < aligned; i++", "i < dwords; i++"), header),
}
with tempfile.TemporaryDirectory(prefix="adreno8-hfi-layout-") as directory:
    path = Path(directory)
    for name, (body, wire_header) in variants.items():
        assert name == "baseline" or body != source or wire_header != header, name
        (path / "check.c").write_text(body)
        (path / "adreno8_wire_private.h").write_text(wire_header)
        for page in (4096, 16384):
            binary = path / f"check-{page}"
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Wno-unused-parameter", "-fsanitize=address,undefined",
                            f"-DPAGE_SIZE={page}u", "-I", str(path), str(path / "check.c"), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            if name == "baseline":
                if result.returncode:
                    print(result.stdout, result.stderr)
                    result.check_returncode()
                print(f"XNU page size {page}: {result.stdout.strip()}")
            else:
                assert result.returncode != 0, f"undetected mutation: {name}, page size {page}"
    print(f"HFI layout mutations rejected: {len(variants) - 1} variants x 2 page sizes")
