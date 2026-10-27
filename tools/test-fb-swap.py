#!/usr/bin/env python3
"""Check the driver header against the 20F66 and 23G90 swap wire offsets."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "xnu/files/iokit/Kernel/MiniFramebufferSwap.h"

SOURCE = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "MiniFramebufferSwap.h"

struct WireLayout {
    unsigned size, id, surface, source, enabled, dest, set, cleared;
};

/* Independent wire positions from each system's SwapBegin/SetLayer/SwapEnd. */
static const WireLayout layouts[] = {
    {0x46c, 0x50, 0x54, 0x64, 0xa4, 0xc4, 0x104, 0x108},
    {0x588, 0x98, 0x9c, 0xac, 0xec, 0x10c, 0x14c, 0x150},
};

static void put(uint8_t *bytes, unsigned offset, uint32_t value)
{
    memcpy(bytes + offset, &value, sizeof(value));
}

static uint32_t word(const uint8_t *bytes, unsigned offset)
{
    uint32_t value;
    memcpy(&value, bytes + offset, sizeof(value));
    return value;
}

static void check_layout(const WireLayout &wire)
{
    uint8_t *allocation = (uint8_t *)malloc(wire.size + 1);
    assert(allocation);
    uint8_t *request = allocation + 1; /* The kernel also accepts unaligned input. */
    memset(request, 0xa7, wire.size);
    put(request, wire.id, 0x1234abcd);
    put(request, wire.set, 0xb);
    put(request, wire.cleared, 0x4);
    for (unsigned layer = 0; layer < 4; ++layer) {
        put(request, wire.surface + 4 * layer, layer == 2 ? 0 : 71 + layer);
        put(request, wire.enabled + 4 * layer, layer != 2);
        for (unsigned component = 0; component < 4; ++component) {
            put(request, wire.source + 16 * layer + 4 * component,
                (uint32_t)(-19 + (int)(100 * layer + 7 * component)));
            put(request, wire.dest + 16 * layer + 4 * component,
                (uint32_t)(-31 + (int)(200 * layer + 11 * component)));
        }
    }
    const uint8_t *payload = miniFramebufferSwapPayload(request, wire.size);
    assert(payload);
    assert(payload >= request && payload + SWAP_SIZE <= request + wire.size);
    uint8_t queued[SWAP_SIZE];
    memcpy(queued, payload, sizeof(queued)); /* Same copy size as present(). */
    assert(word(queued, SWAP_ID) == 0x1234abcd);
    assert(word(queued, SWAP_LAYERS_SET) == 0xb);
    assert(word(queued, SWAP_LAYERS_CLEARED) == 0x4);
    assert(SWAP_LAYERS == 4);
    for (unsigned layer = 0; layer < 4; ++layer) {
        assert(word(queued, SWAP_SURFACE(layer)) == (layer == 2 ? 0 : 71 + layer));
        assert(word(queued, SWAP_ENABLED(layer)) == (layer != 2));
        for (unsigned component = 0; component < 4; ++component) {
            assert(word(queued, SWAP_SOURCE(layer) + 4 * component) ==
                (uint32_t)(-19 + (int)(100 * layer + 7 * component)));
            assert(word(queued, SWAP_DEST(layer) + 4 * component) ==
                (uint32_t)(-31 + (int)(200 * layer + 11 * component)));
        }
    }
    free(allocation);
}

int main(void)
{
    for (const WireLayout &wire : layouts)
        check_layout(wire);
    uint8_t request[0x589] = {};
    for (uint32_t size = 0; size <= sizeof(request); ++size) {
        assert(miniFramebufferSwapPayload(NULL, size) == NULL);
        if (size != 0x46c && size != 0x588)
            assert(miniFramebufferSwapPayload(request, size) == NULL);
    }
    assert(miniFramebufferSwapPayload(request, UINT32_MAX) == NULL);
    puts("fb swap: both layouts, four layers, copy bounds and invalid inputs passed");
}
'''


def main():
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "swap.cc"
        executable = Path(directory) / "swap"
        source.write_text(SOURCE)
        subprocess.run([
            "clang++", "-std=c++11", "-Wall", "-Wextra", "-Werror", "-O1",
            "-fsanitize=address,undefined", "-I", str(HEADER.parent),
            str(source), "-o", str(executable),
        ], check=True)
        subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    main()
