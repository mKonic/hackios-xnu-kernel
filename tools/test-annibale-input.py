#!/usr/bin/env python3
"""Check GT9916K command bytes and CPU affinity using the actual C functions."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def function(path, name):
    source = (ROOT / path).read_text()
    start = source.index(name + "(")
    begin = source.index("{", start)
    depth = 1
    end = begin + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


command = function("xnu/files/bsd/dev/redwood_goodix.c", "goodix_command")
sgi = function("xnu/files/osfmk/arm64/gic_v3_irq.c", "gic_send_sgi")
source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include "xnuloader/platform_private.h"
static uint32_t command_address = 0x10174;
static uint8_t packet[16], ack = 0x80;
static unsigned writes, reads, busy_reads;
#define GOODIX_ACK_OK 0x80
#define GOODIX_ACK_BUSY 2
static bool goodix_write(uint32_t address, const uint8_t *data, uint32_t size) {
    assert(address == command_address && size == sizeof(packet));
    memcpy(packet, data, size); writes++; return true;
}
static bool goodix_read(uint32_t address, uint8_t *data, uint32_t size) {
    assert(address == command_address && size == sizeof(packet));
    memset(data, 0, size); data[1] = reads++ < busy_reads ? 2 : ack; return true;
}
static void sleep_ms(unsigned ms) { assert(ms == 1); }
static uint64_t sgi_value;
#define __builtin_arm_wsr64(reg, value) (sgi_value = (value))
#define __builtin_arm_isb(value) ((void)0)
#define ISB_SY 0
static bool
''' + command + '\nstatic void\n' + sgi + r'''
int main(void) {
    const uint8_t raw_off[16] = {0, 0, 6, 0x90, 2, 0, 0x98, 0};
    const uint8_t coord_on[16] = {0, 0, 5, 0x91, 1, 0x97, 0};
    assert(goodix_command(0x90, 2, 2));
    assert(memcmp(packet, raw_off, 16) == 0);
    assert(goodix_command(0x91, 1, 1));
    assert(memcmp(packet, coord_on, 16) == 0);
    reads = 0; busy_reads = 3;
    assert(goodix_command(0x91, 1, 1) && reads == 4);
    ack = 0xff; busy_reads = 0; writes = 0;
    assert(!goodix_command(0x91, 1, 1) && writes == 6);
    writes = 0; command_address = 0;
    assert(!goodix_command(0x91, 1, 1) && writes == 0);
    const uint32_t expected[] = {0, 0x100, 0x200, 0x300, 0x400, 0x500, 0x10000, 0x10100};
    for (int i = 0; i < 8; i++) {
        assert(platform_annibale.core_mpidr[i] == expected[i]);
        assert(platform_core_index(&platform_annibale, expected[i]) == i);
        assert(platform_redwood.core_mpidr[i] == (uint32_t)i * 0x100);
        assert(platform_qemu_virt.core_mpidr[i] == (uint32_t)i);
    }
    assert(platform_core_index(&platform_annibale, 0x700) == -1);
    gic_send_sgi(0x10100, 1); assert(sgi_value == 0x101010001ULL);
    gic_send_sgi(0x500, 1); assert(sgi_value == 0x1050001ULL);
    gic_send_sgi(7, 1); assert(sgi_value == 0x1000080ULL);
}
'''
with tempfile.TemporaryDirectory(prefix="annibale-input-") as directory:
    src = Path(directory) / "check.c"
    binary = Path(directory) / "check"
    src.write_text(source)
    subprocess.run(["cc", "-std=c11", "-I", str(ROOT), str(src), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
print("GT9916K commands, ACK handling, MPIDR mapping and GIC Aff2: PASS")
