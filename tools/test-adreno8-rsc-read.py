#!/usr/bin/env python3
"""Check the read-only Apps RSC probe's version gates and MMIO bounds."""
from pathlib import Path
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
text = (root / "xnu/files/bsd/dev/adreno8_cx.c").read_text()
probe = re.search(r"static bool\nadreno8_rsc_read\(void\)\n\{.*?\n\}", text, re.S).group()
source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
struct adreno8_block { const char *name; uint64_t phys; uint32_t size; };
static uint32_t regs[0x10000 / 4], offsets[200], reads;
static uint32_t adreno8_get(struct adreno8_block *b, const char *name, uint32_t off) {
    assert(b->phys == 0x16520000 && b->size == 0x10000);
    assert(off < b->size && !(off & 3) && reads < 200);
    offsets[reads++] = off;
    return regs[off / 4];
}
''' + probe + r'''
static void prepare(unsigned major, unsigned ncpt) {
    memset(regs, 0, sizeof(regs)); reads = 0;
    regs[0] = major << 16 | 7 << 8;
    regs[1] = 1u << 24;
    regs[3] = ncpt << 27 | 8 << 12;
}
int main(void) {
    for (unsigned major = 2; major <= 3; major++) {
        prepare(major, 16);
        unsigned control = major == 3 ? 0x24 : 0x14;
        unsigned command = major == 3 ? 0x34 : 0x30;
        unsigned stride = major == 3 ? 24 : 20;
        regs[(0xd00 + control + 8) / 4] = 1 | 1 << 15;
        regs[(0xd00 + 7 * 0x2a0 + control + 8) / 4] = 1 << 15;
        assert(adreno8_rsc_read() && reads == 46);
        assert(offsets[0] == 0 && offsets[1] == 4 && offsets[2] == 0xc);
        assert(offsets[3] == 0xd00 && offsets[4] == 0xd04);
        assert(offsets[5] == 0xd00 + control - 4);
        assert(offsets[9] == 0xd00 + command);
        assert(offsets[12] == 0xd00 + command + 15 * stride);
        assert(offsets[45] == 0xd00 + 7 * 0x2a0 + command + 15 * stride + 8);
        prepare(major, 16);
        assert(adreno8_rsc_read() && reads == 37);
    }
    for (unsigned major = 0; major < 6; major++) {
        if (major == 2 || major == 3) continue;
        prepare(major, 16);
        assert(!adreno8_rsc_read() && reads == 3);
    }
    const unsigned bad_counts[] = {0, 17, 31};
    for (unsigned i = 0; i < 3; i++) {
        prepare(3, bad_counts[i]);
        assert(!adreno8_rsc_read() && reads == 3);
    }
    prepare(3, 16); regs[3] = UINT32_MAX;
    assert(!adreno8_rsc_read() && reads == 3);
    prepare(3, 16); regs[3] = 16u << 27 | 7 << 12;
    assert(!adreno8_rsc_read() && reads == 3);
    prepare(3, 16); regs[0] = UINT32_MAX;
    assert(!adreno8_rsc_read() && reads == 3);
    prepare(3, 16); regs[0] = 0;
    assert(!adreno8_rsc_read() && reads == 3);
}
'''
with tempfile.TemporaryDirectory(prefix="adreno8-rsc-") as directory:
    path, binary = Path(directory) / "check.c", Path(directory) / "check"
    path.write_text(source)
    subprocess.run(["cc", "-std=c11", "-fsanitize=address,undefined", str(path), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, stdout=subprocess.DEVNULL)
print("Apps RSC read-only v2/v3 layout, enabled slots and rejection bounds: PASS")
